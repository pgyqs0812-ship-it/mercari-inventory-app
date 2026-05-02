"""
Entry point for the Mercari Inventory desktop app.

Starts the Flask web server in a background thread, then opens the
browser automatically. The terminal stays open so that Selenium sync
steps (login prompt, input() calls) are visible and interactive.
"""
import os
import socket
import sys
import threading
import time
import webbrowser

PORT = 5050


def get_data_dir() -> str:
    """
    Return the directory where user data (products.db, .env) should live.

    PyInstaller frozen binary  → ~/Library/Application Support/MercariInventory/
                                 Survives app updates (new dist.zip extracts never
                                 touch this path).
    Normal Python script       → project root.
    """
    if getattr(sys, "frozen", False):
        app_support = os.path.expanduser("~/Library/Application Support")
        data_dir = os.path.join(app_support, "MercariInventory")
        os.makedirs(data_dir, exist_ok=True)
        return data_dir
    return os.path.dirname(os.path.abspath(__file__))


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


# macOS paths where Google Chrome may be installed
_CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
]


def check_chrome_browser() -> None:
    """Exit with a clear message if Google Chrome is not installed.

    Selenium Manager (bundled with Selenium 4.6+) handles ChromeDriver
    automatically, so only the Chrome browser itself is required.
    Works for both local Python runs and PyInstaller packaged builds.
    """
    if not any(os.path.exists(p) for p in _CHROME_CANDIDATES):
        print("----------------------------------------")
        print("Chrome browser not found.")
        print("Please install Google Chrome.")
        print("")
        print("  https://www.google.com/chrome/")
        print("----------------------------------------")
        sys.exit(1)


def _migrate_db_if_needed(data_dir: str) -> None:
    """One-time migration: copy products.db from the old location (next to the
    executable) to the new persistent app data directory, so existing users do
    not lose their sync history after updating the app."""
    import shutil  # noqa: PLC0415

    new_db = os.path.join(data_dir, "products.db")
    if os.path.exists(new_db):
        return  # already migrated or fresh install

    old_db = os.path.join(os.path.dirname(sys.executable), "products.db")
    if os.path.exists(old_db):
        print(f"[migration] データを新しい保存先にコピーします: {new_db}")
        shutil.copy2(old_db, new_db)
        print("[migration] 完了")


def main() -> None:
    check_chrome_browser()

    data_dir = get_data_dir()

    # For frozen builds, migrate products.db from the old location if needed.
    if getattr(sys, "frozen", False):
        _migrate_db_if_needed(data_dir)

    # Set DB_NAME to an absolute path before importing mercari_sync so the
    # database is always found regardless of the process working directory.
    # (os.chdir is kept as a fallback for .env loading via python-dotenv.)
    os.chdir(data_dir)

    # Disable Flask/Werkzeug debug output noise in production
    os.environ.setdefault("FLASK_ENV", "production")

    import mercari_sync as _ms  # noqa: PLC0415
    _ms.DB_NAME = os.path.join(data_dir, "products.db")
    flask_app = _ms.app
    init_db = _ms.init_db

    print("=" * 54)
    print("  Mercari Inventory App")
    print(f"  データ保存先: {data_dir}")
    print(f"  URL:          http://127.0.0.1:{PORT}")
    print("=" * 54)

    # If a server is already listening, just open the browser and exit.
    if is_port_in_use(PORT):
        print(f"\nポート {PORT} はすでに使用中です。")
        print("アプリはすでに起動している可能性があります。")
        print(f"ブラウザを開きます: http://127.0.0.1:{PORT}\n")
        webbrowser.open(f"http://127.0.0.1:{PORT}")
        return

    init_db()

    server = threading.Thread(
        target=lambda: flask_app.run(
            host="127.0.0.1",
            port=PORT,
            debug=False,
            use_reloader=False,
        ),
        daemon=True,
        name="flask-server",
    )
    server.start()

    # Poll until Flask is actually accepting connections (max 10 s).
    deadline = time.time() + 10
    while not is_port_in_use(PORT):
        if time.time() > deadline:
            print("Error: Flask server did not start within 10 seconds.")
            sys.exit(1)
        time.sleep(0.2)

    webbrowser.open(f"http://127.0.0.1:{PORT}")

    print("\nアプリが起動しました。")
    print("同期を行う際はこのウィンドウで Mercari のログイン操作が必要です。")
    print("終了するには Ctrl+C を押してください。\n")

    try:
        server.join()
    except KeyboardInterrupt:
        print("\nアプリを終了します。")


if __name__ == "__main__":
    main()
