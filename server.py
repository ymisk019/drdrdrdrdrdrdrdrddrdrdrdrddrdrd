"""
RemoteLauncher - خادم تشغيل التطبيقات عن بعد
- يعمل في الخلفية كأيقونة في شريط المهام.
- يرسل أيقونات التطبيقات (PNG بصيغة base64) مع قائمتها.
- لإضافة أمر جديد: اكتب دالة async وزيّنها بـ @message_handler("اسم_النوع").
"""

import asyncio
import http.server
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import websockets

# ----------------------------------------------------------------------------
# الإعدادات
# ----------------------------------------------------------------------------
HOST = "0.0.0.0"              # الاستماع على كل واجهات الشبكة المحلية
WS_PORT = 8765                # منفذ WebSocket للأوامر
HTTP_PORT = 8080              # منفذ HTTP لخدمة صفحة index.html
AUTH_TIMEOUT = 10             # ثواني مهلة المصادقة

# رمز الاقتران: يتولد عشوائياً في كل تشغيل ولا يُكتب في الكود أبداً
PAIRING_TOKEN = secrets.token_urlsafe(16)

ALLOWED_APPS: dict[str, str] = {}   # مفتاح مُطبَّع -> المسار الأصلي
ICON_CACHE: dict[str, str] = {}     # مسار الاختصار -> PNG base64 (أو "" عند الفشل)
HANDLERS = {}                       # نوع الرسالة -> الدالة المسؤولة عنها

# كلمات تُستبعد من أسماء الاختصارات لأنها غير مفيدة للتشغيل
EXCLUDED_KEYWORDS = ("uninstall", "uninstaller", "readme", "read me", "help")

CREATE_NO_WINDOW = 0x08000000       # يمنع ظهور نافذة PowerShell عند استخراج الأيقونات

SERVER_LOOP = None                  # حلقة الأحداث الخاصة بخادم WebSocket
STOP_EVENT = None                   # حدث إيقاف خادم WebSocket


# ----------------------------------------------------------------------------
# مسارات الملفات (تعمل في السكربت العادي و PyInstaller)
# ----------------------------------------------------------------------------
def resource_dir() -> Path:
    # عند التجميع بـ PyInstaller تُفكّ الملفات المضمنة داخل sys._MEIPASS
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
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
    """يمرّ على مجلدات ابدأ ويعيد اختصارات .lnk فقط بعد الفلترة."""
    found: dict[str, dict] = {}
    for base in start_menu_dirs():
        if not base.is_dir():
            continue
        for root, _dirs, files in os.walk(base):
            for filename in files:
                lower = filename.lower()
                if not lower.endswith(".lnk"):          # نعتمد على .lnk فقط
                    continue
                if any(k in lower for k in EXCLUDED_KEYWORDS):
                    continue
                full_path = os.path.join(root, filename)
                key = normalize(full_path)
                if key not in found:
                    found[key] = {"name": Path(filename).stem, "path": full_path}
    return sorted(found.values(), key=lambda a: a["name"].lower())


# ----------------------------------------------------------------------------
# استخراج الأيقونات عبر PowerShell (مرة واحدة لكل تطبيق ثم تُحفظ في الذاكرة)
# ----------------------------------------------------------------------------
# السكربت يقرأ قائمة المسارات من ملف JSON ويطبع سطراً لكل مسار: base64 أو سطر فارغ
PS_SCRIPT = r"""
Add-Type -AssemblyName System.Drawing
$paths = Get-Content -Raw -Encoding UTF8 -LiteralPath $args[0] | ConvertFrom-Json
foreach ($p in @($paths)) {
    try {
        $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($p)
        $bmp = $icon.ToBitmap()
        $ms = New-Object System.IO.MemoryStream
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
        [Console]::Out.WriteLine([Convert]::ToBase64String($ms.ToArray()))
    } catch {
        [Console]::Out.WriteLine("")
    }
}
"""


def fetch_icons(paths: list[str]) -> None:
    """يستخرج أيقونات المسارات غير الموجودة في الكاش."""
    missing = [p for p in paths if p not in ICON_CACHE]
    if not missing:
        return

    tmp = Path(tempfile.mkdtemp())
    try:
        script = tmp / "icons.ps1"
        data = tmp / "paths.json"
        script.write_text(PS_SCRIPT, encoding="utf-8-sig")
        data.write_text(json.dumps(missing, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(script), str(data)],
            capture_output=True, timeout=180, creationflags=CREATE_NO_WINDOW,
        )
        lines = result.stdout.decode("ascii", errors="ignore").splitlines()
    except (OSError, subprocess.SubprocessError):
        lines = []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for i, p in enumerate(missing):
        ICON_CACHE[p] = lines[i].strip() if i < len(lines) else ""


async def refresh_allowed(with_icons: bool = False) -> list[dict]:
    """يعيد بناء القائمة المسموحة، ويستخرج الأيقونات عند الطلب."""
    global ALLOWED_APPS
    apps = await asyncio.to_thread(scan_apps)   # لا نحجب حلقة الأحداث أثناء المسح
    ALLOWED_APPS = {normalize(a["path"]): a["path"] for a in apps}
    if with_icons:
        await asyncio.to_thread(fetch_icons, [a["path"] for a in apps])
    return apps


# ----------------------------------------------------------------------------
# نظام الأوامر القابل للتوسعة
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
    apps = await refresh_allowed(with_icons=True)
    return {
        "type": "apps",
        "apps": [
            {"name": a["name"], "path": a["path"], "icon": ICON_CACHE.get(a["path"], "")}
            for a in apps
        ],
    }


@message_handler("launch")
async def handle_launch(ws, msg):
    path = msg.get("path")
    if not isinstance(path, str) or not path:
        return {"type": "error", "message": "مسار غير صالح"}

    key = normalize(path)
    # إن لم يكن المسار في القائمة نعيد المسح مرة واحدة قبل الرفض
    if key not in ALLOWED_APPS:
        await refresh_allowed()
    if key not in ALLOWED_APPS:
        print(f"[!] رُفض تشغيل مسار غير مسموح: {path}")
        return {"type": "error", "message": "التطبيق غير مسموح"}

    try:
        # نشغّل المسار المحفوظ من المسح، لا النص القادم من العميل
        os.startfile(ALLOWED_APPS[key])
    except OSError as exc:
        return {"type": "error", "message": f"فشل التشغيل: {exc}"}

    print(f"[>] تم تشغيل: {ALLOWED_APPS[key]}")
    return {"type": "launched"}


# ----------------------------------------------------------------------------
# المصادقة ومعالجة الاتصال
# ----------------------------------------------------------------------------
async def authenticate(ws) -> bool:
    """أول رسالة يجب أن تكون {"type": "auth", "token": "..."}، وإلا يُرفض الاتصال."""
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT)
        msg = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError, websockets.ConnectionClosed):
        return False

    if not isinstance(msg, dict) or msg.get("type") != "auth":
        await send_json(ws, {"type": "auth_failed"})
        return False

    token = str(msg.get("token", ""))
    # compare_digest بالبايتات لتفادي أخطاء الأحرف غير ASCII ومقارنة آمنة من هجمات التوقيت
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
    http.server.ThreadingHTTPServer((HOST, HTTP_PORT), IndexHandler).serve_forever()


# ----------------------------------------------------------------------------
# خادم WebSocket في خيط خلفي (لأن الخيط الرئيسي للأيقونة)
# ----------------------------------------------------------------------------
async def serve_ws():
    global STOP_EVENT
    STOP_EVENT = asyncio.Event()
    async with websockets.serve(connection_handler, HOST, WS_PORT):
        await STOP_EVENT.wait()


def ws_thread():
    global SERVER_LOOP
    SERVER_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(SERVER_LOOP)
    SERVER_LOOP.run_until_complete(serve_ws())


def stop_ws():
    if SERVER_LOOP and STOP_EVENT:
        SERVER_LOOP.call_soon_threadsafe(STOP_EVENT.set)


# ----------------------------------------------------------------------------
# أدوات مساعدة
# ----------------------------------------------------------------------------
def get_local_ip() -> str:
    """يحصل على عنوان IP المحلي (بدون إرسال بيانات فعلياً)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def save_token_file() -> Path:
    """يحفظ رمز الاقتران في ملف، لأن النسخة النهائية بدون نافذة terminal."""
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / "RemoteLauncher"
    base.mkdir(parents=True, exist_ok=True)
    token_file = base / "token.txt"
    token_file.write_text(PAIRING_TOKEN, encoding="utf-8")
    return token_file


def make_tray_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(79, 140, 255, 255))
    d.polygon([(24, 20), (24, 44), (46, 32)], fill=(255, 255, 255, 255))
    return img


def run_tray(ip: str, token_file: Path):
    """أيقونة شريط المهام. تبقى حية على الخيط الرئيسي حتى يختار المستخدم خروج."""
    import pystray

    def show_token(icon, item):
        icon.notify(f"رمز الاقتران: {PAIRING_TOKEN}", "RemoteLauncher")

    def open_token_file(icon, item):
        os.startfile(str(token_file))

    def quit_app(icon, item):
        stop_ws()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(f"IP: {ip}:{WS_PORT}", None, enabled=False),
        pystray.MenuItem("عرض رمز الاقتران", show_token),
        pystray.MenuItem("فتح ملف الرمز", open_token_file),
        pystray.MenuItem("خروج", quit_app),
    )
    icon = pystray.Icon("RemoteLauncher", make_tray_image(),
                        f"RemoteLauncher - {ip}:{WS_PORT}", menu)
    icon.run()


def main():
    ip = get_local_ip()
    token_file = save_token_file()

    threading.Thread(target=run_http_server, daemon=True).start()
    threading.Thread(target=ws_thread, daemon=True).start()

    print("=" * 50)
    print(f" RemoteLauncher يعمل - IP: {ip}")
    print(f" صفحة الـ iPad: http://{ip}:{HTTP_PORT}")
    print(f" رمز الاقتران : {PAIRING_TOKEN}")
    print(f" ملف الرمز    : {token_file}")
    print("=" * 50)

    run_tray(ip, token_file)


if __name__ == "__main__":
    main()
