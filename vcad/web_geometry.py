"""Isolated, bounded SVG-to-WebGL preparation for the local browser viewer.

The HTTP process never creates a Qt application. Fonts and tessellation run in
an offscreen child, including in the frozen executable. Only plain JSON and
little-endian buffers cross the boundary; no pickle or uploaded code is used.
"""

import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time

MAX_INPUT = 128 * 1024 * 1024
MAX_OUTPUT = 160 * 1024 * 1024
_compiler = threading.BoundedSemaphore(1)


def scene_chunks(scene):
    """Aligned transport chunks, with lossless integer selection metadata."""
    keys = ("supported", "reason", "origin", "entries", "object_components",
            "object_nets", "object_count", "drill_layers", "stats", "stride", "caption_bounds", "snap_points")
    header = {key: scene[key] for key in keys if key in scene}
    vertices, indices = scene.get("vertices", b""), scene.get("indices", b"")
    if scene.get("supported"):
        import numpy as np

        # Keep every coordinate/paint/expansion float bit for bit. Four float
        # metadata fields become an integer object ID and packed shape/flags.
        raw = np.frombuffer(vertices, dtype="<u4").reshape((-1, 20))
        values = raw.view("<f4")
        packed = np.empty((len(raw), 18), dtype="<u4")
        packed[:, :12] = raw[:, :12]
        packed[:, 12] = values[:, 13].astype("<u4")
        packed[:, 13] = (values[:, 12].astype("<u4") |
                         (values[:, 14].astype("<u4") << 8) |
                         (values[:, 15].astype("<u4") << 9))
        packed[:, 14:] = raw[:, 16:]
        vertices = memoryview(packed).cast("B")
    header.update(version=2, stride=72, vertex_bytes=len(vertices), index_bytes=len(indices))
    if "stats" in header:
        header["stats"] = {**header["stats"], "bytes": len(vertices) + len(indices)}
    metadata = json.dumps(header, default=lambda value: sorted(value),
                          separators=(",", ":")).encode()
    metadata += b" " * (-len(metadata) % 4)
    return struct.pack("<I", len(metadata)), metadata, vertices, indices


def encode_scene(scene):
    """A padded JSON header followed by two aligned binary GPU buffers."""
    return b"".join(scene_chunks(scene))


def worker(input_path, output_path):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtGui import QGuiApplication
    from .geometry.document import GeometryDocument
    from .geometry.gpu_scene import build_scene

    app = QGuiApplication.instance() or QGuiApplication([])
    data = json.loads(Path(input_path).read_bytes())
    if data.get("kind", "pcb") != "pcb":
        raise ValueError("WebGL geometry is only available for PCB layouts")
    document = GeometryDocument(data)
    scene = build_scene(document)
    if scene.get("supported"):
        # Reuse the worker's world bounds to reject tiny/offscreen captions
        # before expensive DOM geometry/style queries in a large SVG.
        scene["caption_bounds"] = {
            p.id: [p.bounds.x(), p.bounds.y(), p.bounds.width(), p.bounds.height()]
            for p in document.primitives if (p.net or p.pin) and not p.hole
        }
        scene["snap_points"] = [
            {"x": point.x(), "y": point.y(), "layers": sorted(layers)}
            for point, layers in document.snap_points
        ]
    # Picking paths and the Qt/XML document are not part of the browser payload.
    # Free them before packing, then write without concatenating large buffers.
    scene.pop("caption_paths", None)
    scene.pop("hole_paths", None)
    del document, data
    with Path(output_path).open("wb") as output:
        for chunk in scene_chunks(scene):
            output.write(chunk)
    # Retain the application until all Qt objects have finished their work.
    del app


def _run_worker(command, stopping=None):
    options = {"env": {**os.environ, "QT_QPA_PLATFORM": "offscreen"}}
    if stopping is None:
        return subprocess.run(command, capture_output=True, timeout=90, **options)
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options) as child:
        deadline = time.monotonic() + 90
        try:
            while True:
                if stopping.is_set():
                    raise ValueError("Viewer closed; WebGL preparation cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, 90)
                try:
                    output, errors = child.communicate(timeout=min(.2, remaining))
                    return subprocess.CompletedProcess(command, child.returncode, output, errors)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()


def prepare_geometry(body, stopping=None):
    if len(body) > MAX_INPUT:
        raise ValueError("Layout exceeds the WebGL preparation limit")
    if not _compiler.acquire(blocking=False):
        return None  # A caller may retry; never queue unbounded large documents.
    try:
        with tempfile.TemporaryDirectory(prefix="pcb-webgl-") as tmp:
            source, target = Path(tmp) / "input.json", Path(tmp) / "scene.bin"
            source.write_bytes(body)
            command = ([sys.executable] if getattr(sys, "frozen", False) else
                       [sys.executable, "-m", "vcad.cli"])
            try:
                result = _run_worker(
                    [*command, "--web-geometry-worker", str(source), str(target)],
                    stopping,
                )
            except subprocess.TimeoutExpired:
                raise ValueError("WebGL preparation timed out; using SVG") from None
            if result.returncode or not target.is_file():
                raise ValueError("Layout geometry could not be prepared; SVG remains available")
            if target.stat().st_size > MAX_OUTPUT:
                raise ValueError("Prepared layout exceeds the WebGL memory limit")
            return target.read_bytes()
    finally:
        _compiler.release()
