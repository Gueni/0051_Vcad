"""Render Gerber (RS-274X / X2) and Excellon drill sets as a layered PCB view.

Each file becomes one layer. Geometry is built exactly with shapely: draws are
buffered by their aperture, flashes place aperture shapes, regions become
polygons, and clear polarity subtracts from what was drawn before it.
"""

import ast
import io
import json
import math
import operator
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import shapely
from shapely import affinity
from shapely.geometry import GeometryCollection, LineString, Point, Polygon, box
from shapely.ops import polygonize_full, unary_union

from .kicad_common import element, svg_document
from .layer_colors import layer_colors

ARC_STEP = math.radians(4)
QUAD = 8  # buffer segments per quarter circle for pads and holes
STROKE_QUAD = 4  # and for draw end caps
OUTLINE_SNAP = 0.13  # mm
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_SET_BYTES = 256 * 1024 * 1024
DRILL_FILL = "#1b1d21"
EMPTY = GeometryCollection()


# ----------------------------------------------------------------- layer identity

LAYER_NAMES = {
    "TOP": "Top Layer", "BOTTOM": "Bottom Layer", "TOPOVERLAY": "Top Overlay", "BOTTOMOVERLAY": "Bottom Overlay",
    "TOPSOLDER": "Top Solder", "BOTTOMSOLDER": "Bottom Solder", "TOPPASTE": "Top Paste", "BOTTOMPASTE": "Bottom Paste",
    "BOARD_OUTLINE": "Board Outline", "DRILLS": "Drills",
}
EXTENSIONS = {
    "gtl": "TOP", "cmp": "TOP", "top": "TOP", "gbl": "BOTTOM", "sol": "BOTTOM", "bot": "BOTTOM",
    "gto": "TOPOVERLAY", "plc": "TOPOVERLAY", "gbo": "BOTTOMOVERLAY", "pls": "BOTTOMOVERLAY",
    "gts": "TOPSOLDER", "stc": "TOPSOLDER", "gbs": "BOTTOMSOLDER", "sts": "BOTTOMSOLDER",
    "gtp": "TOPPASTE", "crc": "TOPPASTE", "gbp": "BOTTOMPASTE", "crs": "BOTTOMPASTE",
    "gko": "BOARD_OUTLINE", "gml": "BOARD_OUTLINE", "dim": "BOARD_OUTLINE", "outline": "BOARD_OUTLINE",
}
KICAD_NAMES = [
    (r"f[._]cu", "TOP"), (r"b[._]cu", "BOTTOM"), (r"in(\d+)[._]cu", "MID{}"),
    (r"f[._]silk(s|screen)", "TOPOVERLAY"), (r"b[._]silk(s|screen)", "BOTTOMOVERLAY"),
    (r"f[._]mask", "TOPSOLDER"), (r"b[._]mask", "BOTTOMSOLDER"),
    (r"f[._]paste", "TOPPASTE"), (r"b[._]paste", "BOTTOMPASTE"), (r"edge[._]cuts", "BOARD_OUTLINE"),
]


def layer_identity(name, text):
    """Return (token, display name, role) from X2 attributes or the filename."""
    function = re.search(r"TF\.FileFunction,([^*%\r\n]+)", text)
    if function:
        parts = [part.strip() for part in function[1].split(",")]
        kind, side = parts[0].lower(), (parts[-1].lower() if len(parts) > 1 else "")
        if kind == "copper" and len(parts) >= 3:
            number = re.sub(r"\D", "", parts[1]) or "0"
            token = "TOP" if side == "top" else "BOTTOM" if side == "bot" else f"MID{int(number) - 1}"
        else:
            token = {("soldermask", "top"): "TOPSOLDER", ("soldermask", "bot"): "BOTTOMSOLDER",
                     ("legend", "top"): "TOPOVERLAY", ("legend", "bot"): "BOTTOMOVERLAY",
                     ("paste", "top"): "TOPPASTE", ("paste", "bot"): "BOTTOMPASTE"}.get((kind, side))
            if kind == "profile":
                token = "BOARD_OUTLINE"
        if token:
            return token
    lower = name.lower()
    extension = lower.rsplit(".", 1)[-1] if "." in lower else ""
    if extension in EXTENSIONS:
        return EXTENSIONS[extension]
    for pattern, token in ((r"g(\d+)", "MID{}"), (r"gp(\d+)", "PLANE{}"), (r"gm(\d+)", "MECHANICAL{}")):
        if match := re.fullmatch(pattern, extension):
            return token.format(int(match[1]))
    for pattern, token in KICAD_NAMES:
        if match := re.search(pattern, lower):
            return token.format(int(match[1]) if "{}" in token else "")
    return None


def role_of(token):
    if token in ("TOP", "BOTTOM") or re.fullmatch(r"(MID|PLANE)\d+", token):
        return "copper"
    if token == "DRILLS":
        return "drill"
    if token.endswith("SOLDER"):
        return "mask"
    if token.endswith("PASTE"):
        return "paste"
    return "mechanical"


def layer_order(token):
    """Physical paint order: bottom side first, top side last (SVG paints later layers on top)."""
    bottom = ["BOTTOMOVERLAY", "BOTTOMSOLDER", "BOTTOMPASTE", "BOTTOM"]
    top = ["TOP", "TOPPASTE", "TOPSOLDER", "TOPOVERLAY"]
    if token in bottom:
        return (0, bottom.index(token))
    if match := re.fullmatch(r"(MID|PLANE)(\d+)", token):
        return (1, -int(match[2]))
    if token in top:
        return (2, top.index(token))
    return (3, {"BOARD_OUTLINE": "~1", "DRILLS": "~2"}.get(token, token))


# ------------------------------------------------------------- aperture macros

_OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
              ast.USub: operator.neg, ast.UAdd: operator.pos}


def evaluate(expression, variables):
    """Evaluate a macro arithmetic expression (+ - x / and $n variables) without eval()."""
    text = re.sub(r"\$(\d+)", r"_v\1", expression.strip().replace("x", "*").replace("X", "*"))

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id.startswith("_v"):
            return variables.get(int(node.id[2:]), 0.0)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            right = walk(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                return 0.0
            return _OPERATORS[type(node.op)](walk(node.left), right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
            return _OPERATORS[type(node.op)](walk(node.operand))
        raise ValueError(f"Unsupported aperture macro expression: {expression}")

    return walk(ast.parse(text or "0", mode="eval"))


def _rotate(geometry, degrees):
    return affinity.rotate(geometry, degrees, origin=(0, 0)) if degrees else geometry


def _regular_polygon(diameter, vertices, rotation=0.0, cx=0.0, cy=0.0):
    vertices = max(3, int(round(vertices)))
    return Polygon([(cx + diameter / 2 * math.cos(math.radians(rotation) + i * math.tau / vertices),
                     cy + diameter / 2 * math.sin(math.radians(rotation) + i * math.tau / vertices))
                    for i in range(vertices)])


def macro_shape(body, parameters, scale):
    """Build a macro aperture at the origin (file units are converted by ``scale``)."""
    variables = {index: value for index, value in enumerate(parameters, 1)}
    shape = EMPTY
    for statement in body:
        statement = statement.strip()
        if not statement or statement.startswith("0"):
            continue
        if statement.startswith("$"):
            name, expression = statement.split("=", 1)
            variables[int(name[1:])] = evaluate(expression, variables)
            continue
        values = [evaluate(item, variables) for item in statement.split(",")]
        code, args = int(values[0]), values[1:]
        exposure, geometry = True, None
        if code == 1:  # circle: exposure, diameter, x, y[, rotation]
            exposure = args[0] != 0
            geometry = _rotate(Point(args[2] * scale, args[3] * scale).buffer(args[1] * scale / 2, QUAD),
                               args[4] if len(args) > 4 else 0)
        elif code in (2, 20):  # vector line: exposure, width, x1, y1, x2, y2, rotation
            exposure = args[0] != 0
            (x1, y1), (x2, y2) = (args[2] * scale, args[3] * scale), (args[4] * scale, args[5] * scale)
            length = math.hypot(x2 - x1, y2 - y1)
            if length:
                angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
                line = box(0, -args[1] * scale / 2, length, args[1] * scale / 2)
                geometry = affinity.translate(affinity.rotate(line, angle, origin=(0, 0)), x1, y1)
                geometry = _rotate(geometry, args[6] if len(args) > 6 else 0)
        elif code == 21:  # center line: exposure, width, height, cx, cy, rotation
            exposure = args[0] != 0
            w, h, cx, cy = (value * scale for value in args[1:5])
            geometry = _rotate(box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), args[5] if len(args) > 5 else 0)
        elif code == 22:  # lower-left line (deprecated): exposure, width, height, x, y, rotation
            exposure = args[0] != 0
            w, h, x, y = (value * scale for value in args[1:5])
            geometry = _rotate(box(x, y, x + w, y + h), args[5] if len(args) > 5 else 0)
        elif code == 4:  # outline: exposure, n, x0, y0, ..., rotation
            exposure, count = args[0] != 0, int(args[1])
            points = [(args[2 + 2 * i] * scale, args[3 + 2 * i] * scale) for i in range(count + 1)]
            rotation = args[4 + 2 * count] if len(args) > 4 + 2 * count else 0
            geometry = _rotate(Polygon(points).buffer(0), rotation)
        elif code == 5:  # polygon: exposure, vertices, cx, cy, diameter, rotation
            exposure = args[0] != 0
            geometry = _rotate(_regular_polygon(args[4] * scale, args[1], 0, args[2] * scale, args[3] * scale),
                               args[5] if len(args) > 5 else 0)
        elif code == 6:  # moire: cx, cy, outer, thickness, gap, rings, cross thickness, cross length, rotation
            cx, cy, outer, thickness, gap = (value * scale for value in args[:5])
            parts, diameter = [], outer
            for _ in range(int(args[5])):
                if diameter <= 0:
                    break
                inner = max(diameter - 2 * thickness, 0)
                parts.append(Point(cx, cy).buffer(diameter / 2, QUAD).difference(Point(cx, cy).buffer(inner / 2, QUAD)))
                diameter = inner - 2 * gap
            cross_t, cross_l = args[6] * scale, args[7] * scale
            parts += [box(cx - cross_l / 2, cy - cross_t / 2, cx + cross_l / 2, cy + cross_t / 2),
                      box(cx - cross_t / 2, cy - cross_l / 2, cx + cross_t / 2, cy + cross_l / 2)]
            geometry = _rotate(unary_union(parts), args[8] if len(args) > 8 else 0)
        elif code == 7:  # thermal: cx, cy, outer, inner, gap, rotation
            cx, cy, outer, inner, gap = (value * scale for value in args[:5])
            ring = Point(cx, cy).buffer(outer / 2, QUAD).difference(Point(cx, cy).buffer(inner / 2, QUAD))
            cross = unary_union([box(cx - outer, cy - gap / 2, cx + outer, cy + gap / 2),
                                 box(cx - gap / 2, cy - outer, cx + gap / 2, cy + outer)])
            geometry = _rotate(ring.difference(cross), args[5] if len(args) > 5 else 0)
        if geometry is None or geometry.is_empty:
            continue
        shape = shape.union(geometry) if exposure else shape.difference(geometry)
    return shape


# ------------------------------------------------------------------- Gerber

class _Layer:
    """Geometry in drawing order; consecutive same-polarity shapes are merged together."""

    def __init__(self):
        self.parts, self.lines = [], []

    def add(self, geometry, dark):
        if geometry is None or geometry.is_empty:
            return
        if not self.parts or self.parts[-1][0] != dark:
            self.parts.append((dark, []))
        self.parts[-1][1].append(geometry)

    def geometry(self):
        if all(dark for dark, _ in self.parts):
            # Nothing is subtracted: skip the costly union; the path is drawn
            # with the nonzero rule, so overlapping shapes simply stay filled.
            return GeometryCollection([shape for _, shapes in self.parts for shape in shapes])
        result = EMPTY
        for dark, shapes in self.parts:
            merged = shapely.union_all(shapes)
            result = result.union(merged) if dark else result.difference(merged)
        return result


class GerberParser:
    def __init__(self, text):
        self.text = text
        self.integer, self.decimal, self.trailing, self.incremental = 2, 4, False, False
        self.scale = 1.0  # file units to millimetres
        self.apertures, self.macros = {}, {}
        self.aperture = None
        self.x = self.y = 0.0
        self.mode, self.multi_quadrant = "G01", True
        self.region, self.contour = False, []
        self.contours = []
        self.dark = True
        self.repeat = None
        self.layer = _Layer()
        self.stats = {"flashes": 0, "draws": 0, "regions": 0}
        self.negative = bool(re.search(r"TF\.FilePolarity,Negative", text))

    # Coordinates -----------------------------------------------------------
    def coordinate(self, text):
        if "." in text:
            return float(text) * self.scale
        sign = -1 if text.startswith("-") else 1
        digits = text.lstrip("+-")
        if self.trailing:
            digits = digits.ljust(self.integer + self.decimal, "0")
        return sign * int(digits or "0") / 10 ** self.decimal * self.scale

    # Apertures ---------------------------------------------------------------
    def define_aperture(self, body):
        match = re.fullmatch(r"ADD(\d+)([A-Za-z_.$][\w.$]*)(?:,(.*))?", body, re.S)
        if not match:
            return
        code, name, raw = int(match[1]), match[2], match[3] or ""
        values = [float(item) for item in raw.split("X") if item.strip()] if raw else []
        s = self.scale
        hole = None
        if name == "C":
            shape = Point(0, 0).buffer(values[0] * s / 2, QUAD) if values and values[0] > 0 else EMPTY
            hole = values[1] if len(values) > 1 else None
            self.apertures[code] = (shape, values[0] * s / 2 if values else 0)
        elif name in ("R", "O"):
            w, h = values[0] * s, values[1] * s
            if name == "R":
                shape = box(-w / 2, -h / 2, w / 2, h / 2)
            else:
                radius = min(w, h) / 2
                shape = LineString([(-(w / 2 - radius), -(h / 2 - radius)), (w / 2 - radius, h / 2 - radius)]).buffer(radius, QUAD) \
                    if w != h else Point(0, 0).buffer(radius, QUAD)
            hole = values[2] if len(values) > 2 else None
            self.apertures[code] = (shape, None)
        elif name == "P":
            shape = _regular_polygon(values[0] * s, values[1], values[2] if len(values) > 2 else 0)
            hole = values[3] if len(values) > 3 else None
            self.apertures[code] = (shape, None)
        elif name in self.macros:
            self.apertures[code] = (macro_shape(self.macros[name], values, s), None)
        else:
            self.apertures[code] = (EMPTY, None)
        if hole:
            shape, radius = self.apertures[code]
            self.apertures[code] = (shape.difference(Point(0, 0).buffer(hole * s / 2, QUAD)), radius)

    # Drawing ---------------------------------------------------------------
    def emit(self, geometry):
        if self.repeat is not None:
            self.repeat[-1].append((geometry, self.dark))
        else:
            self.layer.add(geometry, self.dark)

    def arc_points(self, x0, y0, x1, y1, i, j, clockwise):
        if self.multi_quadrant:
            cx, cy = x0 + i, y0 + j
        else:
            # Single-quadrant offsets are unsigned; pick the center that fits a <= 90 degree arc.
            best = None
            for sx in (1, -1):
                for sy in (1, -1):
                    cx, cy = x0 + sx * abs(i), y0 + sy * abs(j)
                    r0, r1 = math.hypot(x0 - cx, y0 - cy), math.hypot(x1 - cx, y1 - cy)
                    a0, a1 = math.atan2(y0 - cy, x0 - cx), math.atan2(y1 - cy, x1 - cx)
                    sweep = (a0 - a1) % math.tau if clockwise else (a1 - a0) % math.tau
                    error = abs(r0 - r1) + (0 if sweep <= math.pi / 2 + 1e-6 else 1e9)
                    if best is None or error < best[0]:
                        best = (error, cx, cy)
            _, cx, cy = best
        radius = math.hypot(x0 - cx, y0 - cy)
        a0, a1 = math.atan2(y0 - cy, x0 - cx), math.atan2(y1 - cy, x1 - cx)
        if clockwise:
            sweep = -((a0 - a1) % math.tau)
        else:
            sweep = (a1 - a0) % math.tau
        if abs(sweep) < 1e-9 and self.multi_quadrant:
            sweep = -math.tau if clockwise else math.tau  # coincident ends: full circle
        steps = max(2, int(abs(sweep) / ARC_STEP) + 1)
        points = [(cx + radius * math.cos(a0 + sweep * k / steps), cy + radius * math.sin(a0 + sweep * k / steps))
                  for k in range(steps + 1)]
        points[-1] = (x1, y1)
        return points

    def stroke(self, points):
        shape, radius = self.apertures.get(self.aperture, (EMPTY, None))
        if len(points) > 1:
            self.layer.lines.append(LineString(points))
        if radius is not None:  # circular aperture
            if radius <= 0:
                return
            geometry = LineString(points).buffer(radius, STROKE_QUAD) if len(points) > 1 and LineString(points).length > 0 \
                else Point(points[0]).buffer(radius, STROKE_QUAD)
        elif shape.is_empty:
            return
        else:
            # Non-circular draws are deprecated; sweep the aperture between consecutive points.
            geometry = unary_union([shapely.convex_hull(unary_union([affinity.translate(shape, *a), affinity.translate(shape, *b)]))
                                    for a, b in zip(points, points[1:])])
        self.stats["draws"] += 1
        self.emit(geometry)

    def flash(self, x, y):
        shape, _ = self.apertures.get(self.aperture, (EMPTY, None))
        if not shape.is_empty:
            self.stats["flashes"] += 1
            self.emit(affinity.translate(shape, x, y))

    def close_contour(self):
        if len(self.contour) >= 3:
            self.contours.append(self.contour)
        self.contour = []

    def end_region(self):
        self.close_contour()
        polygons = []
        for contour in self.contours:
            polygon = Polygon(contour)
            polygons.append(polygon if polygon.is_valid else polygon.buffer(0))
        if polygons:
            self.stats["regions"] += 1
            self.emit(unary_union(polygons))
        self.contours, self.region = [], False

    def operation(self, words):
        x = self.coordinate(words["X"]) if "X" in words else None
        y = self.coordinate(words["Y"]) if "Y" in words else None
        if self.incremental:
            x = self.x + (x or 0); y = self.y + (y or 0)
        x = self.x if x is None else x
        y = self.y if y is None else y
        code = words.get("D")
        if code == 1:
            if self.mode == "G01":
                points = [(self.x, self.y), (x, y)]
            else:
                i = self.coordinate(words["I"]) if "I" in words else 0.0
                j = self.coordinate(words["J"]) if "J" in words else 0.0
                points = self.arc_points(self.x, self.y, x, y, i, j, self.mode == "G02")
            if self.region:
                if not self.contour:
                    self.contour = [points[0]]
                self.contour.extend(points[1:])
            else:
                self.stroke(points)
        elif code == 2:
            if self.region:
                self.close_contour()
        elif code == 3:
            self.flash(x, y)
        self.x, self.y = x, y

    # Commands --------------------------------------------------------------
    def extended(self, block):
        for command in [part for part in block.split("*") if part.strip()]:
            command = command.strip()
            head = command[:2]
            if head == "FS":
                match = re.search(r"FS([LTD]?)([AI]?).*?X(\d)(\d)", command)
                if match:
                    self.trailing = match[1] == "T"
                    self.incremental = match[2] == "I"
                    self.integer, self.decimal = int(match[3]), int(match[4])
            elif head == "MO":
                self.scale = 25.4 if "IN" in command else 1.0
            elif head == "AD":
                self.define_aperture(command)
            elif head == "LP":
                self.dark = command[2:3] != "C"
            elif head == "SR":
                self.finish_repeat()
                match = re.fullmatch(r"SR(?:X(\d+)Y(\d+)I([\d.+-]+)J([\d.+-]+))?", command)
                if match and match[1]:
                    nx, ny = int(match[1]), int(match[2])
                    dx, dy = float(match[3]) * self.scale, float(match[4]) * self.scale
                    if nx > 1 or ny > 1:
                        self.repeat = [(nx, ny, dx, dy), []]

    def finish_repeat(self):
        if self.repeat is None:
            return
        (nx, ny, dx, dy), shapes = self.repeat
        self.repeat = None
        for ix in range(nx):
            for iy in range(ny):
                for geometry, dark in shapes:
                    self.layer.add(affinity.translate(geometry, ix * dx, iy * dy), dark)

    def parse(self):
        text = self.text
        position = 0
        word = re.compile(r"([GMDXYIJ])([+-]?[\d.]+)")
        for match in re.finditer(r"%([^%]*)%|([^%*]*)\*", text):
            if match[1] is not None:
                block = match[1].strip()
                if block.startswith("AM"):
                    name, _, body = block[2:].partition("*")
                    self.macros[name.strip()] = [part for part in body.replace("\r", "").replace("\n", "").split("*")]
                else:
                    self.extended(block.replace("\r", "").replace("\n", ""))
                continue
            statement = match[2].strip()
            if not statement or statement.startswith("G04"):
                continue
            words = {}
            for letter, value in word.findall(statement):
                if letter in "GMD" and "." not in value:
                    number = int(value)
                    if letter == "G":
                        if number in (1, 2, 3):
                            self.mode = f"G0{number}"
                        elif number == 36:
                            self.region, self.contours, self.contour = True, [], []
                        elif number == 37:
                            self.end_region()
                        elif number == 74:
                            self.multi_quadrant = False
                        elif number == 75:
                            self.multi_quadrant = True
                        elif number == 70:
                            self.scale = 25.4
                        elif number == 71:
                            self.scale = 1.0
                        elif number == 90:
                            self.incremental = False
                        elif number == 91:
                            self.incremental = True
                    elif letter == "D":
                        if number >= 10:
                            self.aperture = number
                        else:
                            words["D"] = number
                    elif letter == "M" and number in (0, 2):
                        self.finish_repeat()
                        return self
                else:
                    words[letter] = value
            if "D" in words or (("X" in words or "Y" in words) and self.last_d):
                words.setdefault("D", self.last_d)
                self.last_d = words["D"]
                self.operation(words)
        self.finish_repeat()
        return self

    last_d = None


# ------------------------------------------------------------------ Excellon

class DrillParser:
    def __init__(self, text):
        self.text = text
        self.metric, self.trailing = True, False
        self.integer, self.decimal = 3, 3
        self.tools, self.tool = {}, None
        self.holes, self.routes = [], []
        self.x = self.y = 0.0
        self.mode, self.down, self.path = "G05", False, []

    def coordinate(self, text):
        scale = 1.0 if self.metric else 25.4
        if "." in text:
            return float(text) * scale
        sign = -1 if text.startswith("-") else 1
        digits = text.lstrip("+-")
        if self.trailing:
            digits = digits.ljust(self.integer + self.decimal, "0")
        return sign * int(digits or "0") / 10 ** self.decimal * scale

    def diameter(self):
        return self.tools.get(self.tool, 0.0)

    def end_path(self):
        if len(self.path) > 1 and self.diameter() > 0:
            self.routes.append((LineString(self.path), self.diameter()))
        self.path = []

    def parse(self):
        format_set = False
        for raw in self.text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith(";"):
                if match := re.search(r"FILE_FORMAT=(\d+):(\d+)", line):
                    self.integer, self.decimal, format_set = int(match[1]), int(match[2]), True
                continue
            upper = line.upper()
            if upper.startswith(("METRIC", "INCH")):
                self.metric = upper.startswith("METRIC")
                self.trailing = ",TZ" in upper
                if not format_set:
                    self.integer, self.decimal = (3, 3) if self.metric else (2, 4)
                if match := re.search(r",(0+)\.(0+)", upper):
                    self.integer, self.decimal, format_set = len(match[1]), len(match[2]), True
                continue
            if upper in ("M71",):
                self.metric = True
                continue
            if upper in ("M72",):
                self.metric = False
                continue
            if match := re.fullmatch(r"T(\d+)(?:[FSBHZ][\d.+-]*)*C([\d.]+).*", upper):
                self.tools[int(match[1])] = float(match[2]) * (1.0 if self.metric else 25.4)
                continue
            if match := re.fullmatch(r"T(\d+)", upper):
                self.end_path()
                self.tool = int(match[1])
                continue
            if upper in ("M15",):
                self.down, self.path = True, [(self.x, self.y)]
                continue
            if upper in ("M16", "M17"):
                self.end_path(); self.down = False
                continue
            if upper.startswith(("M30", "M00")):
                break
            mode = re.match(r"G0?([0-5]|85)", upper)
            if mode and mode.group(1) in ("0", "5"):
                self.end_path()
                self.mode = "G00" if mode.group(1) == "0" else "G05"
            elif mode and mode.group(1) in ("1", "2", "3"):
                self.mode = "G0" + mode.group(1)
            if "G85" in upper:  # slot: X1Y1G85X2Y2
                first, second = upper.split("G85", 1)
                start = self.point(first)
                end = self.point(second)
                if self.diameter() > 0:
                    self.routes.append((LineString([start, end]), self.diameter()))
                continue
            if "X" in upper or "Y" in upper:
                x, y = self.point(upper)
                if self.mode == "G05":
                    if self.diameter() > 0:
                        self.holes.append((x, y, self.diameter()))
                elif self.mode in ("G01", "G02", "G03") and self.down:
                    if self.mode == "G01" or "A" not in upper:
                        self.path.append((x, y))
                    else:
                        self.path.extend(self.arc(upper, x, y)[1:])
                elif self.mode == "G00":
                    self.path = [(x, y)] if self.down else []
        self.end_path()
        return self

    def point(self, text):
        x = re.search(r"X([+-]?[\d.]+)", text)
        y = re.search(r"Y([+-]?[\d.]+)", text)
        self.x = self.coordinate(x[1]) if x else self.x
        self.y = self.coordinate(y[1]) if y else self.y
        return self.x, self.y

    def arc(self, text, x1, y1):
        x0, y0 = self.path[-1] if self.path else (x1, y1)
        radius = self.coordinate(re.search(r"A([+-]?[\d.]+)", text)[1])
        chord = math.hypot(x1 - x0, y1 - y0)
        if chord == 0 or radius < chord / 2:
            return [(x0, y0), (x1, y1)]
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        offset = math.sqrt(radius ** 2 - (chord / 2) ** 2)
        clockwise = self.mode == "G02"
        nx, ny = -(y1 - y0) / chord, (x1 - x0) / chord
        cx, cy = (mx - nx * offset, my - ny * offset) if clockwise else (mx + nx * offset, my + ny * offset)
        a0, a1 = math.atan2(y0 - cy, x0 - cx), math.atan2(y1 - cy, x1 - cx)
        sweep = -((a0 - a1) % math.tau) if clockwise else (a1 - a0) % math.tau
        steps = max(2, int(abs(sweep) / ARC_STEP) + 1)
        return [(cx + radius * math.cos(a0 + sweep * k / steps), cy + radius * math.sin(a0 + sweep * k / steps))
                for k in range(steps + 1)]

    def geometry(self):
        holes = [Point(x, y).buffer(d / 2, QUAD) for x, y, d in self.holes]
        routes = [line.buffer(d / 2, STROKE_QUAD) for line, d in self.routes]
        return shapely.union_all(holes + routes) if holes or routes else EMPTY


# --------------------------------------------------------------- file sets

GERBER_EXTENSION = re.compile(r"(gbr|ger|pho|art|gtl|gbl|gto|gbo|gts|gbs|gtp|gbp|gko|gml|g\d+|gp\d+|gm\d+|"
                              r"cmp|sol|plc|pls|stc|sts|crc|crs|drl|xln|exc|drd|gerbers)", re.I)


def is_gerber(path: Path) -> bool:
    return bool(GERBER_EXTENSION.fullmatch(path.suffix[1:]))


def bundle(paths) -> bytes:
    """Pack several Gerber/drill files into one .gerbers document (as the browser does)."""
    import base64
    return json.dumps({"files": [{"name": path.name, "data": base64.b64encode(path.read_bytes()).decode("ascii")}
                                 for path in paths]}).encode()


def sniff(name, data):
    """Classify a file as 'gerber', 'drill' or None from its content."""
    head = data[:65536].decode("latin-1", "replace")
    if re.search(r"%FS[LTD]?[AI]?.*X\d\dY\d\d", head) or re.search(r"%MO(MM|IN)\*%", head):
        return "gerber"
    if head.lstrip().startswith("M48") or re.search(r"^(METRIC|INCH)\b", head, re.M) and re.search(r"^T\d+.*C[\d.]+", head, re.M):
        return "drill"
    return None


def read_set(path: Path):
    """Return [(name, bytes)] for a Gerber file, a set (JSON bundle or folder) or a .zip."""
    suffix = path.suffix.lower()
    if path.is_dir():
        files = [(item.name, item.read_bytes()) for item in sorted(path.iterdir())
                 if item.is_file() and item.stat().st_size <= MAX_FILE_BYTES]
    elif suffix == ".zip":
        files = []
        with zipfile.ZipFile(path) as archive:
            total = 0
            for info in archive.infolist():
                if info.is_dir() or info.file_size > MAX_FILE_BYTES:
                    continue
                total += info.file_size
                if total > MAX_SET_BYTES:
                    raise ValueError("The archive's Gerber files exceed the 256 MB limit.")
                files.append((info.filename.rsplit("/", 1)[-1], archive.read(info)))
    elif suffix == ".gerbers":
        import base64
        bundle = json.loads(path.read_text(encoding="utf-8"))
        files = [(item["name"], base64.b64decode(item["data"])) for item in bundle["files"]]
    else:
        files = [(path.name, path.read_bytes())]
    return files


def _polygons(geometry):
    if geometry.geom_type == "Polygon":
        if not geometry.is_empty:
            yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _polygons(part)


def _path(polygon):
    """SVG path data for one polygon and its holes, at 1 micron resolution."""
    def ring(coords):
        return "M" + "L".join(f"{x:.3f} {y:.3f}".replace(".000", "") for x, y in coords[:-1]) + "Z"
    return ring(list(polygon.exterior.coords)) + "".join(ring(list(hole.coords)) for hole in polygon.interiors)


def _profile(lines):
    if not lines:
        return EMPTY
    # Outline strokes overlap at corners instead of meeting exactly (Altium
    # leaves gaps of tens of microns under a 0.25 mm aperture); snap them closed.
    merged = unary_union(lines)
    faces, _cuts, _dangles, _invalid = polygonize_full(unary_union(shapely.snap(merged, merged, OUTLINE_SNAP)))
    rings = sorted((Polygon(face.exterior) for face in faces.geoms), key=lambda polygon: polygon.area, reverse=True)
    profile = EMPTY
    for ring in rings:  # alternate nested rings: board, cutout, island ...
        profile = profile.symmetric_difference(ring)
    return profile


def render_gerbers(path: Path) -> dict:
    files = read_set(path)
    scene = ET.Element("g", {"id": "scene"})
    layers, warnings, ignored = [], set(), []
    drills = EMPTY
    stats = dict(components=0, pads=0, tracks=0, vias=0, polygons=0, regions=0)
    for name, data in files:
        kind = sniff(name, data)
        if kind is None:
            ignored.append(name)
            continue
        text = data.decode("latin-1")
        if kind == "drill":
            parser = DrillParser(text).parse()
            geometry = parser.geometry()
            stats["vias"] += len(parser.holes)
            plated = "non" not in name.lower() and "npth" not in name.lower()
            if "rout" in name.lower() and not parser.holes:
                layers.append(("MECHANICAL_ROUT", "Board Edge Rout", name, geometry, [line for line, _ in parser.routes]))
            else:
                drills = drills.union(geometry)
            continue
        try:
            parser = GerberParser(text).parse()
        except (ValueError, IndexError, KeyError) as error:
            warnings.add(f"{name}: {error}")
            continue
        token = layer_identity(name, text) or f"GERBER_{len(layers)}"
        geometry = parser.layer.geometry()
        stats["pads"] += parser.stats["flashes"]
        stats["tracks"] += parser.stats["draws"]
        stats["regions"] += parser.stats["regions"]
        layers.append((token, LAYER_NAMES.get(token) or (token.title() if not token.startswith("GERBER_") else name),
                       name, geometry, parser.layer.lines, parser.negative))
    if not layers and drills.is_empty:
        raise ValueError("No Gerber or Excellon drill files were found." +
                         (f" Ignored: {', '.join(ignored[:8])}" if ignored else ""))
    if not drills.is_empty:
        layers.append(("DRILLS", "Drills", "", drills, []))
    # One token per layer; repeated tokens (e.g. two outline files) get a suffix.
    seen, unique = {}, []
    for token, *rest in layers:
        seen[token] = seen.get(token, 0) + 1
        unique.append((token if seen[token] == 1 else f"{token}_{seen[token]}", *rest))
    layers = sorted(unique, key=lambda layer: layer_order(layer[0]))
    outline = next((layer for layer in layers if layer[0] == "BOARD_OUTLINE"), None)
    profile = _profile(outline[4]) if outline else EMPTY
    colors = layer_colors([layer[0] for layer in layers])
    bounds, count = [], 0
    for token, label, filename, geometry, _lines, *flags in layers:
        if flags and flags[0] and role_of(token) == "copper" and not profile.is_empty:
            # A negative copper image (plane) draws the clearances. Negative
            # mask files simply draw their openings, as the viewer expects.
            geometry = profile.difference(geometry)
        if geometry.is_empty:
            continue
        geometry = affinity.scale(geometry, 1, -1, origin=(0, 0))  # Gerber Y up; SVG Y down
        if not geometry.is_empty:
            bounds.append(geometry.bounds)
        role = role_of(token)
        color = colors[token]
        group = element(scene, "g", id="board-outline" if token == "BOARD_OUTLINE" else f"gb-layer-{len(scene)}",
                        data_layer_name=label, data_layer_token=token, data_layer_role=role,
                        data_color=color, fill=color, stroke=color, stroke_linecap="round", stroke_linejoin="round")
        if filename:
            element(group, "title", text=filename)
        # One path per polygon keeps every path a simple valid polygon for the
        # WebGL compiler; overlapping shapes on a layer are separate paths.
        extra = {"fill": DRILL_FILL, "data_primitive": "pad-hole", "data_hole_owner": "pad"} if token == "DRILLS" else {}
        for polygon in _polygons(geometry):
            count += 1
            element(group, "path", id=f"gb-{count}", d=_path(polygon), fill_rule="evenodd", stroke="none", **extra)
    if not bounds:
        raise ValueError("The Gerber files contain no drawable geometry.")
    extent = (min(b[0] for b in bounds), min(b[1] for b in bounds), max(b[2] for b in bounds), max(b[3] for b in bounds))
    copper = sorted((layer[0] for layer in layers if role_of(layer[0]) == "copper"),
                    key=lambda token: (token != "TOP", token == "BOTTOM", layer_order(token)[1]))
    shapes, notes = [], []
    surface = {}
    if not profile.is_empty:
        flipped = affinity.scale(profile, 1, -1, origin=(0, 0))
        center = flipped.centroid
        drilled = affinity.scale(drills, 1, -1, origin=(0, 0)) if not drills.is_empty else EMPTY
        solid = flipped.difference(drilled) if not drilled.is_empty else flipped

        def ring(coords):
            return [[round(x - center.x, 6), round(center.y - y, 6)] for x, y in list(coords)[:-1]]

        polygons = [polygon for polygon in _polygons(solid) if polygon.area > 0.01]  # drop slivers from edge drills
        shapes = [{"outline": ring(p.exterior.coords), "holes": [ring(h.coords) for h in p.interiors]} for p in polygons]
        surface = {"surface_origin": [center.x, center.y]}
        notes.append("Board thickness is not stored in Gerber files; showing 1.6 mm.")
    else:
        notes.append("3D is unavailable: no closed board outline (profile/keep-out layer) was found.")
    if ignored:
        warnings.add(f"Skipped {len(ignored)} file{'s' if len(ignored) != 1 else ''} that are not Gerber or drill data.")
    title = path.name if path.is_dir() else path.stem if path.suffix.lower() in (".gerbers", ".zip") else path.name
    return {
        "svg": svg_document(scene, extent), "name": title, "kind": "pcb", "warnings": sorted(warnings),
        "stats": stats, "copper_layer_order": copper, "polygon_elements": [], "layout_components": [],
        "scene3d": {"board": shapes, "thickness": 1.6, "bodies": [], "step_models": {}, "notes": notes, "units": "mm", **surface},
    }
