"""Compile SVG paint once into retained, indexed layout geometry.

Qt remains responsible for document parsing, bounds and exact hit testing. This
worker-side compiler emits analytic shape quads and triangulated filled paths;
unsupported SVG composition falls back for the entire document.
"""

import math
import re
import time
from array import array
from collections import defaultdict

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPainterPathStroker, QTransform
from shapely import constrained_delaunay_triangles, get_coordinates
from shapely.errors import GEOSException
from shapely.geometry import MultiLineString, Polygon
from shapely.ops import polygonize, unary_union

NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
TOKENS = re.compile(r"[A-Za-z]|" + NUMBER)
SPACED_LINEAR = re.compile(r"[^MLZ0-9eE+.\-\s]")
TRANSFORMS = re.compile(r"([A-Za-z]+)\s*\(([^)]*)\)")
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
PAINT = (
    "fill",
    "stroke",
    "stroke-width",
    "fill-rule",
    "stroke-linecap",
    "stroke-linejoin",
    "stroke-miterlimit",
    "stroke-dasharray",
    "stroke-dashoffset",
    "fill-opacity",
    "stroke-opacity",
    "visibility",
    "color",
    "vector-effect",
)
STRIDE = 80


class UnsupportedScene(ValueError):
    """A source feature needs the existing SVG renderer."""


def number(value, default=0.0):
    if value is None:
        return default
    value = str(value).strip().removesuffix("px")
    if not re.fullmatch(NUMBER, value):
        raise UnsupportedScene(f"Unsupported SVG length: {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise UnsupportedScene("Non-finite SVG geometry")
    return result


def multiply(p, q):
    a, b, c, d, e, f = p
    g, h, i, j, k, l = q
    return (
        a * g + c * h,
        b * g + d * h,
        a * i + c * j,
        b * i + d * j,
        a * k + c * l + e,
        b * k + d * l + f,
    )


def transform(value):
    result = IDENTITY
    for match in TRANSFORMS.finditer(value):
        name, text = match.groups()
        values = [float(v) for v in re.findall(NUMBER, text)]
        if any(not math.isfinite(v) for v in values):
            raise UnsupportedScene("Non-finite SVG transform")
        if name == "matrix" and len(values) == 6:
            matrix = tuple(values)
        elif name == "translate" and len(values) in (1, 2):
            matrix = (1, 0, 0, 1, values[0], values[1] if len(values) == 2 else 0)
        elif name == "scale" and len(values) in (1, 2):
            matrix = (values[0], 0, 0, values[-1], 0, 0)
        elif name == "rotate" and len(values) in (1, 3):
            angle = math.radians(values[0])
            c, s = math.cos(angle), math.sin(angle)
            matrix = (c, s, -s, c, 0, 0)
            if len(values) == 3:
                x, y = values[1:]
                matrix = multiply(
                    multiply((1, 0, 0, 1, x, y), matrix), (1, 0, 0, 1, -x, -y)
                )
        elif name in ("skewX", "skewY") and len(values) == 1:
            skew = math.tan(math.radians(values[0]))
            matrix = (1, 0, skew, 1, 0, 0) if name == "skewX" else (1, skew, 0, 1, 0, 0)
        else:
            raise UnsupportedScene(f"Unsupported SVG transform: {match[0]}")
        result = multiply(result, matrix)
    if TRANSFORMS.sub("", value).strip(" ,\t\r\n"):
        raise UnsupportedScene(f"Invalid SVG transform: {value}")
    return result


def _arc(path, start, values):
    rx, ry, rotation, large, sweep, x2, y2 = values
    x1, y1 = start
    rx, ry = abs(rx), abs(ry)
    if (x1, y1) == (x2, y2):
        return
    if not rx or not ry:
        path.lineTo(x2, y2)
        return
    if large not in (0, 1) or sweep not in (0, 1):
        raise UnsupportedScene("Invalid SVG arc flags")
    phi = math.radians(rotation)
    c, s = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    xp, yp = c * dx + s * dy, -s * dx + c * dy
    ratio = (xp / rx) ** 2 + (yp / ry) ** 2
    if ratio > 1:
        scale = math.sqrt(ratio)
        rx *= scale
        ry *= scale
    denominator = rx * rx * yp * yp + ry * ry * xp * xp
    coefficient = math.sqrt(max(0, (rx * rx * ry * ry - denominator) / denominator))
    if large == sweep:
        coefficient = -coefficient
    cxp, cyp = coefficient * rx * yp / ry, -coefficient * ry * xp / rx
    cx, cy = c * cxp - s * cyp + (x1 + x2) / 2, s * cxp + c * cyp + (y1 + y2) / 2
    u, v = ((xp - cxp) / rx, (yp - cyp) / ry), ((-xp - cxp) / rx, (-yp - cyp) / ry)
    angle = math.atan2(u[1], u[0])
    delta = math.atan2(u[0] * v[1] - u[1] * v[0], u[0] * v[0] + u[1] * v[1])
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi
    steps = max(1, math.ceil(abs(delta) / (math.pi / 2)))
    step = delta / steps
    for index in range(steps):
        a, b = angle + index * step, angle + (index + 1) * step
        factor = 4 / 3 * math.tan(step / 4)
        ca, sa, cb, sb = math.cos(a), math.sin(a), math.cos(b), math.sin(b)
        p = (cx + rx * c * ca - ry * s * sa, cy + rx * s * ca + ry * c * sa)
        q = (cx + rx * c * cb - ry * s * sb, cy + rx * s * cb + ry * c * sb)
        da = (-rx * c * sa - ry * s * ca, -rx * s * sa + ry * c * ca)
        db = (-rx * c * sb - ry * s * cb, -rx * s * sb + ry * c * cb)
        path.cubicTo(
            p[0] + factor * da[0],
            p[1] + factor * da[1],
            q[0] - factor * db[0],
            q[1] - factor * db[1],
            *q,
        )


def _spaced_linear_path(data, fill_rule):
    """Fast path for the renderer's explicit, space-separated M/L/Z pours.

    Compact, relative and curved SVG syntax goes through the general parser.
    Avoid regex tokenization and per-segment command dispatch on large pours.
    """
    if not data.startswith("M ") or SPACED_LINEAR.search(data):
        return None
    tokens = data.split()
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill if fill_rule == "evenodd" else Qt.FillRule.WindingFill)
    subpaths, points, index = [], None, 0
    try:
        while index < len(tokens):
            command = tokens[index]
            if command == "Z":
                if points is None:
                    return None
                path.closeSubpath()
                if points[-1] != points[0]:
                    points.append(points[0])
                index += 1
                if index < len(tokens) and tokens[index] not in ("M", "Z"):
                    return None
                continue
            if command not in ("M", "L") or index + 2 >= len(tokens):
                return None
            x, y = float(tokens[index + 1]), float(tokens[index + 2])
            if not math.isfinite(x) or not math.isfinite(y):
                raise UnsupportedScene("Non-finite SVG path")
            if command == "M":
                path.moveTo(x, y)
                points = [(x, y)]
                subpaths.append(points)
            else:
                path.lineTo(x, y)
                points.append((x, y))
            index += 3
    except (ValueError, IndexError):
        return None
    return path, subpaths


def parse_path(data, fill_rule="nonzero"):
    """Return a Qt path and exact linear subpaths, or None when curved."""
    if linear := _spaced_linear_path(data, fill_rule):
        return linear
    tokens = TOKENS.findall(data)
    if TOKENS.sub("", data).strip(" ,\t\r\n"):
        raise UnsupportedScene("Invalid SVG path data")
    path = QPainterPath()
    path.setFillRule(
        Qt.FillRule.OddEvenFill if fill_rule == "evenodd" else Qt.FillRule.WindingFill
    )
    lengths = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}
    x = y = sx = sy = 0.0
    cubic = quadratic = None
    command = previous = ""
    subpaths, points, curved = [], None, False
    index = 0
    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
            if command.upper() == "Z":
                path.closeSubpath()
                if points is not None and points[-1] != points[0]:
                    points.append(points[0])
                x, y = sx, sy
                previous = "Z"
                command = ""
                if index < len(tokens) and tokens[index].upper() not in ("M", "Z"):
                    # A following segment implicitly starts another subpath.
                    # Keep Qt's closed-join/open-cap semantics in this case.
                    curved = True
                continue
        upper = command.upper()
        count = lengths.get(upper)
        if count is None or index + count > len(tokens):
            raise UnsupportedScene(
                f"Unsupported or incomplete SVG path command: {command!r}"
            )
        try:
            values = [float(v) for v in tokens[index : index + count]]
        except ValueError as exc:
            raise UnsupportedScene("Incomplete SVG path command") from exc
        if any(not math.isfinite(v) for v in values):
            raise UnsupportedScene("Non-finite SVG path")
        index += count
        relative = command.islower()
        if upper in ("M", "L", "T"):
            nx, ny = values
            if relative:
                nx += x
                ny += y
            if upper == "M":
                path.moveTo(nx, ny)
                sx, sy = nx, ny
                points = [(nx, ny)]
                subpaths.append(points)
                command = "l" if relative else "L"
            elif upper == "L":
                path.lineTo(nx, ny)
                if points is not None:
                    points.append((nx, ny))
            else:
                control = (
                    (2 * x - quadratic[0], 2 * y - quadratic[1])
                    if previous in ("Q", "T")
                    else (x, y)
                )
                path.quadTo(*control, nx, ny)
                quadratic = control
                curved = True
            x, y = nx, ny
        elif upper == "H":
            x = values[0] + x if relative else values[0]
            path.lineTo(x, y)
            if points is not None:
                points.append((x, y))
        elif upper == "V":
            y = values[0] + y if relative else values[0]
            path.lineTo(x, y)
            if points is not None:
                points.append((x, y))
        elif upper in ("C", "S", "Q"):
            if relative:
                values = [v + (x if i % 2 == 0 else y) for i, v in enumerate(values)]
            if upper == "C":
                path.cubicTo(*values)
                cubic = tuple(values[2:4])
            elif upper == "S":
                control = (
                    (2 * x - cubic[0], 2 * y - cubic[1])
                    if previous in ("C", "S")
                    else (x, y)
                )
                path.cubicTo(*control, *values)
                cubic = tuple(values[:2])
            else:
                path.quadTo(*values)
                quadratic = tuple(values[:2])
            x, y = values[-2:]
            curved = True
        elif upper == "A":
            if relative:
                values[-2] += x
                values[-1] += y
            _arc(path, (x, y), values)
            x, y = values[-2:]
            curved = True
        previous = upper
    return path, None if curved else subpaths


def _rings(path):
    # Qt flattens curves in transformed device space. A 1000x transform gives
    # sub-micrometre chords in the document's millimetre coordinate system.
    return [
        np.asarray([(p.x() / 1000, p.y() / 1000) for p in polygon], dtype=np.float64)
        for polygon in path.toSubpathPolygons(QTransform.fromScale(1000, 1000))
        if len(polygon) >= 3
    ]


def _geometry(path, rings):
    if rings is None:
        rings = _rings(path)
    rings = [np.asarray(r, dtype=np.float64) for r in rings if len(r) >= 3]
    rings = [np.vstack((r, r[:1])) if np.any(r[0] != r[-1]) else r for r in rings]
    if not rings:
        return None
    polygon = Polygon(rings[0], rings[1:])
    if polygon.is_valid:
        if path.fillRule() == Qt.FillRule.OddEvenFill or len(rings) == 1:
            return polygon

        def signed(r):
            return np.sum(r[:-1, 0] * r[1:, 1] - r[1:, 0] * r[:-1, 1])

        sign = signed(rings[0])
        if all(signed(r) * sign < 0 for r in rings[1:]):
            return polygon
    # Normalize overlapping/nested rings according to Qt's SVG fill rule.
    # Most manufacturing pours take the fast exterior-plus-holes route above.
    faces = polygonize(unary_union(MultiLineString(rings)))
    accepted = []
    for face in faces:
        point = face.representative_point()
        if path.contains(QPointF(point.x, point.y)):
            accepted.append(face)
    return unary_union(accepted) if accepted else None


class _Builder:
    def __init__(self, document):
        self.document = document
        self.vertices, self.indices, self.entries = array("f"), array("I"), []
        self.origin = (document.home.center().x(), document.home.center().y())
        self.objects = {p.id: (i + 1, p) for i, p in enumerate(document.primitives)}
        self.components, self.nets = defaultdict(set), defaultdict(set)
        self.capture_ids = set()
        for oid, p in self.objects.values():
            for name in p.schematic_components or (
                [p.component] if p.component else []
            ):
                self.components[name].add(oid)
            for name in p.schematic_nets or ([p.net] if p.net else []):
                self.nets[name].add(oid)
            if p.pin or p.hole:
                self.capture_ids.add(oid)
        self.paths = {}
        self.restored = defaultdict(list)
        for (identifier, name), value in document.style_changes.items():
            self.restored[identifier].append((name, value))
        self.shape_counts = defaultdict(int)
        self.layer = ""

    def attributes(self, node):
        changes = self.restored.get(node.get("id"))
        if not changes:
            return node.attrib
        attrs = dict(node.attrib)
        for name, value in changes:
            if value is None:
                attrs.pop(name, None)
            else:
                attrs[name] = value
        return attrs

    def color(self, value, props, opacity, name):
        if value == "none":
            return None
        if value == "currentColor":
            value = props.get("color", "black")
        color = QColor(value)
        if not color.isValid():
            raise UnsupportedScene(f"Unsupported SVG paint: {value!r}")
        alpha = color.alphaF() * opacity * number(props.get(name + "-opacity"), 1)
        return (
            (color.redF(), color.greenF(), color.blueF(), max(0, min(1, alpha)))
            if alpha
            else None
        )

    def append_indices(self, indices):
        first = len(self.indices)
        self.indices.extend(indices)
        count = len(self.indices) - first
        if count:
            if self.entries and self.entries[-1]["layer"] == self.layer:
                self.entries[-1]["count"] += count
            else:
                self.entries.append(
                    {"layer": self.layer, "first": first, "count": count}
                )

    def quad(self, bounds, params, color, kind, matrix, metadata):
        x0, y0, x1, y1 = bounds
        a, b, c, d, e, f = matrix
        lu, lv = math.hypot(a, b), math.hypot(c, d)
        if not lu or not lv:
            return
        start = len(self.vertices) // 20
        oid, polygon, hole = metadata
        ox, oy = self.origin
        tail = (*params, *color, kind, oid, polygon, hole)
        for x, y, sx, sy in (
            (x0, y0, -1, -1),
            (x1, y0, 1, -1),
            (x1, y1, 1, 1),
            (x0, y1, -1, 1),
        ):
            self.vertices.extend(
                (
                    a * x + c * y + e - ox,
                    b * x + d * y + f - oy,
                    x,
                    y,
                    *tail,
                    sx * a / lu + sy * c / lv,
                    sx * b / lu + sy * d / lv,
                    sx / lu,
                    sy / lv,
                )
            )
        self.append_indices((start, start + 1, start + 2, start, start + 2, start + 3))
        self.shape_counts[kind] += 1

    def line(self, p, q, width, cap, color, matrix, metadata):
        dx, dy = q[0] - p[0], q[1] - p[1]
        length = math.hypot(dx, dy)
        radius = width / 2
        if radius <= 0:
            return
        if not length:
            if cap == "round":
                self.ellipse(p[0], p[1], radius, radius, 0, color, matrix, metadata)
            elif cap == "square":
                self.rectangle(
                    p[0] - radius,
                    p[1] - radius,
                    width,
                    width,
                    0,
                    0,
                    color,
                    matrix,
                    metadata,
                )
            return
        shape = multiply(
            matrix,
            (
                dx / length,
                dy / length,
                -dy / length,
                dx / length,
                (p[0] + q[0]) / 2,
                (p[1] + q[1]) / 2,
            ),
        )
        extension = radius if cap in ("round", "square") else 0
        kind = {"round": 1, "butt": 2, "square": 3}.get(cap)
        if kind is None:
            raise UnsupportedScene(f"Unsupported SVG cap: {cap}")
        self.quad(
            (-length / 2 - extension, -radius, length / 2 + extension, radius),
            (length / 2, radius, 0, 0),
            color,
            kind,
            shape,
            metadata,
        )

    def ellipse(self, x, y, rx, ry, width, color, matrix, metadata):
        if rx <= 0 or ry <= 0:
            return
        shape = multiply(matrix, (1, 0, 0, 1, x, y))
        r = width / 2
        self.quad(
            (-rx - r, -ry - r, rx + r, ry + r),
            (rx, ry, width, 0),
            color,
            5 if width else 4,
            shape,
            metadata,
        )

    def rectangle(self, x, y, width, height, rx, ry, color, matrix, metadata):
        if width <= 0 or height <= 0:
            return
        shape = multiply(matrix, (1, 0, 0, 1, x + width / 2, y + height / 2))
        self.quad(
            (-width / 2, -height / 2, width / 2, height / 2),
            (width / 2, height / 2, rx, ry),
            color,
            6,
            shape,
            metadata,
        )

    def fill(self, path, rings, color, matrix, metadata):
        geometry = _geometry(path, rings)
        if geometry is None or geometry.is_empty:
            return
        triangles = constrained_delaunay_triangles(geometry)
        coords = get_coordinates(triangles)
        if not len(coords):
            return
        if len(coords) % 4:
            raise UnsupportedScene("Unexpected polygon tessellation")
        points = coords.reshape((-1, 4, 2))[:, :3].reshape((-1, 2))
        # Exact row keys avoid NumPy's slower structured floating-point sort.
        # Normalize signed zero so deduplication retains numeric equality.
        points[points == 0] = 0.0
        keys = points.view(np.dtype((np.void, points.dtype.itemsize * 2))).ravel()
        _, first, inverse = np.unique(keys, return_index=True, return_inverse=True)
        points = points[first]
        count = len(points)
        start = len(self.vertices) // 20
        rows = np.zeros((count, 20), dtype="<f4")
        a, b, c, d, e, f = matrix
        rows[:, 0] = a * points[:, 0] + c * points[:, 1] + e - self.origin[0]
        rows[:, 1] = b * points[:, 0] + d * points[:, 1] + f - self.origin[1]
        rows[:, 8:12] = color
        rows[:, 13:16] = metadata
        self.vertices.frombytes(rows.tobytes())
        self.append_indices(array("I", (inverse.astype("<u4") + start).tobytes()))
        self.shape_counts[0] += 1

    def stroke(self, path, color, props, matrix, metadata):
        width = number(props.get("stroke-width"), 1)
        if width <= 0:
            return QPainterPath()
        stroker = QPainterPathStroker()
        stroker.setWidth(width)
        cap = props.get("stroke-linecap", "butt")
        join = props.get("stroke-linejoin", "miter")
        caps = {
            "butt": Qt.PenCapStyle.FlatCap,
            "round": Qt.PenCapStyle.RoundCap,
            "square": Qt.PenCapStyle.SquareCap,
        }
        joins = {
            "miter": Qt.PenJoinStyle.SvgMiterJoin,
            "round": Qt.PenJoinStyle.RoundJoin,
            "bevel": Qt.PenJoinStyle.BevelJoin,
        }
        if cap not in caps or join not in joins:
            raise UnsupportedScene("Unsupported stroke cap or join")
        stroker.setCapStyle(caps[cap])
        stroker.setJoinStyle(joins[join])
        stroker.setMiterLimit(number(props.get("stroke-miterlimit"), 4))
        dash = props.get("stroke-dasharray", "none")
        if dash != "none":
            pattern = [float(v) / width for v in re.findall(NUMBER, dash)]
            if not pattern or any(v < 0 for v in pattern) or not any(pattern):
                raise UnsupportedScene("Invalid SVG dash pattern")
            if len(pattern) % 2:
                pattern *= 2
            stroker.setDashPattern(pattern)
            stroker.setDashOffset(number(props.get("stroke-dashoffset")) / width)
        outline = stroker.createStroke(path)
        if color is not None:
            self.fill(outline, None, color, matrix, metadata)
        return outline

    def capture(self, oid, path, matrix):
        if oid not in self.capture_ids or path.isEmpty():
            return
        world = QTransform(*matrix).map(path)
        if oid in self.paths:
            self.paths[oid] = self.paths[oid].united(world)
        else:
            self.paths[oid] = world

    def graphic(self, kind, attrs, props, matrix, metadata):
        opacity = number(attrs.get("opacity"), 1)
        fill = self.color(props.get("fill", "black"), props, opacity, "fill")
        stroke = self.color(props.get("stroke", "none"), props, opacity, "stroke")
        width = number(props.get("stroke-width"), 1)
        cap = props.get("stroke-linecap", "butt")
        dashed = props.get("stroke-dasharray", "none") != "none"
        path, rings = QPainterPath(), None
        path.setFillRule(
            Qt.FillRule.OddEvenFill
            if props.get("fill-rule") == "evenodd"
            else Qt.FillRule.WindingFill
        )
        analytic_fill = analytic_stroke = False
        get = lambda name, default=0: number(attrs.get(name), default)
        if kind in ("circle", "ellipse"):
            cx, cy = get("cx"), get("cy")
            rx = get("r") if kind == "circle" else get("rx")
            ry = rx if kind == "circle" else get("ry")
            if rx < 0 or ry < 0:
                raise UnsupportedScene("Negative SVG ellipse radius")
            if not rx or not ry:
                return
            path.addEllipse(QRectF(cx - rx, cy - ry, 2 * rx, 2 * ry))
            if fill is not None:
                self.ellipse(cx, cy, rx, ry, 0, fill, matrix, metadata)
                analytic_fill = True
            if stroke is not None and not dashed and rx == ry and width > 0:
                if width >= 2 * rx:
                    # A stroke reaching the centre is a disk, with no inner edge
                    # to antialias. Common for KiCad footprint polarity dots.
                    self.ellipse(cx, cy, rx + width / 2, ry + width / 2, 0, stroke, matrix, metadata)
                else:
                    self.ellipse(cx, cy, rx, ry, width, stroke, matrix, metadata)
                analytic_stroke = True
        elif kind == "rect":
            x, y, w, h = get("x"), get("y"), get("width"), get("height")
            if w < 0 or h < 0:
                raise UnsupportedScene("Negative SVG rectangle dimensions")
            if not w or not h:
                return
            rx, ry = get("rx", get("ry")), get("ry", get("rx"))
            rx, ry = min(max(0, rx), w / 2), min(max(0, ry), h / 2)
            path.addRoundedRect(QRectF(x, y, w, h), rx, ry, Qt.SizeMode.AbsoluteSize)
            if fill is not None:
                self.rectangle(x, y, w, h, rx, ry, fill, matrix, metadata)
                analytic_fill = True
        elif kind == "line":
            p, q = (get("x1"), get("y1")), (get("x2"), get("y2"))
            path.moveTo(*p)
            path.lineTo(*q)
            if stroke is not None and not dashed:
                self.line(p, q, width, cap, stroke, matrix, metadata)
                analytic_stroke = True
        elif kind == "path":
            path, rings = parse_path(
                attrs.get("d", ""), props.get("fill-rule", "nonzero")
            )
            if stroke is not None and not dashed and rings is not None:
                if fill is not None:
                    self.fill(path, rings, fill, matrix, metadata)
                    self.capture(metadata[0], path, matrix)
                    fill = None
                for points in rings:
                    if len(points) == 2:
                        self.line(*points, width, cap, stroke, matrix, metadata)
                    elif len(points) > 2:
                        part = QPainterPath()
                        part.moveTo(*points[0])
                        for point in points[1:]:
                            part.lineTo(*point)
                        if points[0] == points[-1]:
                            part.closeSubpath()
                        self.stroke(part, stroke, props, matrix, metadata)
                analytic_stroke = True
        elif kind in ("polyline", "polygon"):
            values = [float(v) for v in re.findall(NUMBER, attrs.get("points", ""))]
            if len(values) % 2:
                raise UnsupportedScene("Invalid SVG points")
            points = list(zip(values[::2], values[1::2], strict=True))
            if points:
                path.moveTo(*points[0])
                for point in points[1:]:
                    path.lineTo(*point)
                if kind == "polygon":
                    path.closeSubpath()
                    points.append(points[0])
                rings = [points]
        else:
            raise UnsupportedScene(f"Unsupported SVG graphic: {kind}")
        if fill is not None:
            if not analytic_fill:
                self.fill(path, rings, fill, matrix, metadata)
            self.capture(metadata[0], path, matrix)
        if (
            stroke is not None
            and width > 0
            and (not analytic_stroke or metadata[0] in self.capture_ids)
        ):
            outline = self.stroke(
                path, None if analytic_stroke else stroke, props, matrix, metadata
            )
            self.capture(metadata[0], outline, matrix)

    def visit(
        self,
        node,
        inherited,
        matrix=IDENTITY,
        metadata=(0, False, False),
        layer="",
        root=False,
    ):
        if node is self.document.selection_overlay:
            return
        kind = node.tag.rsplit("}", 1)[-1]
        if kind in ("defs", "metadata", "title", "desc"):
            return
        attrs = self.attributes(node)
        own = dict(attrs)
        own.update(
            (k.strip(), v.strip())
            for k, v in re.findall(r"([\w-]+)\s*:\s*([^;]+)", attrs.get("style", ""))
        )
        if own.get("display") == "none":
            return
        if any(
            own.get(name, "none") != "none" for name in ("clip-path", "mask", "filter")
        ):
            raise UnsupportedScene(
                "SVG clipping, masks or filters require raster fallback"
            )
        if kind in ("g", "svg") and number(own.get("opacity"), 1) != 1:
            raise UnsupportedScene("SVG group opacity requires isolated compositing")
        if kind == "svg" and not root:
            raise UnsupportedScene("Nested SVG viewports require raster fallback")
        props = dict(inherited)
        props.update(
            (name, own[name])
            for name in PAINT
            if name in own and own[name] != "inherit"
        )
        if props.get("vector-effect", "none") != "none":
            raise UnsupportedScene("Non-scaling SVG strokes require raster fallback")
        if own.get("paint-order", "normal") != "normal":
            raise UnsupportedScene("Custom SVG paint order requires raster fallback")
        if own.get("transform"):
            matrix = multiply(matrix, transform(own["transform"]))
        identifier = attrs.get("id")
        if identifier in self.document.layer_ids:
            layer = identifier
        polygon = (
            metadata[1]
            or identifier in self.document.polygon_ids
            or attrs.get("data-primitive") in ("polygon-outline", "polygon-cutout")
        )
        if identifier in self.objects:
            oid, p = self.objects[identifier]
            metadata = (oid, polygon or p.polygon, p.hole)
        else:
            metadata = (metadata[0], polygon, metadata[2])
        if layer in self.document.drill_layers:
            metadata = (metadata[0], metadata[1], True)
        previous_layer, self.layer = self.layer, layer
        if kind in ("svg", "g"):
            for child in node:
                self.visit(child, props, matrix, metadata, layer)
        elif props.get("visibility", "visible") == "visible":
            self.graphic(kind, own, props, matrix, metadata)
        self.layer = previous_layer


def build_scene(document):
    """Build a complete scene or explicitly request the existing SVG fallback."""
    started = time.perf_counter()
    builder = _Builder(document)
    try:
        if document.data.get("kind", "pcb") != "pcb":
            raise UnsupportedScene(
                "Retained geometry is currently available for PCB layouts"
            )
        builder.visit(document.root, {"fill": "black", "stroke": "none"}, root=True)
        if (len(builder.vertices) * 4 + len(builder.indices) * 4) > 128 * 1024 * 1024:
            raise UnsupportedScene(
                "Retained layout geometry exceeds the 128 MiB memory limit"
            )
        caption_paths = [
            builder.paths.get(builder.objects[p.id][0], QPainterPath())
            for p in document.primitives
            if p.pin and not p.hole
        ]
        hole_paths = [
            (p.bounds, builder.paths.get(builder.objects[p.id][0], QPainterPath()))
            for p in document.primitives
            if p.hole
        ]
        return {
            "supported": True,
            "reason": "",
            "vertices": builder.vertices.tobytes(),
            "indices": builder.indices.tobytes(),
            "stride": STRIDE,
            "origin": builder.origin,
            "entries": builder.entries,
            "object_components": dict(builder.components),
            "object_nets": dict(builder.nets),
            "object_count": len(document.primitives) + 1,
            "drill_layers": tuple(document.drill_layers),
            "caption_paths": caption_paths,
            "hole_paths": hole_paths,
            "stats": {
                "seconds": time.perf_counter() - started,
                "vertices": len(builder.vertices) // 20,
                "indices": len(builder.indices),
                "bytes": len(builder.vertices) * 4 + len(builder.indices) * 4,
                "shapes": dict(builder.shape_counts),
            },
        }
    except (UnsupportedScene, GEOSException, ValueError, ArithmeticError) as exc:
        return {
            "supported": False,
            "reason": str(exc),
            "vertices": b"",
            "indices": b"",
            "entries": [],
            "stats": {"seconds": time.perf_counter() - started},
        }
