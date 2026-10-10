"""BD-1 panel hardware: ILI9341 display and XPT2046 touch on SPI, via libgpiod.

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
        self.W, self.H = 320, 240
        chip = _find_chip()
        self.dc, self.rst, self.irq = cfg["pin_dc"], cfg["pin_rst"], cfg["pin_touch_irq"]
        self.outs = gpiod.request_lines(chip, consumer="companion-panel", config={
            (self.dc, self.rst): gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE)})
        self.ins = gpiod.request_lines(chip, consumer="companion-panel", config={
            self.irq: gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP)})
        self.lcd = spidev.SpiDev()
        self.lcd.open(cfg["spi_bus"], cfg["spi_lcd_cs"])
        self.lcd.max_speed_hz = cfg["spi_lcd_hz"]
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
        self.outs.set_value(self.dc, self.V.INACTIVE)
        self.lcd.writebytes([c])
        if data:
            self.outs.set_value(self.dc, self.V.ACTIVE)
            self.lcd.writebytes2(bytes(data))

    def _init_lcd(self):
        self.outs.set_value(self.rst, self.V.INACTIVE)
        time.sleep(0.02)
        self.outs.set_value(self.rst, self.V.ACTIVE)
        time.sleep(0.15)
        self._cmd(0x01)                      # software reset
        time.sleep(0.15)
        self._cmd(0x11)                      # sleep out
        time.sleep(0.12)
        self._cmd(0x3A, [0x55])              # 16-bit colour
        self._cmd(0x36, [self.cfg["lcd_madctl"]])   # orientation
        self._cmd(0x29)                      # display on

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
        self._cmd(0x2C, rgb565(img.crop(box)))
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
        c = self.cfg
        if c["touch_swap_xy"]:
            rx, ry = ry, rx
        tx = (rx - c["touch_x_min"]) / max(1, c["touch_x_max"] - c["touch_x_min"])
        ty = (ry - c["touch_y_min"]) / max(1, c["touch_y_max"] - c["touch_y_min"])
        if c["touch_invert_x"]:
            tx = 1 - tx
        if c["touch_invert_y"]:
            ty = 1 - ty
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

    def __init__(self, out_dir, taps=()):
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

    def tap(self):
        return self.taps.pop(0) if self.taps else None

    def close(self):
        pass
