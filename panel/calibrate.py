"""Touch calibration: tap four crosses, the result is saved to panel.json.

  sudo systemctl stop companion-panel
  python3 /opt/companion-sync/panel/calibrate.py
  sudo systemctl start companion-panel
"""
import json
import os
import sys
import time

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import panel as P                                    # noqa: E402
from hw import Hardware                              # noqa: E402

W, H, M = 320, 240, 30
TARGETS = [(M, M), (W - M, M), (W - M, H - M), (M, H - M)]


def screen(cfg, text, cross=None, dots=()):
    C = {k: P.hexrgb(v) for k, v in cfg["colours"].items()}
    img = Image.new("RGB", (W, H), C["bg"])
    d = ImageDraw.Draw(img)
    d.text((W // 2, H // 2), text, font=P.Fonts.head, fill=C["lavender"], anchor="mm")
    if cross:
        x, y = cross
        d.line((x - 12, y, x + 12, y), fill=C["purple"], width=2)
        d.line((x, y - 12, x, y + 12), fill=C["purple"], width=2)
        d.ellipse((x - 3, y - 3, x + 3, y + 3), outline=C["lavender"])
    for x, y in dots:
        d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=C["green"])
    return img


def wait_raw(hw):
    while True:
        r = hw.raw_tap()
        if r:
            return r
        time.sleep(0.01)


def solve(raws):
    """Work out swap / invert / range from the four corner taps."""
    rx = [r[0] for r in raws]
    ry = [r[1] for r in raws]
    left, right = (0, 3), (1, 2)
    top, bottom = (0, 1), (2, 3)
    mean = lambda v, idx: sum(v[i] for i in idx) / len(idx)   # noqa: E731
    swap = abs(mean(ry, right) - mean(ry, left)) > abs(mean(rx, right) - mean(rx, left))
    a, b = (ry, rx) if swap else (rx, ry)

    def axis(v, lo_idx, hi_idx, lo_px, hi_px, size):
        lo, hi = mean(v, lo_idx), mean(v, hi_idx)
        per_px = (hi - lo) / (hi_px - lo_px)
        at0, at_end = lo - per_px * lo_px, hi + per_px * (size - hi_px)
        return (min(at0, at_end), max(at0, at_end), at0 > at_end)

    xmin, xmax, xinv = axis(a, left, right, M, W - M, W)
    ymin, ymax, yinv = axis(b, top, bottom, M, H - M, H)
    return {"touch_swap_xy": swap, "touch_x_min": int(xmin), "touch_x_max": int(xmax),
            "touch_invert_x": xinv, "touch_y_min": int(ymin), "touch_y_max": int(ymax),
            "touch_invert_y": yinv}


def main():
    cfg = P.load_settings()
    hw = Hardware(cfg)
    try:
        raws = []
        for i, t in enumerate(TARGETS):
            hw.show(screen(cfg, f"TAP THE CROSS  ({i + 1}/4)", t))
            raws.append(wait_raw(hw))
            print(f"target {t}: raw {raws[-1]}", flush=True)
        result = solve(raws)
        print("calibration:", result, flush=True)
        os.makedirs(os.path.dirname(P.SETTINGS_FILE), exist_ok=True)
        try:
            with open(P.SETTINGS_FILE) as f:
                mine = json.load(f)
        except (OSError, ValueError):
            mine = {}
        mine.update(result)
        with open(P.SETTINGS_FILE, "w") as f:
            json.dump(mine, f, indent=2)
        cfg.update(result)
        hw.cfg = cfg
        dots = []
        end = time.time() + 20
        hw.show(screen(cfg, "SAVED - TAP TO TEST (20 s)"))
        while time.time() < end:
            p = hw.tap()
            if p:
                dots.append(p)
                print("tap ->", p, flush=True)
                hw.show(screen(cfg, "SAVED - TAP TO TEST (20 s)", dots=dots[-12:]))
            time.sleep(0.01)
        hw.show(screen(cfg, "CALIBRATION DONE"))
    finally:
        hw.close()


if __name__ == "__main__":
    main()
