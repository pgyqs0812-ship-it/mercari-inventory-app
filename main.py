"""
Entry point for the MIA Inventory desktop app.

Starts the Flask web server in a background thread, then opens the
browser automatically. Runs as a windowed .app bundle (no terminal window);
errors and status are surfaced via macOS dialogs and notifications.
"""
import logging
import logging.handlers
import os
import platform
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime

PORT = 5050
_APP_NAME = "MIA Inventory"
_APP_NAME_LEGACY = "MercariInventory"   # old bundle name — migration source only


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class _LevelRangeFilter(logging.Filter):
    """Pass only log records whose levelno is in [min_level, max_level]."""

    def __init__(self, min_level: int, max_level: int = logging.CRITICAL) -> None:
        super().__init__()
        self._min = min_level
        self._max = max_level

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        return self._min <= record.levelno <= self._max


def _setup_logging(data_dir: str) -> tuple:
    """Configure root logger with file handlers.

    Creates per-launch logs under ~/Documents/MIAInventory/logs/:
      - app-runtime.log             rotating aggregate log (all levels)
      - YYYYMMDD_HHMMSS.log         full per-launch session log
      - YYYYMMDD_HHMMSS_information.log  INFO events only
      - YYYYMMDD_HHMMSS_warning.log      WARNING events only
      - YYYYMMDD_HHMMSS_error.log        ERROR/CRITICAL events

    Returns (logs_dir, launch_log_path).
    """
    logs_dir = os.path.join(os.path.expanduser("~"), "Documents", "MIAInventory", "logs")
    os.makedirs(logs_dir, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. Rotating aggregate log
    rotating_handler = logging.handlers.RotatingFileHandler(
        os.path.join(logs_dir, "app-runtime.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    rotating_handler.setFormatter(fmt)

    # 2. Per-launch full session log
    launch_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    launch_log_path = os.path.join(logs_dir, f"{launch_ts}.log")
    launch_handler = logging.FileHandler(launch_log_path, encoding="utf-8")
    launch_handler.setFormatter(fmt)

    # 3. Per-launch categorized logs (information / warning / error)
    info_path = os.path.join(logs_dir, f"{launch_ts}_information.log")
    warn_path = os.path.join(logs_dir, f"{launch_ts}_warning.log")
    err_path  = os.path.join(logs_dir, f"{launch_ts}_error.log")

    for _path in (info_path, warn_path, err_path):
        open(_path, "a", encoding="utf-8").close()  # pre-create files

    info_handler = logging.FileHandler(info_path, encoding="utf-8")
    info_handler.setFormatter(fmt)
    info_handler.addFilter(_LevelRangeFilter(logging.INFO, logging.INFO))

    warn_handler = logging.FileHandler(warn_path, encoding="utf-8")
    warn_handler.setFormatter(fmt)
    warn_handler.addFilter(_LevelRangeFilter(logging.WARNING, logging.WARNING))

    err_handler = logging.FileHandler(err_path, encoding="utf-8")
    err_handler.setFormatter(fmt)
    err_handler.addFilter(_LevelRangeFilter(logging.ERROR))

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(rotating_handler)
    root.addHandler(launch_handler)
    root.addHandler(info_handler)
    root.addHandler(warn_handler)
    root.addHandler(err_handler)
    root.addHandler(stream_handler)

    return logs_dir, launch_log_path


def _log(msg: str) -> None:
    logging.getLogger("mia.startup").info(msg)


# ---------------------------------------------------------------------------
# UI helpers (dialogs + notifications) — macOS and Windows
# ---------------------------------------------------------------------------

def _show_dialog(title: str, message: str) -> None:
    """Show a blocking alert dialog. Works in windowed (no-terminal) mode."""
    try:
        if platform.system() == "Darwin":
            script = (
                f'display alert {_osa_quote(title)} '
                f'message {_osa_quote(message)} '
                f'as critical buttons {{"OK"}} default button "OK"'
            )
            subprocess.run(["osascript", "-e", script], timeout=30)
        elif platform.system() == "Windows":
            import ctypes  # noqa: PLC0415
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass  # fall back silently — error is also in app-runtime.log


def _notify(message: str) -> None:
    """Show a transient notification banner (macOS only; silently skipped on Windows)."""
    try:
        if platform.system() == "Darwin":
            script = (
                f'display notification {_osa_quote(message)} '
                f'with title {_osa_quote(_APP_NAME)}'
            )
            subprocess.run(["osascript", "-e", script], timeout=5)
    except Exception:
        pass


def _osa_quote(s: str) -> str:
    """Wrap a string in AppleScript double quotes, escaping backslashes and quotes."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Data directory
# ---------------------------------------------------------------------------

def get_data_dir() -> str:
    """
    Return the directory where user data (products.db, .env) should live.

    macOS  frozen → ~/Library/Application Support/MIA Inventory/
    Windows frozen → %APPDATA%/MIA Inventory/
    Dev (script)   → project root.
    """
    if getattr(sys, "frozen", False):
        if platform.system() == "Windows":
            base = os.environ.get("APPDATA") or os.path.expanduser("~")
            data_dir = os.path.join(base, _APP_NAME)
        else:
            app_support = os.path.expanduser("~/Library/Application Support")
            data_dir = os.path.join(app_support, _APP_NAME)
        os.makedirs(data_dir, exist_ok=True)
        return data_dir
    return os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Port check
# ---------------------------------------------------------------------------

def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


# ---------------------------------------------------------------------------
# Chrome check
# ---------------------------------------------------------------------------

_CHROME_CANDIDATES = [
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    # Windows — system-wide install
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    # Windows — per-user install (%LOCALAPPDATA%)
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
]


def check_chrome_browser() -> None:
    """Exit with a user-visible dialog if Google Chrome is not installed.

    Selenium Manager (bundled with Selenium 4.6+) handles ChromeDriver
    automatically, so only the Chrome browser itself is required.
    """
    if not any(os.path.exists(p) for p in _CHROME_CANDIDATES):
        msg = (
            "Google Chrome が見つかりません。\n\n"
            "https://www.google.com/chrome/ から Chrome をインストールしてから、"
            "もう一度アプリを起動してください。"
        )
        _log("ERROR: Chrome not found — showing dialog")
        _show_dialog("Chrome が必要です", msg)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Data migrations (one-time, run only from frozen .app bundle)
# ---------------------------------------------------------------------------

def _migrate_db_if_needed(data_dir: str) -> None:
    """One-time: copy products.db from the old .command-era location (next to
    the executable) to the persistent app-support directory."""
    import shutil  # noqa: PLC0415

    new_db = os.path.join(data_dir, "products.db")
    if os.path.exists(new_db):
        return

    old_db = os.path.join(os.path.dirname(sys.executable), "products.db")
    if os.path.exists(old_db):
        _log(f"[migration] データを新しい保存先にコピーします: {new_db}")
        shutil.copy2(old_db, new_db)
        _log("[migration] 完了")


def _migrate_app_support_dir(data_dir: str) -> None:
    """One-time: move all user data from the legacy MercariInventory app-support
    dir to the new MIA Inventory dir so existing users keep their DB, Chrome
    profile, and license after the bundle rename. macOS only."""
    if platform.system() != "Darwin":
        return
    import shutil  # noqa: PLC0415

    app_support = os.path.expanduser("~/Library/Application Support")
    old_dir = os.path.join(app_support, _APP_NAME_LEGACY)
    if not os.path.isdir(old_dir):
        return

    files_to_copy = [
        "products.db",
        "mercari_session.json",
        "license.json",
    ]
    dirs_to_copy = ["chrome-profile"]

    migrated = False
    for fname in files_to_copy:
        src = os.path.join(old_dir, fname)
        dst = os.path.join(data_dir, fname)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)
            _log(f"[migration] {fname} を移行しました")
            migrated = True

    for dname in dirs_to_copy:
        src = os.path.join(old_dir, dname)
        dst = os.path.join(data_dir, dname)
        if os.path.isdir(src) and not os.path.exists(dst):
            shutil.copytree(src, dst)
            _log(f"[migration] {dname}/ を移行しました")
            migrated = True

    if migrated:
        _log(f"[migration] {_APP_NAME_LEGACY} → {_APP_NAME} データ移行完了")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    data_dir = get_data_dir()
    logs_dir, launch_log_path = _setup_logging(data_dir)

    # Resolve app version (set by build_mac.sh via _version.py injection)
    try:
        from _version import APP_VERSION  # noqa: PLC0415
    except ImportError:
        APP_VERSION = "dev"

    db_path = os.path.join(data_dir, "products.db")
    launch_ts = os.path.splitext(os.path.basename(launch_log_path))[0]
    info_log_path = os.path.join(logs_dir, f"{launch_ts}_information.log")
    warn_log_path = os.path.join(logs_dir, f"{launch_ts}_warning.log")
    err_log_path  = os.path.join(logs_dir, f"{launch_ts}_error.log")

    _log("=" * 54)
    _log("  MIA Inventory App — 起動")
    _log(f"  バージョン:   {APP_VERSION}")
    _log(f"  OS:           {platform.system()} {platform.release()} ({platform.machine()})")
    _log(f"  Python:       {platform.python_version()}")
    _log(f"  データ保存先: {data_dir}")
    _log(f"  DB パス:      {db_path}")
    _log(f"  ログ保存先:   {logs_dir}")
    _log(f"  起動ログ:     {launch_log_path}")
    _log(f"  情報ログ:     {info_log_path}")
    _log(f"  警告ログ:     {warn_log_path}")
    _log(f"  エラーログ:   {err_log_path}")
    _log(f"  URL:          http://0.0.0.0:{PORT}")
    _log("=" * 54)

    check_chrome_browser()
    _log("[startup] Chrome browser found")

    if getattr(sys, "frozen", False):
        _migrate_app_support_dir(data_dir)
        _migrate_db_if_needed(data_dir)

    os.chdir(data_dir)
    os.environ.setdefault("FLASK_ENV", "production")

    import mercari_sync as _ms  # noqa: PLC0415
    _ms.DB_NAME            = os.path.join(data_dir, "products.db")
    _ms.COOKIE_FILE        = os.path.join(data_dir, "mercari_session.json")
    _ms.CHROME_PROFILE_DIR = os.path.join(data_dir, "chrome-profile")
    _ms.LICENSE_FILE       = os.path.join(data_dir, "license.json")
    _ms.setup_app_logging(logs_dir, launch_log_path)   # hand dirs to Flask/sync module
    flask_app    = _ms.app
    init_db      = _ms.init_db
    init_license = _ms.init_license

    # SIGTERM handler — macOS sends SIGTERM when the user quits the .app via
    # Cmd+Q or the Dock menu.  Python's default handler kills the process
    # immediately without running atexit, leaving Chrome processes and a
    # SingletonLock in the profile dir.  This handler runs the same cleanup
    # that atexit would normally run and then exits cleanly.
    def _sigterm_handler(signum, frame):  # noqa: ANN001
        _log("[startup] SIGTERM 受信 — Chrome をシャットダウンします")
        try:
            _ms._shutdown_chrome()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    port_busy = is_port_in_use(PORT)
    _log(f"[startup] ポート {PORT} 使用中: {port_busy}")

    if port_busy:
        _log(f"[startup] 既存インスタンスを検出 — ブラウザを開きます")
        webbrowser.open(f"http://127.0.0.1:{PORT}")
        return

    init_db()
    init_license()

    _notify("起動中…")

    server = threading.Thread(
        target=lambda: flask_app.run(
            host="0.0.0.0",
            port=PORT,
            debug=False,
            use_reloader=False,
        ),
        daemon=True,
        name="flask-server",
    )
    server.start()
    _log("[startup] Flask サーバースレッド開始")

    # Poll until Flask is accepting connections (max 10 s).
    deadline = time.time() + 10
    while not is_port_in_use(PORT):
        if time.time() > deadline:
            msg = "Flask サーバーが 10 秒以内に起動しませんでした。\nlogs/ フォルダ内のログファイルを確認してください。"
            _log("[startup] ERROR: Flask did not start within 10 seconds")
            _show_dialog("起動エラー", msg)
            sys.exit(1)
        time.sleep(0.2)

    webbrowser.open(f"http://127.0.0.1:{PORT}")
    _notify("アプリが起動しました")
    _log("[startup] アプリが起動しました。ブラウザ画面から操作してください。")

    try:
        server.join()
    except KeyboardInterrupt:
        _log("[startup] アプリを終了します。")


if __name__ == "__main__":
    main()
