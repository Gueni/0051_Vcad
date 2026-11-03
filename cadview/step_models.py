"""STEP asset inventories and placements; tessellation is deferred to the browser."""

import base64
import hashlib
import math
import os
from pathlib import Path
from urllib.parse import unquote
import zlib

import numpy as np
import zstandard

MAX_MODEL_BYTES = 32 * 1024 * 1024
MAX_EMBEDDED_BYTES = 64 * 1024 * 1024


def rotation(x=0, y=0, z=0):
    """Column-vector X, then Y, then Z rotation in degrees."""
    x, y, z = map(math.radians, (x, y, z))
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    return np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]) @ np.array(
        [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]) @ np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])


def matrix(linear, position):
    result = np.eye(4)
    result[:3, :3], result[:3, 3] = linear, position
    if not np.isfinite(result).all():
        raise ValueError("Non-finite STEP placement")
    return result.flatten(order="F").tolist()


def asset(data, name, compressed=False):
    if not data or len(data) > MAX_MODEL_BYTES:
        raise ValueError("STEP model exceeds the 32 MB input limit or is empty")
    key = hashlib.sha256(data).hexdigest()
    return key, {"name": name, "data": base64.b64encode(data).decode("ascii"), "compressed": compressed}


def altium_assets(pcb):
    assets, references = {}, {}
    for model, data in pcb.get_embedded_model_entries():
        if not str(model.name).lower().endswith((".step", ".stp")):
            continue
        try:
            key, entry = asset(data, model.name, compressed=True)
        except ValueError:
            continue  # The body retains its fallback and a missing-model notice.
        assets.setdefault(key, entry)
        references.setdefault(str(model.id).upper(), key)
    return assets, references


def altium_placement(body, origin, thickness, bottom):
    unit = 0.0254 / 10000
    linear = rotation(body.model_3d_rotx, body.model_3d_roty, body.model_3d_rotz)
    # Source XY and Z angles are already in board space. Bottom bodies mirror
    # Y (and Z in 3D); applying the component rotation again would double it.
    if bottom:
        linear = np.diag([1, -1, -1]) @ linear
    return matrix(linear, [body.model_2d_x * unit - origin[0], body.model_2d_y * unit - origin[1],
                           (-1 if bottom else 1) * (thickness / 2 + body.model_3d_dz * unit)])


def local_step(reference, directory):
    """Resolve project/environment-relative paths only; never fetch a remote URL."""
    directory = Path(directory) if directory is not None else Path.cwd()
    text = reference.replace("\\", "/").replace("${KIPRJMOD}", str(directory))
    text = os.path.expandvars(text)
    if "$" in text or "://" in text or Path(text).suffix.lower() not in (".step", ".stp"):
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = directory / path
    try:
        if path.stat().st_size > MAX_MODEL_BYTES:
            return None
        data = path.read_bytes()
        return data if b"ISO-10303-21" in data[:1024] else None
    except OSError:
        return None


class _EmbeddedSteps:
    """Resolve KiCad's board/footprint-local, base64/Zstandard STEP files once."""

    def __init__(self, root):
        self.board = self.index(root)
        self.cache, self.assets, self.bytes = {}, {}, 0

    @staticmethod
    def index(node):
        from .kicad_common import form, forms, value

        files = {}
        for row in forms(form(node, "embedded_files"), "file"):
            name = value(row, "name")
            if name.lower().endswith((".step", ".stp")):
                files.setdefault(name, []).append(row)
        return files

    def load(self, reference, footprint):
        from .kicad_common import form, value

        explicit = reference.startswith("kicad-embed://")
        if "://" in reference and not explicit:
            return None
        name = unquote(reference[len("kicad-embed://"):]) if explicit else reference.replace("\\", "/").rsplit("/", 1)[-1].casefold()
        row = None
        for scope in (footprint, self.board):
            matches = scope.get(name, []) if explicit else [row for path, rows in scope.items()
                if path.replace("\\", "/").rsplit("/", 1)[-1].casefold() == name for row in rows]
            if matches:
                # A local embedded file shadows the board's file, including a
                # corrupt or ambiguous one. Never silently substitute another.
                row = matches[0] if len(matches) == 1 else None
                break
        if row is None:
            return None
        identifier = id(row)
        if identifier not in self.cache:
            self.cache[identifier] = None
            try:
                parts = form(row, "data")[1:]
                if any(isinstance(part, list) for part in parts):
                    return None
                if sum(len(str(part)) for part in parts) > 4 * ((MAX_MODEL_BYTES + 2) // 3) + 2:
                    return None
                encoded = "".join(str(part) for part in parts).removeprefix("|").removesuffix("|")
                compressed = base64.b64decode(encoded, validate=True)
                limit = MAX_MODEL_BYTES
                size = zstandard.frame_content_size(compressed)
                if len(compressed) > MAX_MODEL_BYTES or size > limit:
                    return None
                # Check the frame's size before allocation. Unknown-size frames
                # also get an explicit bounded output buffer and window limit.
                data = zstandard.ZstdDecompressor(max_window_size=MAX_MODEL_BYTES // 1024).decompress(
                    compressed, max_output_size=limit + 1, allow_extra_data=False)
                if not data or len(data) > limit or b"ISO-10303-21" not in data[:1024]:
                    return None
                key = hashlib.sha256(data).hexdigest()
                # The existing browser worker accepts deflate, not Zstandard.
                # Transcode once while retaining the decoded-content cache key.
                if key not in self.assets:
                    if self.bytes + len(data) > MAX_EMBEDDED_BYTES:
                        return None
                    self.assets[key] = {"name": value(row, "name"), "data": base64.b64encode(zlib.compress(data)).decode("ascii"), "compressed": True}
                    self.bytes += len(data)
                self.cache[identifier] = key, self.assets[key]
            except (ValueError, zstandard.ZstdError):
                pass  # Missing-model UI remains available; other models load.
        return self.cache[identifier]


def kicad_models(root, directory, origin, thickness):
    from .kicad_common import angle, flag, form, forms, number, point, scalar, value

    def xyz(node, key, default):
        row = form(form(node, key), "xyz")
        return np.array([number(item) for item in row[1:4]]) if len(row) == 4 else np.array(default, dtype=float)

    assets, bodies, missing = {}, [], 0
    embedded, resolved = _EmbeddedSteps(root), {}
    for index, footprint in enumerate(forms(root, "footprint") + forms(root, "module")):
        local_files = embedded.index(footprint)
        properties = {str(p[1]): str(p[2]) for p in forms(footprint, "property") if len(p) > 2}
        name = properties.get("Reference") or next((str(t[2]) for t in forms(footprint, "fp_text")
                                                    if len(t) > 2 and str(t[1]) == "reference"), "Component")
        x, y = point(footprint)
        bottom = value(footprint, "layer") == "B.Cu"
        fp_rotation = rotation(z=angle(footprint)) @ (np.diag([1, -1, -1]) if bottom else np.eye(3))
        for model_index, model in enumerate(forms(footprint, "model")):
            if len(model) < 2 or flag(model, "hide"):
                continue
            reference = str(model[1])
            if not reference.lower().endswith((".step", ".stp")):
                missing += 1
                continue
            offset = xyz(model, "offset", [0, 0, 0])
            if not form(model, "offset") and form(model, "at"):
                offset = xyz(model, "at", [0, 0, 0]) * 25.4  # KiCad 5 legacy inches.
            position = np.array([x - origin[0], origin[1] - y, (-1 if bottom else 1) * thickness / 2]) + fp_rotation @ offset
            transform = fp_rotation @ rotation(*(-xyz(model, "rotate", [0, 0, 0]))) @ np.diag(xyz(model, "scale", [1, 1, 1]))
            key = "external-" + hashlib.sha256(reference.encode()).hexdigest()
            lookup = (index if local_files else -1, reference)
            if lookup not in resolved:
                data = local_step(reference, directory) if not reference.startswith("kicad-embed://") else None
                resolved[lookup] = asset(data, reference) if data else embedded.load(reference, local_files)
            if resolved[lookup]:
                key, entry = resolved[lookup]
                assets.setdefault(key, entry)
            else:
                assets.setdefault(key, {"name": reference})
                missing += 1
            bodies.append({"id": f"kc-body-{index}-{model_index}", "name": name, "model": key,
                           "matrix": matrix(transform, position), "color": 0x999999,
                           "opacity": max(0, min(1, scalar(model, "opacity", 1)))})
    return assets, bodies, missing
