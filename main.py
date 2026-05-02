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

PORT = 5000


def get_data_dir() -> str:
    """
    Return the directory where user data (products.db, .env) should live.

    PyInstaller frozen binary  → directory containing the executable,
                                 so data survives app updates.
    Normal Python script       → project root.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    data_dir = get_data_dir()

    # Resolve all relative paths (products.db, .env) against data_dir
    # before importing mercari_sync, which sets DB_NAME at module level.
    os.chdir(data_dir)

    # Disable Flask/Werkzeug debug output noise in production
    os.environ.setdefault("FLASK_ENV", "production")

    from mercari_sync import app as flask_app, init_db  # noqa: PLC0415

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
