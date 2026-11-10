#!/usr/bin/env python3
"""BD-1 panel: Rebel Sync status and controls on the touchscreen.

  python3 panel.py                  run on the Pi (systemd: rebel-sync-panel.service)
  python3 panel.py --preview DIR    render sample screens to PNG files in DIR,
                                    one folder per screen and orientation

Every screen is laid out for the size it's drawn at: 2.4" (320x240) or
3.5" (480x320), landscape or portrait (the "screen" and "rotation" settings).
The 2.4" landscape layout is the original design; the others are worked out
from it (see Layout).

Settings: ~/.config/rebel-sync/panel.json overrides any of DEFAULTS below.
"""
import copy
import dataclasses
import json
import logging
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request

from PIL import Image, ImageDraw, ImageFont, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # engine.py lives one level up
import engine as E                                  # noqa: E402
import info                                         # noqa: E402
from deckview import DeckView, fit as deck_fit      # noqa: E402

DEFAULTS = {
    # screen: "2.4" (SHCHV 2.4", 320x240) or "3.5" (3.5" RPi Display, 480x320).
    # Pins, SPI speed and touch calibration for each are in hw.SCREENS and can
    # be overridden under "screens": {"3.5": {...}}.
    "screen": "2.4",
    "rotation": 0,                            # 0 / 180 landscape, 90 / 270 portrait
    "spi_bus": 0, "spi_lcd_cs": 0, "spi_touch_cs": 1, "spi_touch_hz": 1_000_000,
    "touch_release_s": 0.15,
    # behaviour
    "confirm_s": 4, "flash_s": 0.5, "frame_s": 0.25,
    "boot_screen_max_s": 150, "temp_warn_c": 75,
    # screen chooser at power-on: seconds to offer it (0 = off), and each screen's turn
    "screen_choice_s": 15, "screen_choice_turn_s": 3,
    "sys_check_s": 2, "net_check_s": 15, "companion_check_s": 5,
    "sync_check_s": 120, "latest_check_h": 6, "latest_retry_min": 5,
    # look
    "logo": "startup-logo.png",
    "colours": {
        "bg": "#06080c", "scan": "#0c1016", "purple": "#b973ff", "purple_dim": "#5500aa",
        "lavender": "#e8d6ff", "cyan": "#5adcff", "cyan_dim": "#16465a", "green": "#50f078",
        "red": "#ff3c32", "red_dim": "#64140f", "amber": "#ffa628", "grey": "#9696a0",
        "faint": "#3c3c46", "rule": "#1c2028",
    },
    # Companion
    "companion_port": 8000,
    "companion_package_json": "/opt/companion/package.json",
    "companion_api": "https://api.bitfocus.io/v1/product/companion/packages",
    "companion_majors": [3, 4, 5],
    # sync and updates
    "boot_report": "/run/rebel-sync/boot.json",
    "log_file": "~/.local/state/rebel-sync/panel.log",
    "github_repo": "nisted/rebel-sync",
    "github_token_file": "~/.config/rebel-sync/github-token",
    "pi_asset": "Rebel-Sync-Pi.tar.gz",
}
SETTINGS_FILE = os.path.expanduser("~/.config/rebel-sync/panel.json")
# v2.0.0: panel.json values written for Companion Sync, moved to their new names
RENAMED = {"/run/companion-sync/": "/run/rebel-sync/", "/companion-sync/": "/rebel-sync/",
           "nisted/companion-sync": "nisted/rebel-sync", "Companion-Sync-Pi.tar.gz": "Rebel-Sync-Pi.tar.gz"}

TABS = ["SYSTEM", "NETWORK", "COMPANION", "SYNC"]
NEEDS_CONFIRM = {"restart", "shutdown", "return", "update_companion", "calibrate"}
ROTATION_NAMES = {0: "0° LANDSCAPE", 90: "90° PORTRAIT", 180: "180° LANDSCAPE", 270: "270° PORTRAIT"}
FAST_RCLONE = ["--contimeout", "10s", "--timeout", "20s", "--retries", "1", "--low-level-retries", "2"]

log = logging.getLogger("panel")


def load_settings():
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(SETTINGS_FILE) as f:
            mine = json.load(f)
        cols = mine.pop("colours", {})
        cfg.update({k: v for k, v in mine.items() if not k.startswith("_")})
        for k in ("boot_report", "log_file", "github_repo", "github_token_file", "pi_asset"):
            if isinstance(cfg.get(k), str):
                for old, new in RENAMED.items():
                    cfg[k] = cfg[k].replace(old, new)
        cfg["colours"].update(cols)
    except FileNotFoundError:
        pass
    except ValueError as e:
        print(f"Ignoring {SETTINGS_FILE}: {e}", file=sys.stderr)
    return cfg


def hexrgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def font(name, size):
    for d in ("/usr/share/fonts/truetype/dejavu/", ""):
        try:
            return ImageFont.truetype(d + name, size)
        except OSError:
            continue
    return ImageFont.load_default()


class Fonts:
    """The panel's fonts at a scale (1 = the 2.4" design)."""

    def __init__(self, k=1.0):
        def f(name, size):
            return font(name, max(7, round(size * k)))
        self.mono = f("DejaVuSansMono.ttf", 11)
        self.mono_b = f("DejaVuSansMono-Bold.ttf", 11)
        self.small = f("DejaVuSansMono.ttf", 9)
        self.msg = f("DejaVuSansMono.ttf", 10)
        self.big = f("DejaVuSansMono-Bold.ttf", 16)
        self.head = f("DejaVuSansCondensed-Bold.ttf", 12)
        self.btn = f("DejaVuSansCondensed-Bold.ttf", 11)
        self.btn_s = f("DejaVuSansCondensed-Bold.ttf", 9)


class Layout:
    """Where things go on a screen of W x H.

    The original design is the 2.4" landscape (320x240): header 0-21, content
    24-204, tabs 204-238. Other sizes keep that design:
      k   scales fonts, margins and element sizes (1 on the 2.4", 1.33 on the 3.5");
      y() maps a design content height (24-204) into this screen's content area,
          so portrait spreads the rows out over its extra height;
      x positions are measured from the edges, so wider screens get wider rows.
    Portrait also lays some button groups out in fewer columns."""

    def __init__(self, w, h):
        self.W, self.H = w, h
        self.portrait = h > w
        self.k = min(w / 240, h / 320) if self.portrait else min(w / 320, h / 240)
        self.tab_y = h - 2 - self.u(34)
        self.top = self.u(24)
        self.stretch = (self.tab_y - self.top) / 180

    def u(self, v):
        return int(round(v * self.k))

    def y(self, v):
        return int(round(self.top + (v - 24) * self.stretch))


Fonts.default = Fonts(1.0)                 # for calibrate.py and other one-offs


def build_number():
    for p in (os.path.join(os.path.dirname(HERE), "BUILD"), os.path.join(HERE, "BUILD")):
        try:
            with open(p) as f:
                return f.read().strip()
        except OSError:
            continue
    return "dev"


def version_label():
    """'v1.1.0 (build 27)', or 'build 27' for a package without a VERSION file."""
    b = build_number()
    for p in (os.path.join(os.path.dirname(HERE), "VERSION"), os.path.join(HERE, "VERSION")):
        try:
            with open(p) as f:
                return f"v{f.read().strip()} (build {b})"
        except OSError:
            continue
    return f"build {b}"


PROGRESS_FILE = os.path.expanduser("~/.rebel-sync-progress.json")


class Panel:
    def __init__(self, cfg, hw, settings=None, live=True):
        self.cfg, self.hw, self.live = cfg, hw, live
        self.relayout()
        self.C = {k: hexrgb(v) for k, v in cfg["colours"].items()}
        self.s = settings
        self.tab = "SYSTEM"
        self.page = "sync"                  # sync (the four tabs) | menu | screen | rebeldeck
        self.deck = DeckView(cfg) if live else None
        self.calibrating = False
        self.data = {"system": {}, "network": {}, "companion": {}, "sync": {}, "latest": None,
                     "boot": None}
        self.started = not live
        self.start_time = time.monotonic()
        # panel restarted long after boot (e.g. an update): just a quick look at the logo
        up = info.system().get("uptime") if live else None
        self.quick_start = bool(up and up > 300)
        self.boot_done_at = None
        self.confirm = None                 # (button id, expiry)
        self.overlay = None                 # dict: kind busy|message, title, lines, buttons
        self.hits = []
        self.lock = threading.Lock()
        self.sync_now = threading.Event()
        self.worker = None
        self.stopping = False
        self.build = build_number()
        self.logo = self._load_logo()

    def relayout(self):
        """Fit everything to the screen's current size (it changes with rotation)."""
        self.L = Layout(getattr(self.hw, "W", 320), getattr(self.hw, "H", 240))
        self.W, self.H = self.L.W, self.L.H
        self.F = Fonts(self.L.k)
        if hasattr(self, "logo"):
            self.logo = self._load_logo()

    # ---- data ----------------------------------------------------------------
    def _load_logo(self):
        p = self.cfg["logo"]
        p = p if os.path.isabs(p) else os.path.join(HERE, p)
        try:
            im = Image.open(p).convert("RGBA")
            # the 2.4" design shows it at its own size; scale with the screen,
            # but never wider than the screen or taller than the space above
            # the start-up list
            w = min(self.W, int(im.width * self.L.k))
            h = int(im.height * w / im.width)
            room = self.startup_y() - self.L.u(32)
            if h > room:
                w, h = int(w * room / h), room
            if (w, h) != im.size:
                im = im.resize((w, h), Image.LANCZOS)
            return im
        except OSError:
            return None

    def start_pollers(self):
        c = self.cfg

        def loop(fn, every, event=None):
            while not self.stopping:
                try:
                    fn()
                except Exception:
                    log.exception("poller failed")
                if event:
                    event.wait(every)
                    event.clear()
                else:
                    time.sleep(every)

        def sys_():
            self.data["system"] = info.system()

        def net():
            self.data["network"] = info.network()

        def comp():
            self.data["companion"] = info.companion(c)
            self.data["boot"] = info.boot_report(c["boot_report"])

        def latest():
            # Until Bitfocus answers (the panel often starts before the network),
            # try again every few minutes; after that, every latest_check_h hours.
            while not self.stopping:
                try:
                    v = info.latest_stable(c)
                except Exception:
                    log.exception("latest Companion version check failed")
                    v = None
                if v:
                    self.data["latest"] = v
                    time.sleep(c["latest_check_h"] * 3600)
                else:
                    log.info("latest Companion version: no answer from Bitfocus (this Pi: %s); "
                             "trying again in %s min", os.uname().machine, c["latest_retry_min"])
                    time.sleep(c["latest_retry_min"] * 60)

        def sync():
            self.data["sync"] = info.sync(E, self.fast_settings())

        for fn, every, ev in ((sys_, c["sys_check_s"], None), (net, c["net_check_s"], None),
                              (comp, c["companion_check_s"], None),
                              (sync, c["sync_check_s"], self.sync_now)):
            threading.Thread(target=loop, args=(fn, every, ev), daemon=True).start()
        threading.Thread(target=latest, daemon=True).start()

    def fast_settings(self):
        """Engine settings that give up quickly when Drive can't be reached."""
        return dataclasses.replace(self.s, rclone_flags=FAST_RCLONE)

    def update_available(self):
        v, latest = self.data["companion"].get("version"), self.data.get("latest")
        return bool(v and latest and info.version_tuple(latest) > info.version_tuple(v))

    def alerts(self):
        sy, ne, co, sn = (self.data[k] for k in ("system", "network", "companion", "sync"))
        out = set()
        if (sy.get("temp") or 0) >= self.cfg["temp_warn_c"] or sy.get("undervolt_now") or sy.get("throttled_now"):
            out.add("SYSTEM")
        if ne and (not ne.get("ip") or not ne.get("iface") or not ne.get("internet")):
            out.add("NETWORK")
        if co and (not co.get("running") or self.update_available()):
            out.add("COMPANION")
        if self.sync_problem():
            out.add("SYNC")
        return out

    def sync_problem(self):
        sn = self.data["sync"]
        if not sn:
            return None
        if sn.get("signed_in") is False:
            return "NOT SIGNED IN"
        if sn.get("offline"):
            return "DRIVE UNREACHABLE"
        if sn.get("holder") and not sn.get("mine"):
            return f"HELD BY {sn['holder'].upper()}"
        if sn.get("stranded"):
            return "LOCK WAS TAKEN OVER"
        err, errt = sn.get("last_error"), sn.get("last_error_time")
        if err and (not sn.get("last_save") or (errt or "") > sn.get("last_save")):
            return "LAST SYNC FAILED"
        return None

    # ---- drawing helpers -------------------------------------------------------
    def chamfer(self, d, box, cut, outline, fill=None, width=1):
        x0, y0, x1, y1 = box
        pts = [(x0 + cut, y0), (x1, y0), (x1, y1 - cut), (x1 - cut, y1), (x0, y1), (x0, y0 + cut)]
        d.polygon(pts, fill=fill, outline=outline, width=width)

    def readout(self, d, y, label, value, colour=None):
        """A label and value on one row; y is a design height (see Layout)."""
        C, L, W = self.C, self.L, self.W
        y = L.y(y)
        value = value if value not in (None, "") else "-"
        d.text((L.u(10), y), label, font=self.F.mono, fill=C["grey"])
        # a long value on a narrow (portrait) screen drops to the small font
        room = W - L.u(20) - self.F.mono.getlength(label + "  ")
        f = self.F.mono_b if self.F.mono_b.getlength(value) <= room else self.F.small
        d.text((W - L.u(10), y + (0 if f is self.F.mono_b else L.u(2))), value, font=f,
               fill=colour or C["cyan"], anchor="ra")
        d.line((L.u(10), y + L.u(15), W - L.u(10), y + L.u(15)), fill=C["rule"])

    def bar(self, d, y, label, frac, colour=None):
        C, L, W = self.C, self.L, self.W
        y = L.y(y)
        colour = colour or C["cyan"]
        x0, x1 = L.u(80), W - L.u(50)
        d.text((L.u(10), y), label, font=self.F.mono, fill=C["grey"])
        d.rectangle((x0, y + L.u(2), x1, y + L.u(11)), outline=C["cyan_dim"])
        if frac is not None:
            d.rectangle((x0 + 1, y + L.u(3), x0 + 1 + int((x1 - x0 - 2) * frac), y + L.u(10)), fill=colour)
        d.text((W - L.u(10), y), f"{int(frac * 100)}%" if frac is not None else "-",
               font=self.F.mono_b, fill=colour, anchor="ra")

    def button(self, d, box, bid, label, style="purple", enabled=True, current=False):
        C = self.C
        L, F = self.L, self.F
        cut = L.u(6)
        if self.confirm and self.confirm[0] == bid:
            self.chamfer(d, box, cut, C["red"], C["red_dim"])
            cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
            if F.btn.getlength("TAP AGAIN TO CONFIRM") < box[2] - box[0] - L.u(10):
                d.text((cx, cy), "TAP AGAIN TO CONFIRM", font=F.btn, fill=C["lavender"], anchor="mm")
            else:
                d.text((cx, cy - L.u(6)), "TAP AGAIN", font=F.btn_s, fill=C["lavender"], anchor="mm")
                d.text((cx, cy + L.u(6)), "TO CONFIRM", font=F.btn_s, fill=C["lavender"], anchor="mm")
        else:
            if current:
                outline, fill, text = C["green"], hexrgb("#0c2a14"), C["green"]
                enabled = False
            elif not enabled:
                outline, fill, text = C["faint"], C["bg"], C["faint"]
            elif style == "red":
                outline, fill, text = C["red"], C["red_dim"], C["red"]
            elif style == "cyan":
                outline, fill, text = C["cyan"], C["cyan_dim"], C["cyan"]
            else:
                outline, fill, text = C["purple"], C["purple_dim"], C["lavender"]
            self.chamfer(d, box, cut, outline, fill)
            f = F.btn if F.btn.getlength(label) < box[2] - box[0] - L.u(8) else F.btn_s
            d.text(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), label, font=f,
                   fill=text, anchor="mm")
        if enabled:
            self.hits.append((box, bid))

    def frame(self, title, alerts, flash_on, tabs=True):
        C, L, F, W, H = self.C, self.L, self.F, self.W, self.H
        u, tab_y = L.u, L.tab_y
        img = Image.new("RGB", (W, H), C["bg"])
        d = ImageDraw.Draw(img)
        for y in range(L.top, (tab_y - 2) if tabs else H, u(6)):
            d.line((0, y, W, y), fill=C["scan"])
        # header: title block, status pips, clock (the block shrinks on narrow screens)
        clock_w = F.mono_b.getlength("00:00")
        pip = u(11)
        pips_x = min(u(204), int(W - u(6) - clock_w - u(8) - pip * len(TABS)))
        hb = u(20)
        d.polygon([(0, 0), (pips_x - u(4), 0), (pips_x - u(4) - hb * 7 // 10, hb), (0, hb)], fill=C["purple_dim"])
        # the title block is the menu button: three bars, then the title
        bx, bw = u(6), u(10)
        for i in range(3):
            y = u(5) + i * u(4)
            d.rectangle((bx, y, bx + bw, y + max(1, u(2)) - 1), fill=C["lavender"])
        d.text((bx + bw + u(6), u(3)), f"BD-1 // {title}", font=F.head, fill=C["lavender"])
        # generous: the whole header and a little below, so a tap near the edge still counts
        self.hits.append(((0, 0, W, u(32)), "menu"))
        d.text((W - u(6), u(4)), time.strftime("%H:%M"), font=F.mono_b, fill=C["cyan"], anchor="ra")
        for i, t in enumerate(TABS):                       # one status pip per tab
            col = C["red"] if t in alerts else C["green"]
            d.rectangle((pips_x + i * pip, u(7), pips_x + i * pip + u(6), u(13)), fill=col)
        d.line((0, u(21), W, u(21)), fill=C["purple"])
        if not tabs:
            return img, d
        tw = W / len(TABS)
        tab_font = F.btn if max(F.btn.getlength(t) for t in TABS) < tw - u(8) else F.btn_s
        for i, name in enumerate(TABS):
            x0, x1 = int(i * tw) + 1, int((i + 1) * tw) - 2
            on, alarm = name == self.tab, name in alerts and flash_on
            if alarm:
                outline, fill, text = C["red"], C["red"] if on else C["red_dim"], C["lavender"]
            elif on:
                outline, fill, text = C["purple"], C["purple"], C["bg"]
            else:
                outline, fill, text = C["purple_dim"], C["bg"], C["purple"]
            self.chamfer(d, (x0, tab_y, x1, H - 2), u(7), outline, fill)
            d.text(((x0 + x1) // 2, (tab_y + H) // 2), name, font=tab_font, fill=text, anchor="mm")
            self.hits.append(((x0, tab_y, x1, H - 1), "tab:" + name))
        return img, d

    # ---- screens ---------------------------------------------------------------
    def draw_system(self, d):
        C, sy = self.C, self.data["system"]
        t = sy.get("temp")
        self.readout(d, 30, "CORE TEMP", f"{t:.1f} °C" if t is not None else None,
                     C["red"] if t and t >= self.cfg["temp_warn_c"] else None)
        if sy.get("undervolt_now"):
            power = ("UNDER-VOLTAGE", C["red"])
        elif sy.get("throttled_now"):
            power = ("THROTTLED", C["red"])
        elif sy.get("undervolt_since_boot"):
            power = ("LOW SINCE BOOT", C["amber"])
        elif sy:
            power = ("NOMINAL", C["green"])
        else:
            power = (None, None)
        self.readout(d, 48, "POWER", *power)
        self.readout(d, 66, "UPTIME", info.duration(sy.get("uptime")))
        self.bar(d, 86, "CPU LOAD", sy.get("cpu"))
        self.bar(d, 104, "MEMORY", sy.get("mem"))
        self.bar(d, 122, "STORAGE", sy.get("disk"), C["red"] if (sy.get("disk") or 0) > 0.9 else None)
        L, W = self.L, self.W
        y0, y1 = L.y(152), L.y(194)
        mid, gap = W // 2, L.u(8)
        self.button(d, (L.u(10), y0, mid - gap, y1), "restart", "RESTART")
        self.button(d, (mid + gap, y0, W - L.u(10), y1), "shutdown", "SHUTDOWN", "red")

    def draw_network(self, d):
        C, ne, L = self.C, self.data["network"], self.L
        d.text((L.u(10), L.y(30)), ne.get("ip") or "NO ADDRESS", font=self.F.big,
               fill=C["cyan"] if ne.get("ip") else C["red"])
        d.text((L.u(10), L.y(30) + L.u(20)), "PRIMARY ADDRESS", font=self.F.mono, fill=C["grey"])
        self.readout(d, 72, "HOSTNAME", ne.get("hostname"))
        if ne.get("link"):
            sp = ne.get("speed")
            speed = f" {sp // 1000} Gb" if sp and sp >= 1000 else f" {sp} Mb" if sp else ""
            link = ("ETHERNET" + speed) if ne["link"] == "ethernet" else "WI-FI"
            self.readout(d, 90, "LINK", link, C["green"])
        else:
            self.readout(d, 90, "LINK", "DOWN" if ne else None, C["red"])
        wifi = ne.get("wifi")
        self.readout(d, 108, "WI-FI", (wifi or "none").upper()[:20] if ne else None,
                     C["grey"] if wifi in (None, "off") else C["cyan"])
        self.readout(d, 126, "GATEWAY", ne.get("gateway"))
        for y, label, key in ((144, "INTERNET", "internet"), (162, "GOOGLE DRIVE", "drive")):
            ok = ne.get(key)
            self.readout(d, y, label, ("REACHABLE" if ok else "UNREACHABLE") if ne else None,
                         C["green"] if ok else C["red"])

    def draw_companion(self, d):
        C, co, ne, L, W = self.C, self.data["companion"], self.data["network"], self.L, self.W
        u = L.u
        running = co.get("running")
        col = C["green"] if running else C["red"]
        y = L.y(29)
        d.ellipse((u(10), y + u(3), u(22), y + u(15)), fill=col if co else C["faint"])
        d.text((u(30), y), ("RUNNING" if running else "STOPPED") if co else "CHECKING", font=self.F.big,
               fill=col if co else C["grey"])
        v, latest, upd = co.get("version"), self.data.get("latest"), self.update_available()
        self.readout(d, 56, "INSTALLED", f"v{v}" if v else None)
        self.readout(d, 74, "LATEST STABLE", f"v{latest}" if latest else None, C["purple"] if upd else None)
        ip = ne.get("ip")
        self.readout(d, 92, "ADMIN PAGE", f"{ip}:{self.cfg['companion_port']}" if ip else None)
        self.readout(d, 110, "RUNNING FOR", info.duration(co.get("running_for")) if running else None)
        box = (u(10), L.y(156), W - u(10), L.y(196))
        if upd:
            y = L.y(132)
            self.chamfer(d, (u(10), y, W - u(10), y + u(16)), u(4), C["purple_dim"], hexrgb("#220046"))
            d.text((W // 2, y + u(8)), "UPDATE AVAILABLE", font=self.F.btn, fill=C["lavender"], anchor="mm")
            self.button(d, box, "update_companion", f"UPDATE TO v{latest} (STABLE)")
        else:
            self.button(d, box, "update_companion",
                        "UP TO DATE" if latest else "LATEST VERSION UNKNOWN", enabled=False)

    def draw_sync(self, d):
        C, sn, L, W = self.C, self.data["sync"], self.L, self.W
        u = L.u
        d.text((u(10), L.y(27)), "CONFIG HELD BY", font=self.F.mono, fill=C["grey"])
        holder = sn.get("holder")
        if not sn:
            who, col = "CHECKING", C["grey"]
        elif sn.get("offline") or sn.get("signed_in") is False:
            who, col = "?", C["red"]
        else:
            who = holder.upper() if holder else "NOBODY"
            col = C["green"] if sn.get("mine") else C["amber"] if holder else C["grey"]
        d.text((W - u(10), L.y(25)), who, font=self.F.big, fill=col, anchor="ra")
        self.readout(d, 44, "LAST SAVE", info.ago(sn.get("last_save")) or ("never" if sn else None))
        if sn.get("signed_in") is False:
            drive = ("NOT SIGNED IN", C["red"])
        elif sn.get("offline"):
            drive = ("UNREACHABLE", C["red"])
        elif sn:
            drive = ("SIGNED IN", C["green"])
        else:
            drive = (None, None)
        self.readout(d, 60, "DRIVE", *drive)
        self.readout(d, 76, "REBEL SYNC", version_label().upper())
        problem = self.sync_problem()
        if not self.draw_progress(d, L.y(88)) and problem:
            d.text((W // 2, L.y(96)), problem, font=self.F.btn, fill=C["red"], anchor="mm")
        buttons = (("take", "TAKE", "purple"), ("save", "SAVE NOW", "cyan"),
                   ("return", "RETURN", "purple"), ("status", "STATUS", "cyan"),
                   ("log", "LOG", "cyan"), ("update_cs", "UPDATE RS", "cyan"))
        cols = 2 if L.portrait else 3
        rows = -(-len(buttons) // cols)
        gap = u(6)
        top, bottom = L.y(108), L.y(182)                   # design: two rows, 108-182
        bw = (W - u(20) - gap * (cols - 1)) / cols
        bh = (bottom - top - gap * (rows - 1)) / rows
        for i, (bid, label, style) in enumerate(buttons):
            x = u(10) + (i % cols) * (bw + gap)
            y = top + (i // cols) * (bh + gap)
            self.button(d, (int(x), int(y), int(x + bw), int(y + bh)), bid, label, style)

    def draw_menu(self, d):
        """What the panel shows. Tap the title block to come back here."""
        L, W, H, u = self.L, self.W, self.H, self.L.u
        entries = [("page:sync", "REBEL SYNC", "purple", True),
                   ("page:rebeldeck", "REBELDECK", "purple", True),
                   ("page:screen", "SCREEN", "cyan", True)]
        top, bottom, gap = L.top + u(6), H - u(10), u(8)
        bh = (bottom - top - gap * (len(entries) - 1)) / len(entries)
        for i, (bid, label, style, on) in enumerate(entries):
            y = top + i * (bh + gap)
            self.button(d, (u(10), int(y), W - u(10), int(y + bh)), bid, label, style, enabled=on)

    def deck_area(self):
        """Where the deck's buttons go: below the header's tap area, to the bottom."""
        u = self.L.u
        return u(6), u(34), self.W - u(6), self.H - u(6)

    def draw_deck(self, img, d):
        """A RebelDeck deck (see deckview.py)."""
        C, F, u = self.C, self.F, self.L.u
        x0, y0, x1, y1 = self.deck_area()
        dv = self.deck
        if dv is None:
            return
        dv.show(True, deck_fit(x1 - x0, y1 - y0, self.L.k))
        with dv.lock:
            cols, rows, keys = dv.cols, dv.rows, dict(dv.keys)
        if not cols or dv.status in ("not running", "not installed", "no deck", "starting") and not keys:
            lines = {"not installed": ["REBELDECK ISN'T INSTALLED", "github.com/nisted/RebelDeck",
                                       "sudo rebeldeck/pi/install.sh --headless"],
                     "not running": ["REBELDECK ISN'T RUNNING", "sudo systemctl start rebeldeck"],
                     "no deck": ["NO DECK CALLED", f'"{dv.problem}"', "CHECK rebeldeck_deck IN panel.json"]}.get(
                dv.status, ["LOOKING FOR REBELDECK..."])
            cy = (y0 + y1) // 2 - u(12) * (len(lines) - 1) // 2
            for i, line in enumerate(lines):
                d.text((self.W // 2, cy + i * u(24)), line, font=F.btn if i == 0 else F.mono,
                       fill=C["lavender"] if i == 0 else C["grey"], anchor="mm")
            return
        gap = u(4)
        side = int(min((x1 - x0 - gap * (cols - 1)) / cols, (y1 - y0 - gap * (rows - 1)) / rows))
        ox = x0 + (x1 - x0 - (side * cols + gap * (cols - 1))) // 2
        oy = y0 + (y1 - y0 - (side * rows + gap * (rows - 1))) // 2
        for i in range(cols * rows):
            bx, by = ox + (i % cols) * (side + gap), oy + (i // cols) * (side + gap)
            box = (bx, by, bx + side - 1, by + side - 1)
            k = keys.get(i)
            pic = dv.image(i, (side, side)) if k else None
            if pic is not None:
                img.paste(pic, (bx, by))
            else:
                d.rectangle(box, fill=k["color"] if k else C["bg"], outline=C["rule"])
                if k and k["text"]:
                    d.multiline_text(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), k["text"][:24],
                                     font=F.btn_s, fill=k["text_color"], anchor="mm", align="center")
            if dv.held == i:
                d.rectangle(box, outline=C["lavender"], width=max(1, u(2)))
            self.hits.append((box, f"key:{i}"))
        if dv.status != "connected":
            msg = "WAITING FOR COMPANION" if dv.status in ("connecting", "disconnected", "refused") else \
                "LOST REBELDECK" if dv.status == "lost" else dv.status.upper()
            w = F.btn.getlength(msg) + u(20)
            cx, cy = self.W // 2, (y0 + y1) // 2
            d.rectangle((cx - w // 2, cy - u(14), cx + w // 2, cy + u(14)), fill=C["bg"], outline=C["red"])
            d.text((cx, cy), msg, font=F.btn, fill=C["red"], anchor="mm")

    def draw_screen(self, d):
        """Screen settings: rotation and touch calibration."""
        from hw import screen_name, screen_size
        C, L, W, H, u, F = self.C, self.L, self.W, self.H, self.L.u, self.F
        rot = int(self.cfg.get("rotation", 0)) % 360
        w, h = screen_size({**self.cfg, "rotation": 0})
        y = L.top + u(4)
        for label, value in (("SCREEN", f'{screen_name(self.cfg)}"  {w}x{h}'),
                             ("ROTATION", ROTATION_NAMES.get(rot, str(rot)))):
            d.text((u(10), y), label, font=F.mono, fill=C["grey"])
            d.text((W - u(10), y), value, font=F.mono_b, fill=C["cyan"], anchor="ra")
            d.line((u(10), y + u(15), W - u(10), y + u(15)), fill=C["rule"])
            y += u(19)
        # four rotation buttons, two by two, then calibrate across the bottom
        gap = u(8)
        cal_h = u(38)
        top, bottom = y + u(6), H - u(10) - cal_h - gap
        bw, bh = (W - u(20) - gap) / 2, (bottom - top - gap) / 2
        for i, r in enumerate((0, 90, 180, 270)):
            x = u(10) + (i % 2) * (bw + gap)
            yy = top + (i // 2) * (bh + gap)
            self.button(d, (int(x), int(yy), int(x + bw), int(yy + bh)), f"rot:{r}",
                        ROTATION_NAMES[r], "purple", current=(r == rot))
        self.button(d, (u(10), H - u(10) - cal_h, W - u(10), H - u(10)), "calibrate", "CALIBRATE TOUCH", "cyan")

    def set_rotation(self, rot):
        """Turn the panel now and remember it in panel.json."""
        self.cfg["rotation"] = rot
        if self.live:
            save_setting("rotation", rot)
        self.hw.set_rotation(rot)
        self.relayout()
        log.info("rotation set to %s on the panel", rot)

    def do_calibrate(self):
        """Hand the screen to calibrate.py's steps, then take it back."""
        import calibrate
        self.calibrating = True
        try:
            time.sleep(0.3)                 # let the main loop stand back
            calibrate.run(self.hw, self.cfg, timeout=60, test_s=10)
            self.message("CALIBRATED", "Touch calibration saved for this screen.")
        except calibrate.TimedOut:
            self.message("NOT CALIBRATED", "No tap for a minute, so nothing was changed.",
                         colour=self.C["amber"])
        finally:
            self.hw.frame = None
            self.calibrating = False

    def draw_progress(self, d, y, x0=None, x1=None):
        """A take / save / return running here or in the tray app or timer:
        a bar with step and %. y is in pixels. True if one is running."""
        pr = E.read_progress(PROGRESS_FILE)
        if not pr.get("active"):
            return False
        C, u, F = self.C, self.L.u, self.F
        x0 = u(10) if x0 is None else x0
        x1 = self.W - u(10) if x1 is None else x1
        pct = pr.get("pct")
        steps = pr.get("steps") or 0
        frac = pct / 100 if pct is not None else ((pr.get("step", 1) - 1) / steps if steps else 0)
        label = f"{pr.get('action', '').upper()} {pr.get('step', '')}/{steps}" if steps else pr.get("action", "").upper()
        d.text((x0, y), label, font=F.mono, fill=C["cyan"])
        if pct is not None:
            d.text((x1, y), f"{pct}%", font=F.mono, fill=C["cyan"], anchor="ra")
        d.rectangle((x0, y + u(12), x1, y + u(17)), fill=hexrgb("#0e0618"), outline=C["purple_dim"])
        d.rectangle((x0, y + u(12), x0 + int((x1 - x0) * max(0, min(frac, 1))), y + u(17)), fill=C["purple"])
        return True

    def draw_overlay(self, d):
        C, o, L, F, W = self.C, self.overlay, self.L, self.F, self.W
        u = L.u
        box = (u(8), u(26), W - u(8), L.tab_y - u(4))
        self.chamfer(d, box, u(8), C["purple"], hexrgb("#0e0618"), max(2, u(2)))
        title = o.get("title", "")
        if o["kind"] == "busy":
            title += "." * (int(time.time() * 2) % 4)
        d.text((u(18), u(32)), title, font=F.head, fill=C["lavender"])
        d.line((u(18), u(48), W - u(18), u(48)), fill=C["purple_dim"])
        if o["kind"] == "busy":
            self.draw_progress(d, box[3] - u(30), u(18), W - u(18))
        small = o.get("small")
        f = F.small if small else F.msg
        y = u(54)
        bh = u(28) if not L.portrait else u(36)
        for line in wrap(o.get("lines", []), self.wrap_chars(small)):
            if y > box[3] - (bh + u(12) if o.get("buttons") else u(14)):
                break
            d.text((u(18), y), line, font=f, fill=o.get("colour") or C["cyan"])
            y += u(11) if small else u(13)
        buttons = o.get("buttons") or []
        if buttons:
            gap = u(8)
            bw = (W - u(36) - gap * (len(buttons) - 1)) // len(buttons)
            for i, (label, bid, style) in enumerate(buttons):
                x = u(18) + i * (bw + gap)
                self.button(d, (x, box[3] - u(8) - bh, x + bw, box[3] - u(8)), bid, label, style)

    def wrap_chars(self, small=False):
        """How many characters of message text fit across the overlay."""
        f = self.F.small if small else self.F.msg
        return max(20, int((self.W - self.L.u(36)) / f.getlength("M")))

    def startup_y(self):
        """Top of the start-up list (the rule above its title)."""
        return self.H - self.L.u(106)

    def draw_startup(self):
        C, L, F, W, H = self.C, self.L, self.F, self.W, self.H
        u = L.u
        img = Image.new("RGB", (W, H), C["bg"])
        d = ImageDraw.Draw(img)
        for y in range(0, H, u(6)):
            d.line((0, y, W, y), fill=C["scan"])
        top = self.startup_y()
        if self.logo:
            # centred in the space above the list
            img.paste(self.logo, ((W - self.logo.width) // 2, max(u(6), (top - self.logo.height) // 2)), self.logo)
        d.line((u(10), top, W - u(10), top), fill=C["purple_dim"])
        d.text((u(10), top + u(4)), "BD-1 // SYSTEM START", font=F.head, fill=C["lavender"])
        steps = self.startup_steps()
        y = top + u(22)
        dot_w = F.mono.getlength(".")
        for label, value, colour in steps:
            d.text((u(10), y), label, font=F.mono, fill=C["grey"])
            x = u(10) + F.mono.getlength(label + " ")
            room = W - u(10) - F.mono_b.getlength(value + " ") - x
            d.text((x, y), "." * max(0, int(room / dot_w)), font=F.mono, fill=hexrgb("#32323c"))
            d.text((W - u(10), y), value, font=F.mono_b, fill=colour, anchor="ra")
            y += u(13)
        done = sum(1 for _, _, c in steps if c != C["faint"] and c != C["purple"])
        d.rectangle((u(10), H - u(18), W - u(10), H - u(8)), outline=C["purple_dim"])
        d.rectangle((u(12), H - u(16), u(12) + int((W - u(24)) * done / len(steps)), H - u(10)), fill=C["purple"])
        return img, done == len(steps)

    def startup_steps(self):
        C = self.C
        ne, co, boot = self.data["network"], self.data["companion"], self.data.get("boot")
        waiting = ("-", C["faint"])
        net = (ne["ip"], C["cyan"]) if ne.get("ip") else ("NO NETWORK", C["amber"]) if ne else ("WAITING", C["purple"])
        if boot:
            r = boot.get("result")
            drive = {"not_signed_in": ("NOT SIGNED IN", C["red"]),
                     "offline": ("OFFLINE", C["amber"])}.get(r, ("SIGNED IN", C["green"]))
            holder = (boot.get("holder") or "?").upper()
            config = {"taken": (f"TAKEN BY {holder}", C["green"]),
                      "already": (f"{holder} HAS IT", C["green"]),
                      "held": (f"HELD BY {holder}", C["amber"]),
                      "needs_update": ("NEEDS NEWER COMPANION", C["amber"])}.get(r, ("LOCAL COPY", C["amber"]))
        elif time.monotonic() - self.start_time > 100:
            drive, config = ("NO ANSWER", C["amber"]), ("LOCAL COPY", C["amber"])
        else:
            drive, config = ("CONNECTING", C["purple"]), waiting
        if co.get("running"):
            comp = (f"v{co.get('version') or '?'} RUNNING", C["green"])
        elif co and time.monotonic() - self.start_time > self.cfg["boot_screen_max_s"] - 5:
            comp = ("NOT RUNNING", C["red"])
        else:
            comp = ("STARTING", C["purple"]) if boot else waiting
        return [("DISPLAY", "ONLINE", C["green"]), ("NETWORK",) + net, ("GOOGLE DRIVE",) + drive,
                ("CONFIG",) + config, ("COMPANION",) + comp]

    def render(self):
        self.hits = []
        if not self.started:
            img, done = self.draw_startup()
            now = time.monotonic()
            if done and self.boot_done_at is None:
                self.boot_done_at = now
            if (self.boot_done_at and now - self.boot_done_at > 2) or now - self.start_time > self.cfg["boot_screen_max_s"] \
                    or (self.quick_start and now - self.start_time > 3):
                self.started = True
            return img
        if self.confirm and time.monotonic() > self.confirm[1]:
            self.confirm = None
        flash_on = int(time.time() / self.cfg["flash_s"]) % 2 == 0
        title = {"menu": "MENU", "screen": "SCREEN", "rebeldeck": "REBELDECK"}.get(self.page, self.tab)
        img, d = self.frame(title, self.alerts(), flash_on, tabs=self.page == "sync")
        if self.overlay:
            if self.overlay["kind"] == "busy":
                self.hits = []              # nothing to tap while a job runs
            self.draw_overlay(d)
        elif self.page == "menu":
            self.draw_menu(d)
        elif self.page == "screen":
            self.draw_screen(d)
        elif self.page == "rebeldeck":
            self.draw_deck(img, d)
        else:
            {"SYSTEM": self.draw_system, "NETWORK": self.draw_network,
             "COMPANION": self.draw_companion, "SYNC": self.draw_sync}[self.tab](d)
        return img

    # ---- touch -----------------------------------------------------------------
    def on_tap(self, x, y):
        if not self.started:
            return
        hit = None
        # overlay buttons are drawn last, so search from the end
        for (x0, y0, x1, y1), bid in reversed(self.hits):
            if x0 <= x <= x1 and y0 <= y <= y1:
                hit = bid
                break
        if self.overlay and self.overlay["kind"] == "busy":
            return
        if hit is None:
            self.confirm = None
            return
        if hit == "menu":
            if self.deck:
                self.deck.show(False)
            self.page = "menu"
            self.overlay = None
            self.confirm = None
            return
        if hit.startswith("key:"):
            if self.deck:
                self.deck.press(int(hit[4:]), True)
            return
        if hit.startswith("page:"):
            self.page = hit[5:]
            self.confirm = None
            return
        if hit.startswith("tab:"):
            self.tab = hit[4:]
            self.overlay = None
            self.confirm = None
            return
        if hit == "close":
            self.overlay = None
            self.confirm = None
            return
        needs = hit in NEEDS_CONFIRM or hit.startswith("rot:")
        if needs and not (self.confirm and self.confirm[0] == hit):
            self.confirm = (hit, time.monotonic() + self.cfg["confirm_s"])
            return
        self.confirm = None
        if hit.startswith("rot:"):
            self.set_rotation(int(hit[4:]))
            return
        self.start_action(hit)

    # ---- actions ---------------------------------------------------------------
    def busy(self, title, *lines):
        self.overlay = {"kind": "busy", "title": title, "lines": list(lines)}

    def message(self, title, *lines, buttons=None, colour=None, small=False):
        self.overlay = {"kind": "message", "title": title, "lines": list(lines),
                        "buttons": buttons or [("OK", "close", "purple")], "colour": colour, "small": small}

    def start_action(self, bid):
        if self.worker and self.worker.is_alive():
            return
        fn = getattr(self, "do_" + bid, None)
        if not fn:
            return

        def run():
            try:
                fn()
            except E.SyncError as e:
                log.info("%s: %s", bid, e)
                self.message("NOT DONE", str(e), colour=self.C["amber"])
            except Exception as e:
                log.exception("%s failed", bid)
                self.message("SOMETHING WENT WRONG", f"{type(e).__name__}: {e}", colour=self.C["red"])
            finally:
                self.sync_now.set()

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _return_first(self, what):
        s = self.fast_settings()
        try:
            h = E.lock_holder(s)
            if E.holder_name(h) == s.rig or E.was_holding(s):
                self.busy(what, "Returning the config to Google Drive first.")
                log.info("%s: %s", what, E.return_(s))
        except E.SyncError as e:
            log.info("%s: couldn't return the config first: %s", what, e)
            self.busy(what, "Couldn't reach Google Drive.", "Carrying on: local changes are kept on BD-1.")
            time.sleep(3)

    def do_restart(self):
        self.busy("RESTARTING", "Checking the config.")
        self._return_first("RESTARTING")
        self.busy("RESTARTING", "BD-1 is restarting.")
        log.info("restart requested on the panel")
        subprocess.run(["sudo", "systemctl", "reboot"])

    def do_shutdown(self):
        self.busy("SHUTTING DOWN", "Checking the config.")
        self._return_first("SHUTTING DOWN")
        self.busy("SHUTTING DOWN", "BD-1 is shutting down.")
        log.info("shutdown requested on the panel")
        subprocess.run(["sudo", "systemctl", "poweroff"])

    def do_update_companion(self):
        latest = self.data.get("latest")
        self.busy("UPDATING COMPANION", "Saving the config to Google Drive first.")
        s = self.fast_settings()
        try:
            if E.holder_name(E.lock_holder(s)) == s.rig:
                log.info("before Companion update: %s", E.save(s))
        except E.SyncError as e:
            log.info("before Companion update, save failed: %s", e)
        self.busy("UPDATING COMPANION", f"Installing v{latest}. This takes a few minutes;",
                  "Companion is stopped until it's done.")
        log.info("Companion update to latest stable (v%s) started", latest)
        try:
            r = subprocess.run(["sudo", "companion-update", "stable"], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=1800)
            out = (r.stdout + r.stderr).strip().splitlines()
            ok = r.returncode == 0
        except subprocess.TimeoutExpired:
            out, ok = ["Timed out after 30 minutes."], False
        for line in out[-40:]:
            log.info("companion-update: %s", line)
        self.data["companion"] = info.companion(self.cfg)
        if ok:
            self.message("COMPANION UPDATED", f"Now running v{self.data['companion'].get('version')}.",
                         colour=self.C["green"])
        else:
            self.message("UPDATE FAILED", *out[-6:], colour=self.C["red"], small=True)

    def do_take(self):
        s = self.fast_settings()
        self.busy("TAKE", "Checking who has the config.")
        h = E.lock_holder(s)
        if h and E.holder_name(h) != s.rig:
            age = E.lock_age_hours(s)
            if age < s.stale_hours:
                self.message("CAN'T TAKE IT", f"{E.holder_name(h).upper()} has the config "
                             f"(last seen {age:.1f} hours ago).", "Return it on that rig first.",
                             colour=self.C["amber"])
                return
            self.message("TAKE OVER?", f"{E.holder_name(h).upper()} hasn't been seen for {age:.0f} hours.",
                         "Anything it hasn't saved stays on it.",
                         buttons=[("CANCEL", "close", "cyan"), ("TAKE OVER", "takeover", "red")],
                         colour=self.C["amber"])
            return
        self.do_takeover()

    def do_takeover(self):
        self.busy("TAKE", "Downloading the config; Companion restarts.")
        msg = E.take(self.s, lambda m: True)
        self.message("TAKEN", msg, colour=self.C["green"])

    def do_save(self):
        self.busy("SAVE NOW", "Uploading to Google Drive.")
        self.message("SAVED", E.save(self.s), colour=self.C["green"])

    def do_return(self):
        self.busy("RETURN", "Uploading and unlocking.")
        self.message("RETURNED", E.return_(self.s), colour=self.C["green"])

    def do_status(self):
        self.busy("STATUS", "Asking Google Drive.")
        sn = info.sync(E, self.fast_settings())
        self.data["sync"] = sn
        lines = []
        if sn.get("offline"):
            lines.append(f"Drive: {sn['offline']}")
        elif sn.get("signed_in") is False:
            lines.append("Not signed in to Google Drive.")
        else:
            if sn.get("holder"):
                lines.append(f"Held by {sn['holder']} (seen {sn.get('age_hours') or 0:.1f} h ago).")
            else:
                lines.append("Nobody has the config.")
            try:
                lv = E.local_version(self.s)
            except E.SyncError:
                lv = None
            lines.append(f"Drive has Companion {E.vtext(sn.get('drive_version'))} config; "
                         f"BD-1 has {E.vtext(lv)}.")
            if sn.get("stranded"):
                lines.append("BD-1's lock was taken over; its next save goes to 'stranded'.")
        lines.append(f"Last save: {info.ago(sn.get('last_save')) or 'never'}")
        if sn.get("last_error"):
            lines.append(f"Last problem: {sn['last_error']}")
        boot = self.data.get("boot")
        if boot:
            when = str(boot.get("time", ""))[11:16]
            lines.append(f"At start-up ({when}): {boot.get('message') or boot.get('result')}")
        self.message("STATUS", *lines, small=True)

    def do_log(self):
        lines = []
        out = info.sh(["sudo", "journalctl", "-u", "rebel-sync-panel", "-u", "rebel-sync-boot",
                       "-u", "rebel-sync-save", "-n", "40", "--no-pager", "-o", "short"])
        for line in (out or "").splitlines():
            if "pam_unix" in line or "COMMAND=" in line:     # sudo's own bookkeeping
                continue
            parts = line.split(None, 4)                 # 'Oct 10 23:01:02 bd-1 unit[pid]: message'
            if len(parts) == 5:
                msg = parts[4].split(": ", 1)[-1]
                lines.append(f"{parts[2][:5]} {msg}")
        self.message("LOG", *(lines[-12:] or ["Nothing logged yet."]), small=True)

    def do_update_cs(self):
        token_file = os.path.expanduser(self.cfg["github_token_file"])
        if not os.path.exists(token_file):
            self.message("NO GITHUB KEY", f"Put a read-only GitHub key in {self.cfg['github_token_file']}",
                         "on BD-1, then try again.", colour=self.C["amber"])
            return
        with open(token_file) as f:
            token = f.read().strip()
        self.busy("UPDATE RS", "Checking GitHub for a newer build.")
        hdr = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "User-Agent": "rebel-sync-panel"}
        rel, err = None, None
        # the repo under its new name, else its old one (before the rename)
        for repo in dict.fromkeys((self.cfg["github_repo"], "nisted/rebel-sync", "nisted/companion-sync")):
            api = f"https://api.github.com/repos/{repo}/releases/latest"
            try:
                with urllib.request.urlopen(urllib.request.Request(api, headers=hdr), timeout=20) as r:
                    rel = json.loads(r.read().decode())
                break
            except urllib.error.HTTPError as e:
                err = e
        if rel is None:
            raise err
        new = rel.get("tag_name", "").replace("build-", "")
        if new == self.build:
            self.message("UP TO DATE", f"BD-1 already has {version_label()}.", colour=self.C["green"])
            return
        asset = next((a for a in rel.get("assets", []) if a.get("name") == self.cfg["pi_asset"]), None)
        if not asset:
            self.message("NOT DONE", f"Build {new} has no Pi package.", colour=self.C["amber"])
            return
        self.busy("UPDATE RS", f"Downloading build {new}.")
        tmp = tempfile.mkdtemp(prefix="rebel-sync-update-")
        req = urllib.request.Request(asset["url"], headers=dict(hdr, Accept="application/octet-stream"))
        tgz = os.path.join(tmp, self.cfg["pi_asset"])
        with urllib.request.urlopen(req, timeout=120) as r, open(tgz, "wb") as f:
            f.write(r.read())
        with tarfile.open(tgz) as t:
            t.extractall(tmp, filter="data")
        installer = os.path.join(tmp, "rebel-sync", "pi", "install.sh")
        if not os.path.exists(installer):
            self.message("NOT DONE", "The download doesn't contain the installer.", colour=self.C["red"])
            return
        log.info("Rebel Sync update: build %s -> %s", self.build, new)
        self.busy("UPDATE RS", f"Installing build {new}.", "The panel restarts when it's done.")
        # run outside this service, so restarting the panel doesn't stop the install
        subprocess.run(["sudo", "systemd-run", "--unit", f"rebel-sync-update-{int(time.time())}",
                        "--collect", "/bin/bash", installer, "--from-update"])

    # ---- main loop -------------------------------------------------------------
    def run(self):
        self.start_pollers()
        next_frame = 0.0
        while not self.stopping:
            if self.calibrating:
                time.sleep(0.05)
                next_frame = 0.0
                continue
            tap = self.hw.tap()
            now = time.monotonic()
            if tap:
                self.on_tap(*tap)
                next_frame = 0.0
            if self.deck and self.page == "rebeldeck":
                if self.deck.held is not None and not self.hw.pressed():
                    self.deck.press(None, False)            # finger lifted: let the button go
                    next_frame = 0.0
                if self.deck.changed.is_set():
                    self.deck.changed.clear()
                    next_frame = 0.0
            if now >= next_frame:
                self.hw.show(self.render())
                next_frame = now + self.cfg["frame_s"]
            time.sleep(0.02)

    def goodbye(self):
        C = self.C
        W, H, u = self.W, self.H, self.L.u
        img = Image.new("RGB", (W, H), C["bg"])
        d = ImageDraw.Draw(img)
        if self.logo:
            img.paste(self.logo, ((W - self.logo.width) // 2, max(u(6), (self.startup_y() - self.logo.height) // 2)),
                      self.logo)
        try:      # is-system-running exits non-zero unless 'running', so read its output regardless
            state = subprocess.run(["systemctl", "is-system-running"], capture_output=True,
                                   text=True, timeout=5).stdout.strip()
        except Exception:
            state = ""
        text = "PANEL RESTARTING" if state in ("running", "degraded") else "BD-1 // OFFLINE"
        d.text((W // 2, H - u(50)), text, font=self.F.head, fill=C["lavender"], anchor="mm")
        self.hw.frame = None
        self.hw.show(img)


def draw_picked(cfg, size, name):
    """Confirmation after a tap on the chooser."""
    w, h = size
    L = Layout(w, h)
    C = {k: hexrgb(v) for k, v in cfg["colours"].items()}
    img = Image.new("RGB", size, C["bg"])
    d = ImageDraw.Draw(img)
    d.text((w // 2, h // 2 - L.u(14)), f'{name}" SCREEN', font=Fonts(L.k * 1.8).head, fill=C["green"], anchor="mm")
    d.text((w // 2, h // 2 + L.u(18)), "SELECTED", font=Fonts(L.k).btn, fill=C["green"], anchor="mm")
    return img


def draw_choice(cfg, size, name, current, left):
    """The chooser's picture for one screen type."""
    w, h = size
    L = Layout(w, h)
    F = Fonts(L.k)
    u = L.u
    C = {k: hexrgb(v) for k, v in cfg["colours"].items()}
    img = Image.new("RGB", size, C["bg"])
    d = ImageDraw.Draw(img)
    for y in range(0, h, u(6)):
        d.line((0, y, w, y), fill=C["scan"])
    d.text((w // 2, h // 2 - u(36)), f'{name}" SCREEN', font=Fonts(L.k * 1.8).head, fill=C["lavender"], anchor="mm")
    d.text((w // 2, h // 2 + u(4)), "TAP ANYWHERE TO USE THIS SCREEN", font=F.btn, fill=C["purple"], anchor="mm")
    note = (f'CARRYING ON WITH THE {current}" IN {left} S' if left > 0 else f'CARRYING ON WITH THE {current}"')
    d.text((w // 2, h // 2 + u(26)), note, font=F.mono, fill=C["grey"], anchor="mm")
    return img


def choose_screen(cfg):
    """At power-on, offer each screen type in turn: whichever screen is fitted
    shows its own turn clearly (the other turn looks blank or scrambled), and a
    tap during it picks that type. No tap: the current setting. Returns the
    screen name to use."""
    import hw as HW
    total, turn = cfg.get("screen_choice_s", 0), cfg.get("screen_choice_turn_s", 3)
    current = HW.screen_name(cfg)
    if not total:
        return current
    order = [current] + [n for n in HW.SCREENS if n != current]
    end = time.monotonic() + total
    i = 0
    while time.monotonic() < end:
        name = order[i % len(order)]
        i += 1
        try:
            dev = HW.Hardware({**cfg, "screen": name})
        except Exception:
            log.exception("screen chooser: couldn't start the %s\" driver", name)
            continue
        try:
            started = time.monotonic()
            while time.monotonic() - started < turn and time.monotonic() < end:
                left = max(0, int(end - time.monotonic()))
                dev.frame = None
                dev.show(draw_choice(cfg, (dev.W, dev.H), name, current, left))
                # ignore a tap in the first moments of a turn: it may have been
                # meant for the turn before
                t0 = time.monotonic()
                while time.monotonic() - t0 < 1.0:
                    if time.monotonic() - started > 0.7 and dev.pressed():
                        log.info("screen chooser: %s\" picked", name)
                        dev.frame = None
                        dev.show(draw_picked(cfg, (dev.W, dev.H), name))
                        time.sleep(1.5)
                        let_go = time.monotonic() + 3      # never wait for ever on a stuck touch
                        while dev.pressed() and time.monotonic() < let_go:
                            time.sleep(0.02)
                        return name
                    time.sleep(0.02)
        finally:
            dev.close()
    return current


def save_setting(key, value):
    """Change one setting in panel.json, keeping everything else."""
    try:
        with open(SETTINGS_FILE) as f:
            mine = json.load(f)
    except (OSError, ValueError):
        mine = {}
    mine[key] = value
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(mine, f, indent=2)


def wrap(lines, width=44):
    out = []
    for line in lines:
        out += textwrap.wrap(str(line), width) or [""]
    return out


def main(argv):
    cfg = load_settings()
    if argv[:1] == ["--preview"]:
        from hw import screen_size
        from preview import render_samples
        out = argv[1] if len(argv) > 1 else "preview"
        for screen in ("2.4", "3.5"):
            for rot, shape in ((0, "landscape"), (90, "portrait")):
                size = screen_size({**cfg, "screen": screen, "rotation": rot})
                render_samples(Panel, cfg, os.path.join(out, f"{screen}-{shape}"), size)
        return 0
    log_file = os.path.expanduser(cfg["log_file"])
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
                        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)])
    from hw import Hardware
    # the screen chooser only at power-on, not when just the panel restarts
    up = info.system().get("uptime")
    if cfg.get("screen_choice_s") and up is not None and up < 300:
        picked = choose_screen(cfg)
        if picked != str(cfg.get("screen", "2.4")):
            save_setting("screen", picked)
            cfg["screen"] = picked
            log.info("screen set to %s\" at start-up", picked)
    hw = Hardware(cfg)
    s = E.default_settings(log=log.info)
    panel = Panel(cfg, hw, s)

    def stop(*_):
        panel.stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("panel started (build %s)", panel.build)
    try:
        panel.run()
    finally:
        try:
            panel.goodbye()
        finally:
            hw.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
