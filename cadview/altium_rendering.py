"""Keep Altium's SVG output while avoiding repeated per-layer discovery work.

These small renderer hooks use the library's own layer resolution and emitters.
Pads and vias still go through its full multi-layer rendering path.
"""

from collections import defaultdict
from contextlib import contextmanager
from functools import lru_cache
from html import escape
from types import SimpleNamespace

from altium_monkey.altium_pcb_layer_ref import PcbLayerRef, PcbLayerResolutionError
from altium_monkey.altium_pcb_svg_renderer import PcbSvgRenderer
from altium_monkey.altium_record_pcb__text import AltiumPcbText
from altium_monkey.altium_record_types import PcbLayer


class _CompactStrokeText(AltiumPcbText):
    """Emit independent stroke segments in one path, sharing their style."""

    def _render_stroke_text_result(self, result, *, ctx, color, meta_attrs):
        from altium_monkey.altium_text_to_polygon import StrokeTextResult

        if not isinstance(result, StrokeTextResult) or not result.lines:
            return []
        parts = []
        for x1, y1, x2, y2 in result.lines:
            parts.append(
                f"M {ctx.fmt(ctx.x_to_svg(x1 / 0.0254))} {ctx.fmt(ctx.y_to_svg(y1 / 0.0254))} "
                f"L {ctx.fmt(ctx.x_to_svg(x2 / 0.0254))} {ctx.fmt(ctx.y_to_svg(y2 / 0.0254))}"
            )
        attrs = [
            f'd="{" ".join(parts)}"',
            f'stroke="{escape(color)}"',
            f'stroke-width="{ctx.fmt(max(float(result.stroke_width_mm), 0.001))}"',
            'stroke-linecap="round"',
            'fill="none"',
            *meta_attrs,
        ]
        return [f"<path {' '.join(attrs)}/>"]


class IndexedPcbSvgRenderer(PcbSvgRenderer):
    def __init__(self, pcb, options, *, compact_metadata=False, distinct_layer_colors=False):
        super().__init__(options)
        self._pcb = pcb
        self._stack_cached = False
        self.resolved_stack = None
        self._compact_metadata = compact_metadata
        self._distinct_layer_colors = distinct_layer_colors
        self._signatures = {}
        self._collections = {}
        self._layer_refs = {}
        self._visible_primitive_refs = set()
        self._drills = {}
        for name in (
            "tracks",
            "arcs",
            "fills",
            "regions",
            "shapebased_regions",
            "texts",
            "pads",
        ):
            collection = getattr(pcb, name)
            layers = defaultdict(list)
            resolved = True
            for primitive in collection:
                ref = self._primitive_layer_ref(primitive)
                if ref is not None and not self._should_skip_primitive_for_svg(
                    primitive
                ):
                    self._visible_primitive_refs.add(ref)
                if ref is None:
                    # Preserve upstream handling of unresolved legacy layers.
                    resolved = False
                else:
                    layers[ref].append(primitive)
            if resolved and name != "pads":
                self._collections[id(collection)] = (collection, layers)

    def render_board(self, pcbdoc, *args, **kwargs):
        # Resolution is immutable within a render, but callers may edit the
        # document before using the renderer again.
        self._stack_cached = False
        self.resolved_stack = None
        return super().render_board(pcbdoc, *args, **kwargs)

    def _collect_visible_layers(self, pcbdoc, *, force_all=False):
        layers = super()._collect_visible_layers(pcbdoc, force_all=force_all)
        if self._distinct_layer_colors:
            from .layer_colors import layer_colors

            colors = layer_colors(layer.ref.token for layer in layers)
            self.options.layer_colors.update({layer.ref: colors[layer.ref.token] for layer in layers})
        return layers

    def _render_drill_layer_group(self, ctx, drill_primitives, *, clip_path_id=None):
        lines = super()._render_drill_layer_group(ctx, drill_primitives, clip_path_id=clip_path_id)
        if lines and self._distinct_layer_colors and self.options.include_metadata:
            from .layer_colors import BASE_COLORS

            # Knockout holes keep their background paint; only their inspector
            # swatch needs a reserved color instead of the generic gray fallback.
            lines[0] = lines[0].replace("<g ", f'<g data-color="{BASE_COLORS["DRILLS"]}" ', 1)
        return lines

    def _resolved_layer_stack_safe(self, pcbdoc):
        if pcbdoc is not self._pcb:
            return super()._resolved_layer_stack_safe(pcbdoc)
        if not self._stack_cached:
            self.resolved_stack = super()._resolved_layer_stack_safe(pcbdoc)
            self._stack_cached = True
        return self.resolved_stack

    def _primitive_layer_ref(self, primitive):
        identity = id(primitive)
        cached = self._layer_refs.get(identity)
        if cached is None or cached[0] is not primitive:
            ref = super()._primitive_layer_ref(primitive)
            self._layer_refs[identity] = (primitive, ref)
            return ref
        return cached[1]

    def _collect_visible_primitive_layers(self, pcbdoc, visible, stackup_copper_layers):
        if pcbdoc is not self._pcb:
            return super()._collect_visible_primitive_layers(
                pcbdoc, visible, stackup_copper_layers
            )
        for ref in self._visible_primitive_refs:
            self._add_visible_layer(visible, ref, stackup_copper_layers)

    def _collect_visible_via_layers(self, pcbdoc, visible, stackup_copper_layers):
        # Most boards have thousands of identical through-via spans. Discover
        # their layers once per distinct span, rather than once per via.
        spans = set()
        for via in pcbdoc.vias:
            if self._should_skip_primitive_for_svg(via):
                continue
            span = (
                self._safe_int(via.layer_start, default=1),
                self._safe_int(via.layer_end, default=32),
            )
            if span in spans:
                continue
            spans.add(span)
            for layer in self._iter_via_span_layers(via):
                self._add_visible_layer(
                    visible, PcbLayerRef.from_legacy(layer), stackup_copper_layers
                )

    def _collect_visible_derived_pad_layers(self, pcbdoc, visible):
        self._collect_derived_layers(
            pcbdoc.pads,
            visible,
            (
                PcbLayer.TOP,
                PcbLayer.BOTTOM,
                PcbLayer.TOP_SOLDER,
                PcbLayer.BOTTOM_SOLDER,
                PcbLayer.TOP_PASTE,
                PcbLayer.BOTTOM_PASTE,
            ),
        )

    def _collect_visible_derived_via_layers(self, pcbdoc, visible):
        self._collect_derived_layers(
            pcbdoc.vias, visible, (PcbLayer.TOP_SOLDER, PcbLayer.BOTTOM_SOLDER)
        )

    def _collect_derived_layers(self, primitives, visible, candidates):
        pending = {
            layer: PcbLayerRef.from_legacy(layer)
            for layer in candidates
            if PcbLayerRef.from_legacy(layer) not in visible
        }
        for primitive in primitives:
            if not pending:
                break
            if self._should_skip_primitive_for_svg(primitive):
                continue
            for layer, ref in tuple(pending.items()):
                if primitive._should_render_on_layer(layer):
                    self._add_visible_layer(visible, ref, None)
                    del pending[layer]

    def _build_context(self, pcbdoc, project_parameters=None):
        ctx = super()._build_context(pcbdoc, project_parameters)
        self.context = ctx
        # The context is immutable during a render. Return fresh lists so an
        # upstream emitter can safely append its own per-element attributes.
        for name in (
            "layer_metadata_attrs",
            "layer_metadata_attrs_for_ref",
            "relationship_metadata_attrs",
        ):
            method = getattr(ctx, name)
            cached = lru_cache(maxsize=None)(
                lambda *args, _method=method, **kwargs: tuple(_method(*args, **kwargs))
            )
            setattr(
                ctx,
                name,
                lambda *args, _cached=cached, **kwargs: list(_cached(*args, **kwargs)),
            )
        return ctx

    def _render_drill_holes_for_layer(self, ctx, pcbdoc, layer):
        if self._deduplicate_drill_overlay():
            return []
        # Upstream asks for these once for the drill overlay and again while
        # composing each native layer, even when the overlay owns all holes.
        key = (id(ctx), id(pcbdoc), layer)
        if key not in self._drills:
            self._drills[key] = (
                ctx,
                super()._render_drill_holes_for_layer(ctx, pcbdoc, layer),
            )
        return self._drills[key][1].copy()

    def _deduplicate_drill_overlay(self):
        return (
            self._compact_metadata
            and self.options.drill_holes_as_layer_group
            and (self.options.drill_hole_mode or "knockout").strip().lower()
            == "knockout"
        )

    def _collect_drill_group_primitives(
        self, ctx, pcbdoc, layers, render_cache, *, drill_source_layers
    ):
        if not self._deduplicate_drill_overlay():
            return super()._collect_drill_group_primitives(
                ctx,
                pcbdoc,
                layers,
                render_cache,
                drill_source_layers=drill_source_layers,
            )
        if (self.options.drill_hole_mode or "").strip().lower() == "none":
            return []
        from altium_monkey.altium_pcb_drill_rendering import (
            should_render_via_drill_hole,
        )

        source_layers = drill_source_layers
        if source_layers is None:
            source_layers = [
                layer.legacy_layer
                for layer in layers
                if layer.legacy_layer is not None and layer.is_legacy_copper()
            ]
        pads = [
            pad
            for pad in pcbdoc.pads
            if not self._should_skip_primitive_for_svg(pad) and pad.hole_size > 0
        ]
        vias = [
            via
            for via in pcbdoc.vias
            if not self._should_skip_primitive_for_svg(via)
            and should_render_via_drill_hole(via)
            and via.hole_size_mils > 0
        ]
        elements = []
        for layer in source_layers:
            if not layer.is_copper():
                continue
            # Physical pad holes and via barrels are independent of the copper
            # layer. The shared drill overlay needs each owner only once, on its
            # first contributing layer; blind vias and missing pad copper still
            # use the upstream layer eligibility and hole emitter.
            layer_pads = []
            for pad in pads:
                if pad._should_render_on_layer(layer):
                    width, height = pad._layer_size(layer)
                    if width > 0 and height > 0:
                        layer_pads.append(pad)
            layer_vias = [via for via in vias if via._spans_layer(layer)]
            selected_pads = {id(pad) for pad in layer_pads}
            selected_vias = {id(via) for via in layer_vias}
            pads = [pad for pad in pads if id(pad) not in selected_pads]
            vias = [via for via in vias if id(via) not in selected_vias]
            elements.extend(
                super()._render_drill_holes_for_layer(
                    ctx,
                    SimpleNamespace(pads=layer_pads, vias=layer_vias),
                    layer,
                )
            )
        return elements

    @contextmanager
    def _inherited_layer_metadata(self, ctx):
        if not self._compact_metadata:
            yield
            return
        legacy, ref = ctx.layer_metadata_attrs, ctx.layer_metadata_attrs_for_ref
        ctx.layer_metadata_attrs = lambda layer: []
        ctx.layer_metadata_attrs_for_ref = lambda layer: []
        try:
            yield
        finally:
            ctx.layer_metadata_attrs, ctx.layer_metadata_attrs_for_ref = legacy, ref

    def _to_svg_accepts_for_layer(self, to_svg):
        # Cache the function, not its bound method (which retains a primitive).
        function = getattr(to_svg, "__func__", to_svg)
        if function not in self._signatures:
            self._signatures[function] = super()._to_svg_accepts_for_layer(to_svg)
        return self._signatures[function]

    def _on_layer(self, collection, layer):
        indexed = self._collections.get(id(collection))
        if indexed is None or indexed[0] is not collection:
            return collection
        try:
            ref = layer.ref if hasattr(layer, "ref") else PcbLayerRef.from_legacy(layer)
        except PcbLayerResolutionError:
            return collection
        return indexed[1].get(ref, ())

    def _render_primitive_collection(self, ctx, collection, layer, color, **kwargs):
        with self._inherited_layer_metadata(ctx):
            return super()._render_primitive_collection(
                ctx, self._on_layer(collection, layer), layer, color, **kwargs
            )

    def _render_region_collection(self, ctx, collection, layer, color):
        with self._inherited_layer_metadata(ctx):
            return super()._render_region_collection(
                ctx, self._on_layer(collection, layer), layer, color
            )

    def _render_v7_ref_primitive_collection(self, ctx, collection, layer, color):
        with self._inherited_layer_metadata(ctx):
            return super()._render_v7_ref_primitive_collection(
                ctx, self._on_layer(collection, layer), layer, color
            )

    def _render_texts_for_layer(self, ctx, pcbdoc, layer, layer_color):
        tt, stroke, barcode = self._text_renderers()
        font_resolver = self._embedded_font_resolver(pcbdoc)
        elements = []
        with self._inherited_layer_metadata(ctx):
            for text in self._on_layer(pcbdoc.texts, layer):
                if self._should_skip_primitive_for_svg(
                    text
                ) or not self._should_render_component_linked_text(pcbdoc, text):
                    continue
                if self._compact_metadata:
                    compact_text = _CompactStrokeText.__new__(_CompactStrokeText)
                    compact_text.__dict__ = text.__dict__.copy()
                else:
                    compact_text = text
                rendered = compact_text.to_svg(
                    ctx,
                    stroke=layer_color,
                    include_metadata=self.options.include_metadata
                    and not self._compact_metadata,
                    for_layer=layer,
                    text_as_polygons=self.options.text_as_polygons,
                    truetype_renderer=tt,
                    stroke_renderer=stroke,
                    barcode_renderer=barcode,
                    font_resolver=font_resolver,
                )
                if (
                    rendered
                    and self._compact_metadata
                    and self.options.include_metadata
                ):
                    attrs = text._svg_metadata_attrs(ctx, include_metadata=True)
                    elements.append(
                        f"<g {' '.join(attrs)}>\n{'\n'.join(rendered)}\n</g>"
                    )
                else:
                    elements.extend(rendered)
        return elements
