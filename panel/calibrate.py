"""Touch calibration: tap four crosses, the result is saved to panel.json.

  sudo systemctl stop companion-panel
  python3 /opt/companion-sync/panel/calibrate.py
  sudo systemctl start companion-panel

Works on either screen and in any rotation. The result is stored for the
current screen only (panel.json "screens" block), in the rotation-0 frame, so
it carries on working if the rotation is changed later.
"""
import json
import os
import sys
import time

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import panel as P                                    # noqa: E402
from hw import Hardware, screen_name                 # noqa: E402

def targets(w, h):
    m = round(30 * min(w, h) / 240)                  # 30 px in from the edges on the 2.4"
    return [(m, m), (w - m, m), (w - m, h - m), (m, h - m)]


def to_rotation0(px, py, rot):
    """A drawn position (0..1) back to the rotation-0 frame (inverse of hw.turn)."""
    if rot == 90:
        return py, 1 - px
    if rot == 180:
        return 1 - px, 1 - py
    if rot == 270:
        return 1 - py, px
    return px, py


def screen(cfg, size, text, cross=None, dots=()):
    w, h = size
    k = min(w, h) / 240
    F = P.Fonts(k)
    C = {key: P.hexrgb(v) for key, v in cfg["colours"].items()}
    img = Image.new("RGB", size, C["bg"])
    d = ImageDraw.Draw(img)
    d.text((w // 2, h // 2), text, font=F.head, fill=C["lavender"], anchor="mm")
    arm, dot = round(12 * k), round(3 * k)
    if cross:
        x, y = cross
        d.line((x - arm, y, x + arm, y), fill=C["purple"], width=2)
        d.line((x, y - arm, x, y + arm), fill=C["purple"], width=2)
        d.ellipse((x - dot, y - dot, x + dot, y + dot), outline=C["lavender"])
    for x, y in dots:
        d.ellipse((x - dot, y - dot, x + dot, y + dot), fill=C["green"])
    return img


class TimedOut(Exception):
    pass


def wait_raw(hw, timeout=None):
    end = time.monotonic() + timeout if timeout else None
    while True:
        r = hw.raw_tap()
        if r:
            return r
        if end and time.monotonic() > end:
            raise TimedOut()
        time.sleep(0.01)


def fit(t, v):
    """Least-squares v = a + b*t; returns v at t = 0 and t = 1."""
    n = len(t)
    mt, mv = sum(t) / n, sum(v) / n
    b = sum((ti - mt) * (vi - mv) for ti, vi in zip(t, v)) / max(1e-9, sum((ti - mt) ** 2 for ti in t))
    a = mv - b * mt
    return a, a + b


def solve(raws, norm):
    """Swap / invert / range from four taps. norm holds each target's position
    (0..1) in the rotation-0 frame."""
    rx = [r[0] for r in raws]
    ry = [r[1] for r in raws]
    tx = [n[0] for n in norm]
    ty = [n[1] for n in norm]

    def spread(v, t):                                # how well a raw axis follows t
        at0, at1 = fit(t, v)
        return abs(at1 - at0)
    swap = spread(ry, tx) > spread(rx, tx)
    a, b = (ry, rx) if swap else (rx, ry)
    x0, x1 = fit(tx, a)
    y0, y1 = fit(ty, b)
    return {"touch_swap_xy": swap,
            "touch_x_min": int(min(x0, x1)), "touch_x_max": int(max(x0, x1)), "touch_invert_x": x0 > x1,
            "touch_y_min": int(min(y0, y1)), "touch_y_max": int(max(y0, y1)), "touch_invert_y": y0 > y1}


def run(hw, cfg, timeout=None, test_s=20):
    """Calibrate on an open screen and save the result. timeout: give up
    (TimedOut) if a cross isn't tapped within that many seconds. Returns the
    calibration."""
    size = (hw.W, hw.H)
    name = screen_name(cfg)
    raws, norm = [], []
    for i, t in enumerate(targets(*size)):
        hw.show(screen(cfg, size, f"TAP THE CROSS  ({i + 1}/4)", t))
        raws.append(wait_raw(hw, timeout))
        norm.append(to_rotation0(t[0] / size[0], t[1] / size[1], hw.rotation))
        print(f"target {t}: raw {raws[-1]}", flush=True)
    result = solve(raws, norm)
    print(f"calibration for the {name}\" screen:", result, flush=True)
    os.makedirs(os.path.dirname(P.SETTINGS_FILE), exist_ok=True)
    try:
        with open(P.SETTINGS_FILE) as f:
            mine = json.load(f)
    except (OSError, ValueError):
        mine = {}
    mine.setdefault("screens", {}).setdefault(name, {}).update(result)
    with open(P.SETTINGS_FILE, "w") as f:
        json.dump(mine, f, indent=2)
    cfg.setdefault("screens", {}).setdefault(name, {}).update(result)
    hw.cfg = cfg
    dots = []
    end = time.time() + test_s
    text = f"SAVED - TAP TO TEST ({test_s} s)"
    hw.show(screen(cfg, size, text))
    while time.time() < end:
        p = hw.tap()
        if p:
            dots.append(p)
            print("tap ->", p, flush=True)
            hw.show(screen(cfg, size, text, dots=dots[-12:]))
        time.sleep(0.01)
    hw.show(screen(cfg, size, "CALIBRATION DONE"))
    return result


def main():
    cfg = P.load_settings()
    hw = Hardware(cfg)
    try:
        run(hw, cfg)
    finally:
        hw.close()


if __name__ == "__main__":
    main()
