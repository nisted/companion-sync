#!/usr/bin/env python3
"""BD-1 panel: Companion Sync status and controls on the 2.4" touchscreen.

  python3 panel.py                  run on the Pi (systemd: companion-panel.service)
  python3 panel.py --preview DIR    render sample screens to PNG files in DIR

Settings: ~/.config/companion-sync/panel.json overrides any of DEFAULTS below.
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
import urllib.request

from PIL import Image, ImageDraw, ImageFont, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # engine.py lives one level up
import engine as E                                  # noqa: E402
import info                                         # noqa: E402

DEFAULTS = {
    # wiring (SHCHV 2.4" RPi Display)
    "spi_bus": 0, "spi_lcd_cs": 0, "spi_touch_cs": 1,
    "spi_lcd_hz": 32_000_000, "spi_touch_hz": 1_000_000,
    "pin_dc": 22, "pin_rst": 27, "pin_touch_irq": 17,
    "lcd_madctl": 0x28,                       # landscape
    # touch calibration (raw XPT2046 readings at the screen edges)
    # (measured on BD-1's screen; run calibrate.py to redo)
    "touch_x_min": 85, "touch_x_max": 3747, "touch_y_min": 348, "touch_y_max": 3805,
    "touch_swap_xy": True, "touch_invert_x": False, "touch_invert_y": True,
    "touch_release_s": 0.15,
    # behaviour
    "confirm_s": 4, "flash_s": 0.5, "frame_s": 0.25,
    "boot_screen_max_s": 150, "temp_warn_c": 75,
    "sys_check_s": 2, "net_check_s": 15, "companion_check_s": 5,
    "sync_check_s": 120, "latest_check_h": 6,
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
    "boot_report": "/run/companion-sync/boot.json",
    "log_file": "~/.local/state/companion-sync/panel.log",
    "github_repo": "nisted/companion-sync",
    "github_token_file": "~/.config/companion-sync/github-token",
    "pi_asset": "Companion-Sync-Pi.tar.gz",
}
SETTINGS_FILE = os.path.expanduser("~/.config/companion-sync/panel.json")

W, H = 320, 240
TAB_Y = 204
TABS = ["SYSTEM", "NETWORK", "COMPANION", "SYNC"]
NEEDS_CONFIRM = {"restart", "shutdown", "return", "update_companion"}
FAST_RCLONE = ["--contimeout", "10s", "--timeout", "20s", "--retries", "1", "--low-level-retries", "2"]

log = logging.getLogger("panel")


def load_settings():
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(SETTINGS_FILE) as f:
            mine = json.load(f)
        cols = mine.pop("colours", {})
        cfg.update({k: v for k, v in mine.items() if not k.startswith("_")})
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
    mono = font("DejaVuSansMono.ttf", 11)
    mono_b = font("DejaVuSansMono-Bold.ttf", 11)
    small = font("DejaVuSansMono.ttf", 9)
    msg = font("DejaVuSansMono.ttf", 10)
    big = font("DejaVuSansMono-Bold.ttf", 16)
    head = font("DejaVuSansCondensed-Bold.ttf", 12)
    btn = font("DejaVuSansCondensed-Bold.ttf", 11)
    btn_s = font("DejaVuSansCondensed-Bold.ttf", 9)


def build_number():
    for p in (os.path.join(os.path.dirname(HERE), "BUILD"), os.path.join(HERE, "BUILD")):
        try:
            with open(p) as f:
                return f.read().strip()
        except OSError:
            continue
    return "dev"


class Panel:
    def __init__(self, cfg, hw, settings=None, live=True):
        self.cfg, self.hw, self.live = cfg, hw, live
        self.C = {k: hexrgb(v) for k, v in cfg["colours"].items()}
        self.s = settings
        self.tab = "SYSTEM"
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

    # ---- data ----------------------------------------------------------------
    def _load_logo(self):
        p = self.cfg["logo"]
        p = p if os.path.isabs(p) else os.path.join(HERE, p)
        try:
            im = Image.open(p).convert("RGBA")
            if im.width > W:
                im = im.resize((W, int(im.height * W / im.width)), Image.LANCZOS)
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
            v = info.latest_stable(c)
            if v:
                self.data["latest"] = v

        def sync():
            self.data["sync"] = info.sync(E, self.fast_settings())

        for fn, every, ev in ((sys_, c["sys_check_s"], None), (net, c["net_check_s"], None),
                              (comp, c["companion_check_s"], None),
                              (latest, c["latest_check_h"] * 3600, None),
                              (sync, c["sync_check_s"], self.sync_now)):
            threading.Thread(target=loop, args=(fn, every, ev), daemon=True).start()

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
        C = self.C
        d.text((10, y), label, font=Fonts.mono, fill=C["grey"])
        d.text((W - 10, y), value if value not in (None, "") else "-", font=Fonts.mono_b,
               fill=colour or C["cyan"], anchor="ra")
        d.line((10, y + 15, W - 10, y + 15), fill=C["rule"])

    def bar(self, d, y, label, frac, colour=None):
        C = self.C
        colour = colour or C["cyan"]
        d.text((10, y), label, font=Fonts.mono, fill=C["grey"])
        d.rectangle((80, y + 2, W - 50, y + 11), outline=C["cyan_dim"])
        if frac is not None:
            d.rectangle((81, y + 3, 81 + int((W - 132) * frac), y + 10), fill=colour)
        d.text((W - 10, y), f"{int(frac * 100)}%" if frac is not None else "-",
               font=Fonts.mono_b, fill=colour, anchor="ra")

    def button(self, d, box, bid, label, style="purple", enabled=True):
        C = self.C
        if self.confirm and self.confirm[0] == bid:
            self.chamfer(d, box, 6, C["red"], C["red_dim"])
            cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
            if box[2] - box[0] > 150:
                d.text((cx, cy), "TAP AGAIN TO CONFIRM", font=Fonts.btn, fill=C["lavender"], anchor="mm")
            else:
                d.text((cx, cy - 6), "TAP AGAIN", font=Fonts.btn_s, fill=C["lavender"], anchor="mm")
                d.text((cx, cy + 6), "TO CONFIRM", font=Fonts.btn_s, fill=C["lavender"], anchor="mm")
        else:
            if not enabled:
                outline, fill, text = C["faint"], C["bg"], C["faint"]
            elif style == "red":
                outline, fill, text = C["red"], C["red_dim"], C["red"]
            elif style == "cyan":
                outline, fill, text = C["cyan"], C["cyan_dim"], C["cyan"]
            else:
                outline, fill, text = C["purple"], C["purple_dim"], C["lavender"]
            self.chamfer(d, box, 6, outline, fill)
            d.text(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), label, font=Fonts.btn,
                   fill=text, anchor="mm")
        if enabled:
            self.hits.append((box, bid))

    def frame(self, title, alerts, flash_on):
        C = self.C
        img = Image.new("RGB", (W, H), C["bg"])
        d = ImageDraw.Draw(img)
        for y in range(24, TAB_Y - 2, 6):
            d.line((0, y, W, y), fill=C["scan"])
        d.polygon([(0, 0), (200, 0), (186, 20), (0, 20)], fill=C["purple_dim"])
        d.text((6, 3), f"BD-1 // {title}", font=Fonts.head, fill=C["lavender"])
        d.text((W - 6, 4), time.strftime("%H:%M"), font=Fonts.mono_b, fill=C["cyan"], anchor="ra")
        for i, t in enumerate(TABS):                       # one status pip per tab
            col = C["red"] if t in alerts else C["green"]
            d.rectangle((204 + i * 11, 7, 210 + i * 11, 13), fill=col)
        d.line((0, 21, W, 21), fill=C["purple"])
        for i, name in enumerate(TABS):
            x0, x1 = i * 80 + 1, i * 80 + 78
            on, alarm = name == self.tab, name in alerts and flash_on
            if alarm:
                outline, fill, text = C["red"], C["red"] if on else C["red_dim"], C["lavender"]
            elif on:
                outline, fill, text = C["purple"], C["purple"], C["bg"]
            else:
                outline, fill, text = C["purple_dim"], C["bg"], C["purple"]
            self.chamfer(d, (x0, TAB_Y, x1, H - 2), 7, outline, fill)
            d.text(((x0 + x1) // 2, (TAB_Y + H) // 2), name, font=Fonts.btn, fill=text, anchor="mm")
            self.hits.append(((x0, TAB_Y, x1, H - 1), "tab:" + name))
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
        self.button(d, (10, 152, 152, 194), "restart", "RESTART")
        self.button(d, (168, 152, 310, 194), "shutdown", "SHUTDOWN", "red")

    def draw_network(self, d):
        C, ne = self.C, self.data["network"]
        d.text((10, 30), ne.get("ip") or "NO ADDRESS", font=Fonts.big,
               fill=C["cyan"] if ne.get("ip") else C["red"])
        d.text((10, 50), "PRIMARY ADDRESS", font=Fonts.mono, fill=C["grey"])
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
        C, co, ne = self.C, self.data["companion"], self.data["network"]
        running = co.get("running")
        col = C["green"] if running else C["red"]
        d.ellipse((10, 32, 22, 44), fill=col if co else C["faint"])
        d.text((30, 29), ("RUNNING" if running else "STOPPED") if co else "CHECKING", font=Fonts.big,
               fill=col if co else C["grey"])
        v, latest, upd = co.get("version"), self.data.get("latest"), self.update_available()
        self.readout(d, 56, "INSTALLED", f"v{v}" if v else None)
        self.readout(d, 74, "LATEST STABLE", f"v{latest}" if latest else None, C["purple"] if upd else None)
        ip = ne.get("ip")
        self.readout(d, 92, "ADMIN PAGE", f"{ip}:{self.cfg['companion_port']}" if ip else None)
        self.readout(d, 110, "RUNNING FOR", info.duration(co.get("running_for")) if running else None)
        if upd:
            self.chamfer(d, (10, 132, 310, 148), 4, C["purple_dim"], hexrgb("#220046"))
            d.text((160, 140), "UPDATE AVAILABLE", font=Fonts.btn, fill=C["lavender"], anchor="mm")
            self.button(d, (10, 156, 310, 196), "update_companion", f"UPDATE TO v{latest} (STABLE)")
        else:
            self.button(d, (10, 156, 310, 196), "update_companion",
                        "UP TO DATE" if latest else "LATEST VERSION UNKNOWN", enabled=False)

    def draw_sync(self, d):
        C, sn = self.C, self.data["sync"]
        d.text((10, 27), "CONFIG HELD BY", font=Fonts.mono, fill=C["grey"])
        holder = sn.get("holder")
        if not sn:
            who, col = "CHECKING", C["grey"]
        elif sn.get("offline") or sn.get("signed_in") is False:
            who, col = "?", C["red"]
        else:
            who = holder.upper() if holder else "NOBODY"
            col = C["green"] if sn.get("mine") else C["amber"] if holder else C["grey"]
        d.text((W - 10, 25), who, font=Fonts.big, fill=col, anchor="ra")
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
        self.readout(d, 76, "COMPANION SYNC", f"build {self.build}")
        problem = self.sync_problem()
        if problem:
            d.text((160, 96), problem, font=Fonts.btn, fill=C["red"], anchor="mm")
        bw, bh, gap = 96, 34, 6
        for i, (bid, label, style) in enumerate((("take", "TAKE", "purple"), ("save", "SAVE NOW", "cyan"),
                                                 ("return", "RETURN", "purple"), ("status", "STATUS", "cyan"),
                                                 ("log", "LOG", "cyan"), ("update_cs", "UPDATE CS", "cyan"))):
            x = 10 + (i % 3) * (bw + gap)
            y = 108 + (i // 3) * (bh + gap)
            self.button(d, (x, y, x + bw, y + bh), bid, label, style)

    def draw_overlay(self, d):
        C, o = self.C, self.overlay
        box = (8, 26, W - 8, TAB_Y - 4)
        self.chamfer(d, box, 8, C["purple"], hexrgb("#0e0618"), 2)
        title = o.get("title", "")
        if o["kind"] == "busy":
            title += "." * (int(time.time() * 2) % 4)
        d.text((18, 32), title, font=Fonts.head, fill=C["lavender"])
        d.line((18, 48, W - 18, 48), fill=C["purple_dim"])
        f = Fonts.small if o.get("small") else Fonts.msg
        y = 54
        for line in o.get("lines", []):
            if y > box[3] - (40 if o.get("buttons") else 14):
                break
            d.text((18, y), line, font=f, fill=o.get("colour") or C["cyan"])
            y += 11 if o.get("small") else 13
        buttons = o.get("buttons") or []
        if buttons:
            n, bw = len(buttons), (W - 36 - 8 * (len(buttons) - 1)) // len(buttons)
            for i, (label, bid, style) in enumerate(buttons):
                x = 18 + i * (bw + 8)
                self.button(d, (x, box[3] - 36, x + bw, box[3] - 8), bid, label, style)

    def draw_startup(self):
        C = self.C
        img = Image.new("RGB", (W, H), C["bg"])
        d = ImageDraw.Draw(img)
        for y in range(0, H, 6):
            d.line((0, y, W, y), fill=C["scan"])
        if self.logo:
            img.paste(self.logo, ((W - self.logo.width) // 2, 6), self.logo)
        d.line((10, 134, W - 10, 134), fill=C["purple_dim"])
        d.text((10, 138), "BD-1 // SYSTEM START", font=Fonts.head, fill=C["lavender"])
        steps = self.startup_steps()
        y = 156
        for label, value, colour in steps:
            d.text((10, y), label, font=Fonts.mono, fill=C["grey"])
            d.text((10 + 7 * len(label), y), "." * (22 - len(label)), font=Fonts.mono, fill=hexrgb("#32323c"))
            d.text((W - 10, y), value, font=Fonts.mono_b, fill=colour, anchor="ra")
            y += 13
        done = sum(1 for _, _, c in steps if c != C["faint"] and c != C["purple"])
        d.rectangle((10, 222, W - 10, 232), outline=C["purple_dim"])
        d.rectangle((12, 224, 12 + int((W - 24) * done / len(steps)), 230), fill=C["purple"])
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
        img, d = self.frame(self.tab, self.alerts(), flash_on)
        if self.overlay:
            self.hits = [h for h in self.hits if not h[1].startswith("tab:")] if self.overlay["kind"] == "busy" else self.hits
            self.draw_overlay(d)
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
        if hit.startswith("tab:"):
            self.tab = hit[4:]
            self.overlay = None
            self.confirm = None
            return
        if hit == "close":
            self.overlay = None
            self.confirm = None
            return
        if hit in NEEDS_CONFIRM and not (self.confirm and self.confirm[0] == hit):
            self.confirm = (hit, time.monotonic() + self.cfg["confirm_s"])
            return
        self.confirm = None
        self.start_action(hit)

    # ---- actions ---------------------------------------------------------------
    def busy(self, title, *lines):
        self.overlay = {"kind": "busy", "title": title, "lines": wrap(lines)}

    def message(self, title, *lines, buttons=None, colour=None, small=False):
        self.overlay = {"kind": "message", "title": title, "lines": wrap(lines, 50 if small else 44),
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
            lines.append(f"Start-up: {boot.get('message') or boot.get('result')}")
        self.message("STATUS", *lines, small=True)

    def do_log(self):
        lines = []
        out = info.sh(["sudo", "journalctl", "-u", "companion-panel", "-u", "companion-sync-boot",
                       "-u", "companion-sync-save", "-n", "40", "--no-pager", "-o", "short"])
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
        self.busy("UPDATE CS", "Checking GitHub for a newer build.")
        api = f"https://api.github.com/repos/{self.cfg['github_repo']}/releases/latest"
        hdr = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "User-Agent": "companion-panel"}
        with urllib.request.urlopen(urllib.request.Request(api, headers=hdr), timeout=20) as r:
            rel = json.loads(r.read().decode())
        new = rel.get("tag_name", "").replace("build-", "")
        if new == self.build:
            self.message("UP TO DATE", f"BD-1 already has build {self.build}.", colour=self.C["green"])
            return
        asset = next((a for a in rel.get("assets", []) if a.get("name") == self.cfg["pi_asset"]), None)
        if not asset:
            self.message("NOT DONE", f"Build {new} has no Pi package.", colour=self.C["amber"])
            return
        self.busy("UPDATE CS", f"Downloading build {new}.")
        tmp = tempfile.mkdtemp(prefix="companion-sync-update-")
        req = urllib.request.Request(asset["url"], headers=dict(hdr, Accept="application/octet-stream"))
        tgz = os.path.join(tmp, self.cfg["pi_asset"])
        with urllib.request.urlopen(req, timeout=120) as r, open(tgz, "wb") as f:
            f.write(r.read())
        with tarfile.open(tgz) as t:
            t.extractall(tmp, filter="data")
        installer = os.path.join(tmp, "companion-sync", "pi", "install.sh")
        if not os.path.exists(installer):
            self.message("NOT DONE", "The download doesn't contain the installer.", colour=self.C["red"])
            return
        log.info("Companion Sync update: build %s -> %s", self.build, new)
        self.busy("UPDATE CS", f"Installing build {new}.", "The panel restarts when it's done.")
        # run outside this service, so restarting the panel doesn't stop the install
        subprocess.run(["sudo", "systemd-run", "--unit", f"companion-sync-update-{int(time.time())}",
                        "--collect", "/bin/bash", installer, "--from-update"])

    # ---- main loop -------------------------------------------------------------
    def run(self):
        self.start_pollers()
        next_frame = 0.0
        while not self.stopping:
            tap = self.hw.tap()
            now = time.monotonic()
            if tap:
                self.on_tap(*tap)
                next_frame = 0.0
            if now >= next_frame:
                self.hw.show(self.render())
                next_frame = now + self.cfg["frame_s"]
            time.sleep(0.02)

    def goodbye(self):
        C = self.C
        img = Image.new("RGB", (W, H), C["bg"])
        d = ImageDraw.Draw(img)
        if self.logo:
            img.paste(self.logo, ((W - self.logo.width) // 2, 30), self.logo)
        state = info.sh(["systemctl", "is-system-running"]) or ""
        text = "BD-1 // OFFLINE" if state == "stopping" else "PANEL RESTARTING"
        d.text((W // 2, 190), text, font=Fonts.head, fill=C["lavender"], anchor="mm")
        self.hw.frame = None
        self.hw.show(img)


def wrap(lines, width=44):
    out = []
    for line in lines:
        out += textwrap.wrap(str(line), width) or [""]
    return out


def main(argv):
    cfg = load_settings()
    if argv[:1] == ["--preview"]:
        from preview import render_samples
        render_samples(Panel, cfg, argv[1] if len(argv) > 1 else "preview")
        return 0
    log_file = os.path.expanduser(cfg["log_file"])
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
                        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)])
    from hw import Hardware
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
