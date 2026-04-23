from __future__ import annotations

from PIL import Image, ImageDraw

COLORS = {
    "green": (46, 160, 67),
    "red": (207, 34, 46),
    "yellow": (212, 167, 44),
}

_SIZE = 64


def _rounded_rect(draw: ImageDraw.ImageDraw, box, radius: int, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def make_icon(color: str) -> Image.Image:
    rgb = COLORS[color]
    img = Image.new("RGBA", (_SIZE, _SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    body = (6, 18, _SIZE - 6, _SIZE - 18)
    _rounded_rect(draw, body, radius=6, fill=rgb, outline=(0, 0, 0, 220), width=2)

    slot_top = 26
    slot_bottom = _SIZE - 26
    for y in range(slot_top, slot_bottom, 4):
        draw.line([(12, y), (_SIZE - 28, y)], fill=(0, 0, 0, 80), width=1)

    led_center = (_SIZE - 16, _SIZE // 2)
    led_radius = 4
    draw.ellipse(
        [
            (led_center[0] - led_radius, led_center[1] - led_radius),
            (led_center[0] + led_radius, led_center[1] + led_radius),
        ],
        fill=(255, 255, 255, 230),
        outline=(0, 0, 0, 220),
        width=1,
    )

    return img


_cache: dict[str, Image.Image] = {}


def get_icon(color: str) -> Image.Image:
    if color not in _cache:
        _cache[color] = make_icon(color)
    return _cache[color]
