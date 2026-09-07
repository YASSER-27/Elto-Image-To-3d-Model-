import threading
import time
import webbrowser

from server import app

HOST = "127.0.0.1"
PORT = 5732


def _open_browser_when_ready():
    # Flask needs a moment to bind the port before the browser can connect.
    time.sleep(1.0)
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
