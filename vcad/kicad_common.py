"""Read KiCad's S-expression files and draw safe, millimetre-based SVG."""

import math
import re
import xml.etree.ElementTree as ET

import sexpdata


_TOKENS = re.compile(r'(?P<string>"(?:\\.|[^"\\])*")|(?P<comment>^[ \t]*[#;][^\r\n]*)|(?P<atom>[^\s()"]+)', re.MULTILINE)


def _normalize_atom(match):
    if match.lastgroup == "comment":
        return ""
    atom = match[0]
    return sexpdata.dumps(atom) if match.lastgroup == "atom" and any(c in atom for c in "[]';\\") else atom


def forms(node, key):
    return [item for item in node[1:] if isinstance(item, list) and item and str(item[0]) == key]


def form(node, key):
    return next(iter(forms(node, key)), [])


def value(node, key, default=""):
    item = form(node, key)
    return str(item[1]) if len(item) > 1 else default


def number(item):
    result = float(item)
    if not math.isfinite(result):
        raise ValueError("KiCad coordinates and sizes must be finite numbers.")
    return result


def scalar(node, key, default=0):
    item = form(node, key)
    return number(item[1]) if len(item) > 1 else default


def point(node, key="at"):
    item = form(node, key)
    return (number(item[1]), number(item[2])) if len(item) >= 3 else (0.0, 0.0)


def angle(node):
    at = form(node, "at")
    # Footprint text permits (at X Y [ANGLE] [unlocked]); the flag can
    # occupy the optional angle slot when the rotation is omitted.
    return number(at[3]) if len(at) > 3 and str(at[3]) != "unlocked" else 0.0


def flag(node, key):
    return any(not isinstance(item, list) and str(item) == key for item in node[1:]) or value(node, key) == "yes"


def rotate(point_, degrees):
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return (c * point_[0] - s * point_[1], s * point_[0] + c * point_[1])


def load(path, expected):
    try:
        source = path.read_text(encoding="utf-8-sig")
        # KiCad 5 writes some strings as bare atoms. Lisp brackets, apostrophes,
        # semicolons and backslashes are literal characters there, not syntax.
        # Quote only those atoms before handing the expression to the Lisp reader;
        # already-quoted strings and structural parentheses stay untouched.
        source = _TOKENS.sub(_normalize_atom, source)
        root = sexpdata.loads(source, nil=None, true=None, false=None)
    except (ValueError, TypeError, AssertionError, RecursionError, sexpdata.ExpectClosingBracket,
            sexpdata.ExpectNothing, sexpdata.ExpectSExp) as exc:
        raise ValueError(f"Invalid KiCad S-expression file: {exc}") from exc
    if not isinstance(root, list) or not root or str(root[0]) != expected:
        raise ValueError(f"Expected a KiCad {expected} document.")
    return root


def element(parent, tag, text=None, **attributes):
    node = ET.SubElement(parent, tag, {key.rstrip('_').replace('_', '-'): str(val)
                                    for key, val in attributes.items() if val is not None})
    if text is not None:
        node.text = str(text)
    return node


def fmt(value_):
    return f"{value_:.6f}".rstrip("0").rstrip(".") if value_ else "0"


def path_points(points, close=False, *, spaced=False):
    if not points:
        return ""
    move, line = ("M ", " L ") if spaced else ("M", " L")
    return move + line.join(f"{fmt(x)} {fmt(y)}" for x, y in points) + (" Z" if close else "")


def polygon_parts(geometry):
    if geometry.geom_type == "Polygon" and not geometry.is_empty:
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from polygon_parts(part)


def geometry_path(geometry):
    return " ".join(path_points(list(polygon.exterior.coords), True, spaced=True) + " " +
                    " ".join(path_points(list(hole.coords), True, spaced=True) for hole in polygon.interiors)
                    for polygon in polygon_parts(geometry))


def arc_points(start, mid, end):
    ax, ay = start; bx, by = mid; cx, cy = end
    determinant = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(determinant) < 1e-12:
        return [start, mid, end]
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ox = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / determinant
    oy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / determinant
    first, middle, last = [math.atan2(y - oy, x - ox) for x, y in (start, mid, end)]
    sweep = (last - first) % math.tau
    if (middle - first) % math.tau > sweep:
        sweep -= math.tau
    radius = math.hypot(ax - ox, ay - oy)
    # Limit both angular error and chord error, including large board arcs.
    step = min(math.radians(5), 2 * math.acos(max(-1, min(1, 1 - 0.005 / radius))))
    steps = max(2, min(4096, math.ceil(abs(sweep) / max(step, 1e-6))))
    return [start] + [(ox + radius * math.cos(first + sweep * i / steps),
                      oy + radius * math.sin(first + sweep * i / steps)) for i in range(1, steps)] + [end]


def graphic_points(node):
    kind = str(node[0]).removeprefix("gr_").removeprefix("fp_")
    if kind == "arc":
        if form(node, "mid"):
            return arc_points(point(node, "start"), point(node, "mid"), point(node, "end")), False
        center, start = point(node, "start"), point(node, "end")
        sweep = scalar(node, "angle")
        relative = (start[0] - center[0], start[1] - center[1])
        return [(center[0] + p[0], center[1] + p[1]) for i in range(max(2, math.ceil(abs(sweep) / 5)) + 1)
                for p in [rotate(relative, sweep * i / max(2, math.ceil(abs(sweep) / 5)))]], False
    if kind == "circle":
        center, end = point(node, "center"), point(node, "end")
        radius = scalar(node, "radius", math.dist(center, end))
        return [(center[0] + radius * math.cos(i * math.tau / 96),
                 center[1] + radius * math.sin(i * math.tau / 96)) for i in range(97)], True
    if kind in ("rect", "rectangle"):
        a, b = point(node, "start"), point(node, "end")
        return [(a[0], a[1]), (b[0], a[1]), (b[0], b[1]), (a[0], b[1])], True
    if kind in ("poly", "polygon", "polyline", "curve", "bezier"):
        points = [point(["root", xy], "xy") for xy in forms(form(node, "pts"), "xy")]
        if kind in ("curve", "bezier") and len(points) == 4:
            a, b, c, d = points
            points = [tuple((1-t)**3*a[j] + 3*(1-t)**2*t*b[j] + 3*(1-t)*t*t*c[j] + t**3*d[j]
                            for j in (0, 1)) for t in [i / 64 for i in range(65)]]
        return points, kind in ("poly", "polygon") or bool(points and points[0] == points[-1])
    if kind == "line":
        return [point(node, "start"), point(node, "end")], False
    return [], False


def width(node, default=0.15):
    return scalar(form(node, "stroke"), "width", scalar(node, "width", default)) or default


def draw_text(parent, node, text, position=None, rotation=None, color="currentColor"):
    effects = form(node, "effects")
    if flag(effects, "hide") or flag(node, "hide"):
        return
    size = point(form(effects, "font"), "size")
    height = size[0] or 1.27
    x, y = position if position is not None else point(node)
    degrees = -angle(node) if rotation is None else rotation
    justify = [str(item) for item in form(effects, "justify")[1:]]
    text_node = element(parent, "text", x=fmt(x), y=fmt(y), fill=color, stroke="none",
                        font_family="sans-serif", font_size=fmt(height),
                        font_weight="bold" if flag(form(effects, "font"), "bold") else None,
                        font_style="italic" if flag(form(effects, "font"), "italic") else None,
                        text_anchor="start" if "left" in justify else "end" if "right" in justify else "middle",
                        dominant_baseline="text-after-edge" if "bottom" in justify else "hanging" if "top" in justify else "central",
                        transform=f"rotate({fmt(degrees)} {fmt(x)} {fmt(y)})" if degrees else None)
    for index, line in enumerate(str(text).splitlines() or [""]):
        element(text_node, "tspan", text=line, x=fmt(x), dy=fmt(height * 1.3) if index else "0")


def svg_document(scene, bounds, defs=None):
    x0, y0, x1, y1 = bounds
    margin = max(1, min(5, max(x1 - x0, y1 - y0) * 0.03))
    root = ET.Element("svg", {"xmlns": "http://www.w3.org/2000/svg",
        "viewBox": f"{fmt(x0-margin)} {fmt(y0-margin)} {fmt(max(x1-x0, 1)+2*margin)} {fmt(max(y1-y0, 1)+2*margin)}"})
    if defs is not None:
        root.append(defs)
    root.append(scene)
    return ET.tostring(root, encoding="unicode")
