"""One Vcad per user: later launches hand their documents to the running viewer.

The running viewer holds an exclusive lock and publishes its port and a random
token. A new launch posts its paths to that viewer's /open endpoint with the
token (web pages cannot read it), brings the viewer window forward and exits.
"""

import json
import os
from pathlib import Path
import secrets
import sys
import time
import urllib.error
import urllib.request


def state_directory() -> Path:
    base = os.environ.get("LOCALAPPDATA") if sys.platform == "win32" else os.environ.get("XDG_RUNTIME_DIR")
    directory = Path(base or Path.home() / ".cache") / "vcad"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class Instance:
    def __init__(self):
        directory = state_directory()
        self.lock_path, self.info_path = directory / "instance.lock", directory / "instance.json"
        self.token = secrets.token_urlsafe(32)
        self._handle = None

    def acquire(self) -> bool:
        """Become the running viewer if no other Vcad holds the lock."""
        handle = open(self.lock_path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle  # The OS releases the lock when this process exits.
        return True

    def publish(self, port: int) -> None:
        temporary = self.info_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"port": port, "token": self.token, "pid": os.getpid()}))
        os.replace(temporary, self.info_path)

    def withdraw(self) -> None:
        try:
            if json.loads(self.info_path.read_text()).get("pid") == os.getpid():
                self.info_path.unlink()
        except (OSError, ValueError):
            pass


def forward(paths, timeout=2.0) -> bool:
    """Hand paths to the running viewer. False if none answers."""
    try:
        info = json.loads((state_directory() / "instance.json").read_text())
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(info['port'])}/open", method="POST",
            data=json.dumps({"paths": [str(Path(path).resolve()) for path in paths]}).encode(),
            headers={"Content-Type": "application/json", "X-Vcad-Token": info["token"]})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return False


def hand_off(instance: Instance, paths, wait=30.0) -> bool:
    """Forward to the running viewer, or return False once this process may run as the viewer."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if forward(paths):
            bring_viewer_forward()
            return True
        if instance.acquire():
            return False  # The previous viewer has exited.
        time.sleep(0.25)  # It is still starting (Explorer launched several of us at once).
    raise SystemExit("Vcad is already running but did not respond.")


def bring_viewer_forward(title="Vcad") -> None:
    """Raise the viewer window. A process the user just launched may take the foreground."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value == title or buffer.value.startswith(title + " - "):
                found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    for hwnd in found[:1]:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
