"""BD-1 panel hardware: SPI display and XPT2046 touch, via libgpiod.

Two screens are supported, picked by the "screen" setting:
  "2.4"  SHCHV 2.4" RPi Display: 320x240 ILI9341, driven directly.
  "3.5"  3.5" RPi Display (LCDwiki MPI3501 design): 480x320 ILI9486 behind
         74HC4094 shift registers, so every command and parameter goes out as
         a 16-bit word. Pins proven on BD-1 on 6 Nov 2026.
"rotation" is 0, 90, 180 or 270. 0 and 180 are landscape, 90 and 270 are
portrait (the width and height swap). Touch calibration is always kept in the
rotation-0 frame and turned to match, so one calibration covers all four.

Preview classes stand in for the hardware, so screens can be rendered to
PNG files on any computer.
"""
import os
import time

from PIL import Image, ImageChops

try:
    import numpy as np
except ImportError:          # slower fallback below
    np = None


def rgb565(img):
    """PIL image -> big-endian RGB565 bytes, as the ILI9341 wants them."""
    if np is not None:
        a = np.asarray(img.convert("RGB"), dtype=np.uint16)
        v = ((a[..., 0] & 0xF8) << 8) | ((a[..., 1] & 0xFC) << 3) | (a[..., 2] >> 3)
        return v.astype(">u2").tobytes()
    px = img.convert("RGB").tobytes()
    out = bytearray(len(px) // 3 * 2)
    j = 0
    for i in range(0, len(px), 3):
        v = ((px[i] & 0xF8) << 8) | ((px[i + 1] & 0xFC) << 3) | (px[i + 2] >> 3)
        out[j], out[j + 1] = v >> 8, v & 255
        j += 2
    return bytes(out)


# Per-screen settings. Anything here can be overridden in panel.json under
# "screens": {"3.5": {...}}. The older flat keys (pin_dc, touch_x_min, ...)
# still apply, but to the 2.4" only, so an old calibration can't leak onto
# the 3.5".
SCREENS = {
    "2.4": {
        "width": 320, "height": 240, "controller": "ili9341",
        "pin_dc": 22, "pin_rst": 27, "pin_touch_irq": 17,
        "spi_lcd_hz": 32_000_000, "lcd_madctl": 0x28,
        "touch_x_min": 85, "touch_x_max": 3747, "touch_y_min": 348, "touch_y_max": 3805,
        "touch_swap_xy": True, "touch_invert_x": False, "touch_invert_y": True,
    },
    "3.5": {
        "width": 480, "height": 320, "controller": "ili9486_16bit",
        "pin_dc": 24, "pin_rst": 25, "pin_touch_irq": 17,
        "spi_lcd_hz": 16_000_000, "lcd_madctl": 0x28,
        # calibrate.py on BD-1's screen (6 Nov); run it again to redo
        "touch_x_min": 383, "touch_x_max": 4029, "touch_y_min": 153, "touch_y_max": 3725,
        "touch_swap_xy": True, "touch_invert_x": True, "touch_invert_y": False,
    },
}
ROTATIONS = (0, 90, 180, 270)


def screen_name(cfg):
    name = str(cfg.get("screen", "2.4"))
    if name not in SCREENS:
        raise ValueError(f'Unknown screen "{name}": use one of {", ".join(SCREENS)}')
    return name


def rotation(cfg):
    r = int(cfg.get("rotation", 0)) % 360
    if r not in ROTATIONS:
        raise ValueError(f"Rotation {r} isn't supported: use 0, 90, 180 or 270")
    return r


def screen_settings(cfg):
    """The settings for the chosen screen: built-in values, then (2.4" only)
    the older flat keys, then panel.json's "screens" block."""
    name = screen_name(cfg)
    s = dict(SCREENS[name])
    if name == "2.4":
        s.update({k: cfg[k] for k in SCREENS["2.4"] if k in cfg and k not in ("width", "height", "controller")})
    s.update((cfg.get("screens") or {}).get(name, {}))
    return s


def screen_size(cfg):
    """(width, height) as drawn: swapped for the portrait rotations."""
    s = screen_settings(cfg)
    w, h = s["width"], s["height"]
    return (h, w) if rotation(cfg) in (90, 270) else (w, h)


def madctl(base, rot):
    """The orientation byte for a rotation. base is the rotation-0 (landscape)
    value; its low bits (colour order etc.) are kept."""
    low = base & 0x1F
    return {0: base, 180: base ^ 0xC0, 90: low | 0x40, 270: low | 0x80}[rot]


def turn(tx, ty, rot):
    """Touch position (0..1, rotation-0 frame) -> the same point as drawn."""
    if rot == 90:
        return 1 - ty, tx
    if rot == 180:
        return 1 - tx, 1 - ty
    if rot == 270:
        return ty, 1 - tx
    return tx, ty


def _find_chip():
    import gpiod
    for n in range(16):
        path = f"/dev/gpiochip{n}"
        if not os.path.exists(path):
            continue
        try:
            with gpiod.Chip(path) as c:
                if c.get_info().label.startswith(("pinctrl-rp1", "pinctrl-bcm2")):
                    return path
        except OSError:
            continue
    raise RuntimeError("No Raspberry Pi GPIO chip found")


class Hardware:
    """Display + touch. Only sends the part of the screen that changed."""

    def __init__(self, cfg):
        import gpiod
        import spidev
        from gpiod.line import Bias, Direction, Value
        self.V = Value
        self.cfg = cfg
        s = screen_settings(cfg)
        self.screen = screen_name(cfg)
        self.rotation = rotation(cfg)
        self.wide = s["controller"] == "ili9486_16bit"     # 16-bit command words
        self.W, self.H = screen_size(cfg)
        chip = _find_chip()
        self.dc, self.rst, self.irq = s["pin_dc"], s["pin_rst"], s["pin_touch_irq"]
        self.outs = gpiod.request_lines(chip, consumer="rebel-sync-panel", config={
            (self.dc, self.rst): gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE)})
        self.ins = gpiod.request_lines(chip, consumer="rebel-sync-panel", config={
            self.irq: gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP)})
        self.lcd = spidev.SpiDev()
        self.lcd.open(cfg["spi_bus"], cfg["spi_lcd_cs"])
        self.lcd.max_speed_hz = s["spi_lcd_hz"]
        self.lcd.mode = 0
        self.tp = spidev.SpiDev()
        self.tp.open(cfg["spi_bus"], cfg["spi_touch_cs"])
        self.tp.max_speed_hz = cfg["spi_touch_hz"]
        self.tp.mode = 0
        self.frame = None
        self._down = False
        self._up_since = 0.0
        self._init_lcd()

    # -- display ---------------------------------------------------------
    def _cmd(self, c, data=None):
        """A command and its parameters. Pixel data goes through _pixels."""
        self.outs.set_value(self.dc, self.V.INACTIVE)
        self.lcd.writebytes([0x00, c] if self.wide else [c])
        if data:
            self.outs.set_value(self.dc, self.V.ACTIVE)
            if self.wide:
                data = [b for v in data for b in (0x00, v)]
            self.lcd.writebytes2(bytes(data))

    def _pixels(self, data):
        self._cmd(0x2C)
        self.outs.set_value(self.dc, self.V.ACTIVE)
        self.lcd.writebytes2(data)

    def _madctl(self):
        return madctl(screen_settings(self.cfg)["lcd_madctl"], self.rotation)

    def _init_lcd(self):
        self.outs.set_value(self.rst, self.V.INACTIVE)
        time.sleep(0.05 if self.wide else 0.02)
        self.outs.set_value(self.rst, self.V.ACTIVE)
        time.sleep(0.15)
        if self.wide:
            self._init_ili9486()
        else:
            self._init_ili9341()

    def _init_ili9341(self):
        self._cmd(0x01)                      # software reset
        time.sleep(0.15)
        self._cmd(0x11)                      # sleep out
        time.sleep(0.12)
        self._cmd(0x3A, [0x55])              # 16-bit colour
        self._cmd(0x36, [self._madctl()])    # orientation
        self._cmd(0x29)                      # display on

    def _init_ili9486(self):
        """Same sequence as the usual driver for this board (proven by test35.py)."""
        self._cmd(0xB0, [0x00])
        self._cmd(0x11)                      # sleep out
        time.sleep(0.25)
        self._cmd(0x3A, [0x55])              # 16-bit colour
        self._cmd(0x36, [self._madctl()])    # orientation
        self._cmd(0xC2, [0x44])
        self._cmd(0xC5, [0x00, 0x00, 0x00, 0x00])
        g2 = [0x0F, 0x32, 0x2E, 0x0B, 0x0D, 0x05, 0x47, 0x75, 0x37, 0x06, 0x10, 0x03, 0x24, 0x20, 0x00]
        self._cmd(0xE0, [0x0F, 0x1F, 0x1C, 0x0C, 0x0F, 0x08, 0x48, 0x98, 0x37, 0x0A, 0x13, 0x04, 0x11, 0x0D, 0x00])
        self._cmd(0xE1, g2)
        self._cmd(0xE2, g2)
        self._cmd(0x36, [self._madctl()])
        self._cmd(0x11)
        time.sleep(0.12)
        self._cmd(0x29)                      # display on

    def set_rotation(self, rot):
        """Turn the picture now (the touch mapping follows)."""
        self.cfg["rotation"] = rot
        self.rotation = rotation(self.cfg)
        self.W, self.H = screen_size(self.cfg)
        self._cmd(0x36, [self._madctl()])
        self.frame = None

    def _window(self, x0, y0, x1, y1):
        self._cmd(0x2A, [x0 >> 8, x0 & 255, x1 >> 8, x1 & 255])
        self._cmd(0x2B, [y0 >> 8, y0 & 255, y1 >> 8, y1 & 255])

    def show(self, img):
        img = img.convert("RGB")
        if self.frame is None:
            box = (0, 0, self.W, self.H)
        else:
            box = ImageChops.difference(self.frame, img).getbbox()
            if not box:
                return
        x0, y0, x1, y1 = box
        self._window(x0, y0, x1 - 1, y1 - 1)
        self._pixels(rgb565(img.crop(box)))
        self.frame = img

    # -- touch -----------------------------------------------------------
    def _raw(self):
        def rd(c):
            r = self.tp.xfer2([c, 0, 0])
            return ((r[1] << 8) | r[2]) >> 3
        xs, ys = [], []
        for _ in range(7):
            xs.append(rd(0xD0))
            ys.append(rd(0x90))
        xs, ys = sorted(xs[1:]), sorted(ys[1:])          # first reading is often off
        return xs[len(xs) // 2], ys[len(ys) // 2]

    def pressed(self):
        return self._pressed()

    def _pressed(self):
        return self.ins.get_value(self.irq) == self.V.INACTIVE

    def raw_tap(self):
        """Raw (x, y) controller readings for a new touch, else None."""
        now = time.monotonic()
        if not self._pressed():
            if self._down:
                self._down = False
                self._up_since = now
            return None
        if self._down or now - self._up_since < self.cfg["touch_release_s"]:
            return None
        time.sleep(0.03)                                 # let the touch settle
        r = self._raw()
        if not self._pressed():                          # let go mid-reading: ignore
            return None
        self._down = True
        return r

    def tap(self):
        """(x, y) in screen pixels for a new touch, else None."""
        r = self.raw_tap()
        return self.to_screen(*r) if r else None

    def to_screen(self, rx, ry):
        c = screen_settings(self.cfg)          # re-read, so calibrate.py's result applies at once
        if c["touch_swap_xy"]:
            rx, ry = ry, rx
        tx = (rx - c["touch_x_min"]) / max(1, c["touch_x_max"] - c["touch_x_min"])
        ty = (ry - c["touch_y_min"]) / max(1, c["touch_y_max"] - c["touch_y_min"])
        if c["touch_invert_x"]:
            tx = 1 - tx
        if c["touch_invert_y"]:
            ty = 1 - ty
        tx, ty = turn(tx, ty, self.rotation)
        x = max(0, min(self.W - 1, int(tx * self.W)))
        y = max(0, min(self.H - 1, int(ty * self.H)))
        return x, y

    def close(self):
        for h in (self.lcd, self.tp):
            try:
                h.close()
            except Exception:
                pass
        for r in (self.outs, self.ins):
            try:
                r.release()
            except Exception:
                pass


class Preview:
    """Stand-in for the hardware: saves each new frame as a PNG; taps come from a list."""

    def __init__(self, out_dir, taps=(), size=(320, 240)):
        self.W, self.H = size
        self.rotation = 0
        self.out_dir = out_dir
        self.taps = list(taps)
        self.frame = None
        self.count = 0
        os.makedirs(out_dir, exist_ok=True)

    def show(self, img):
        img = img.convert("RGB")
        if self.frame is not None and not ImageChops.difference(self.frame, img).getbbox():
            return
        self.frame = img
        self.count += 1
        img.save(os.path.join(self.out_dir, f"frame-{self.count:03d}.png"))

    def set_rotation(self, rot):
        if (rot in (90, 270)) != (self.rotation in (90, 270)):
            self.W, self.H = self.H, self.W
        self.rotation = rot
        self.frame = None

    def tap(self):
        return self.taps.pop(0) if self.taps else None

    def pressed(self):
        return getattr(self, "down", False)

    def close(self):
        pass
