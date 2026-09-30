"""The Companion Sync icon: purple 'CS', the S with an arrowhead on each end."""
import math

from PIL import Image, ImageDraw

PURPLE = (150, 80, 230, 255)
S_SCALE = 4          # draw big, shrink down: smooth edges at any size


def _arrow(d, cx, cy, r, angle_deg, length, width, colour):
    """Arrowhead at a point on a circle, pointing clockwise along it."""
    a = math.radians(angle_deg)
    px, py = cx + r * math.cos(a), cy + r * math.sin(a)          # point on the circle
    tx, ty = -math.sin(a), math.cos(a)                           # clockwise direction
    nx, ny = math.cos(a), math.sin(a)                            # sideways
    tip = (px + tx * length, py + ty * length)
    left = (px + nx * width / 2, py + ny * width / 2)
    right = (px - nx * width / 2, py - ny * width / 2)
    d.polygon([tip, left, right], fill=colour)


def draw_cs(size, background=None, status=None):
    """Return a size x size image of the CS mark.

    background: optional (r, g, b, a) rounded-square behind the letters.
    status: optional (r, g, b) dot in the bottom-right corner (tray icon).
    """
    S = 1024
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if background:
        d.rounded_rectangle((0, 0, S - 1, S - 1), radius=220, fill=background)

    # C: open to the right
    cx, cy, r, w = 300, 512, 215, 105
    d.arc((cx - r, cy - r, cx + r, cy + r), 45, 315, fill=PURPLE, width=w)

    # S: two half-circles stacked, with arrowheads on both ends
    sx, sr, sw = 735, 125, 95
    top, bot = 512 - sr, 512 + sr
    d.arc((sx - sr - sw // 2, top - sr - sw // 2, sx + sr + sw // 2, top + sr + sw // 2),
          90, 300, fill=PURPLE, width=sw)
    d.arc((sx - sr - sw // 2, bot - sr - sw // 2, sx + sr + sw // 2, bot + sr + sw // 2),
          270, 480, fill=PURPLE, width=sw)
    mid = sr                                                      # centre line of the stroke
    _arrow(d, sx, top, mid, 300, 150, 210, PURPLE)                # top end: points right
    _arrow(d, sx, bot, mid, 120, 150, 210, PURPLE)                # bottom end: points left

    if status:
        d.ellipse((700, 700, 1010, 1010), fill=(20, 20, 20, 255))
        d.ellipse((730, 730, 980, 980), fill=tuple(status) + (255,))
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":                       # build step: write the app icon
    draw_cs(1024, background=(28, 28, 36, 255)).save("icon.png")
