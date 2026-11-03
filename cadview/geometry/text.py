"""Outline text at a large font size so sub-millimetre SVG fonts stay precise.

Qt SVG rounds small pixel font sizes. Constructing native glyph outlines at
1000px and scaling the paths preserves tiny PCB text at arbitrary zoom levels.
"""

import re
import xml.etree.ElementTree as ET
from functools import lru_cache

from PySide6.QtCore import QPointF
from PySide6.QtGui import QFont, QFontDatabase, QFontMetricsF, QPainterPath, QTransform


def properties(node, inherited):
    result = dict(inherited)
    result.update(node.attrib)
    for name, val in re.findall(r"([\w-]+)\s*:\s*([^;]+)", node.get("style", "")):
        result[name] = val.strip()
    return result


def number(value, default=0):
    match = re.match(r"[-+\d.eE]+", str(value))
    return float(match[0]) if match else default


@lru_cache(maxsize=4096)
def glyphs(family, bold, italic, text):
    family = family.split(",")[0].strip(" '\"")
    if family.casefold() in ("sans-serif", "sans serif"):
        family = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
    font = QFont(family)
    font.setPixelSize(1000)
    font.setBold(bold)
    font.setItalic(italic)
    path = QPainterPath()
    path.addText(QPointF(), font, text)
    metrics = QFontMetricsF(font)
    return path, metrics.horizontalAdvance(text), metrics.ascent(), metrics.descent()


def svg_path(path):
    result, index = [], 0
    while index < path.elementCount():
        el = path.elementAt(index)
        if el.isMoveTo():
            result.append(f"M{el.x:.7g} {el.y:.7g}")
        elif el.isLineTo():
            result.append(f"L{el.x:.7g} {el.y:.7g}")
        else:
            b, c = path.elementAt(index + 1), path.elementAt(index + 2)
            result.append(
                f"C{el.x:.7g} {el.y:.7g} {b.x:.7g} {b.y:.7g} {c.x:.7g} {c.y:.7g}"
            )
            index += 2
        index += 1
    return " ".join(result)


def outline_text(root):
    namespace = root.tag.rsplit("}", 1)[0] + "}" if "}" in root.tag else ""
    # Altium PCB text is already emitted as paths. Avoid copying inherited
    # attributes for every copper primitive when there is no text to outline.
    if next(root.iter(namespace + "text"), None) is None:
        return

    def visit(node, inherited):
        props = properties(node, inherited)
        if node.tag.rsplit("}", 1)[-1] != "text":
            for child in node:
                visit(child, props)
            return
        x, y = number(props.get("x", 0)), number(props.get("y", 0))
        paths = []
        for chunk, row, source in [(node.text, props, node)] + [
            (child.text, properties(child, props), child) for child in node
        ]:
            if not chunk:
                continue
            x = number(source.get("x", x))
            y = number(source.get("y", y))
            y += number(source.get("dy", 0))
            x += number(source.get("dx", 0))
            size = number(row.get("font-size", 12), 12)
            path, advance, ascent, descent = glyphs(
                row.get("font-family", "sans-serif"),
                row.get("font-weight") in ("bold", "700", "800", "900"),
                row.get("font-style") in ("italic", "oblique"),
                chunk,
            )
            scale = size / 1000
            anchor = row.get("text-anchor", "start")
            left = x - advance * scale * ({"middle": 0.5, "end": 1}.get(anchor, 0))
            baseline = row.get("dominant-baseline", "alphabetic")
            offset = (
                (ascent - descent) / 2
                if baseline in ("central", "middle")
                else ascent
                if baseline in ("hanging", "text-before-edge")
                else -descent
                if baseline == "text-after-edge"
                else 0
            )
            transform = QTransform()
            transform.translate(left, y + offset * scale)
            transform.scale(scale, scale)
            attrs = {
                "d": svg_path(transform.map(path)),
                "stroke": "none",
                "fill": row.get("fill", "black"),
                "data-native-text": "true",
            }
            paths.append(attrs)
            x += advance * scale
        namespace = node.tag.rsplit("}", 1)[0] + "}" if "}" in node.tag else ""
        node.tag = namespace + "g"
        node.text = None
        node[:] = []
        for attrs in paths:
            ET.SubElement(node, namespace + "path", attrs)

    visit(root, {})
