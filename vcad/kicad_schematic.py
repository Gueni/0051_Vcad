"""Render self-contained KiCad sheets and derive sheet-local connectivity."""

from collections import defaultdict
import re
import xml.etree.ElementTree as ET

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from .kicad_common import (
    angle, draw_text, element, flag, fmt, form, forms, graphic_points, load,
    number, path_points, point, rotate, scalar, svg_document, value, width,
)


class Connectivity:
    def __init__(self):
        self.parents = {}
        self.elements = defaultdict(set)
        self.labels = defaultdict(set)
        self.terminals = defaultdict(set)
        self.segments = []

    def add(self, position, element_id=None, name=None, terminal=None):
        key = tuple(round(coordinate, 6) for coordinate in position)
        self.parents.setdefault(key, key)
        if element_id:
            self.elements[key].add(element_id)
        if name:
            self.labels[key].add(name)
        if terminal:
            self.terminals[key].add(terminal)
        return key

    def find(self, key):
        root = key
        while self.parents[root] != root:
            root = self.parents[root]
        while key != root:
            parent = self.parents[key]; self.parents[key] = root; key = parent
        return root

    def join(self, a, b):
        self.parents[self.find(a)] = self.find(b)

    def wire(self, points, element_id):
        for a, b in zip(points, points[1:]):
            first, last = self.add(a, element_id), self.add(b, element_id)
            self.join(first, last)
            if first != last:
                self.segments.append((LineString([a, b]), first))

    def nets(self):
        if self.segments:
            tree = STRtree([segment for segment, key in self.segments])
            # Endpoints, pins, labels, and explicit junctions connect to wires.
            # Bare crossings do not introduce an anchor, so remain separate.
            for anchor in self.parents:
                for index in tree.query(Point(anchor), predicate="dwithin", distance=0.00001):
                    self.join(anchor, self.segments[index][1])
        same_name = {}
        for anchor, names in self.labels.items():
            for name in names:
                if name in same_name:
                    self.join(anchor, same_name[name])
                else:
                    same_name[name] = anchor
        rows = {}
        for anchor in self.parents:
            row = rows.setdefault(self.find(anchor), {"elements": set(), "names": set(), "terminals": set()})
            row["elements"].update(self.elements[anchor])
            row["names"].update(self.labels[anchor])
            row["terminals"].update(self.terminals[anchor])
        result = []
        for row in rows.values():
            if not row["elements"]:
                continue
            names, terminals = sorted(row["names"]), sorted(row["terminals"])
            name = names[0] if names else f"Net-({terminals[0][0]}-Pad{terminals[0][1]})" if terminals else f"Net {len(result)+1}"
            result.append({"id": f"kc-net-{len(result)}", "name": name, "aliases": names[1:],
                           "elements": sorted(row["elements"]),
                           "terminals": [{"component": component, "pin": pin} for component, pin in terminals]})
        return result


class SchematicRenderer:
    def __init__(self, root):
        self.root = root
        self.scene = ET.Element("g", {"id": "scene"})
        self.connections, self.components, self.warnings = Connectivity(), [], set()
        self.libraries = {str(symbol[1]): symbol for symbol in forms(form(root, "lib_symbols"), "symbol")}
        self.instance_references = {}
        for item in forms(form(root, "symbol_instances"), "path"):
            self.instance_references[str(item[1]).rstrip('/').split('/')[-1]] = value(item, "reference")
        self.counter = 0

    def identifier(self, kind):
        self.counter += 1
        return f"kc-{kind}-{self.counter}"

    def graphic(self, parent, node):
        kind = str(node[0])
        if kind == "text":
            draw_text(parent, node, node[1], color="#774422")
            return
        points, closed = graphic_points(node)
        if len(points) < 2:
            return
        fill = value(form(node, "fill"), "type", "none")
        element(parent, "path", d=path_points(points, closed),
                stroke="#995b27", stroke_width=fmt(width(node, 0.1524)), stroke_linecap="round", stroke_linejoin="round",
                fill="#ffffcc" if fill == "background" else "#995b27" if fill == "outline" else "none")

    def library_parts(self, name, unit, style, seen=None):
        seen = set() if seen is None else seen
        if name in seen:
            self.warnings.add(f"Cyclic KiCad symbol inheritance for {name}; inherited graphics were omitted.")
            return []
        seen.add(name)
        symbol = self.libraries.get(name)
        if symbol is None:
            self.warnings.add(f'Embedded KiCad symbol "{name}" is unavailable; showing a placeholder.')
            return []
        result = []
        parent = value(symbol, "extends")
        if parent:
            result.extend(self.library_parts(parent, unit, style, seen))
        result.append(symbol)
        for part in forms(symbol, "symbol"):
            suffix = re.search(r"_(\d+)_(\d+)$", str(part[1]))
            if suffix and int(suffix[1]) in (0, unit) and int(suffix[2]) in (0, style):
                result.append(part)
        return result

    def symbol(self, node):
        uid = value(node, "uuid", self.identifier("symbol"))
        properties = {str(prop[1]): str(prop[2]) for prop in forms(node, "property") if len(prop) > 2}
        reference = self.instance_references.get(uid) or properties.get("Reference", uid)
        name = value(node, "lib_id")
        unit = int(scalar(node, "unit", 1))
        style = int(scalar(node, "convert", scalar(node, "body_style", 1)))
        parts = self.library_parts(name, unit, style)
        x, y = point(node); degrees = angle(node)
        mirror = value(node, "mirror")
        sx, sy = (-1 if mirror == "y" else 1), (-1 if mirror == "x" else 1)
        def world(position):
            p = rotate((position[0], -position[1]), -degrees)
            return (p[0] * sx + x, p[1] * sy + y)
        group_id = self.identifier("component")
        group = element(self.scene, "g", id=group_id)
        body_id = self.identifier("body")
        body = element(group, "g", id=body_id, transform=f"translate({fmt(x)} {fmt(y)}) scale({sx} {sy}) rotate({fmt(-degrees)}) scale(1 -1)")
        library = self.libraries.get(name, [])
        for part in parts:
            for child in part[1:]:
                if isinstance(child, list) and child and str(child[0]) in ("rectangle", "circle", "arc", "polyline", "bezier"):
                    self.graphic(body, child)
        if not parts:
            element(body, "rect", x="-2.5", y="-2.5", width="5", height="5", fill="none", stroke="#c44", stroke_width="0.3")
        for prop in forms(node, "property"):
            if len(prop) > 2 and form(prop, "at"):
                draw_text(group, prop, self.instance_references.get(uid, prop[2]) if str(prop[1]) == "Reference" else prop[2], color="#774422")
        hide_names = flag(form(library, "pin_names"), "hide")
        hide_numbers = flag(form(library, "pin_numbers"), "hide")
        for part in parts:
            for pin in forms(part, "pin"):
                pin_number = value(pin, "number")
                pin_name = value(pin, "name")
                pin_id = self.identifier("pin")
                pin_group = element(group, "g", id=pin_id)
                start = point(pin)
                length = scalar(pin, "length", 2.54)
                delta = rotate((length, 0), angle(pin))
                end = (start[0] + delta[0], start[1] + delta[1])
                a, b = world(start), world(end)
                if not flag(pin, "hide"):
                    element(pin_group, "path", d=path_points([a, b]), stroke="#995b27", stroke_width="0.1524", fill="none")
                    if len(pin) > 2 and "inverted" in str(pin[2]):
                        element(pin_group, "circle", cx=fmt(b[0]), cy=fmt(b[1]), r="0.5", stroke="#995b27", stroke_width="0.1524", fill="#fffcf8")
                    if not hide_numbers and pin_number:
                        self.pin_text(pin_group, (a[0]+b[0])/2, (a[1]+b[1])/2-0.5, pin_number, 0.9)
                    if not hide_names and pin_name and pin_name != "~":
                        self.pin_text(pin_group, b[0]+0.5, b[1]-0.5, pin_name, 0.9)
                implicit = properties.get("Value", pin_name) if form(library, "power") and len(pin) > 1 and str(pin[1]) == "power_in" else None
                terminal = (reference, pin_number) if not reference.startswith('#') and pin_number else None
                self.connections.add(a, pin_id, implicit, terminal)
                if implicit:
                    self.connections.add(a, body_id)
        self.components.append({"id": uid, "name": reference, "elements": [group_id], "body_elements": [body_id]})

    @staticmethod
    def pin_text(parent, x, y, text, height):
        element(parent, "text", text=text, x=fmt(x), y=fmt(y), fill="#774422", font_family="sans-serif", font_size=height, stroke="none")

    def render(self):
        paper = form(self.root, "paper")
        size = {"A0": (1189, 841), "A1": (841, 594), "A2": (594, 420), "A3": (420, 297),
                "A4": (297, 210), "A5": (210, 148), "A": (279.4, 215.9), "B": (431.8, 279.4),
                "USLetter": (279.4, 215.9), "USLegal": (355.6, 215.9)}
        w, h = (number(paper[2]), number(paper[3])) if len(paper) > 3 and str(paper[1]) == "User" else size.get(str(paper[1]) if len(paper) > 1 else "A4", size["A4"])
        if flag(paper, "portrait"):
            w, h = h, w
        element(self.scene, "rect", x="0", y="0", width=fmt(w), height=fmt(h), fill="#fffcf8", stroke="#bbb", stroke_width="0.15")
        wires, labels = 0, 0
        for node in self.root[1:]:
            if not isinstance(node, list) or not node:
                continue
            kind = str(node[0])
            if kind == "symbol":
                self.symbol(node)
            elif kind in ("wire", "bus", "polyline"):
                points = [point(["root", xy], "xy") for xy in forms(form(node, "pts"), "xy")]
                group_id = self.identifier(kind)
                group = element(self.scene, "g", id=group_id)
                element(group, "path", d=path_points(points), fill="none", stroke="#16823d" if kind == "wire" else "#3456aa",
                        stroke_width=fmt(width(node, 0.1524)), stroke_linecap="round")
                if kind == "wire":
                    self.connections.wire(points, group_id); wires += 1
                elif kind == "bus":
                    self.warnings.add("KiCad buses are drawn, but bus-member connectivity is not expanded.")
            elif kind in ("label", "global_label", "hierarchical_label"):
                group_id = self.identifier("label")
                group = element(self.scene, "g", id=group_id)
                name = str(node[1])
                draw_text(group, node, name, color="#16823d")
                self.connections.add(point(node), group_id, name); labels += 1
            elif kind == "junction":
                group_id = self.identifier("junction")
                group = element(self.scene, "g", id=group_id)
                x, y = point(node)
                element(group, "circle", cx=fmt(x), cy=fmt(y), r=fmt((scalar(node, "diameter") or 0.8) / 2), fill="#16823d")
                self.connections.add((x, y), group_id)
            elif kind == "no_connect":
                x, y = point(node)
                element(self.scene, "path", d=path_points([(x-0.5, y-0.5), (x+0.5, y+0.5)]) + " " +
                        path_points([(x-0.5, y+0.5), (x+0.5, y-0.5)]), stroke="#3456aa", stroke_width="0.15", fill="none")
            elif kind == "sheet":
                x, y = point(node); sw, sh = point(node, "size")
                group = element(self.scene, "g")
                element(group, "rect", x=fmt(x), y=fmt(y), width=fmt(sw), height=fmt(sh), fill="#eaf2f5", stroke="#8a647a", stroke_width="0.2")
                for prop in forms(node, "property"):
                    if len(prop) > 2:
                        draw_text(group, prop, prop[2], color="#774422")
                for pin in forms(node, "pin"):
                    group_id = self.identifier("sheet-pin")
                    pin_group = element(group, "g", id=group_id)
                    draw_text(pin_group, pin, pin[1], color="#16823d")
                    # A child sheet's pin name does not globally label the parent wire.
                    self.connections.add(point(pin), group_id)
                self.warnings.add("Open child .kicad_sch sheets individually; hierarchical project connectivity is not compiled.")
            elif kind in ("text", "rectangle", "circle", "arc", "bezier"):
                self.graphic(self.scene, node)
            elif kind == "bus_entry":
                x, y = point(node); dx, dy = point(node, "size")
                element(self.scene, "path", d=path_points([(x, y), (x+dx, y+dy)]), fill="none", stroke="#3456aa", stroke_width="0.2")
                self.warnings.add("KiCad buses are drawn, but bus-member connectivity is not expanded.")
            elif kind in ("image", "text_box", "table"):
                self.warnings.add("KiCad images, text boxes, or tables are omitted from this preview.")
        return {"svg": svg_document(self.scene, (0, 0, w, h)), "kind": "schematic", "warnings": sorted(self.warnings),
                "selection": {"components": self.components, "nets": self.connections.nets()},
                "stats": {"components": len(self.components), "wires": wires, "net_labels": labels}}


def render_schematic(path):
    return {"name": path.name, **SchematicRenderer(load(path, "kicad_sch")).render()}
