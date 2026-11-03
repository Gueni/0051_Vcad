"""Standalone 3D models from 3D PDF (U3D) annotations."""

import base64
import hashlib
from pathlib import Path

import numpy as np

from . import u3d

MAX_U3D_BYTES = 128 * 1024 * 1024
MAX_TRIANGLES = 8_000_000
ALTIUM_UNIT_MM = 0.0254 / 10000  # Altium internal units are 1/10000 mil.
DEFAULT_COLOR = np.array([0.69, 0.73, 0.79], np.float32)


def _streams(path: Path):
    from pypdf import PdfReader

    reader = PdfReader(path)
    info = reader.metadata or {}
    producer = " ".join(str(info.get(key, "")) for key in ("/Producer", "/Creator"))
    found = []
    for page in reader.pages:
        for annotation in page.get("/Annots") or []:
            annotation = annotation.get_object()
            if annotation.get("/Subtype") != "/3D":
                continue
            stream = annotation["/3DD"].get_object()
            if "/3DD" in stream:  # A 3D reference dictionary
                stream = stream["/3DD"].get_object()
            found.append((str(stream.get("/Subtype", "")), stream))
    if not found:
        raise ValueError("This PDF has no 3D content.")
    subtypes = {subtype for subtype, _ in found}
    found = [stream for subtype, stream in found if subtype == "/U3D"]
    if not found:
        if "/PRC" in subtypes:
            raise ValueError("This 3D PDF uses the PRC format (written by SolidWorks, Creo and others), which cadview "
                             "cannot read yet. Open the original SolidWorks file or a STEP export instead.")
        raise ValueError(f"3D PDFs in {', '.join(sorted(subtypes))} format are not supported; only U3D content can be shown.")
    return producer, found


def _part(mesh, shading_colors):
    """Compact indexed mesh; the browser derives crease-aware normals."""
    position = mesh["position"].astype(np.float64)
    offset = (position.min(0) + position.max(0)) / 2  # local origin keeps float32 precise
    faces = mesh["faces"]
    part = {"position": (position - offset).astype(np.float32),
            ("index16" if len(position) < 65536 else "index"): faces.astype(np.uint16 if len(position) < 65536 else np.uint32)}
    face_colors = shading_colors[mesh["shading"]]
    if mesh["colors"] is not None:  # per-vertex diffuse colors; keep one per face
        face_colors = np.where(np.isnan(mesh["colors"][:, 0]), face_colors, mesh["colors"][:, 0])
    uniform = np.allclose(face_colors, face_colors[0])
    if not uniform:
        part["face_color"] = (face_colors ** 2.2).astype(np.float32)  # linear RGB, as STEP vertex colors
    return part, offset, face_colors[0], uniform


def _encode(part):
    return {name: base64.b64encode(np.ascontiguousarray(array).tobytes()).decode("ascii")
            for name, array in part.items()}


def _world_transforms(nodes):
    cache = {}

    def resolve(name, depth=0):
        if name == "":
            return [np.eye(4)]
        if name not in nodes or depth > 64:
            return []
        if name not in cache:
            # Some exporters (Altium) leave top-level nodes without the world parent.
            parents = nodes[name]["parents"] or [("", np.eye(4))]
            cache[name] = [parent @ local for parent_name, local in parents
                           for parent in resolve(parent_name, depth + 1)]
        return cache[name]

    return resolve


def render_pdf3d(path: Path) -> dict:
    producer, streams = _streams(path)
    assets, bodies, notes, triangles = {}, [], [], 0
    instances, unit = [], None
    for stream_index, stream in enumerate(streams):
        data = stream.get_data()
        if len(data) > MAX_U3D_BYTES:
            raise ValueError("The 3D content exceeds the 128 MB limit.")
        scene = u3d.parse(data)
        if scene["units"]:
            scale = scene["units"] * 1000
        elif "altium" in producer.lower():
            scale = ALTIUM_UNIT_MM
        else:
            scale = 1.0
            unit = unit or "Model units are not declared; dimensions are shown as millimetres."
        resolve = _world_transforms(scene["nodes"])
        for node_name, node in scene["nodes"].items():
            mesh = scene["meshes"].get(node["resource"] or "")
            if mesh is None or not len(mesh["faces"]):
                continue
            transforms = resolve(node_name)
            if not transforms:
                continue
            shaders = scene["shading"].get(node_name) or []
            shading_colors, opacities = [], []
            for shading_id in range(int(mesh["shading"].max()) + 1):
                shader = shaders[shading_id][0] if shading_id < len(shaders) and shaders[shading_id] else ""
                diffuse, opacity = scene["materials"].get(scene["shaders"].get(shader, ""), (DEFAULT_COLOR, 1.0))
                shading_colors.append(diffuse); opacities.append(opacity)
            shading_colors = np.clip(np.array(shading_colors, np.float32), 0, 1)
            triangles += len(mesh["faces"]) * len(transforms)
            if triangles > MAX_TRIANGLES:
                raise ValueError("The 3D content exceeds the 8,000,000 triangle limit.")
            part, offset, color, uniform = _part(mesh, shading_colors)
            encoded = _encode(part)
            key = "pdf3d-" + hashlib.sha256("".join(encoded.values()).encode()).hexdigest()
            assets.setdefault(key, {"name": node_name, "mesh": [encoded]})
            local = np.eye(4)
            local[:3, 3] = offset
            low, high = part["position"].min(0), part["position"].max(0)
            box = np.array([[x, y, z, 1] for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])])
            hex_color = int.from_bytes(bytes(int(round(c * 255)) for c in color)) if uniform else 0xffffff
            for number, world in enumerate(transforms):
                world = np.diag([scale, scale, scale, 1]) @ world @ local
                instances.append((world, box @ world.T, {
                    "id": f"pdf3d-{stream_index}-{len(bodies) + len(instances)}", "name": node_name, "model": key,
                    "z": 0, "height": 0, "color": hex_color, "opacity": float(min(opacities))}))
    if not instances:
        raise ValueError("The 3D content contains no viewable meshes.")
    corners = np.concatenate([box for _, box, _ in instances])[:, :3]
    center = (corners.min(0) + corners.max(0)) / 2
    for world, _, body in instances:
        world = world.copy()
        world[:3, 3] -= center  # Keep large CAD coordinates precise in float32.
        body["matrix"] = world.flatten(order="F").tolist()
        bodies.append(body)
    if unit:
        notes.append(unit)
    placeholder = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><g id="scene"/></svg>'
    return {
        "svg": placeholder, "name": path.name, "kind": "model", "warnings": notes,
        "scene3d": {"standalone": True, "thickness": 0, "board": [], "bodies": bodies, "step_models": assets},
        "stats": None,
    }
