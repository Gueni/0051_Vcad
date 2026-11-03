"""Stable PCB layers and viewport-local selection masks, without SVG restyling."""

import math
import weakref
from collections import OrderedDict, defaultdict
from itertools import groupby

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen


class PcbRaster:
    def __init__(self, document):
        self.document = weakref.proxy(document)
        self.base_key = self.base = self.holes = None
        self.polygons = True
        self.element_geometry = {}
        self.mask_shapes = {}
        self.selection_cache = OrderedDict()
        self.components, self.nets = defaultdict(set), defaultdict(set)
        self.hole_indices = []
        for index, primitive in enumerate(document.primitives):
            if primitive.hole:
                self.hole_indices.append(index)
                continue
            for name in primitive.schematic_components or (primitive.component,):
                if name:
                    self.components[name].add(index)
            for name in primitive.schematic_nets or (primitive.net,):
                if name:
                    self.nets[name].add(index)
        self.entries = []

        def visit(node):
            name = node.tag.rsplit("}", 1)[-1]
            identifier = node.get("id")
            if name in ("defs", "clipPath", "mask", "pattern"):
                return
            if identifier in document.layer_ids:
                self.entries.append((identifier, True))
                return
            if name in (
                "path",
                "rect",
                "circle",
                "ellipse",
                "line",
                "polyline",
                "polygon",
                "text",
                "use",
            ):
                self.entries.append((identifier, False))
                return
            for child in node:
                visit(child)

        visit(document.root)

    def world_transform(self, painter, rect, width, height):
        painter.scale(width / rect.width(), height / rect.height())
        painter.translate(-rect.x(), -rect.y())

    def mask_shape(self, identifier):
        if identifier in self.mask_shapes:
            return self.mask_shapes[identifier]
        shape = None
        node = self.document.elements[identifier]
        kind = node.tag.rsplit("}", 1)[-1]
        if identifier.startswith(("pcb-track-", "pcb-via-", "pcb-pad-")) and kind in (
            "line",
            "circle",
            "ellipse",
            "rect",
        ):
            parent = node
            opaque = True
            while parent is not None:
                if (
                    any(
                        parent.get(name, "1") != "1"
                        for name in ("opacity", "fill-opacity", "stroke-opacity")
                    )
                    or any(
                        name in parent.get("style", "")
                        for name in ("opacity", "stroke-dasharray")
                    )
                    or parent.get("stroke-dasharray")
                ):
                    opaque = False
                    break
                parent = self.document.parents.get(parent)
            paint = self.document.paints[identifier]
            if (
                opaque
                and not node.get("transform")
                and not node.get("style")
                and all(
                    value == "none"
                    or QColor(value).isValid()
                    and QColor(value).alpha() == 255
                    for value in paint.values()
                )
            ):
                try:
                    width = float(self.document.stroke_widths[identifier])
                    pen = (
                        QPen(QColor("white"), width)
                        if paint["stroke"] != "none"
                        else QPen(Qt.PenStyle.NoPen)
                    )
                    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
                    pen.setCapStyle(
                        {
                            "round": Qt.PenCapStyle.RoundCap,
                            "square": Qt.PenCapStyle.SquareCap,
                        }.get(node.get("stroke-linecap"), Qt.PenCapStyle.FlatCap)
                    )
                    brush = (
                        Qt.GlobalColor.white
                        if paint["fill"] != "none"
                        else Qt.BrushStyle.NoBrush
                    )
                    number = lambda name, default="0": float(node.get(name, default))
                    if kind == "line":
                        geometry = (
                            QPointF(number("x1"), number("y1")),
                            QPointF(number("x2"), number("y2")),
                        )
                    elif kind in ("circle", "ellipse"):
                        rx, ry = (
                            (number("r"), number("r"))
                            if kind == "circle"
                            else (number("rx"), number("ry"))
                        )
                        geometry = (
                            QRectF(
                                number("cx") - rx, number("cy") - ry, rx * 2, ry * 2
                            ),
                        )
                    else:
                        geometry = (
                            QRectF(
                                number("x"),
                                number("y"),
                                number("width"),
                                number("height"),
                            ),
                            number("rx", node.get("ry", "0")),
                            number("ry", node.get("rx", "0")),
                        )
                    shape = (kind, pen, brush, geometry)
                except ValueError:
                    # Unit-bearing or uncommon geometry stays with Qt SVG.
                    pass
        self.mask_shapes[identifier] = shape
        return shape

    def draw_element(self, painter, identifier, mask=False):
        if identifier not in self.element_geometry:
            renderer = self.document.renderer
            self.element_geometry[identifier] = (
                renderer.transformForElement(identifier),
                renderer.boundsOnElement(identifier),
            )
        transform, bounds = self.element_geometry[identifier]
        painter.save()
        painter.setTransform(transform, True)
        shape = self.mask_shape(identifier) if mask else None
        if shape is None:
            self.document.renderer.render(painter, identifier, bounds)
        else:
            kind, pen, brush, geometry = shape
            painter.setPen(pen)
            painter.setBrush(brush)
            if kind == "line":
                painter.drawLine(*geometry)
            elif kind in ("circle", "ellipse"):
                painter.drawEllipse(*geometry)
            else:
                painter.drawRoundedRect(*geometry)
        painter.restore()

    def image(self, width, height, background=Qt.GlobalColor.transparent):
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(background)
        return image

    def render_base(self, rect, width, height, visible, polygons, order):
        document = self.document
        if polygons != self.polygons:
            # Polygon visibility changes infrequently. Selection, navigation,
            # layer visibility and layer order never rebuild the SVG renderer.
            document.apply_style(document.layer_ids, polygons, set(), set())
            self.polygons = polygons
            self.element_geometry.clear()
        self.base = self.image(width, height, QColor("#1b1d21"))
        painter = QPainter(self.base)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.world_transform(painter, rect, width, height)
        entries = self.entries
        if order:
            ordered = iter(document.ordered_layers(order))
            entries = [
                (next(ordered), True) if layer else (identifier, False)
                for identifier, layer in entries
            ]
        for identifier, layer in entries:
            if not layer or identifier in visible:
                self.draw_element(painter, identifier)
        painter.end()
        self.holes = None

    def render_holes(self, rect, width, height, visible, polygons):
        self.holes = self.image(width, height)
        painter = QPainter(self.holes)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.world_transform(painter, rect, width, height)
        document = self.document
        for layer in document.drill_layers.intersection(visible):
            self.draw_element(painter, layer)
        for index in self.hole_indices:
            primitive = document.primitives[index]
            if (
                primitive.layer in document.drill_layers
                or primitive.layer
                and primitive.layer not in visible
                or primitive.polygon
                and not polygons
                or not primitive.bounds.intersects(rect)
            ):
                continue
            self.draw_element(painter, primitive.id)
        painter.end()

    def draw_selection(self, painter, indices, color, rect, width, height):
        document = self.document
        bounds = QRectF()
        for index in indices:
            bounds = bounds.united(document.primitives[index].bounds)
        x_scale, y_scale = width / rect.width(), height / rect.height()
        left = math.floor((bounds.left() - rect.left()) * x_scale) - 2
        top = math.floor((bounds.top() - rect.top()) * y_scale) - 2
        right = math.ceil((bounds.right() - rect.left()) * x_scale) + 2
        bottom = math.ceil((bounds.bottom() - rect.top()) * y_scale) + 2
        crop = QRect(left, top, right - left, bottom - top).intersected(
            QRect(0, 0, width, height)
        )
        if crop.isEmpty():
            return
        mask = self.image(crop.width(), crop.height())
        mask_painter = QPainter(mask)
        mask_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        mask_painter.translate(-crop.x(), -crop.y())
        self.world_transform(mask_painter, rect, width, height)
        for index in indices:
            self.draw_element(mask_painter, document.primitives[index].id, mask=True)
        mask_painter.resetTransform()
        mask_painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceIn
        )
        mask_painter.fillRect(mask.rect(), QColor(color))
        mask_painter.end()
        painter.drawImage(crop.topLeft(), mask)

    def render(self, rect, width, height, style):
        visible, polygons, components, nets, order = style
        key = (
            rect.x(),
            rect.y(),
            rect.width(),
            rect.height(),
            width,
            height,
            visible,
            polygons,
            order,
        )
        if key != self.base_key:
            self.render_base(rect, width, height, visible, polygons, order)
            self.base_key = key
            self.selection_cache.clear()
        if not (components or nets):
            return self.base
        selection = components, nets
        if selection in self.selection_cache:
            self.selection_cache.move_to_end(selection)
            return self.selection_cache[selection]
        selected_components = set().union(
            *(self.components.get(name, ()) for name in components)
        )
        selected_nets = set().union(*(self.nets.get(name, ()) for name in nets))
        candidates = selected_components | selected_nets
        ranks = (
            {
                identifier: index
                for index, identifier in enumerate(self.document.ordered_layers(order))
            }
            if order
            else {}
        )
        indices = sorted(
            (
                index
                for index in candidates
                if (
                    not self.document.primitives[index].layer
                    or self.document.primitives[index].layer in visible
                )
                and (polygons or not self.document.primitives[index].polygon)
                and self.document.primitives[index].bounds.intersects(rect)
            ),
            key=lambda index: (
                ranks.get(self.document.primitives[index].layer, -1),
                index,
            ),
        )
        image = self.base.copy()
        painter = QPainter(image)
        fade = QColor("#1b1d21")
        fade.setAlphaF(0.85)
        painter.fillRect(image.rect(), fade)
        for component, group in groupby(
            indices, key=lambda index: index in selected_components
        ):
            self.draw_selection(
                painter,
                list(group),
                "#ffe066" if component else "#39e0f3",
                rect,
                width,
                height,
            )
        if self.holes is None:
            self.render_holes(rect, width, height, visible, polygons)
        painter.drawImage(QPointF(), self.holes)
        painter.end()
        self.selection_cache[selection] = image
        while (
            len(self.selection_cache) > 2
            or sum(value.sizeInBytes() for value in self.selection_cache.values())
            > 64 * 1024 * 1024
        ):
            self.selection_cache.popitem(last=False)
        return image
