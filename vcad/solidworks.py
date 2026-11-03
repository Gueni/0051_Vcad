"""Standalone 3D models from SolidWorks parts and assemblies.

The display meshes SolidWorks saves in .sldprt/.sldasm files are decoded
natively with sldkit; Vcad never starts SolidWorks or other software.
"""

import base64
import hashlib
from pathlib import Path

import numpy as np

DEFAULT_COLOR = (0.69, 0.73, 0.79)
UNIT_MM = {"millimeter": 1.0, "centimeter": 10.0, "meter": 1000.0, "inch": 25.4, "foot": 304.8}


def render_solidworks(path: Path) -> dict:
    import sldkit

    result = sldkit.decode_display_mesh_file(path)
    mesh = result.mesh
    if mesh is None or not mesh.triangle_count or result.positions is None or result.indices is None:
        raise ValueError(f"{path.name} has no saved display data Vcad can read. Open and save it in SolidWorks "
                         "with display data, or export it as STEP.")
    scale = UNIT_MM.get(mesh.length_unit, 1.0)
    position = np.frombuffer(result.positions, "<f4").reshape(-1, 3).astype(np.float64) * scale
    index = np.frombuffer(result.indices, "<u4")
    if len(index) % 3 or (len(index) and index.max() >= len(position)):
        raise ValueError(f"{path.name} has damaged display data.")
    center = (position.min(0) + position.max(0)) / 2
    part = {"position": (position - center).astype(np.float32), "index": index}
    if result.normals is not None and len(result.normals) == len(result.positions):
        part["normal"] = np.frombuffer(result.normals, "<f4")
    colors = {part_.color[:3] for part_ in mesh.parts if part_.color}
    if colors:
        # Per-face colors become vertex colors (face meshes do not share vertices).
        color = np.tile(np.array(DEFAULT_COLOR, np.float32), (len(position), 1))
        for face in mesh.parts:
            if face.color:
                color[face.vertex_start:face.vertex_start + face.vertex_count] = face.color[:3]
        part["color"] = (color ** 2.2).astype(np.float32)  # linear RGB, as STEP vertex colors
    encoded = {name: base64.b64encode(np.ascontiguousarray(array).tobytes()).decode("ascii")
               for name, array in part.items()}
    key = "solidworks-" + hashlib.sha256("".join(encoded.values()).encode()).hexdigest()
    low, high = (position.min(0) - center).tolist(), (position.max(0) - center).tolist()
    placeholder = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><g id="scene"/></svg>'
    return {
        "svg": placeholder, "name": path.name, "kind": "model", "warnings": [],
        "scene3d": {
            "standalone": True, "thickness": 0, "board": [], "bounds": [low, high],
            "bodies": [{"id": key, "name": path.name, "model": key, "z": 0, "height": 0,
                        "color": 0xffffff if colors else 0xb0b9c9, "opacity": 1,
                        "matrix": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}],
            "step_models": {key: {"name": path.name, "mesh": [encoded]}},
        },
        "stats": None,
    }
