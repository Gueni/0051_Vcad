"""KiCad PCB geometry, viewer metadata, and board-only 3D scenes."""

from collections import defaultdict
import fnmatch
import math
import xml.etree.ElementTree as ET

from shapely import make_valid, union_all
from shapely.affinity import rotate as rotate_geometry, translate
from shapely.geometry import GeometryCollection, LineString, Point, Polygon, box
from shapely.ops import polygonize_full

from .kicad_common import (
    angle, draw_text, element, flag, fmt, form, forms, geometry_path, graphic_points,
    load, number, path_points, point, polygon_parts, rotate, scalar, svg_document, value, width,
)
from .step_models import kicad_models
from .layer_colors import layer_colors


class PcbRenderer:
    def __init__(self, root, side, clip, pour_color, polygon_outlines, directory=None):
        self.directory = directory
        self.root, self.side, self.clip = root, side, clip
        self.pour_color, self.polygon_outlines = pour_color, polygon_outlines
        self.layers, self.names, self.layer_types = [], {}, {}
        for layer in form(root, "layers")[1:]:
            if isinstance(layer, list) and len(layer) >= 2:
                name = str(layer[1]); self.layers.append(name)
                self.names[name] = str(layer[3]) if len(layer) > 3 else name
                self.layer_types[name] = str(layer[2]) if len(layer) > 2 else ""
        self.colors = layer_colors([*self.layers, "Edge.Cuts", "Drills"])
        self.scene = ET.Element("g", {"id": "scene"})
        self.groups, self.edges, self.drills, self.polygon_ids = {}, [], [], []
        self.bounds, self.warnings = [], set()
        self.filled_layers = set()
        self.net_names = {str(node[1]): str(node[2]) for node in forms(root, "net") if len(node) > 2}
        self.stats = dict(components=len(forms(root, "footprint")) + len(forms(root, "module")),
                          pads=0, tracks=0, vias=0, polygons=0, regions=0)
        self.layout_components = []
        self.count = 0

    def group(self, layer):
        if layer not in self.groups:
            color = self.color(layer)
            copper = layer.endswith(".Cu")
            role = "copper" if copper else "drill" if layer == "Drills" else "mask" if layer.endswith(".Mask") else "paste" if layer.endswith(".Paste") else "mechanical"
            token = {"F.Cu": "TOP", "B.Cu": "BOTTOM", "F.SilkS": "TOPOVERLAY", "B.SilkS": "BOTTOMOVERLAY",
                     "Drills": "DRILLS"}.get(layer, layer)
            if copper and layer.startswith("In"):
                token = "MID" + layer[2:-3]
            self.groups[layer] = element(self.scene, "g", id="board-outline" if layer == "Edge.Cuts" else f"kc-layer-{len(self.groups)}",
                data_layer_name=self.names.get(layer, layer), data_layer_token=token,
                data_layer_role=role,
                data_color=color, fill=color,
                stroke=color, stroke_linecap="round", stroke_linejoin="round")
        return self.groups[layer]

    def color(self, layer):
        # Some graphics refer to layers omitted from the declaration table.
        if layer not in self.colors:
            self.colors.update(layer_colors([layer], existing=self.colors))
        return self.colors[layer]

    def allowed(self, layer):
        if self.side == "all" or layer in ("Edge.Cuts", "Drills"):
            return True
        return layer.startswith("F." if self.side == "top" else "B.")

    def metadata(self, node, component="", primitive=""):
        net = form(node, "net")
        if len(net) > 2:
            name = str(net[2])
        elif len(net) > 1:
            # New boards store (net "GND") directly, without a board-level ID
            # table. A quoted numeric name is also a name, not a legacy ID.
            name = net[1] if isinstance(net[1], str) else self.net_names.get(str(net[1]), "")
        else:
            name = ""
        name = value(node, "net_name", name)
        return {"data_component": component or None, "data_net": name or None, "data_primitive": primitive or None}

    def remember(self, geometry):
        if not geometry.is_empty:
            self.bounds.append(geometry.bounds)

    def shape(self, layer, geometry, metadata=None, **style):
        self.remember(geometry)
        if not self.allowed(layer) or geometry.is_empty:
            return None
        self.count += 1
        return element(self.group(layer), "path", id=f"kc-shape-{self.count}", d=geometry_path(geometry),
                       fill_rule="evenodd", stroke="none", **(metadata or {}), **style)

    def primitive(self, layer, kind, metadata=None, **attributes):
        """Keep simple KiCad shapes analytic in SVG and the WebGL compiler."""
        if not self.allowed(layer):
            return None
        self.count += 1
        return element(self.group(layer), kind, id=f"kc-shape-{self.count}",
                       **(metadata or {}), **attributes)

    def route(self, layer, points, stroke_width, metadata=None, closed=False):
        if len(points) < 2:
            return
        self.remember(LineString(points).buffer(stroke_width / 2))
        if layer == "Edge.Cuts":
            self.edges.append(LineString(points + [points[0]] if closed and points[-1] != points[0] else points))
        if self.allowed(layer):
            self.count += 1
            return element(self.group(layer), "path", id=f"kc-route-{self.count}", d=path_points(points, closed),
                fill="none", stroke_width=fmt(stroke_width), **(metadata or {}))

    def graphic(self, node, position=(0, 0), rotation=0, component=""):
        kind = str(node[0])
        layer = value(node, "layer", "F.SilkS")
        if kind in ("fp_text", "gr_text", "property"):
            text = str(node[2]) if kind in ("fp_text", "property") else str(node[1])
            if kind == "property" and (len(node) < 3 or not form(node, "at")):
                return
            if component:
                text = text.replace("${REFERENCE}", component)
            p = rotate(point(node), -rotation)
            p = (p[0] + position[0], p[1] + position[1])
            self.remember(Point(p).buffer(2))
            if self.allowed(layer):
                group = element(self.group(layer), "g", **self.metadata(node, component, "text"))
                draw_text(group, node, text, position=p, color=self.colors[layer])
            return
        if kind in ("fp_circle", "gr_circle") and layer != "Edge.Cuts":
            center, end = point(node, "center"), point(node, "end")
            radius = scalar(node, "radius", math.dist(center, end))
            if radius <= 0:
                return
            center = rotate(center, -rotation)
            center = (center[0]+position[0], center[1]+position[1])
            filled = value(node, "fill") in ("yes", "solid")
            stroke_width = width(node)
            extent = radius + (0 if filled else stroke_width / 2)
            self.bounds.append((center[0]-extent, center[1]-extent, center[0]+extent, center[1]+extent))
            self.primitive(layer, "circle", self.metadata(node, component, "graphic"),
                           cx=fmt(center[0]), cy=fmt(center[1]), r=fmt(radius),
                           fill=None if filled else "none", stroke="none" if filled else None,
                           stroke_width=None if filled else fmt(stroke_width))
            return
        points, closed = graphic_points(node)
        if points:
            points = [(p[0] + position[0], p[1] + position[1]) for p in [rotate(p, -rotation) for p in points]]
            filled = value(node, "fill") in ("yes", "solid")
            if closed and filled and layer != "Edge.Cuts":
                self.shape(layer, make_valid(Polygon(points)), self.metadata(node, component, "graphic"))
            else:
                self.route(layer, points, width(node), self.metadata(node, component, "graphic"), closed)
        elif kind not in ("fp_text_box", "gr_text_box", "dimension", "target"):
            self.warnings.add(f"Unsupported KiCad graphic {kind} was omitted.")
        else:
            self.warnings.add("Text boxes, dimensions, or targets are not rendered in this KiCad preview.")

    def pad(self, node, origin, rotation, component):
        self.stats["pads"] += 1
        position = rotate(point(node), -rotation)
        position = (position[0] + origin[0], position[1] + origin[1])
        w, h = point(node, "size")
        if w <= 0 or h <= 0:
            return
        shape = str(node[3]) if len(node) > 3 else "rect"
        radius = 0
        if shape == "circle":
            geometry = Point(0, 0).buffer(w / 2, quad_segs=24)
        elif shape == "oval":
            half = abs(w - h) / 2
            endpoints = [(-half, 0), (half, 0)] if w >= h else [(0, -half), (0, half)]
            geometry = LineString(endpoints).buffer(min(w, h) / 2, quad_segs=16)
        elif shape == "roundrect":
            radius = min(w, h) * min(0.5, max(0, scalar(node, "roundrect_rratio", 0.25)))
            geometry = box(-w/2+radius, -h/2+radius, w/2-radius, h/2-radius).buffer(radius, quad_segs=12) if radius else box(-w/2, -h/2, w/2, h/2)
        else:
            geometry = box(-w/2, -h/2, w/2, h/2)
            if shape == "custom":
                pieces = [geometry] if value(form(node, "options"), "anchor") == "rect" else [Point(0, 0).buffer(w/2)]
                for primitive in form(node, "primitives")[1:]:
                    if not isinstance(primitive, list):
                        continue
                    points, closed = graphic_points(primitive)
                    if len(points) >= 2:
                        pieces.append(make_valid(Polygon(points)) if closed and value(primitive, "fill") == "yes" else
                                      LineString(points + [points[0]] if closed else points).buffer(width(primitive) / 2))
                geometry = union_all(pieces)
            elif shape not in ("rect",):
                self.warnings.add(f"KiCad {shape} pads are shown with rectangular outlines.")
        drill = form(node, "drill")
        hole = GeometryCollection()
        concentric_drill = None
        if len(drill) > 1:
            values = [number(item) for item in drill[1:] if not isinstance(item, list) and str(item) != "oval"]
            if values and values[0] > 0:
                dx, dy = (values + values[:1])[:2] if flag(drill, "oval") else (values[0], values[0])
                half = abs(dx - dy) / 2
                endpoints = [(-half, 0), (half, 0)] if dx >= dy else [(0, -half), (0, half)]
                hole = LineString(endpoints).buffer(min(dx, dy) / 2, quad_segs=16)
                offset = point(drill, "offset"); hole = translate(hole, *offset)
                if dx == dy and offset == (0, 0):
                    concentric_drill = dx / 2
        def place(geometry_):
            return translate(rotate_geometry(geometry_, -angle(node), origin=(0, 0)), *position)
        hole = place(hole)
        if not hole.is_empty:
            self.drills.append(hole)
        layers = [str(item) for item in form(node, "layers")[1:]]
        expanded = {layer for pattern in layers for layer in self.layers if fnmatch.fnmatchcase(layer, pattern)}
        if "F&B.Cu" in layers:
            expanded.update(("F.Cu", "B.Cu"))
        metadata = {**self.metadata(node, component, "pad"), "data_pad_number": str(node[1]), "data_pad_designator": str(node[1]),
                    "data_snap_x": position[0], "data_snap_y": position[1]}
        for layer in self.layers:
            if layer not in expanded:
                continue
            if len(node) > 2 and str(node[2]) == "np_thru_hole" and layer.endswith(".Cu"):
                continue
            clearance = scalar(node, "solder_mask_margin") if layer.endswith(".Mask") else scalar(node, "solder_paste_margin") if layer.endswith(".Paste") else 0
            copper = place(geometry.buffer(clearance)) if clearance else place(geometry)
            # Mask apertures and paste are not electrical pads. Keeping their
            # metadata separate prevents net probing from revealing them over copper.
            attributes = metadata if layer.endswith(".Cu") else {"data_primitive": "pad-aperture"}
            simple = shape in ("circle", "oval", "rect", "roundrect") and (
                hole.is_empty or shape == "circle" and concentric_drill is not None and concentric_drill < w/2+clearance)
            if simple and not copper.is_empty:
                self.remember(copper)
                # Rotate about the world-space centre, also used by SVG/Qt snapping.
                transform = f"rotate({fmt(-angle(node))} {fmt(position[0])} {fmt(position[1])})" if angle(node) else None
                if shape == "circle":
                    outer, inner = w/2+clearance, concentric_drill or 0
                    self.primitive(layer, "circle", attributes, cx=fmt(position[0]), cy=fmt(position[1]),
                                   r=fmt((outer+inner)/2 if inner else outer),
                                   fill="none" if inner else None, stroke=None if inner else "none",
                                   stroke_width=fmt(outer-inner) if inner else None)
                else:
                    corner = max(0, (min(w, h) / 2 if shape == "oval" else radius) + clearance)
                    self.primitive(layer, "rect", attributes, x=fmt(position[0]-w/2-clearance), y=fmt(position[1]-h/2-clearance),
                                   width=fmt(w+2*clearance), height=fmt(h+2*clearance), rx=fmt(corner), stroke="none", transform=transform)
            else:
                self.shape(layer, copper.difference(hole), attributes)

    def footprint(self, node):
        properties = {str(p[1]): str(p[2]) for p in forms(node, "property") if len(p) > 2}
        reference = properties.get("Reference", "")
        if not reference:
            reference = next((str(text[2]) for text in forms(node, "fp_text") if len(text) > 2 and str(text[1]) == "reference"), "")
        self.layout_components.append({"name": reference, "source_name": reference,
                                       "source_id": value(node, "path").rstrip('/').split('/')[-1]})
        origin, rotation = point(node), angle(node)
        for child in node[1:]:
            if not isinstance(child, list) or not child:
                continue
            kind = str(child[0])
            if kind == "pad":
                self.pad(child, origin, rotation, reference)
            elif kind.startswith("fp_") or kind == "property":
                self.graphic(child, origin, rotation, reference)

    def zone(self, node):
        if form(node, "keepout"):
            return
        self.stats["polygons"] += 1
        layers = [str(item) for item in form(node, "layers")[1:]] or [value(node, "layer", "F.Cu")]
        default_layer = layers[0]
        for fill in forms(node, "filled_polygon"):
            points = [point(["root", xy], "xy") for xy in forms(form(fill, "pts"), "xy")]
            if len(points) < 3:
                continue
            layer = value(fill, "layer", default_layer)
            self.filled_layers.add(layer)
            element_ = self.shape(layer, make_valid(Polygon(points)), self.metadata(node, primitive="region"),
                                  fill=self.pour_color or self.color(layer))
            if element_ is not None:
                self.polygon_ids.append(element_.get("id"))
            self.stats["regions"] += 1
        if self.polygon_outlines or not forms(node, "filled_polygon"):
            for polygon in forms(node, "polygon"):
                points = [point(["root", xy], "xy") for xy in forms(form(polygon, "pts"), "xy")]
                for layer in layers:
                    element_ = self.route(layer, points, 0.1, self.metadata(node, primitive="polygon-outline"), True)
                    if element_ is not None:
                        self.polygon_ids.append(element_.get("id"))
        if not forms(node, "filled_polygon"):
            self.warnings.add("Zones without saved copper fills are shown as outlines. Refill and save them in KiCad to view the pours.")

    def board_profile(self):
        if not self.edges:
            return GeometryCollection(), "3D is unavailable: the PCB has no Edge.Cuts outline."
        # Converted KiCad boards can retain sub-micron endpoint discrepancies.
        # Weld endpoints within 2 microns without rounding the rest of the geometry.
        tolerance, anchors, welded = 0.002, defaultdict(list), []
        for edge in self.edges:
            coords = list(edge.coords)
            for index in (0, -1):
                x, y = coords[index]; cell = (math.floor(x / tolerance), math.floor(y / tolerance))
                near = next((p for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                             for p in anchors[(cell[0]+dx, cell[1]+dy)] if math.dist(p, (x, y)) <= tolerance), None)
                if near is None:
                    anchors[cell].append((x, y))
                else:
                    coords[index] = near
            welded.append(LineString(coords))
        faces, cuts, dangles, invalid = polygonize_full(union_all(welded))
        if faces.is_empty or not cuts.is_empty or not dangles.is_empty or not invalid.is_empty:
            return GeometryCollection(), "3D is unavailable: Edge.Cuts must form closed board outlines."
        # Polygonize includes the holes as separate faces; alternate nested rings.
        rings = [Polygon(face.exterior) for face in faces.geoms]
        profile = GeometryCollection()
        for ring in sorted(rings, key=lambda p: p.area, reverse=True):
            profile = profile.symmetric_difference(ring)
        return profile, ""

    def render(self):
        for node in self.root[1:]:
            if not isinstance(node, list) or not node:
                continue
            kind = str(node[0])
            if kind in ("footprint", "module"):
                self.footprint(node)
            elif kind in ("segment", "arc"):
                self.stats["tracks"] += 1
                points = graphic_points(node)[0] if kind == "arc" else [point(node, "start"), point(node, "end")]
                self.route(value(node, "layer", "F.Cu"), points, width(node), self.metadata(node, primitive="arc" if kind == "arc" else "track"))
            elif kind == "via":
                self.stats["vias"] += 1
                position = point(node)
                hole = Point(position).buffer(scalar(node, "drill") / 2, quad_segs=16)
                outer, inner = scalar(node, "size") / 2, scalar(node, "drill") / 2
                if outer > 0 and inner < outer:
                    self.bounds.append((position[0]-outer, position[1]-outer, position[0]+outer, position[1]+outer))
                copper_layers = [layer for layer in self.layers if layer.endswith(".Cu")]
                endpoints = [str(item) for item in form(node, "layers")[1:]]
                if len(endpoints) == 2 and all(layer in copper_layers for layer in endpoints):
                    a, b = sorted(copper_layers.index(layer) for layer in endpoints)
                    for layer in copper_layers[a:b+1]:
                        if outer <= 0 or inner >= outer:
                            continue
                        attributes = {**self.metadata(node, primitive="via"),
                                      "data_snap_x": position[0], "data_snap_y": position[1]}
                        if inner > 0:
                            self.primitive(layer, "circle", attributes, cx=fmt(position[0]), cy=fmt(position[1]),
                                           r=fmt((outer+inner)/2), fill="none", stroke_width=fmt(outer-inner))
                        else:
                            self.primitive(layer, "circle", attributes, cx=fmt(position[0]), cy=fmt(position[1]),
                                           r=fmt(outer), stroke="none")
                if set(endpoints) == {"F.Cu", "B.Cu"}:
                    self.drills.append(hole)
                else:
                    self.warnings.add("Blind and buried via bores are omitted from the KiCad 3D preview.")
            elif kind == "zone":
                self.zone(node)
            elif kind.startswith("gr_") or kind in ("dimension", "target"):
                self.graphic(node)
        profile, note = self.board_profile()
        empty_planes = [self.names[layer] for layer in self.layers if layer.endswith(".Cu")
                        and self.layer_types[layer] == "power" and self.allowed(layer) and layer not in self.filled_layers]
        if empty_planes:
            self.warnings.add("No saved copper-zone fills on power-plane layers: " + ", ".join(empty_planes) +
                              ". Plane copper requires saved zone geometry.")
        if self.clip and not profile.is_empty:
            defs = element(self.scene, "defs")
            clip_path = element(defs, "clipPath", id="kc-board-clip")
            element(clip_path, "path", d=geometry_path(profile), clip_rule="evenodd")
            for layer, group in self.groups.items():
                if layer.endswith(".Cu"):
                    group.set("clip-path", "url(#kc-board-clip)")
            # The frontend treats direct scene groups as layers.
            self.scene.remove(defs)
        else:
            defs = None
        for hole in self.drills:
            self.shape("Drills", hole, {"data_primitive": "pad-hole", "data_hole_owner": "pad"}, fill="#1b1d21")
        # Deterministic layer order, independent of primitive order in the file.
        order = self.layers + [layer for layer in self.groups if layer not in self.layers]
        self.scene[:] = [self.groups[layer] for layer in order if layer in self.groups]
        if self.bounds:
            bounds = (min(b[0] for b in self.bounds), min(b[1] for b in self.bounds),
                      max(b[2] for b in self.bounds), max(b[3] for b in self.bounds))
        else:
            bounds = (0, 0, 100, 80)
        notes = [note] if note else []
        thickness = scalar(form(self.root, "general"), "thickness")
        if thickness <= 0:
            thickness = 1.6; notes.append("Board thickness is unavailable; showing 1.6 mm.")
        assets, bodies = {}, []
        shapes = []
        if not profile.is_empty:
            center = profile.centroid
            solid = profile.difference(union_all(self.drills)) if self.drills else profile
            def ring(coords):
                return [[round(x-center.x, 6), round(center.y-y, 6)] for x, y in list(coords)[:-1]]
            shapes = [{"outline": ring(p.exterior.coords), "holes": [ring(h.coords) for h in p.interiors]} for p in polygon_parts(solid)]
            try:
                assets, bodies, missing = kicad_models(self.root, self.directory, (center.x, center.y), thickness)
                if missing:
                    notes.append("For unavailable external STEP/VRML models, use Add STEP models to supply .step/.stp files.")
            except (OSError, TypeError, ValueError) as error:
                notes.append(f"Component STEP placements could not be read: {error}")
        if self.stats["components"] and not bodies:
            notes.append("Components have no usable STEP model references; showing the board only.")
        if self.side == "bottom":
            self.scene.set("transform", f"translate({fmt(bounds[0]+bounds[2])} 0) scale(-1 1)")
        svg = svg_document(self.scene, bounds, defs)
        stack = form(form(self.root, "setup"), "stackup")
        copper_order = [
            str(row[1]) for row in forms(stack, "layer")
            if len(row) > 1 and str(row[1]).endswith(".Cu")
        ]
        if not copper_order:
            copper_order = [layer for layer in self.layers if layer.endswith(".Cu")]
        tokens = {"F.Cu": "TOP", "B.Cu": "BOTTOM"}
        copper_order = [
            tokens.get(layer, "MID" + layer[2:-3] if layer.startswith("In") else layer)
            for layer in copper_order
        ]
        return {"svg": svg, "kind": "pcb", "warnings": sorted(self.warnings), "stats": self.stats,
                "copper_layer_order": copper_order,
                "polygon_elements": self.polygon_ids, "layout_components": self.layout_components,
                "scene3d": {"board": shapes, "thickness": thickness, "bodies": bodies, "step_models": assets, "notes": notes, "units": "mm",
                            **({"surface_origin": [center.x, center.y]} if not profile.is_empty else {})}}


def render_pcb(path, side="all", clip=False, pour_color=None, polygon_outlines=False):
    return {"name": path.name, **PcbRenderer(load(path, "kicad_pcb"), side, clip, pour_color, polygon_outlines, path.parent).render()}
