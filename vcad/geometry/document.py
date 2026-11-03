"""Offscreen SVG bounds/metadata index and reference rasterization for the compiler."""

import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from shapely import box as boxes
from shapely.geometry import box
from shapely.strtree import STRtree


SVG = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG)
# Qt SVG expects the literal xlink prefix for image references. ElementTree's
# generated ns1:href is valid XML but loses embedded images when Qt reloads it.
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
GRAPHICS = {
    "path",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "text",
    "use",
}
SELECTION_CONTEXT = 0.15
SCHEMATIC_SELECTION_CONTEXT = 0.45
SCHEMATIC_SELECTION_COLOR = "#008a3b"
SCHEMATIC_NET_WIDTH_SCALE = 3.5


def faded_paint(value, background, contrast=SELECTION_CONTEXT):
    """Keep geometry opaque while reducing its contrast against the canvas.

    Fading colors instead of object opacity preserves drill cutouts, overlapping
    copper and the hit-test alpha of thin, unselected wires.
    """
    color = QColor(value)
    if not color.isValid() or color.alpha() == 0:
        return value
    channels = [
        round(bg + (fg - bg) * contrast)
        for fg, bg in zip(color.getRgb()[:3], background.getRgb()[:3], strict=True)
    ]
    if color.alpha() < 255:
        return f"rgba({channels[0]},{channels[1]},{channels[2]},{color.alphaF()})"
    return QColor(*channels).name()


def set_paint(node, overrides):
    # Qt ignores inline styles beginning with an empty declaration. Replace
    # existing declarations as well as attributes so both use the same paint.
    inline = {
        key.strip(): value.strip()
        for declaration in node.get("style", "").split(";")
        if ":" in declaration
        for key, value in [declaration.split(":", 1)]
    }
    inline.update(overrides)
    node.set("style", ";".join(f"{key}:{value}" for key, value in inline.items()))
    for name, value in overrides.items():
        node.set(name, value)


def tag(node):
    return node.tag.rsplit("}", 1)[-1]


def default_layer(node):
    token = node.get("data-layer-token", "").upper()
    role = node.get("data-layer-role", "")
    name = node.get("data-layer-name", "").lower()
    return (
        node.get("id") == "board-outline"
        or role in ("copper", "drill")
        or node.get("data-layer-family") in ("signal", "internal_plane")
        or token in ("TOP", "BOTTOM", "TOPOVERLAY", "DRILLS", "MULTILAYER")
        or token.startswith(("MID", "PLANE"))
        or node.get("id", "").lower()
        in ("layer-top", "layer-bottom", "layer-topoverlay", "layer-drills")
        or name in ("top copper", "bottom copper", "top overlay")
    )


@dataclass
class Primitive:
    id: str
    bounds: QRectF
    layer: str
    polygon: bool
    component: str = ""
    net: str = ""
    pin: str = ""
    schematic_components: tuple = ()
    schematic_nets: tuple = ()
    hole: bool = False
    fill: bool = True
    stroke: bool = False


class GeometryDocument:
    """Owned by one offscreen compiler process; never creates a viewer window."""

    def __init__(self, data):
        self.data, self.root = data, ET.fromstring(data["svg"])
        from .text import outline_text

        outline_text(self.root)
        self.scene = next(
            (el for el in self.root.iter() if el.get("id") == "scene"), self.root
        )
        self.elements, self.parents, self.text_groups = {}, {}, set()
        index = 0

        def register(node, inside_text=False):
            nonlocal index
            index += 1
            kind = tag(node)
            text_group = (
                data.get("kind", "pcb") == "pcb"
                and kind == "g"
                and node.get("data-primitive") == "text"
            )
            if (
                (kind in GRAPHICS or text_group)
                and not inside_text
                and not node.get("id")
            ):
                node.set("id", f"native-{index}")
            if identifier := node.get("id"):
                self.elements[identifier] = node
                if text_group and not inside_text:
                    self.text_groups.add(identifier)
            for child in node:
                self.parents[child] = node
                register(child, inside_text or text_group)

        register(self.root)
        # Interaction metadata belongs to the XML index, not Qt's paint tree.
        # Large Altium boards repeat it on every primitive and glyph; omitting
        # those attributes makes serialization and Qt's parser much cheaper.
        metadata = []
        for node in self.root.iter():
            attributes = {
                name: value
                for name, value in node.attrib.items()
                if name.startswith("data-")
            }
            if attributes:
                metadata.append((node, attributes))
                for name in attributes:
                    del node.attrib[name]
        self.source = ET.tostring(self.root)
        for node, attributes in metadata:
            node.attrib.update(attributes)
        self.renderer = QSvgRenderer(QByteArray(self.source))
        self.renderer.setAnimationEnabled(False)
        if not self.renderer.isValid():
            raise ValueError("Qt could not read the rendered document.")
        self.home = self.renderer.viewBoxF()
        self.layers = []
        if data.get("kind", "pcb") == "pcb":
            for node in self.scene:
                if tag(node) == "g":
                    self.layers.append(
                        {
                            "id": node.get("id"),
                            "name": node.get("data-layer-display-name")
                            or node.get("data-layer-name")
                            or node.get("id"),
                            "visible": default_layer(node),
                        }
                    )
        # The inspector follows physical copper order. Keep the source/paint
        # sequence separate so merely displaying the list cannot change overlap.
        ranks = {
            token: index
            for index, token in enumerate(data.get("copper_layer_order", []))
        }
        list_ranks = {
            node.get("id"): ranks[node.get("data-layer-token")]
            for node in self.scene
            if node.get("data-layer-token") in ranks
        }
        ordered = iter(
            sorted(
                (layer for layer in self.layers if layer["id"] in list_ranks),
                key=lambda layer: list_ranks[layer["id"]],
            )
        )
        self.layer_list = [
            next(ordered) if layer["id"] in list_ranks else layer
            for layer in self.layers
        ]
        self.layer_ids = {layer["id"] for layer in self.layers}
        copper_layers = {
            node.get("id")
            for node in self.scene
            if node.get("data-layer-role") == "copper"
            or node.get("data-layer-family") in ("signal", "internal_plane")
            or node.get("data-layer-token", "").upper()
            in ("TOP", "BOTTOM", "MULTILAYER")
            or node.get("id", "").lower()
            in ("layer-top", "layer-bottom", "layer-multilayer")
        }
        snap_points = defaultdict(set)
        self.drill_layers = {
            node.get("id")
            for node in self.scene
            if node.get("data-layer-role") == "drill"
            or node.get("data-layer-token", "").upper() == "DRILLS"
            or node.get("id", "").lower() == "layer-drills"
        }
        self.polygon_ids = set(data.get("polygon_elements", []))
        components, nets = defaultdict(list), defaultdict(list)
        self.component_rows, self.net_rows = {}, {}
        for key, output, lookup in (
            ("components", self.component_rows, components),
            ("nets", self.net_rows, nets),
        ):
            for row in data.get("selection", {}).get(key, []):
                output[row["id"]] = row
                for identifier in row["elements"]:
                    lookup[identifier].append(row["id"])
        self.primitives, self.component_bounds, self.net_bounds = (
            [],
            defaultdict(list),
            defaultdict(list),
        )
        self.body_bounds = []
        self.paints = {}
        self.stroke_widths = {}
        metadata_names = (
            "data-component",
            "data-net",
            "data-primitive",
            "data-hole-owner",
            "data-pad-number",
            "data-pad-designator",
            "data-snap-x",
            "data-snap-y",
        )

        def visit(
            node, inherited, paint, stroke_width, layer="", cs=(), ns=(), polygon=False
        ):
            kind = tag(node)
            if kind in ("defs", "clipPath", "mask", "pattern"):
                return
            attributes = node.attrib
            identifier = attributes.get("id")
            if identifier in self.layer_ids:
                layer = identifier
            if own_components := components.get(identifier):
                cs = tuple(dict.fromkeys((*own_components, *cs)))
            if own_nets := nets.get(identifier):
                ns = tuple(dict.fromkeys((*own_nets, *ns)))
            polygon = (
                polygon
                or identifier in self.polygon_ids
                or attributes.get("data-primitive")
                in ("polygon-outline", "polygon-cutout")
            )
            declarations = attributes
            if inline := attributes.get("style"):
                declarations = dict(attributes)
                declarations.update(
                    (name, value.strip())
                    for name, value in re.findall(r"([\w-]+)\s*:\s*([^;]+)", inline)
                )
            fill, stroke = declarations.get("fill"), declarations.get("stroke")
            if fill and fill != "inherit" or stroke and stroke != "inherit":
                paint = {
                    "fill": fill if fill and fill != "inherit" else paint["fill"],
                    "stroke": stroke
                    if stroke and stroke != "inherit"
                    else paint["stroke"],
                }
            if declarations.get("stroke-width") not in (None, "", "inherit"):
                stroke_width = declarations["stroke-width"]

            def attr(name):
                return attributes.get(name) or inherited.get(name, "")

            text_group = identifier in self.text_groups
            if kind in GRAPHICS or text_group:
                component, net = attr("data-component"), attr("data-net")
                primitive_kind = attr("data-primitive")
                hole = bool(attr("data-hole-owner")) or primitive_kind.endswith("-hole")
                if text_group:
                    # Text can contain thousands of independent glyph strokes.
                    # Qt supplies exact painted group bounds and hit testing;
                    # indexing each stroke adds no selection information.
                    paint = {
                        name: next(
                            (
                                child.get(name, paint[name])
                                for child in node.iter()
                                if tag(child) in GRAPHICS
                                and child.get(name, paint[name])
                                not in ("none", "inherit")
                            ),
                            "none",
                        )
                        for name in ("fill", "stroke")
                    }
                self.paints[identifier] = paint
                self.stroke_widths[identifier] = stroke_width
                snap = (
                    layer in copper_layers
                    and primitive_kind in ("pad", "via")
                    and not hole
                )
                indexed = bool(component or net or cs or ns or hole)
                if indexed or snap:
                    bounds = self.bounds(identifier)
                    if snap:
                        snap_x, snap_y = attr("data-snap-x"), attr("data-snap-y")
                        center = (
                            self.renderer.transformForElement(identifier).map(
                                QPointF(float(snap_x), float(snap_y))
                            )
                            if snap_x and snap_y
                            else bounds.center()
                        )
                        snap_points[(center.x(), center.y())].add(layer)
                    if indexed:
                        if bounds.isEmpty():
                            bounds = bounds.adjusted(-0.01, -0.01, 0.01, 0.01)
                        primitive = Primitive(
                            identifier,
                            bounds,
                            layer,
                            polygon,
                            component,
                            net,
                            attr("data-pad-number") or attr("data-pad-designator"),
                            cs,
                            ns,
                            hole,
                            paint["fill"] != "none",
                            paint["stroke"] != "none",
                        )
                        self.primitives.append(primitive)
                        for c in cs or ([component] if component else []):
                            self.component_bounds[c].append(bounds)
                        for n in ns or ([net] if net else []):
                            self.net_bounds[n].append(bounds)
            if len(node) and not text_group:
                # Carry inherited values once per group, rather than walking
                # every primitive's ancestors for each metadata field.
                child_metadata = dict(inherited)
                child_metadata.update(
                    (name, attributes[name])
                    for name in metadata_names
                    if attributes.get(name)
                )
                for child in node:
                    visit(
                        child,
                        child_metadata,
                        paint,
                        stroke_width,
                        layer,
                        cs,
                        ns,
                        polygon,
                    )

        visit(self.root, {}, {"fill": "black", "stroke": "none"}, "1")
        self.snap_points = [
            (QPointF(x, y), frozenset(layers))
            for (x, y), layers in sorted(snap_points.items())
        ]
        for cid, row in self.component_rows.items():
            for identifier in row.get("body_elements", []):
                if identifier in self.elements:
                    self.body_bounds.append((cid, self.bounds(identifier)))
        self.tree = STRtree(
            boxes(
                [p.bounds.left() for p in self.primitives],
                [p.bounds.top() for p in self.primitives],
                [p.bounds.right() for p in self.primitives],
                [p.bounds.bottom() for p in self.primitives],
            )
        )
        self.component_layers, self.net_layers, self.terminals = (
            defaultdict(set),
            defaultdict(set),
            defaultdict(set),
        )
        for p in self.primitives:
            if p.component and p.pin and p.net:
                self.terminals[(p.component, p.pin)].add(p.net)
            for name in p.schematic_components or (
                [p.component] if p.component else []
            ):
                self.component_layers[name].add(p.layer)
            for name in p.schematic_nets or ([p.net] if p.net else []):
                self.net_layers[name].add(p.layer)
        self.style = None
        self.style_changes = {}
        self.selection_overlay = None
        self.scene_children = list(self.scene)
        self.render_count = 0

    def bounds(self, identifier):
        return self.renderer.transformForElement(identifier).mapRect(
            self.renderer.boundsOnElement(identifier)
        )

    def info(self):
        def union(rects):
            result = QRectF()
            for rect in rects:
                result = result.united(rect)
            return result

        return {
            "name": self.data["name"],
            "kind": self.data.get("kind", "pcb"),
            "stats": self.data.get("stats", {}),
            "warnings": self.data.get("warnings", []),
            "layers": self.layers,
            "layer_list": self.layer_list,
            "home": self.home,
            "components": self.component_rows
            or {name: {"name": name, "id": name} for name in self.component_bounds},
            "nets": self.net_rows
            or {name: {"name": name, "id": name} for name in self.net_bounds},
            "component_bounds": {
                key: union(rows) for key, rows in self.component_bounds.items()
            },
            "net_bounds": {key: union(rows) for key, rows in self.net_bounds.items()},
            "layout_components": self.data.get("layout_components", []),
            "terminals": dict(self.terminals),
            "captions": [
                (p.bounds, p.pin, p.net, p.layer, p.polygon, p.component)
                for p in self.primitives
                if p.pin and not p.hole
            ],
            "snap_points": self.snap_points,
            "component_layers": dict(self.component_layers),
            "net_layers": dict(self.net_layers),
            "scene3d": self.data.get("scene3d", {}),
        }

    def ordered_layers(self, layer_order):
        ranks = {identifier: index for index, identifier in enumerate(layer_order)}
        # Drill cutouts must remain open even when copper is brought forward.
        return sorted(
            (layer["id"] for layer in self.layers),
            key=lambda identifier: (
                identifier in self.drill_layers,
                ranks.get(identifier, -1),
            ),
        )

    def apply_style(
        self, visible, polygons, selected_components, selected_nets, layer_order=()
    ):
        style = (
            frozenset(visible),
            polygons,
            frozenset(selected_components),
            frozenset(selected_nets),
            tuple(layer_order),
        )
        if style == self.style:
            return
        self.style = style
        root, elements = self.root, self.elements
        # Restore only attributes touched by the previous style. The worker owns
        # this tree; reusing it avoids copying a whole large board on every click.
        for (identifier, name), value in self.style_changes.items():
            node = elements[identifier]
            if value is None:
                node.attrib.pop(name, None)
            else:
                node.set(name, value)
        self.style_changes.clear()
        if self.selection_overlay is not None:
            root.remove(self.selection_overlay)
            self.selection_overlay = None

        def remember(identifier, names):
            node = elements[identifier]
            for name in names:
                self.style_changes.setdefault((identifier, name), node.get(name))

        if self.layer_ids and layer_order:
            ordered = iter(
                elements[identifier] for identifier in self.ordered_layers(layer_order)
            )
            # Replace only layer slots, preserving backgrounds and other scene nodes.
            children = [
                next(ordered) if child.get("id") in self.layer_ids else child
                for child in self.scene_children
            ]
        else:
            children = self.scene_children
        if list(self.scene) != children:
            self.scene[:] = children
        hidden = set(self.layer_ids) - set(visible)
        for identifier in hidden:
            remember(identifier, ("display",))
            elements[identifier].set("display", "none")
        if not polygons:
            for identifier in self.polygon_ids:
                if identifier in elements:
                    remember(identifier, ("display",))
                    elements[identifier].set("display", "none")
        selected = {}
        schematic_components = set()
        schematic_nets = set()
        schematic = self.data.get("kind") == "schematic"
        component_color = SCHEMATIC_SELECTION_COLOR if schematic else "#ffe066"
        net_color = SCHEMATIC_SELECTION_COLOR if schematic else "#39e0f3"
        context_contrast = (
            SCHEMATIC_SELECTION_CONTEXT if schematic else SELECTION_CONTEXT
        )
        holes = {p.id for p in self.primitives if p.hole}
        for p in self.primitives:
            if p.hole:
                continue
            component_selected = p.component in selected_components or set(
                p.schematic_components
            ).intersection(selected_components)
            net_selected = p.net in selected_nets or set(p.schematic_nets).intersection(
                selected_nets
            )
            if component_selected or net_selected:
                selected[p.id] = component_color if component_selected else net_color
                if component_selected and schematic:
                    schematic_components.add(p.id)
                if net_selected and schematic:
                    schematic_nets.add(p.id)
        if selected_components or selected_nets:
            background = QColor(
                "#fffcf8" if self.data.get("kind") == "schematic" else "#1b1d21"
            )
            faded_colors = {}
            for identifier, paint in self.paints.items():
                if identifier in holes:
                    continue
                node = elements[identifier]
                contour_only = (
                    identifier in schematic_components
                    and node.get("data-native-text") != "true"
                )
                overrides = {}
                for name, value in paint.items():
                    if contour_only and name == "fill":
                        # A schematic symbol's interior is not a selection wash.
                        # Glyphs remain filled so references/values stay legible.
                        overrides[name] = value
                    elif value == "none" or QColor(value).alpha() == 0:
                        overrides[name] = value
                    elif identifier in selected:
                        overrides[name] = selected[identifier]
                    else:
                        if value not in faded_colors:
                            faded_colors[value] = faded_paint(
                                value, background, context_contrast
                            )
                        overrides[name] = faded_colors[value]
                if (
                    contour_only
                    and paint["stroke"] == "none"
                    and paint["fill"] != "none"
                    and QColor(paint["fill"]).alpha() != 0
                ):
                    # Filled-only symbol shapes still need a visible contour;
                    # use their inherited SVG stroke width (default one unit).
                    overrides["stroke"] = selected[identifier]
                if identifier in schematic_nets and paint["stroke"] != "none":
                    # Scale the original width once, retaining SVG units and
                    # inherited/inline widths. Text and filled junctions stay intact.
                    width = re.fullmatch(
                        r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-zA-Z%]*)\s*",
                        self.stroke_widths[identifier],
                    )
                    if width:
                        overrides["stroke-width"] = (
                            f"{float(width[1]) * SCHEMATIC_NET_WIDTH_SCALE:g}{width[2]}"
                        )
                remember(identifier, (*overrides, "style"))
                set_paint(node, overrides)

            # Draw the selection above faded layers so an unselected crossing
            # cannot hide a route. Keep ancestor transforms/clips and drill paint.
            def selection_branch(node):
                if tag(node) in GRAPHICS:
                    return (
                        deepcopy(node)
                        if node.get("id") in selected or node.get("id") in holes
                        else None
                    )
                branch = ET.Element(node.tag, node.attrib)
                for child in node:
                    copy = selection_branch(child)
                    if copy is not None:
                        branch.append(copy)
                return branch if len(branch) else None

            if selected:
                overlay = selection_branch(root)
                # The outer SVG already supplies its viewport and inherited
                # properties; the overlay is only another paint group within it.
                overlay.tag = f"{{{SVG}}}g"
                overlay.attrib.clear()
                for node in overlay.iter():
                    node.attrib.pop("id", None)
                root.append(overlay)
                self.selection_overlay = overlay
        self.renderer.load(QByteArray(ET.tostring(root)))
        self.renderer.setAnimationEnabled(False)
        self.renderer.setAspectRatioMode(Qt.AspectRatioMode.IgnoreAspectRatio)

    def raster(self, request):
        if self.data.get("kind", "pcb") == "pcb":
            from .pcb_raster import PcbRaster

            if not hasattr(self, "pcb_raster"):
                self.pcb_raster = PcbRaster(self)
            visible, polygons, components, nets, *order = request["style"]
            style = (
                frozenset(visible),
                polygons,
                frozenset(components),
                frozenset(nets),
                tuple(order[0]) if order else (),
            )
            image = self.pcb_raster.render(
                request["rect"], request["width"], request["height"], style
            )
            self.render_count += 1
            return {
                "image": image,
                "rect": request["rect"],
                "token": request["token"],
                "renders": self.render_count,
                "style": style,
            }
        self.apply_style(*request["style"])
        rect, width, height = request["rect"], request["width"], request["height"]
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("#1b1d21"))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.renderer.setViewBox(rect)
        self.renderer.render(painter, QRectF(0, 0, width, height))
        painter.end()
        self.render_count += 1
        return {
            "image": image,
            "rect": rect,
            "token": request["token"],
            "renders": self.render_count,
            "style": self.style,
        }

    def pick(self, position, tolerance, visible, polygons, layer_order=()):
        indices = self.tree.query(
            box(
                position.x() - tolerance,
                position.y() - tolerance,
                position.x() + tolerance,
                position.y() + tolerance,
            )
        )
        # Test paint, not just bounding boxes: holes, empty symbol outlines and
        # curved routes must remain click-through.
        ranks = (
            {
                identifier: index
                for index, identifier in enumerate(self.ordered_layers(layer_order))
            }
            if layer_order
            else {}
        )
        for index in sorted(
            indices,
            key=lambda index: (ranks.get(self.primitives[index].layer, -1), index),
            reverse=True,
        ):
            p = self.primitives[index]
            if p.layer and p.layer not in visible or p.polygon and not polygons:
                continue
            image = QImage(9, 9, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            painter.translate(4.5, 4.5)
            painter.scale(4 / tolerance, 4 / tolerance)
            painter.translate(-position.x(), -position.y())
            painter.setTransform(self.renderer.transformForElement(p.id), True)
            self.renderer.render(painter, p.id, self.renderer.boundsOnElement(p.id))
            painter.end()
            if any(
                image.pixelColor(x, y).alpha() > 32 for x in range(9) for y in range(9)
            ):
                return (
                    None
                    if p.hole
                    else {
                        "components": list(p.schematic_components)
                        or ([p.component] if p.component else []),
                        "nets": list(p.schematic_nets) or ([p.net] if p.net else []),
                    }
                )
        for cid, bounds in reversed(self.body_bounds):
            if bounds.contains(position):
                return {"components": [cid], "nets": []}
        return None
