"""Build the app's icon.png and logo.png from brand/icon-source.png.

python brand/make_brand.py
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, "..", "gmc_relay")
AMBER = (242, 163, 60, 255)
BONE = (232, 227, 216, 255)


def trefoil(size: int) -> Image.Image:
    with Image.open(os.path.join(HERE, "icon-source.png")) as source:
        return source.convert("RGBA").resize((size, size), Image.LANCZOS)


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
