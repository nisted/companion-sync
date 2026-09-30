"""The Companion Sync icon, made from logo-source.png (Nige's artwork)."""
import os
import sys

from PIL import Image, ImageDraw

SOURCE = "logo-source.png"


def _source_path():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, SOURCE)


_square = None


def _square_logo():
    """The artwork cropped to the letters, centred on a transparent square."""
    global _square
    if _square is None:
        im = Image.open(_source_path()).convert("RGBA")
        box = im.getchannel("A").point(lambda a: 255 if a > 10 else 0).getbbox()
        im = im.crop(box)
        w, h = im.size
        side = int(max(w, h) * 1.04)
        sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        sq.alpha_composite(im, ((side - w) // 2, (side - h) // 2))
        _square = sq
    return _square


def draw_cs(size, status=None, rounded=False):
    """size x size icon. status: (r, g, b) dot in the corner for the tray.
    rounded=True: the app icon, on a black rounded tile with a margin."""
    img = _square_logo().resize((size, size), Image.LANCZOS)
    if rounded:
        tile = Image.new("RGBA", (size, size), (0, 0, 0, 255))
        inner = int(size * 0.8)
        logo = _square_logo().resize((inner, inner), Image.LANCZOS)
        tile.alpha_composite(logo, ((size - inner) // 2, (size - inner) // 2))
        img = tile
    if status:
        d = ImageDraw.Draw(img)
        r = max(3, size * 0.2)
        x0, y0 = size - 2 * r - 1, size - 2 * r - 1
        d.ellipse((x0 - 1, y0 - 1, size - 1, size - 1), fill=(20, 20, 20, 255))
        d.ellipse((x0, y0, size - 2, size - 2), fill=tuple(status) + (255,))
    if rounded:
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.2), fill=255)
        img.putalpha(mask)
    return img


if __name__ == "__main__":                       # build step: write the app icon
    draw_cs(1024, rounded=True).save("icon.png")
