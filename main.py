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

def _setup_logging(data_dir: str) -> tuple:
    """Configure root logger with two file handlers.

    1. logs/app-runtime.log — rotating aggregate log (preserved across launches)
    2. logs/YYYYMMDD_HHMMSS.log — per-launch session log for troubleshooting

    Returns (logs_dir, launch_log_path).
    """
    logs_dir = os.path.join(data_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. Rotating aggregate log — backward-compatible
    rotating_handler = logging.handlers.RotatingFileHandler(
        os.path.join(logs_dir, "app-runtime.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    rotating_handler.setFormatter(fmt)

    # 2. Per-launch session log — one file per app start
    launch_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    launch_log_path = os.path.join(logs_dir, f"{launch_ts}.log")
    launch_handler = logging.FileHandler(launch_log_path, encoding="utf-8")
    launch_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(rotating_handler)
    root.addHandler(launch_handler)
    root.addHandler(stream_handler)

    return logs_dir, launch_log_path


def _log(msg: str) -> None:
    logging.getLogger("mia.startup").info(msg)


# ---------------------------------------------------------------------------
# macOS UI helpers (dialogs + notifications via osascript)
# ---------------------------------------------------------------------------

def _show_dialog(title: str, message: str) -> None:
    """Show a blocking macOS alert dialog. Works in windowed (no-terminal) mode."""
    try:
        script = (
            f'display alert {_osa_quote(title)} '
            f'message {_osa_quote(message)} '
            f'as critical buttons {{"OK"}} default button "OK"'
        )
        subprocess.run(["osascript", "-e", script], timeout=30)
    except Exception:
        pass  # fall back silently — error is also in app-runtime.log


def _notify(message: str) -> None:
    """Show a transient macOS notification banner."""
    try:
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

    PyInstaller frozen binary  → ~/Library/Application Support/MIA Inventory/
                                 Survives app updates (new dist.zip extracts never
                                 touch this path).
    Normal Python script       → project root.
    """
    if getattr(sys, "frozen", False):
        app_support = os.path.expanduser("~/Library/Application Support")
        data_dir    = os.path.join(app_support, _APP_NAME)
        # One-time migration: move legacy MercariInventory/ → MIAInventory/
        legacy_dir  = os.path.join(app_support, "MercariInventory")
        if os.path.isdir(legacy_dir) and not os.path.exists(data_dir):
            import shutil as _shutil
            try:
                _shutil.move(legacy_dir, data_dir)
                print(f"[data-dir] マイグレーション完了: {legacy_dir} → {data_dir}")
            except Exception as _e:
                print(f"[data-dir] マイグレーション失敗: {_e}")
        os.makedirs(data_dir, exist_ok=True)
        return data_dir
    return os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Port check
# ---------------------------------------------------------------------------

def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _app_is_responding(port: int, timeout: float = 2.0) -> bool:
    """Return True if our Flask app answers a GET / on this port."""
    try:
        import urllib.request
        req = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/", timeout=timeout
        )
        return req.status == 200
    except Exception:
        return False


def _pid_owning_port(port: int):
    """Return the PID (int) that holds the given TCP port, or None."""
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f":{port}"], timeout=5
        ).decode().strip()
        # lsof may return multiple lines; take the first
        return int(out.splitlines()[0]) if out else None
    except Exception:
        return None


def _pid_is_our_app(pid: int) -> bool:
    """Return True if the process with this PID is an MIAInventory process.

    Checks all historical bundle names and macOS 15-char process name truncations:
      MIAInventory (current) → truncates to "MIAInvent" in ps COMMAND column
      MercariInventory (legacy) → truncates to "MercariIn"
    """
    try:
        cmdline = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "args="], timeout=5
        ).decode()
        markers = (
            "mercari_sync",
            "MIAInventory",
            "MIAInvent",        # macOS 15-char truncation
            "MercariInventory",
            "MercariIn",        # macOS 15-char truncation of legacy name
            "mia_inventory",
            "main.py",
        )
        return any(m in cmdline for m in markers)
    except Exception:
        return False


def _kill_pid_wait(pid: int, port: int, timeout: float = 5.0) -> bool:
    """Send SIGTERM to pid, wait up to timeout s for the port to free up."""
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True  # already gone
    except Exception:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        if not is_port_in_use(port):
            return True
    # Force kill if still alive
    try:
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
    except Exception:
        pass
    return not is_port_in_use(port)


# ---------------------------------------------------------------------------
# Chrome check
# ---------------------------------------------------------------------------

_CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
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
    profile, and license after the bundle rename."""
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

    _log("=" * 54)
    _log("  MIA Inventory App — 起動")
    _log(f"  バージョン:   {APP_VERSION}")
    _log(f"  OS:           {platform.system()} {platform.release()} ({platform.machine()})")
    _log(f"  Python:       {platform.python_version()}")
    _log(f"  データ保存先: {data_dir}")
    _log(f"  DB パス:      {db_path}")
    _log(f"  ログ保存先:   {logs_dir}")
    _log(f"  起動ログ:     {launch_log_path}")
    _log(f"  URL:          http://0.0.0.0:{PORT}")
    _log("=" * 54)

    check_chrome_browser()
    _log("[startup] Chrome browser found")

    if getattr(sys, "frozen", False):
        _migrate_app_support_dir(data_dir)
        _migrate_db_if_needed(data_dir)

    os.chdir(data_dir)
    os.environ.setdefault("FLASK_ENV", "production")

    lock_file = os.path.join(data_dir, "app.lock")

    # ── Single-instance check via lock file ───────────────────────────────────
    if os.path.exists(lock_file):
        try:
            stored_pid = int(open(lock_file).read().strip())
        except Exception:
            stored_pid = None

        if stored_pid:
            pid_alive = False
            try:
                os.kill(stored_pid, 0)   # 0 = existence check, no signal sent
                pid_alive = True
            except (ProcessLookupError, PermissionError):
                pid_alive = False

            if pid_alive:
                # Same app instance already running — just open the browser.
                if _app_is_responding(PORT):
                    _log(f"[startup] アプリは既に起動中 (PID={stored_pid}) — ブラウザを開きます")
                    webbrowser.open(f"http://127.0.0.1:{PORT}")
                    return
                # PID alive but not serving — stale process from a crash.
                _log(f"[startup] 古いプロセスを検出 (PID={stored_pid}) — 終了させます")
                if _kill_pid_wait(stored_pid, PORT):
                    _log(f"[startup] 古いプロセスを終了しました (PID={stored_pid})")
                else:
                    _log(f"[startup] 警告: 古いプロセスの終了に失敗しました (PID={stored_pid})")
            else:
                _log(f"[startup] 古いロックファイルを削除します (PID={stored_pid} はすでに存在しません)")

        try:
            os.remove(lock_file)
        except Exception:
            pass

    # ── Port conflict check (no lock file, but port is in use) ────────────────
    if is_port_in_use(PORT):
        if _app_is_responding(PORT):
            # Healthy app on this port — open and exit (another window / instance).
            _log(f"[startup] ポート {PORT} は使用中 (応答あり) — ブラウザを開きます")
            webbrowser.open(f"http://127.0.0.1:{PORT}")
            return

        # Port in use but not responding — find who owns it.
        stale_pid = _pid_owning_port(PORT)
        _log(f"[startup] ポート {PORT} は応答なし (所有PID={stale_pid})")

        if stale_pid and _pid_is_our_app(stale_pid):
            _log(f"[startup] 自アプリのプロセスを検出 (PID={stale_pid}) — 終了させます")
            if _kill_pid_wait(stale_pid, PORT):
                _log(f"[startup] ポート {PORT} を解放しました")
            else:
                msg = (
                    f"ポート {PORT} が解放できませんでした。\n"
                    "ターミナルで以下を実行してください:\n"
                    f"  kill -9 {stale_pid}"
                )
                _log(f"ERROR: ポート {PORT} を解放できませんでした")
                _show_dialog("起動エラー", msg)
                sys.exit(1)
        elif stale_pid:
            msg = (
                f"ポート {PORT} は別のアプリ (PID={stale_pid}) が使用中です。\n"
                "そのアプリを終了してから、もう一度起動してください。"
            )
            _log(f"ERROR: ポート {PORT} は別プロセス (PID={stale_pid}) が使用中")
            _show_dialog("起動エラー", msg)
            sys.exit(1)
        else:
            # Can't determine owner — show generic error.
            msg = (
                f"ポート {PORT} はすでに使用中です。\n"
                "他のアプリを終了してから再起動してください。"
            )
            _show_dialog("起動エラー", msg)
            sys.exit(1)

    # ── Start app ─────────────────────────────────────────────────────────────
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

    # Write PID lock file now that Flask is confirmed running.
    try:
        with open(lock_file, "w") as _f:
            _f.write(str(os.getpid()))
        _log(f"[startup] ロックファイルを作成しました: {lock_file} (PID={os.getpid()})")
    except Exception as _e:
        _log(f"[startup] ロックファイル作成失敗（無視）: {_e}")

    # Remove lock file when this process exits normally.
    import atexit as _atexit

    def _remove_lock():
        try:
            if os.path.exists(lock_file):
                os.remove(lock_file)
                _log(f"[shutdown] ロックファイルを削除しました: {lock_file}")
        except Exception:
            pass

    _atexit.register(_remove_lock)

    webbrowser.open(f"http://127.0.0.1:{PORT}")
    _notify("アプリが起動しました")
    _log("[startup] アプリが起動しました。ブラウザ画面から操作してください。")

    try:
        server.join()
    except KeyboardInterrupt:
        _log("[startup] アプリを終了します。")


if __name__ == "__main__":
    main()
