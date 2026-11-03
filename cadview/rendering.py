"""Render supported EDA documents and build selection metadata for the viewer."""

import re
import zlib
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import freetype
from altium_monkey import (
    AltiumDesign,
    AltiumPcbDoc,
    AltiumSchDoc,
    PcbSvgRenderOptions,
    SchSvgRenderOptions,
)
from altium_monkey.altium_pcb_surface import PCB_SurfaceSide
from altium_monkey.altium_resolved_layer_stack import resolved_layer_stack_from_pcbdoc
from altium_monkey.altium_ttf_metrics import get_font_path_with_style

from .altium_rendering import IndexedPcbSvgRenderer
from .scene3d import build_scene


def prepare_embedded_fonts(pcb: AltiumPcbDoc) -> list[str]:
    """Replace fonts FreeType cannot read in the parsed copy of the board."""
    fonts, warnings = [], []
    for font in pcb.embedded_fonts:
        try:
            freetype.Face(BytesIO(font.decompress()))
        except freetype.FT_Exception:
            style = f"{font.name} {font.style}".lower()
            font_path = get_font_path_with_style(
                font.name, bold="bold" in style,
                italic="italic" in style or "oblique" in style,
            )
            if not font_path:
                raise RuntimeError(f'Embedded font "{font.name}" is unreadable and no replacement font is available.')
            try:
                replacement = Path(font_path).read_bytes()
                freetype.Face(BytesIO(replacement))
            except (OSError, freetype.FT_Exception) as exc:
                raise RuntimeError(f'Could not load a replacement for unreadable embedded font "{font.name}".') from exc
            fonts.append(replace(font, compressed_data=zlib.compress(replacement), raw_data=b""))
            warnings.append(
                f'Embedded font "{font.name}" could not be read; using "{Path(font_path).stem}". '
                'Text shape and spacing may differ.'
            )
        else:
            fonts.append(font)
    pcb.embedded_fonts = fonts
    return warnings


def resolve_polygon_nets(pcb: AltiumPcbDoc) -> None:
    """Give poured copper its parent polygon's net in the parsed rendering copy."""
    def valid_index(index: int | None, size: int) -> bool:
        return isinstance(index, int) and 0 <= index < size and index not in (0xFFFF, 0xFFFFFFFF)

    for collection in ("regions", "shapebased_regions", "tracks", "arcs", "fills"):
        for primitive in getattr(pcb, collection, []):
            if valid_index(primitive.net_index, len(pcb.nets)):
                continue
            polygon_index = getattr(primitive, "polygon_index", None)
            if not valid_index(polygon_index, len(pcb.polygons)):
                continue
            net_index = pcb.polygons[polygon_index].net
            if valid_index(net_index, len(pcb.nets)):
                primitive.net_index = net_index


def schematic_selection(schematic: AltiumSchDoc, svg: str) -> dict:
    """Link selectable source components and compiled nets to their SVG groups."""
    # Imported graphics often have no saved UniqueID. The renderer's local
    # source-to-group map supplies the actual IDs without changing the document.
    rendered_ids = set(re.findall(r'\bid\s*=\s*[\'"]([^\'"]+)[\'"]', svg))
    generated_ids = getattr(schematic, "_geometry_render_group_ids", {})
    def body_ids(component):
        ids = (graphic.unique_id or generated_ids.get(id(graphic)) for graphic in component.record.graphics)
        # Multi-part symbols include graphics for parts/styles not drawn here.
        return list(dict.fromkeys(identifier for identifier in ids if identifier in rendered_ids))
    components = [
        {"id": component.record.unique_id, "name": component.designator or component.record.unique_id,
         "elements": [component.record.unique_id],
         "body_elements": body_ids(component)}
        for component in schematic.get_components()
    ]
    nets = []
    # Logical nets retain wire-only and single-pin networks that manufacturing
    # netlist output may omit. Each tab contains one source sheet.
    for net in AltiumDesign(schdocs=[schematic]).compile().nets:
        if net.scope != "logical_local":
            continue
        elements = {item.element_id for item in net.items if not item.removed}
        nets.append({"id": f"net-{len(nets)}", "name": net.name, "elements": sorted(elements - {""}),
                     "aliases": list(net.aliases),
                     "terminals": [{"component": terminal.designator, "pin": terminal.pin} for terminal in net.terminals]})
    return {"components": components, "nets": nets}


def polygon_element_ids(pcb: AltiumPcbDoc, svg: str) -> list[str]:
    """Identify polygon pours without confusing unlinked regions with custom pads.

    The renderer's deterministic IDs encode primitive kind and collection index;
    one source primitive can appear on several layers in a surface rendering.
    Read IDs without reserializing SVG (including its embedded fonts and styles).
    """
    linked = set()
    for collection, kind in (("regions", "region"), ("shapebased_regions", "shapebased-region"),
                             ("tracks", "track"), ("arcs", "arc"), ("fills", "fill")):
        for index, primitive in enumerate(getattr(pcb, collection, [])):
            polygon_index = getattr(primitive, "polygon_index", None)
            if (isinstance(polygon_index, int) and 0 <= polygon_index < len(pcb.polygons)
                    and polygon_index not in (0xFFFF, 0xFFFFFFFF)):
                linked.add((kind, index))
    # A literal prefix lets the regex engine skip geometry path data and text
    # quickly, instead of testing a word boundary at every byte of a large SVG.
    pattern = r'id="(pcb-(shapebased-region|region|track|arc|fill)-(\d+)(?:-[^"]*)?)"'
    return [match[1] for match in re.finditer(pattern, svg)
            if (not match.start() or not (svg[match.start() - 1].isalnum() or svg[match.start() - 1] == "_"))
            and (match[2], int(match[3])) in linked]


def copper_layer_order(pcb: AltiumPcbDoc, *, layer_stack=None) -> list[str]:
    """Read physical copper order for the layer list without changing paint."""
    try:
        stack = layer_stack if layer_stack is not None else resolved_layer_stack_from_pcbdoc(pcb)
    except Exception:  # noqa: BLE001 - optional stack metadata must not prevent 2D viewing
        return []
    return [
        layer.layer_ref.token
        for layer in stack.layers
        if layer.stack_index is not None
        and layer.layer_ref is not None
        and layer.layer_ref.family.value in ("signal", "internal_plane")
    ]


def render_file(path: Path, side: str = "all", clip: bool = False,
                pour_color: str | None = None, polygon_outlines: bool = False,
                *, native: bool = False) -> dict:
    if path.suffix.lower() == ".kicad_pcb":
        from .kicad_pcb import render_pcb
        return render_pcb(path, side, clip, pour_color, polygon_outlines)
    if path.suffix.lower() == ".kicad_sch":
        from .kicad_schematic import render_schematic
        return render_schematic(path)
    if path.suffix.lower() == ".sch":
        from .kicad_legacy import render_legacy_schematic
        return render_legacy_schematic(path)
    if path.suffix.lower() == ".schdoc":
        schematic = AltiumSchDoc(path)
        svg = schematic.to_svg(options=SchSvgRenderOptions(embed_bundled_fallback_fonts=True))
        return {
            "svg": svg,
            "name": path.name,
            "kind": "schematic",
            "selection": schematic_selection(schematic, svg),
            "warnings": [],
            "stats": {
                "components": len(schematic.components),
                "wires": len(schematic.wires),
                "net_labels": len(schematic.net_labels),
            },
        }
    if path.suffix.lower() != ".pcbdoc":
        raise ValueError("Open an Altium .PcbDoc or .SchDoc, or a KiCad .kicad_pcb, .kicad_sch, or legacy .sch file.")
    pcb = AltiumPcbDoc.from_file(path)
    warnings = prepare_embedded_fonts(pcb)
    resolve_polygon_nets(pcb)
    options = PcbSvgRenderOptions(
        show_board_outline=True,
        clip_copper_to_board_outline=clip,
        polygon_overlay_color=pour_color or None,
        include_polygon_definition_overlays=polygon_outlines,
        svg_display_scale=4.0,
    )
    renderer = None
    if side == "top":
        svg = pcb.to_surface_svg(PCB_SurfaceSide.TOP, options=options)
    elif side == "bottom":
        svg = pcb.to_surface_svg(PCB_SurfaceSide.BOTTOM, options=options)
    else:
        renderer = IndexedPcbSvgRenderer(pcb, options, compact_metadata=native, distinct_layer_colors=True)
        svg = renderer.render_board(pcb)
    # An unsupported 3D record must not prevent opening the existing 2D view.
    try:
        scene = build_scene(pcb, layer_stack=getattr(renderer, "resolved_stack", None))
        if renderer is not None and scene.get("board"):
            ctx = renderer.context
            x, y = scene["origin"]
            scene["surface_origin"] = [ctx.x_to_svg(x / 0.0254), ctx.y_to_svg(y / 0.0254)]
    except Exception as exc:  # noqa: BLE001 - preserve 2D when optional 3D geometry is unsupported
        scene = {"board": [], "notes": [f"3D is unavailable for this document: {type(exc).__name__}: {exc}"]}
    return {
        "svg": svg,
        "scene3d": scene,
        "name": path.name,
        "kind": "pcb",
        "copper_layer_order": copper_layer_order(pcb, layer_stack=getattr(renderer, "resolved_stack", None)),
        "polygon_elements": polygon_element_ids(pcb, svg),
        "layout_components": [
            {"name": component.designator, "source_name": component.source_designator,
             "source_id": component.source_unique_id_segments[-1] if component.source_unique_id_segments else ""}
            for component in pcb.components
        ],
        "warnings": warnings,
        "stats": {
            "components": len(pcb.components),
            "pads": len(pcb.pads),
            "tracks": len(pcb.tracks),
            "vias": len(pcb.vias),
            "polygons": len(pcb.polygons),
            "regions": len(pcb.regions),
        },
    }
