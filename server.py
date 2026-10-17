"""
RemoteLauncher - خادم تشغيل التطبيقات عن بعد
- يعمل على Windows 11 ويستقبل الأوامر عبر WebSocket على الشبكة المحلية فقط.
- كل اتصال يجب أن يبدأ بالمصادقة برمز الاقتران قبل تنفيذ أي أمر.
- لإضافة أمر جديد: اكتب دالة async وزيّنها بـ @message_handler("اسم_النوع").
"""

import asyncio
import http.server
import json
import os
import secrets
import socket
import sys
import threading
from pathlib import Path

import websockets

# ----------------------------------------------------------------------------
# الإعدادات
# ----------------------------------------------------------------------------
HOST = "0.0.0.0"          # الاستماع على كل واجهات الشبكة المحلية
WS_PORT = 8765            # منفذ WebSocket للأوامر
HTTP_PORT = 8080          # منفذ HTTP لخدمة صفحة index.html
AUTH_TIMEOUT = 10         # ثواني مهلة المصادقة

# رمز الاقتران: يتولد عشوائياً في كل تشغيل، ولا يُخزَّن في الكود أبداً
PAIRING_TOKEN = secrets.token_urlsafe(16)

# قائمة التطبيقات المسموح بتشغيلها: مفتاح مُطبَّع -> المسار الأصلي
ALLOWED_APPS: dict[str, str] = {}

# مجموعة الأوامر: نوع الرسالة -> الدالة المسؤولة عنها
HANDLERS = {}

# كلمات تُستبعد من أسماء الاختصارات لأنها غير مفيدة للتشغيل
EXCLUDED_KEYWORDS = ("uninstall", "uninstaller", "readme", "read me", "help")


# ----------------------------------------------------------------------------
# مسارات الملفات (تعمل في السكربت العادي و PyInstaller)
# ----------------------------------------------------------------------------
def resource_dir() -> Path:
    # عند التجميع بـ PyInstaller تُفكّ الملفات المضمنة داخل sys._MEIPASS
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    # وإلا فالمجلد الذي يوجد فيه الملف نفسه
    return Path(__file__).resolve().parent


INDEX_FILE = resource_dir() / "index.html"


# ----------------------------------------------------------------------------
# جمع التطبيقات من قائمة ابدأ
# ----------------------------------------------------------------------------
def start_menu_dirs() -> list[Path]:
    dirs = []
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("ProgramData")
    if appdata:
        dirs.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    if programdata:
        dirs.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return dirs


def normalize(path: str) -> str:
    # توحيد المسار لمقارنة آمنة (حالة الأحرف والفواصل)
    return os.path.normcase(os.path.normpath(path))


def scan_apps() -> list[dict]:
    """يمرّ على مجلدات ابدأ ويعيد الاختصارات (.lnk) فقط بعد الفلترة."""
    found: dict[str, dict] = {}
    for base in start_menu_dirs():
        if not base.is_dir():
            continue
        for root, _dirs, files in os.walk(base):
            for filename in files:
                lower = filename.lower()
                if not lower.endswith(".lnk"):      # نعتمد على .lnk فقط
                    continue
                if any(k in lower for k in EXCLUDED_KEYWORDS):
                    continue
                full_path = os.path.join(root, filename)
                key = normalize(full_path)
                if key not in found:
                    found[key] = {"name": Path(filename).stem, "path": full_path}
    return sorted(found.values(), key=lambda a: a["name"].lower())


async def refresh_allowed() -> list[dict]:
    """يعيد بناء القائمة المسموحة. يُستدعى عند طلب القائمة وعند الحاجة للتحقق."""
    global ALLOWED_APPS
    apps = await asyncio.to_thread(scan_apps)   # لا نحجب حلقة الأحداث أثناء المسح
    ALLOWED_APPS = {normalize(a["path"]): a["path"] for a in apps}
    return apps


# ----------------------------------------------------------------------------
# نظام الأوامر القابل للتوسيع
# ----------------------------------------------------------------------------
def message_handler(msg_type: str):
    """ديكوراتور لتسجيل دالة تعالج نوعاً معيناً من الرسائل."""
    def decorator(fn):
        HANDLERS[msg_type] = fn
        return fn
    return decorator


async def send_json(ws, payload: dict):
    await ws.send(json.dumps(payload, ensure_ascii=False))


@message_handler("list_apps")
async def handle_list_apps(ws, msg):
    apps = await refresh_allowed()
    return {"type": "apps", "apps": [{"name": a["name"], "path": a["path"]} for a in apps]}


@message_handler("launch")
async def handle_launch(ws, msg):
    path = msg.get("path")
    if not isinstance(path, str) or not path:
        return {"type": "error", "message": "مسار غير صالح"}

    key = normalize(path)
    # إن لم يكن المسار موجوداً في القائمة، نعيد المسح مرة واحدة قبل الرفض
    if key not in ALLOWED_APPS:
        await refresh_allowed()
    if key not in ALLOWED_APPS:
        print(f"[!] رُفض تشغيل مسار غير مسموح: {path}")
        return {"type": "error", "message": "التطبيق غير مسموح"}

    try:
        # نشغّل المسار الأصلي المحفوظ من المسح، لا النص القادم من العميل
        os.startfile(ALLOWED_APPS[key])
    except OSError as exc:
        return {"type": "error", "message": f"فشل التشغيل: {exc}"}

    print(f"[>] تم تشغيل: {ALLOWED_APPS[key]}")
    return {"type": "launched"}


# ----------------------------------------------------------------------------
# المصادقة و معالجة الاتصال
# ----------------------------------------------------------------------------
async def authenticate(ws) -> bool:
    """أول رسالة يجب أن تكون {"type": "auth", "token": "..."} ، وإلا يُرفض الاتصال."""
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT)
        msg = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError, websockets.ConnectionClosed):
        return False

    if not isinstance(msg, dict) or msg.get("type") != "auth":
        await send_json(ws, {"type": "auth_failed"})
        return False

    token = str(msg.get("token", ""))
    # compare_digest بالبايتات لتفادي أخطاء الأحرف غير ASCII، ومقارنة آمنة من هجمات التوقيت
    if secrets.compare_digest(token.encode("utf-8"), PAIRING_TOKEN.encode("utf-8")):
        await send_json(ws, {"type": "auth_ok"})
        return True

    await send_json(ws, {"type": "auth_failed"})
    print("[!] محاولة اتصال برمز خاطئ")
    return False


async def dispatch(ws, raw):
    """يحوّل كل رسالة إلى الدالة المناسبة ويرسل الرد إن وُجد."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        await send_json(ws, {"type": "error", "message": "رسالة غير صالحة"})
        return

    if not isinstance(msg, dict):
        await send_json(ws, {"type": "error", "message": "صيغة الرسالة غير صحيحة"})
        return

    handler = HANDLERS.get(msg.get("type"))
    if handler is None:
        await send_json(ws, {"type": "error", "message": "أمر غير معروف"})
        return

    response = await handler(ws, msg)
    if response is not None:
        await send_json(ws, response)


async def connection_handler(ws):
    if not await authenticate(ws):
        await ws.close()
        return

    print(f"[+] جهاز متصل: {ws.remote_address}")
    try:
        async for raw in ws:
            await dispatch(ws, raw)
    except websockets.ConnectionClosed:
        pass
    finally:
        print("[-] انقطع الاتصال")


# ----------------------------------------------------------------------------
# خادم HTTP لخدمة صفحة الـ iPad
# ----------------------------------------------------------------------------
class IndexHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            data = INDEX_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass  # إسكات سجل الطلبات


def run_http_server():
    server = http.server.ThreadingHTTPServer((HOST, HTTP_PORT), IndexHandler)
    server.serve_forever()


# ----------------------------------------------------------------------------
# أدوات مساعدة وتشغيل
# ----------------------------------------------------------------------------
def get_local_ip() -> str:
    """يحصل على عنوان IP المحلي للجهاز في الشبكة (بدون إرسال أي بيانات فعلياً)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


async def main():
    ip = get_local_ip()
    threading.Thread(target=run_http_server, daemon=True).start()

    print("=" * 50)
    print(" RemoteLauncher يعمل")
    print(f" عنوان IP المحلي : {ip}")
    print(f" المنفذ (WebSocket): {WS_PORT}")
    print(f" صفحة الـ iPad   : http://{ip}:{HTTP_PORT}")
    print(f" رمز الاقتران    : {PAIRING_TOKEN}")
    print("=" * 50)

    async with websockets.serve(connection_handler, HOST, WS_PORT):
        await asyncio.Future()  # تشغيل دائم


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nتم الإيقاف.")
