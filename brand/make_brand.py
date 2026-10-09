"""Draw the add-on's icon.png (128x128) and logo.png (256x100): a trefoil.

python brand/make_brand.py
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, "..", "gmc_relay")
NAVY = (16, 16, 20, 255)
AMBER = (242, 163, 60, 255)
BONE = (232, 227, 216, 255)


def trefoil(size: int) -> Image.Image:
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 5, fill=NAVY)
    c = s / 2
    r_out, r_in = s * 0.40, s * 0.15
    for k in range(3):
        start = 90 + k * 120 - 30
        d.pieslice((c - r_out, c - r_out, c + r_out, c + r_out), start, start + 60, fill=AMBER)
    d.ellipse((c - r_in, c - r_in, c + r_in, c + r_in), fill=NAVY)
    r_dot = s * 0.07
    d.ellipse((c - r_dot, c - r_dot, c + r_dot, c + r_dot), fill=AMBER)
    return img.resize((size, size), Image.LANCZOS)


def logo() -> Image.Image:
    img = Image.new("RGBA", (256, 100), (0, 0, 0, 0))
    img.alpha_composite(trefoil(84), (8, 8))
    d = ImageDraw.Draw(img)
    x = 108
    for i, width in enumerate((120, 84, 100)):
        y = 28 + i * 18
        d.rounded_rectangle((x, y, x + width, y + 10), radius=5, fill=AMBER if i == 0 else BONE)
    return img


def main() -> None:
    trefoil(128).save(os.path.join(ADDON, "icon.png"))
    logo().save(os.path.join(ADDON, "logo.png"))
    for name in ("icon.png", "logo.png"):
        path = os.path.join(ADDON, name)
        with Image.open(path) as im:
            print(f"{name}: {im.size[0]}x{im.size[1]} {os.path.getsize(path)} bytes")


if __name__ == "__main__":
    main()
