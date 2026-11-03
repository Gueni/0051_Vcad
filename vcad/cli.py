"""Launch the local browser viewer and its isolated geometry worker."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import threading
import webbrowser

try:
    from .instance import Instance, hand_off
    from .server import Handler, ViewerServer
except ImportError:
    sys.exit("altium-monkey is not installed. Run:  pip install altium-monkey")


APP_BROWSERS = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
)


def open_in_browser(url: str, browser: str = "chrome") -> None:
    if browser == "app" and sys.platform == "win32":
        for candidate in APP_BROWSERS:
            exe = Path(os.path.expandvars(candidate))
            if exe.is_file():
                subprocess.Popen([str(exe), f"--app={url}"])
                return
        browser = "default"
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
    ap = argparse.ArgumentParser(description="Vcad: local browser-based Altium and KiCad PCB and schematic viewer, with Gerber sets, PLECS models, STEP and SolidWorks models")
    ap.add_argument("documents", nargs="*", type=Path, help="Altium, KiCad, PLECS or SolidWorks documents, Gerber/drill files or Gerber folders to open in tabs")
    ap.add_argument("--web", action="store_true", help="compatibility option; the browser viewer is now the default")
    ap.add_argument("--port", type=int, default=None, help="local server port (default: random free port)")
    ap.add_argument("--browser", default=None,
                    help="browser to use: app (a Chrome/Edge app window; default for the installed app), chrome (default), firefox, safari, default, ...")
    ap.add_argument("--no-open", action="store_true", help="run the local server without opening a browser")
    ap.add_argument("--new-instance", action="store_true", help="start a separate viewer instead of using a running one")
    args = ap.parse_args()

    for path in args.documents:
        if not path.is_file() and not path.is_dir():
            ap.error(f"File not found: {path}")
    paths = [path.resolve() for path in args.documents]
    browser = args.browser or ("app" if getattr(sys, "frozen", False) else "chrome")

    # "Open with" starts one process per file: hand them to the running viewer.
    instance = None
    if not (args.new_instance or args.port or args.no_open):
        instance = Instance()
        if not instance.acquire() and hand_off(instance, paths):
            return

    server = ViewerServer(("127.0.0.1", args.port or 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    server.inbox.add(paths)
    server.open_viewer = lambda: open_in_browser(url, browser)
    if instance:
        server.token = instance.token
        instance.publish(server.server_address[1])
    print(f"Vcad running at {url}   (Close the viewer or Ctrl+C to stop)")

    if not args.no_open:
        threading.Timer(0.3, server.open_viewer).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if instance:
            instance.withdraw()
        server.server_close()


if __name__ == "__main__":
    main()
