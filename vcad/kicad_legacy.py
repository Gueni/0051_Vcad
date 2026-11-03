"""Preview legacy Eeschema sheets without inventing missing library symbols.

The line-oriented .sch format stores fields and component positions, but symbol
graphics and pins live in external libraries. Convert the self-contained parts
to the modern renderer's internal representation and mark component positions.
"""

import re
import shlex

from .kicad_common import draw_text, element, fmt, form, forms, number, point, value
from .kicad_schematic import SchematicRenderer


MIL = 0.0254
PREVIEW_NOTICE = (
    "Legacy .sch preview: external symbol libraries are not loaded. Dashed boxes mark "
    "component positions; symbol graphics and pin connectivity are unavailable. "
    "For complete symbols, open the project with its cache library in KiCad and save as .kicad_sch."
)


def mm(raw):
    return number(raw) * MIL


def effects(size, horizontal="C", vertical="C", style="NN"):
    font = ["font", ["size", mm(size), mm(size)]]
    if "I" in style:
        font.append("italic")
    if "B" in style:
        font.append("bold")
    justify = ["justify"]
    justify.extend({"L": ["left"], "R": ["right"]}.get(horizontal, []))
    justify.extend({"T": ["top"], "B": ["bottom"]}.get(vertical, []))
    return ["effects", font, justify]


class LegacySchematicRenderer(SchematicRenderer):
    def symbol(self, node):
        reference, uid = value(node, "reference"), value(node, "uuid")
        x, y = point(node)
        group_id, body_id = self.identifier("component"), self.identifier("body")
        group = element(self.scene, "g", id=group_id)
        body = element(group, "g", id=body_id, data_symbol_preview="placeholder")
        element(body, "rect", x=fmt(x-1.5), y=fmt(y-1.5), width="3", height="3",
                fill="none", stroke="#bc5045", stroke_width="0.2", stroke_dasharray="0.5 0.3")
        for prop in forms(node, "property"):
            draw_text(group, prop, prop[2], color="#774422")
        # Standard legacy power symbols label the net at their origin. PWR_FLAG
        # is an ERC marker, not a global net name. Other symbols have no known pins.
        if value(node, "lib_id").startswith("power:") and reference.startswith("#PWR"):
            properties = {prop[1]: prop[2] for prop in forms(node, "property")}
            name = properties.get("Value", "")
            if name and name != "PWR_FLAG":
                self.connections.add((x, y), body_id, name)
        self.components.append({"id": uid, "name": reference, "elements": [group_id], "body_elements": [body_id]})


def read_legacy(path):
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines or not re.fullmatch(r"EESchema Schematic File Version [1-4]", lines[0]):
        raise ValueError("Expected a KiCad legacy Eeschema .sch document (versions 1–4).")
    if not any(line.strip() == "$EndSCHEMATC" for line in lines):
        raise ValueError("Invalid KiCad legacy schematic: missing $EndSCHEMATC.")
    root, warnings = ["kicad_sch"], {PREVIEW_NOTICE}
    index = 1

    def next_line():
        nonlocal index
        if index >= len(lines):
            raise ValueError("Invalid KiCad legacy schematic: unexpected end of file.")
        line = lines[index].strip()
        index += 1
        return line

    def block(end):
        rows = []
        while True:
            line = next_line()
            if line == end:
                return rows
            if line.startswith("$"):
                raise ValueError(f"Invalid KiCad legacy schematic: expected {end}.")
            rows.append(shlex.split(line, comments=False))

    try:
        while index < len(lines):
            line = next_line()
            tokens = shlex.split(line, comments=False)
            if not tokens:
                continue
            kind = tokens[0]
            if kind == "$Descr":
                w, h = mm(tokens[2]), mm(tokens[3])
                if w <= 0 or h <= 0:
                    raise ValueError("Legacy sheet dimensions must be positive.")
                root.append(["paper", "User", w, h])
                block("$EndDescr")
            elif kind == "$Comp":
                rows = block("$EndComp")
                identity = next(row for row in rows if row and row[0] == "L")
                location = next(row for row in rows if row and row[0] == "P")
                unit = next(row for row in rows if row and row[0] == "U")
                node = ["symbol", ["lib_id", identity[1]], ["reference", identity[2]],
                        ["uuid", unit[3]], ["at", mm(location[1]), mm(location[2])]]
                for row in rows:
                    if not row or row[0] != "F":
                        continue
                    field_id = int(row[1])
                    field_name = {0: "Reference", 1: "Value", 2: "Footprint", 3: "Datasheet"}.get(field_id, f"Field {field_id}")
                    prop = ["property", field_name, row[2], ["at", mm(row[4]), mm(row[5]), 90 if row[3] == "V" else 0],
                            effects(row[6], row[8], row[9][0], row[9][1:])]
                    if int(row[7], 16) & 1:
                        prop.append("hide")
                    node.append(prop)
                root.append(node)
            elif kind in ("Wire", "Entry"):
                coords = shlex.split(next_line(), comments=False)
                a, b = [mm(c) for c in coords[:2]], [mm(c) for c in coords[2:4]]
                if len(a) != 2 or len(b) != 2:
                    raise ValueError("Invalid legacy wire coordinates.")
                if kind == "Entry":
                    root.append(["bus_entry", ["at", *a], ["size", b[0]-a[0], b[1]-a[1]]])
                else:
                    wire_kind = {"Wire": "wire", "Bus": "bus", "Notes": "polyline"}.get(tokens[1])
                    if wire_kind:
                        root.append([wire_kind, ["pts", ["xy", *a], ["xy", *b]]])
            elif kind == "Text":
                text = next_line().replace(r"\n", "\n")
                text_kind = {"Label": "label", "GLabel": "global_label", "HLabel": "hierarchical_label", "Notes": "text"}.get(tokens[1])
                if text_kind:
                    orientation = int(tokens[4])
                    # Legacy orientation 0/2 reads horizontally, 1/3 vertically.
                    root.append([text_kind, text, ["at", mm(tokens[2]), mm(tokens[3]), 90 if orientation % 2 else 0],
                                 effects(tokens[5], "R" if orientation in (0, 3) and text_kind != "text" else "L", "B")])
            elif kind in ("Connection", "NoConn"):
                root.append(["junction" if kind == "Connection" else "no_connect", ["at", mm(tokens[2]), mm(tokens[3])]])
            elif kind == "$Sheet":
                rows = block("$EndSheet")
                size = next(row for row in rows if row and row[0] == "S")
                x, y, w, h = [mm(c) for c in size[1:5]]
                node = ["sheet", ["at", x, y], ["size", w, h]]
                for row in rows:
                    if not row or not re.fullmatch(r"F\d+", row[0]):
                        continue
                    field_id = int(row[0][1:])
                    if field_id < 2:
                        node.append(["property", "Sheetname" if field_id == 0 else "Sheetfile", row[1],
                                     ["at", x, y-1.5 if field_id == 0 else y+h+1.5], effects(row[2], "L")])
                    else:
                        node.append(["pin", row[1], ["at", mm(row[4]), mm(row[5])], effects(row[6], "L")])
                root.append(node)
            elif kind == "$Bitmap":
                block("$EndBitmap")
                warnings.add("Legacy schematic bitmap images are omitted from this preview.")
        if not form(root, "paper"):
            raise ValueError("Invalid KiCad legacy schematic: missing sheet description.")
    except (IndexError, StopIteration, ValueError) as exc:
        raise ValueError(f"Invalid KiCad legacy schematic near line {index}: {exc}") from exc
    return root, warnings


def render_legacy_schematic(path):
    root, warnings = read_legacy(path)
    renderer = LegacySchematicRenderer(root)
    renderer.warnings.update(warnings)
    result = renderer.render()
    result["warnings"] = [PREVIEW_NOTICE] + [warning.replace(".kicad_sch sheets", ".sch sheets")
                                           for warning in result["warnings"] if warning != PREVIEW_NOTICE]
    return {"name": path.name, **result}
