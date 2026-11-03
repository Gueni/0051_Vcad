"""Documents handed to the viewer: from the command line or later "Open with" launches."""

import base64
import re
import tempfile
import threading
import time
from pathlib import Path

from .gerber import bundle, is_gerber
from .rendering import render_file

# Drill and report files that belong to a Gerber set; the set reader skips what it can't use.
GERBER_COMPANION = re.compile(r"\.(txt|nc|tap|drl|xln)$", re.I)


def load_documents(paths):
    """Render paths into viewer documents. Returns (documents, errors).

    A folder is one Gerber set; Gerber files from the same folder form one set,
    with that folder's drill files given alongside them.
    """
    paths = [Path(path) for path in dict.fromkeys(paths)]
    groups = {}
    for path in paths:
        if path.is_file() and is_gerber(path):
            groups.setdefault(path.parent, []).append(path)
    for path in paths:
        if path.is_file() and path.parent in groups and GERBER_COMPANION.search(path.name) and path not in groups[path.parent]:
            groups[path.parent].append(path)
    grouped = {path for files in groups.values() for path in files}
    documents, errors = [], []

    def attempt(label, load):
        try:
            documents.append(load())
        except Exception as exc:  # noqa: BLE001 - reported to the viewer
            errors.append(f"{label}: {exc}")

    for path in paths:
        if path in grouped:
            continue
        if path.is_dir():
            files = sorted(item for item in path.iterdir() if item.is_file())
            attempt(path.name, lambda path=path, files=files: _gerber_set(path.name, files))
        elif path.is_file():
            attempt(path.name, lambda path=path: {**render_file(path, native=True), "webgl": True,
                                                  "source": base64.b64encode(path.read_bytes()).decode("ascii")})
        else:
            errors.append(f"{path.name}: file not found")
    for folder, files in groups.items():
        name = min((file.name.split(".")[0] for file in files), key=len) or folder.name
        attempt(name, lambda name=name, files=files: _gerber_set(name, files))
    return documents, errors


def _gerber_set(name, files):
    data = bundle(files)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{name}.gerbers"
        path.write_bytes(data)
        return {**render_file(path, native=True), "file_name": path.name, "webgl": True,
                "source": base64.b64encode(data).decode("ascii")}


class Inbox:
    """Paths waiting for the viewer page to fetch them."""

    QUIET = 0.6  # Explorer starts one process per selected file; collect the burst.

    def __init__(self):
        self._lock = threading.Lock()
        self._paths, self._last = [], 0.0
        self.version = 0

    def add(self, paths):
        with self._lock:
            self._paths.extend(paths)
            self._last = time.monotonic()
            self.version += 1

    def take(self):
        while True:
            with self._lock:
                wait = self._last + self.QUIET - time.monotonic()
                if wait <= 0:
                    paths, self._paths = self._paths, []
                    break
            time.sleep(wait)
        return load_documents(paths) if paths else ([], [])
