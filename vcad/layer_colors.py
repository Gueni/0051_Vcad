"""Deterministic, distinct PCB layer colors shared by both file formats."""

import colorsys
import hashlib
import re


LAYER_ALIASES = {
    "F.Cu": "TOP", "B.Cu": "BOTTOM",
    "F.SilkS": "TOPOVERLAY", "B.SilkS": "BOTTOMOVERLAY",
    "F.Mask": "TOPSOLDER", "B.Mask": "BOTTOMSOLDER",
    "F.Paste": "TOPPASTE", "B.Paste": "BOTTOMPASTE",
    "Edge.Cuts": "BOARD_OUTLINE", "Drills": "DRILLS",
}
BASE_COLORS = {
    "TOP": "#e64c4c", "BOTTOM": "#5088ee",
    "TOPOVERLAY": "#f5e8a8", "BOTTOMOVERLAY": "#d0b9ed",
    "TOPSOLDER": "#37986e", "BOTTOMSOLDER": "#29734f",
    "TOPPASTE": "#a9a9a9", "BOTTOMPASTE": "#888888",
    "BOARD_OUTLINE": "#c0a000", "DRILLS": "#b7b7b7",
}


def layer_token(layer):
    if match := re.fullmatch(r"In(\d+)\.Cu", layer):
        return "MID" + match[1]
    return LAYER_ALIASES.get(layer, layer)


def layer_colors(layers, *, existing=None):
    """Keep familiar surface colors; spread other layers around the hue wheel.

    Use source identities, not display names or paint order. Reserve base colors
    and resolve RGB rounding/hash collisions so no two layers share a swatch.
    """
    tokens = {layer: layer_token(layer) for layer in layers}
    palette = dict(BASE_COLORS)
    palette.update({layer_token(layer): color for layer, color in (existing or {}).items()})
    used = set(palette.values())
    for token in sorted(set(tokens.values()) - palette.keys()):
        match = re.fullmatch(r"(MID|PLANE|MECHANICAL)(\d+)", token)
        if match:
            family, number = match.groups()
            index = (int(number) - 1) * 3 + ("MID", "PLANE", "MECHANICAL").index(family)
            hue = (0.33 + index * 0.61803398875) % 1
        else:
            hue = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4]) / 2**32
        attempt = 0
        while True:
            rgb = colorsys.hsv_to_rgb(hue, 0.60 + (attempt * 0.071) % 0.25, 0.92)
            color = "#" + "".join(f"{round(channel * 255):02x}" for channel in rgb)
            if color not in used:
                break
            attempt += 1
            hue = (hue + 0.61803398875) % 1
        palette[token] = color
        used.add(color)
    return {layer: palette[token] for layer, token in tokens.items()}
