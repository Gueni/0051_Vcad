"""Standalone 3D models from SolidWorks parts and assemblies.

The installed SolidWorks exports the document to STEP through its COM API; the
browser then tessellates the STEP data like any other standalone STEP model.
One hidden SolidWorks instance is started on first use and kept for the
session, because starting SolidWorks takes much longer than an export.
"""

import atexit
import base64
import hashlib
from pathlib import Path
import queue
import tempfile
import threading
import zlib

MAX_STEP_BYTES = 64 * 1024 * 1024  # decompressed limit in the browser worker
TIMEOUT = 600
STEP_AP = 75  # swUserPreferenceIntegerValue_e.swStepAP

_jobs = queue.Queue()
_worker = None
_lock = threading.Lock()


def _serve():
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    app = None
    try:
        while (job := _jobs.get()) is not None:
            source, target, done = job
            try:
                if app is None:
                    app = win32com.client.DispatchEx("SldWorks.Application")
                    app.Visible = False
                    app.UserControl = False
                done["result"] = _export(app, pythoncom, win32com.client, source, target)
            except Exception as exc:  # noqa: BLE001 - reported to the requesting page
                done["error"] = exc
                if isinstance(exc, pythoncom.com_error):
                    app = None  # SolidWorks may have exited; start it again next time.
            finally:
                done["event"].set()
    finally:
        if app is not None:
            try:
                app.ExitApp()
            except Exception:  # noqa: BLE001 - already closed
                pass
        pythoncom.CoUninitialize()


def _export(app, pythoncom, client, source: Path, target: Path):
    def out():
        return client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

    kind = {".sldprt": 1, ".sldasm": 2}[source.suffix.lower()]  # swDocPART, swDocASSEMBLY
    errors, warnings = out(), out()
    model = app.OpenDoc6(str(source), kind, 1 | 2, "", errors, warnings)  # Silent | ReadOnly
    if model is None:
        raise RuntimeError(f"SolidWorks could not open the document (error code {errors.value}). "
                           "It may be from a newer SolidWorks version or have missing references.")
    # AP214 keeps part and face colors. The preference is persistent, so restore it.
    previous = app.GetUserPreferenceIntegerValue(STEP_AP)
    try:
        app.SetUserPreferenceIntegerValue(STEP_AP, 214)
        errors, warnings = out(), out()
        saved = model.Extension.SaveAs(str(target), 0, 1, client.VARIANT(pythoncom.VT_DISPATCH, None), errors, warnings)
        if not saved or not target.is_file():
            raise RuntimeError(f"SolidWorks could not export STEP (error code {errors.value}).")
    finally:
        app.SetUserPreferenceIntegerValue(STEP_AP, previous)
        app.CloseAllDocuments(True)


def _stop():
    if _worker is not None:
        _jobs.put(None)
        _worker.join(30)


def to_step(source: Path) -> bytes:
    global _worker
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        raise RuntimeError("Viewing SolidWorks files requires Windows, pywin32 and an installed SolidWorks.") from None
    with _lock:
        if _worker is None:
            _worker = threading.Thread(target=_serve, name="solidworks", daemon=True)
            _worker.start()
            atexit.register(_stop)
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / (source.stem + ".step")
        done = {"event": threading.Event()}
        _jobs.put((source.resolve(), target, done))
        if not done["event"].wait(TIMEOUT):
            raise TimeoutError("SolidWorks did not finish exporting STEP in time.")
        if "error" in done:
            error = done["error"]
            if type(error).__name__ == "com_error":
                raise RuntimeError("SolidWorks is not available. Install SolidWorks, or export the model as STEP.") from error
            raise error
        return target.read_bytes()


def render_solidworks(path: Path) -> dict:
    data = to_step(path)
    if len(data) > MAX_STEP_BYTES:
        raise ValueError("The exported STEP model exceeds the 64 MB limit.")
    key = "solidworks-" + hashlib.sha256(data).hexdigest()
    placeholder = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><g id="scene"/></svg>'
    return {
        "svg": placeholder, "name": path.name, "kind": "model", "warnings": [],
        "scene3d": {
            "standalone": True, "thickness": 0, "board": [],
            "bodies": [{"id": key, "name": path.name, "model": key, "z": 0, "height": 0, "color": 0xb0b9c9,
                        "opacity": 1, "matrix": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}],
            "step_models": {key: {"name": path.name, "data": base64.b64encode(zlib.compress(data)).decode("ascii"),
                                  "compressed": True}},
        },
        "stats": None,
    }
