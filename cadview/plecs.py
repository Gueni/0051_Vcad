"""Render PLECS (.plecs) circuit models as schematic SVG.

PLECS files store component placement, frames and wiring but not the library
symbols themselves, so common blocks get simplified symbols and the rest are
drawn as framed boxes labelled with their type. Every subsystem schematic is
drawn as its own panel below its parent.
"""

import html
import math
import re
import xml.etree.ElementTree as ET

from .kicad_common import element, fmt, path_points

_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\[[^\]]*\]|[{}]|[^\s{}"\[\]]+')


class Block(dict):
    """A PLECS block: scalar fields in the mapping, child blocks in ``children``."""

    def __init__(self, kind):
        super().__init__()
        self.kind, self.children = kind, []

    def blocks(self, kind):
        return [child for child in self.children if child.kind == kind]

    def first(self, kind):
        return next(iter(self.blocks(kind)), None)


def _value(tokens):
    if tokens and all(token.startswith('"') for token in tokens):
        # Adjacent string literals are concatenated, as in C.
        return "".join(token[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape") for token in tokens)
    if len(tokens) == 1 and tokens[0].startswith("["):
        rows = [[float(item) for item in row.split(",") if item.strip()] for row in tokens[0][1:-1].split(";")]
        return [row for row in rows if row]
    return " ".join(tokens)


def parse(text: str) -> Block:
    root, stack = Block("root"), []
    current = root
    for line in text.splitlines():
        tokens = _TOKEN.findall(line)
        if not tokens:
            continue
        if tokens == ["}"]:
            current = stack.pop() if stack else root
        elif tokens[-1] == "{" and len(tokens) == 2:
            child = Block(tokens[0])
            current.children.append(child)
            stack.append(current)
            current = child
        elif tokens[0].startswith('"') and current.get("_last"):
            key = current["_last"]  # continuation line of a multi-line string
            current[key] = current[key] + _value(tokens)
        else:
            current[tokens[0]] = _value(tokens[1:])
            current["_last"] = tokens[0]
    if not root.first("Plecs"):
        raise ValueError("Not a PLECS model file.")
    return root.first("Plecs")


def parameters(component):
    return {row.get("Variable", ""): row.get("Value", "") for row in component.blocks("Parameter")}


ROTATION = {"up": 0, "right": 90, "down": 180, "left": 270}


def transform(component, point):
    """Map a component-local point to schematic coordinates."""
    x, y = point
    if component.get("Flipped") == "on":
        y = -y  # Flipping keeps a block's signal direction and swaps its sides.
    angle = math.radians(ROTATION.get(component.get("Direction", "up"), 0))
    cos, sin = round(math.cos(angle)), round(math.sin(angle))
    px, py = component["Position"][0]
    return px + x * cos - y * sin, py + x * sin + y * cos


INK, PAPER, TEXT = "#1f2937", "#ffffff", "#111827"
WIRE_COLORS = {"Wire": "#1d4ed8", "Signal": "#374151"}
OTHER_WIRE = "#b45309"  # thermal and other physical domains
FONT = "sans-serif"
GROUNDS = {"Ground", "ConstantTemperatureGnd", "ThermalGround"}
DEFAULT_FRAMES = {
    "Gain": (-15, -15, 15, 15), "Sum": (-10, -10, 10, 10), "Constant": (-20, -10, 20, 10),
    "Input": (-10, -7, 10, 7), "Output": (-10, -7, 10, 7), "Port": (-10, -7, 10, 7),
    "Ground": (-10, -10, 10, 8), "ConstantTemperatureGnd": (-10, -10, 10, 8), "Scope": (-15, -15, 15, 15),
    "SignalMux": (-2.5, -20, 2.5, 20), "SignalDemux": (-2.5, -20, 2.5, 20), "Display": (-25, -10, 25, 10),
}


class _Bounds:
    def __init__(self):
        self.x0 = self.y0 = math.inf
        self.x1 = self.y1 = -math.inf

    def add(self, x, y):
        self.x0, self.y0, self.x1, self.y1 = min(self.x0, x), min(self.y0, y), max(self.x1, x), max(self.y1, y)

    def valid(self):
        return self.x0 <= self.x1


def _argb(value, fallback):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return fallback
    return f"#{number & 0xFFFFFF:06x}"


def _html_text(markup):
    text = re.sub(r"<br\s*/?>|</p>", "\n", markup, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return [line.strip() for line in text.splitlines() if line.strip()]


def _number(value, default=1):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class _Sheet:
    """Terminal positions and drawing of one PLECS schematic."""

    def __init__(self, renderer, schematic, prefix):
        self.renderer, self.schematic, self.prefix = renderer, schematic, prefix
        self.components = {component.get("Name", ""): component for component in schematic.blocks("Component")
                           if component.get("Position")}
        self.roles = {}
        for connection in schematic.blocks("Connection"):
            signal = connection.get("Type") == "Signal"
            for name, terminal, end in self.endpoints(connection):
                role = ("out" if end == "src" else "in") if signal else "wire"
                self.roles.setdefault(name, {})[terminal] = role
        self.positions = {name: self.terminal_positions(component) for name, component in self.components.items()}

    @staticmethod
    def endpoints(connection):
        def branch(block):
            if block.get("DstComponent"):
                yield block["DstComponent"], _number(block.get("DstTerminal")), "dst"
            for child in block.blocks("Branch"):
                yield from branch(child)
        if connection.get("SrcComponent"):
            yield connection["SrcComponent"], _number(connection.get("SrcTerminal")), "src"
        yield from branch(connection)

    def frame(self, component):
        if component.get("Frame"):
            (x0, y0), (x1, y1) = component["Frame"][:2]
            return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
        kind = component.get("Type", "")
        x0, y0, x1, y1 = DEFAULT_FRAMES.get(kind, (-15, -15, 15, 15))
        if kind in ("Goto", "From"):
            half = 4 + 3 * len(parameters(component).get("Tag", "").strip("'\""))
            x0, x1 = -max(half, 12), max(half, 12)
        roles = self.roles.get(component.get("Name", ""), {}).values()
        ports = max(sum(role == "in" for role in roles), sum(role == "out" for role in roles))
        half = max(-y0, y1, 5 * ports + 5)
        return x0, -half, x1, half

    def terminal_positions(self, component):
        explicit = component.blocks("Terminal")
        if explicit:
            return {index: transform(component, terminal["Position"][0])
                    for index, terminal in enumerate(explicit, 1) if terminal.get("Position")}
        x0, y0, x1, y1 = self.frame(component)
        roles = self.roles.get(component.get("Name", ""), {})
        local = {}
        for role, x in (("in", x0), ("out", x1)):
            numbers = sorted(number for number, kind in roles.items() if kind == role)
            for slot, number in enumerate(numbers):
                local[number] = (x, y0 + (y1 - y0) * (slot + 1) / (len(numbers) + 1))
        wires = sorted(number for number, kind in roles.items() if kind == "wire")
        if component.get("Type") in GROUNDS:
            sides = [(0, y0)]
        elif len(wires) == 1 and not local:
            sides = [(x1, 0)]
        else:
            sides = [(0, y0), (0, y1), (x0, 0), (x1, 0)]
        for slot, number in enumerate(wires):
            local[number] = sides[slot % len(sides)]
        return {number: transform(component, point) for number, point in local.items()}

    def terminal(self, name, number):
        return self.positions.get(name, {}).get(number)

    def draw(self, parent, bounds):
        for annotation in self.schematic.blocks("RectangleAnnotation"):
            x, y = (annotation.get("Position") or [[0, 0]])[0]
            width, height = float(annotation.get("Width", 0)), float(annotation.get("Height", 0))
            element(parent, "rect", x=fmt(x - width / 2), y=fmt(y - height / 2), width=fmt(width), height=fmt(height),
                    rx=fmt(float(annotation.get("CornerRadius", 0))), fill=_argb(annotation.get("BrushColor"), "none"),
                    fill_opacity="0.35", stroke=_argb(annotation.get("PenColor"), INK), stroke_width="1")
            bounds.add(x - width / 2, y - height / 2)
            bounds.add(x + width / 2, y + height / 2)
        wires = 0
        for index, connection in enumerate(self.schematic.blocks("Connection")):
            self.draw_connection(parent, connection, bounds, f"{self.prefix}-w{index}")
            wires += 1
        for index, component in enumerate(self.components.values()):
            self.draw_component(parent, component, f"{self.prefix}-c{index}", bounds)
        for annotation in self.schematic.blocks("Annotation"):
            x, y = (annotation.get("Position") or [[0, 0]])[0]
            markup = annotation.get("Name", "")
            size = float(match[1]) if (match := re.search(r"font-size:\s*([\d.]+)px", markup)) else 10
            for row, line in enumerate(_html_text(markup)):
                element(parent, "text", text=line, x=fmt(x), y=fmt(y + row * size * 1.25), font_size=fmt(size),
                        font_family=FONT, fill=TEXT, text_anchor="middle", dominant_baseline="central")
                bounds.add(x - len(line) * size * 0.3, y + row * size * 1.25 - size)
                bounds.add(x + len(line) * size * 0.3, y + row * size * 1.25 + size)
        return wires

    def draw_connection(self, parent, connection, bounds, identifier):
        kind = connection.get("Type", "Wire")
        color = WIRE_COLORS.get(kind, OTHER_WIRE)
        group = element(parent, "g", id=identifier, fill="none", stroke=color, stroke_width="1",
                        stroke_linejoin="round", stroke_linecap="round")
        self.renderer.link(self, connection, identifier)
        start = self.terminal(connection.get("SrcComponent"), _number(connection.get("SrcTerminal")))

        def route(points, first_is_terminal, last_is_terminal):
            # PLECS stores only bend points; leave and enter terminals orthogonally.
            routed = [points[0]]
            for position, (x, y) in enumerate(points[1:], 1):
                px, py = routed[-1]
                if px != x and py != y:
                    if len(points) == 2 and first_is_terminal and last_is_terminal:
                        middle = (px + x) / 2
                        routed += [(middle, py), (middle, y)]
                    elif position == len(points) - 1 and last_is_terminal:
                        routed.append((px, y))
                    else:
                        routed.append((x, py))
                routed.append((x, y))
            return routed

        def branch(block, origin, origin_is_terminal):
            points = [tuple(point) for point in block.get("Points") or []]
            end = self.terminal(block.get("DstComponent"), _number(block.get("DstTerminal"))) if block.get("DstComponent") else None
            path = ([origin] if origin else []) + points + ([end] if end else [])
            if len(path) >= 2:
                routed = route(path, origin_is_terminal and origin is not None, end is not None)
                element(group, "path", d=path_points(routed))
                for x, y in routed:
                    bounds.add(x, y)
                if end and kind == "Signal":
                    (ax, ay), (bx, by) = routed[-2], routed[-1]
                    length = math.hypot(bx - ax, by - ay) or 1
                    ux, uy = (bx - ax) / length, (by - ay) / length
                    element(group, "path", d=path_points([(bx, by), (bx - 6 * ux + 2.5 * uy, by - 6 * uy - 2.5 * ux),
                                                          (bx - 6 * ux - 2.5 * uy, by - 6 * uy + 2.5 * ux)], close=True),
                            fill=color, stroke="none")
            children = block.blocks("Branch")
            joint = path[-1] if path and not end else origin
            if children and joint:
                element(group, "circle", cx=fmt(joint[0]), cy=fmt(joint[1]), r="2", fill=color, stroke="none")
            for child in children:
                branch(child, joint, False)

        branch(connection, start, True)

    def draw_component(self, parent, component, identifier, bounds):
        kind, name = component.get("Type", ""), component.get("Name", "")
        values = parameters(component)
        x0, y0, x1, y1 = self.frame(component)
        px, py = component["Position"][0]
        angle = ROTATION.get(component.get("Direction", "up"), 0)
        flip = " scale(1,-1)" if component.get("Flipped") == "on" else ""
        group = element(parent, "g", id=identifier)
        self.renderer.components.append({"id": identifier, "name": name or kind, "elements": [identifier],
                                         "body_elements": [identifier]})
        body = element(group, "g", transform=f"translate({fmt(px)},{fmt(py)}) rotate({angle}){flip}",
                       fill=PAPER, stroke=INK, stroke_width="1")
        box = dict(x=fmt(x0), y=fmt(y0), width=fmt(x1 - x0), height=fmt(y1 - y0))
        caption = None
        if kind == "Gain":
            element(body, "path", d=path_points([(x0, y0), (x1, (y0 + y1) / 2), (x0, y1)], close=True))
            caption = values.get("K", "K")
        elif kind == "Sum":
            element(body, "circle", cx="0", cy="0", r=fmt(min(x1 - x0, y1 - y0) / 2))
            caption = "Σ"
        elif kind in GROUNDS:
            element(body, "path", fill="none", d=f"M0 {fmt(y0)} V0 M{fmt(x0)} 0 H{fmt(x1)} "
                    f"M{fmt(x0 * .6)} 3 H{fmt(x1 * .6)} M{fmt(x0 * .25)} 6 H{fmt(x1 * .25)}")
        elif kind in ("SignalMux", "SignalDemux"):
            element(body, "rect", **box, fill=INK)
        elif kind in ("Goto", "From"):
            outline = [(x0, y0), (x1 - 6, y0), (x1, 0), (x1 - 6, y1), (x0, y1)] if kind == "Goto" else \
                      [(x0 + 6, y0), (x1, y0), (x1, y1), (x0 + 6, y1), (x0, 0)]
            element(body, "path", d=path_points(outline, close=True))
            caption = values.get("Tag", "").strip("'\"")
        elif kind in ("Input", "Output", "Port"):
            element(body, "rect", **box, rx="4")
            caption = values.get("Index", "")
        else:
            element(body, "rect", **box)
            if kind == "Scope":
                element(body, "rect", x=fmt(x0 + 4), y=fmt(y0 + 4), width=fmt(x1 - x0 - 8),
                        height=fmt((y1 - y0) * .6), fill="#e5f3ff")
            elif kind == "Constant":
                caption = values.get("Value", "")
            elif kind == "Reference":
                caption = component.get("SrcComponent", "").rsplit("/", 1)[-1]
            elif kind in ("Subsystem", "ConfigurableSubsystem"):
                caption = name if component.get("Show") != "on" else None
            else:
                caption = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", kind)
        corners = [transform(component, point) for point in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
        left, top = min(x for x, _ in corners), min(y for _, y in corners)
        right, bottom = max(x for x, _ in corners), max(y for _, y in corners)
        for x, y in corners:
            bounds.add(x, y)
        if caption:
            size = max(4.0, min(9.0, (right - left) / max(len(caption), 1) * 1.6, (bottom - top) * 0.45))
            element(group, "text", text=caption, x=fmt((left + right) / 2), y=fmt((top + bottom) / 2), font_size=fmt(size),
                    font_family=FONT, fill=TEXT, text_anchor="middle", dominant_baseline="central")
        if component.get("Show") == "on" and name and component.get("LabelPosition") != "none":
            x, y, anchor = {"north": ((left + right) / 2, top - 6, "middle"),
                            "east": (right + 4, (top + bottom) / 2, "start"),
                            "west": (left - 4, (top + bottom) / 2, "end")}.get(
                component.get("LabelPosition", "south"), ((left + right) / 2, bottom + 8, "middle"))
            element(group, "text", text=name, x=fmt(x), y=fmt(y), font_size="9", font_family=FONT, fill=TEXT,
                    text_anchor=anchor, dominant_baseline="central")
            reach = len(name) * 5 if anchor != "middle" else len(name) * 2.5
            bounds.add(x - (reach if anchor != "start" else 0), y - 6)
            bounds.add(x + (reach if anchor != "end" else 0), y + 6)


class _Renderer:
    """Components and nets across all schematics; nets join through Goto/From tags."""

    def __init__(self):
        self.components, self.parent, self.wires, self.names = [], {}, {}, {}

    def find(self, key):
        while self.parent.setdefault(key, key) != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)

    def link(self, sheet, connection, identifier):
        keys = [(sheet.prefix, name, terminal) for name, terminal, _ in sheet.endpoints(connection)]
        if not keys:
            return
        for key in keys[1:]:
            self.union(keys[0], key)
        self.wires[identifier] = keys[0]
        source = connection.get("SrcComponent", "")
        self.names.setdefault(keys[0], f"{source}:{connection.get('SrcTerminal', 1)}" if source else identifier)

    def nets(self):
        groups = {}
        for identifier, key in self.wires.items():
            row = groups.setdefault(self.find(key), {"elements": [], "names": []})
            row["elements"].append(identifier)
            if self.names.get(key) not in row["names"]:
                row["names"].append(self.names.get(key))
        return [{"id": f"net-{index}", "name": row["names"][0], "elements": row["elements"], "aliases": row["names"][1:]}
                for index, row in enumerate(groups.values())]


def _schematics(block, title):
    for component in block.blocks("Component"):
        name = f"{title} / {component.get('Name', '')}"
        for schematic in component.blocks("Schematic"):
            yield schematic, name
            yield from _schematics(schematic, name)
        for configurations in component.blocks("Configurations"):
            for configuration in configurations.children:
                label = f"{name} [{configuration.get('Name', configuration.kind)}]"
                for schematic in configuration.blocks("Schematic"):
                    yield schematic, label
                    yield from _schematics(schematic, label)


def render_plecs(path) -> dict:
    model = parse(path.read_text(encoding="utf-8", errors="replace"))
    top = model.first("Schematic")
    if top is None:
        raise ValueError("The PLECS model has no schematic.")
    renderer, scene = _Renderer(), ET.Element("g", {"id": "scene"})
    offset, width, wires, tags = 0.0, 0.0, 0, 0
    title = model.get("Name") or path.stem
    for index, (schematic, label) in enumerate([(top, title), *_schematics(top, title)]):
        sheet = _Sheet(renderer, schematic, f"plecs-s{index}")
        for component in sheet.components.values():
            if component.get("Type") in ("Goto", "From"):
                tags += 1
                tag = parameters(component).get("Tag", "").strip("'\"")
                renderer.union((sheet.prefix, component.get("Name", ""), 1), (sheet.prefix, "#tag", tag))
        bounds, content = _Bounds(), ET.Element("g")
        wires += sheet.draw(content, bounds)
        if not bounds.valid():
            continue
        # Each schematic is a panel; panels stack top to bottom.
        margin = 20
        x, y = bounds.x0 - margin, bounds.y0 - margin
        w, h = bounds.x1 - bounds.x0 + 2 * margin, bounds.y1 - bounds.y0 + 2 * margin
        panel = element(scene, "g", id=f"{sheet.prefix}-panel", transform=f"translate({fmt(-x)},{fmt(offset + 24 - y)})")
        element(panel, "rect", x=fmt(x), y=fmt(y), width=fmt(w), height=fmt(h), fill=PAPER, stroke="#9ca3af",
                stroke_width="1", rx="4")
        element(panel, "text", text=label, x=fmt(x), y=fmt(y - 8), font_size="14", font_weight="600",
                font_family=FONT, fill="#9ca3af")
        panel.append(content)
        offset += h + 64
        width = max(width, w)
    root = ET.Element("svg", {"xmlns": "http://www.w3.org/2000/svg",
                              "viewBox": f"-20 -20 {fmt(width + 40)} {fmt(max(offset, 1) + 20)}"})
    root.append(scene)
    return {"svg": ET.tostring(root, encoding="unicode"), "name": path.name, "kind": "schematic", "warnings": [],
            "selection": {"components": renderer.components, "nets": renderer.nets()},
            "stats": {"components": len(renderer.components), "wires": wires, "net_labels": tags}}
