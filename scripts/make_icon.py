"""Draw the Windows application icon: python scripts/make_icon.py (needs Pillow)."""

from pathlib import Path

from PIL import Image, ImageDraw

BACKGROUND, BOARD, TRACE, PAD = "#1f2329", "#16533d", "#35c2d8", "#e8c86a"


def draw(size):
    scale = 4  # supersample, then downscale for smooth edges
    s = size * scale
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    unit = s / 64
    pen.rounded_rectangle((2 * unit, 2 * unit, 62 * unit, 62 * unit), radius=12 * unit, fill=BACKGROUND)
    pen.rounded_rectangle((9 * unit, 9 * unit, 55 * unit, 55 * unit), radius=5 * unit, fill=BOARD)
    width = max(round(4 * unit), 1)
    pen.line([(16 * unit, 46 * unit), (16 * unit, 32 * unit), (30 * unit, 18 * unit), (48 * unit, 18 * unit)],
             fill=TRACE, width=width, joint="curve")
    pen.line([(30 * unit, 46 * unit), (30 * unit, 36 * unit), (40 * unit, 28 * unit), (48 * unit, 28 * unit)],
             fill=TRACE, width=width, joint="curve")
    for x, y in ((16, 46), (30, 46), (48, 18), (48, 28)):
        r = 4.5 * unit
        pen.ellipse((x * unit - r, y * unit - r, x * unit + r, y * unit + r), fill=PAD)
        h = 1.6 * unit
        pen.ellipse((x * unit - h, y * unit - h, x * unit + h, y * unit + h), fill=BACKGROUND)
    return image.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "vcad.ico"
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [draw(size) for size in sizes]
    images[-1].save(target, sizes=[(size, size) for size in sizes], append_images=images[:-1])
    images[-1].save(target.with_suffix(".png"))
    print(f"Wrote {target}")
