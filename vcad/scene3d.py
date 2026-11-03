"""Build millimetre-based board solids, body fallbacks and STEP placements."""

import hashlib
import math

from altium_monkey.altium_board import BoardOutlineVertex, resolve_outline_arc_segment
from altium_monkey.altium_pcb_drill_rendering import should_render_via_drill_hole
from altium_monkey.altium_resolved_layer_stack import resolved_layer_stack_from_pcbdoc
from shapely import make_valid, union_all
from shapely.affinity import rotate, translate
from shapely.geometry import LineString, Point, Polygon, box

from .step_models import altium_assets, altium_placement

MIL_MM = 0.0254
IU_MM = MIL_MM / 10000


def contour(vertices):
    """Sample native arc segments to at most 5 degrees per chord."""
    points = []
    for i, vertex in enumerate(vertices):
        start = BoardOutlineVertex(
            vertex.x_mils, vertex.y_mils,
            is_arc=getattr(vertex, "is_arc", getattr(vertex, "is_round", False)),
            center_x_mils=getattr(vertex, "center_x_mils", 0),
            center_y_mils=getattr(vertex, "center_y_mils", 0),
            radius_mils=getattr(vertex, "radius_mils", 0),
            start_angle_deg=getattr(vertex, "start_angle_deg", getattr(vertex, "start_angle", None)),
            end_angle_deg=getattr(vertex, "end_angle_deg", getattr(vertex, "end_angle", None)),
        )
        points.append((start.x_mils * MIL_MM, start.y_mils * MIL_MM))
        if start.is_arc:
            end = vertices[(i + 1) % len(vertices)]
            clockwise, sweep = resolve_outline_arc_segment(start, BoardOutlineVertex(end.x_mils, end.y_mils))
            angle = math.atan2(start.y_mils - start.center_y_mils, start.x_mils - start.center_x_mils)
            radius = start.radius_mils or math.hypot(start.x_mils - start.center_x_mils,
                                                    start.y_mils - start.center_y_mils)
            steps = max(1, math.ceil(sweep / 5))
            for step in range(1, steps):
                a = angle + (-1 if clockwise else 1) * math.radians(sweep) * step / steps
                points.append(((start.center_x_mils + radius * math.cos(a)) * MIL_MM,
                               (start.center_y_mils + radius * math.sin(a)) * MIL_MM))
    return points


def polygons(geometry):
    if geometry.geom_type == "Polygon":
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from polygons(part)


def solid_shapes(geometry, origin):
    def ring(coords):
        return [[round(x - origin[0], 6), round(y - origin[1], 6)] for x, y in list(coords)[:-1]]
    return [{"outline": ring(p.exterior.coords), "holes": [ring(h.coords) for h in p.interiors]}
            for p in polygons(geometry) if not p.is_empty and p.area > 1e-10]


def build_scene(pcb, *, layer_stack=None):
    outline = pcb.board.outline if pcb.board else None
    if not outline or len(outline.vertices) < 3:
        return {"board": [], "notes": ["3D is unavailable: this document has no board outline."]}
    profile = make_valid(Polygon(contour(outline.vertices)))
    x0, y0, x1, y1 = profile.bounds
    origin = ((x0 + x1) / 2, (y0 + y1) / 2)
    notes = []
    substacks = []
    thickness = 0
    try:
        stack = layer_stack if layer_stack is not None else resolved_layer_stack_from_pcbdoc(pcb)
        substacks = [s for s in stack.substacks if not s.service_stackup]
        if substacks:
            thickness = stack.stack_envelope_for_substack(substacks[0].source_stackup_ref).total_thickness_mils * MIL_MM
    except ValueError:
        # Incomplete legacy stack metadata should still allow a preview.
        pass
    if thickness <= 0 or not math.isfinite(thickness):
        thickness = 1.6
        notes.append("Board thickness is unavailable; showing 1.6 mm.")
    if len(substacks) > 1:
        notes.append("Showing a flat board using the first layer stack; regional thickness and folding are not represented.")

    voids = [make_valid(Polygon(points)) for vertices in outline.cutouts
             if len(points := contour(vertices)) >= 3]
    for pad in pcb.pads:
        diameter = pad.hole_size_mils * MIL_MM
        if diameter <= 0:
            continue
        x, y = pad.hole_center_mils()
        if int(pad.hole_shape) == 1:
            hole = box(-diameter / 2, -diameter / 2, diameter / 2, diameter / 2)
        elif int(pad.hole_shape) == 2 and pad.slot_size * IU_MM > diameter:
            half = (pad.slot_size * IU_MM - diameter) / 2
            hole = LineString([(-half, 0), (half, 0)]).buffer(diameter / 2, quad_segs=12)
        else:
            hole = Point(0, 0).buffer(diameter / 2, quad_segs=12)
        hole = rotate(hole, pad.rotation + (pad.slot_rotation if int(pad.hole_shape) == 2 else 0))
        voids.append(translate(hole, x * MIL_MM, y * MIL_MM))
    omitted_vias = 0
    for via in pcb.vias:
        if not should_render_via_drill_hole(via):
            continue
        if {int(via.layer_start), int(via.layer_end)} != {1, 32}:
            omitted_vias += 1
            continue
        voids.append(Point(via.x_mils * MIL_MM, via.y_mils * MIL_MM).buffer(
            via.hole_size_mils * MIL_MM / 2, quad_segs=8))
    if omitted_vias:
        notes.append("Blind and buried via bores are omitted in this preview.")
    if voids:
        profile = profile.difference(union_all(voids))

    bodies = []
    assets, model_references = altium_assets(pcb)
    covered_components = set()
    # Both streams commonly contain the same body. Prefer the standard stream,
    # and use shape-based records only for owners without standard bodies.
    standard = pcb.component_bodies
    owners = {b.component_index for b in standard}
    records = list(standard) + [b for b in pcb.shapebased_component_bodies if b.component_index not in owners]
    approximated = 0
    for body_number, body in enumerate(records):
        points = contour(body.outline)
        low, high = body.standoff_height * IU_MM, body.overall_height * IU_MM
        index = body.component_index
        component = pcb.components[index] if isinstance(index, int) and 0 <= index < len(pcb.components) else None
        projection = int(body.body_projection)
        if projection in (2, 3):
            bottom = component is not None and component.get_layer_normalized() == "bottom"
        else:
            bottom = projection == 1
        z = -thickness / 2 - high if bottom else thickness / 2 + low
        color = body.body_color_3d
        rgb = ((color & 255) << 16) | (color & 0x00FF00) | ((color >> 16) & 255)
        model = model_references.get(str(body.model_id).upper()) if body.model_type != 0 else None
        if not model and body.model_type != 0 and str(body.model_name).lower().endswith((".step", ".stp")):
            model = "external-" + hashlib.sha256(body.model_name.encode()).hexdigest()
            assets.setdefault(model, {"name": body.model_name})
        placement = {"model": model, "matrix": altium_placement(body, origin, thickness, bottom)} if model else {}
        shapes = solid_shapes(make_valid(Polygon(points, [contour(h) for h in body.holes])), origin) if len(points) >= 3 and high > low else []
        if not shapes and not model:
            continue
        for shape in shapes or [{}]:
            bodies.append({**shape, **placement, "id": f"body-{body_number}", "z": z, "height": high - low, "color": rgb,
                           "opacity": max(0, min(1, body.body_opacity_3d)),
                           "name": component.designator if component else body.name.strip() or "Body"})
        if component:
            covered_components.add(index)
        if body.model_type != 0 and not model:
            approximated += 1
    if approximated:
        notes.append(f"{approximated} body model(s) are missing or unsupported; showing extruded outlines.")
    missing = len(pcb.components) - len(covered_components)
    if missing:
        notes.append(f"{missing} component(s) have no usable body geometry and are omitted.")
    used = {body["model"] for body in bodies if body.get("model")}
    return {"board": solid_shapes(profile, origin), "thickness": thickness, "bodies": bodies,
            "origin": list(origin),
            "step_models": {key: assets[key] for key in used},
            "notes": notes, "units": "mm"}
