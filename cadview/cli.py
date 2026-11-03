"""Launch the local browser viewer and its isolated geometry worker."""

import argparse
import base64
from pathlib import Path
import subprocess
import sys
import threading
import webbrowser

try:
    from .rendering import render_file
    from .server import Handler, ViewerServer
except ImportError:
    sys.exit("altium-monkey is not installed. Run:  pip install altium-monkey")


def open_in_browser(url: str, browser: str = "chrome") -> None:
    if browser == "default":
        webbrowser.open(url)
        return
    for name in (browser, f"{browser}.exe", f"google-{browser}", f"{browser}-browser"):
        try:
            webbrowser.get(name).open(url)
            return
        except webbrowser.Error:
            pass
    if sys.platform == "darwin":
        app = {"chrome": "Google Chrome", "firefox": "Firefox", "safari": "Safari"}.get(browser, browser)
        if subprocess.run(["open", "-a", app, url]).returncode == 0:
            return
    elif sys.platform == "win32" and browser == "chrome":
        for exe in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
            if Path(exe).is_file():
                subprocess.Popen([exe, url])
                return
    print(f"Could not find {browser}; opening with the system default browser instead.")
    webbrowser.open(url)


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--web-geometry-worker":
        from .web_geometry import worker
        worker(sys.argv[2], sys.argv[3])
        return
    ap = argparse.ArgumentParser(description="cadview: local browser-based Altium and KiCad PCB and schematic viewer, with STEP, SolidWorks and 3D PDF models")
    ap.add_argument("documents", nargs="*", type=Path, help="Altium, KiCad, SolidWorks or 3D PDF documents to open in tabs")
    ap.add_argument("--web", action="store_true", help="compatibility option; the browser viewer is now the default")
    ap.add_argument("--port", type=int, default=None, help="local server port (default: random free port)")
    ap.add_argument("--browser", default=None, help="browser to use: chrome (default), firefox, safari, default, ...")
    ap.add_argument("--no-open", action="store_true", help="run the local server without opening a browser")
    args = ap.parse_args()

    for path in args.documents:
        if not path.is_file():
            ap.error(f"File not found: {path}")
    Handler.initial = None
    initial = []
    for path in args.documents:
        print(f"Loading {path.name} ...")
        initial.append({**render_file(path, native=True), "source": base64.b64encode(path.read_bytes()).decode("ascii"), "webgl": True})
    if initial:
        Handler.initial = initial[0] if len(initial) == 1 else {"documents": initial}

    server = ViewerServer(("127.0.0.1", args.port or 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"cadview running at {url}   (Close the viewer or Ctrl+C to stop)")

    if not args.no_open:
        threading.Timer(0.3, open_in_browser, args=(url, args.browser or "chrome")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
