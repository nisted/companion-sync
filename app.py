"""Companion Sync: system-tray (Windows, Raspberry Pi desktop) / menu-bar (Mac) app.

Sits in the tray, shows who has the Companion config, and does the sync:
  - Open Companion: takes the config (if it can), then launches Companion
  - saves to the chosen storage (Google Drive by default; see Storage…) every
    15 minutes while this rig has the config
  - returns the config automatically when Companion is quit
All the real work is in engine.py, shared with the headless Pi command.

On the Pi it runs from /opt/companion-sync (installed by pi/install.sh) and
copes with Companion as the desktop app, CompanionPi's service or a Docker
container. With a service or container, Open Companion opens its admin page,
and the config isn't returned when Companion stops (the service is restarted
by updates and takes); return it from the menu instead.
"""
import dataclasses
import datetime
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import webbrowser

import json as _json


def _rig_setting(key, default=None):
    """One value from this rig's settings.json, before the engine is loaded."""
    try:
        with open(os.path.expanduser("~/.config/companion-sync/settings.json")) as f:
            return _json.load(f).get(key, default)
    except (OSError, ValueError, AttributeError):
        return default


# Pi: AppIndicator always opens the menu on a click. The GTK tray icon can tell
# a left click from a right one, but not every taskbar shows it, so it's a
# setting (tray_left_click), off by default.
if sys.platform.startswith("linux") and _rig_setting("tray_left_click") and "--popup" not in sys.argv:
    os.environ.setdefault("PYSTRAY_BACKEND", "gtk")

import pystray
from PIL import Image, ImageDraw

import engine as E
from icon import draw_cs

APP = "Companion Sync"
SAVE_EVERY = 15 * 60        # seconds (save_every_min in settings.json changes it)
GUI_PORT = 8090             # web GUI (gui_port in settings.json changes it)
CHECK_EVERY = 30            # seconds between Companion / status checks
STATUS_EVERY = 5 * 60       # seconds between lock checks on the storage when idle
SINGLE_INSTANCE_PORT = 47823
LOGGED_IN_START = "--login" in sys.argv   # started at login (autostart), not opened by hand

try:                          # written by the build from GitHub secrets
    from _client import CLIENT_ID, CLIENT_SECRET, BUILD
except ImportError:
    CLIENT_ID = os.environ.get("CS_CLIENT_ID", "")
    CLIENT_SECRET = os.environ.get("CS_CLIENT_SECRET", "")
    BUILD = "dev"

LINUX = not E.WIN and not E.MAC
RELEASES = "https://github.com/nisted/companion-sync/releases/latest"
LATEST_API = "https://api.github.com/repos/nisted/companion-sync/releases/latest"
UPDATE_ASSET = ("Companion-Sync-Setup.exe" if E.WIN else
                "Companion-Sync-Mac.zip" if E.MAC else
                "Companion-Sync-Pi.tar.gz")
WIN_MUTEX = "CompanionSyncRunning"      # the Windows installer waits for this to go


# A left click runs the menu's default item (Show/Hide window); a right click
# opens the menu. Windows does this already. The Mac's menu-bar icon opens its
# menu on any click, so there the clicks come to us and we tell them apart.
if E.MAC and "--popup" not in sys.argv:
    try:
        import AppKit
        from pystray import _darwin as _pd

        _orig_init, _orig_update_menu = _pd.Icon.__init__, _pd.Icon._update_menu

        def _mac_init(self, *a, **k):
            _orig_init(self, *a, **k)
            self._status_item.button().sendActionOn_(
                AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskRightMouseUp)

        def _mac_update_menu(self):
            _orig_update_menu(self)
            self._status_item.setMenu_(None)          # clicks come to __call__ below

        def _mac_click(self):
            ev = AppKit.NSApp.currentEvent()
            right = ev is not None and (ev.type() == AppKit.NSEventTypeRightMouseUp
                                        or ev.modifierFlags() & AppKit.NSEventModifierFlagControl)
            if right and self._menu_handle:
                self._status_item.popUpStatusItemMenu_(self._menu_handle[0])
            else:
                _pd._base.Icon.__call__(self)          # the default item

        _pd.Icon.__init__, _pd.Icon._update_menu, _pd.Icon.__call__ = _mac_init, _mac_update_menu, _mac_click
    except Exception:                                  # keep the plain menu if this ever breaks
        pass


# ---- places ------------------------------------------------------------------

def data_dir():
    if E.WIN:
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
    elif LINUX:
        d = os.path.expanduser("~/.config/companion-sync")
    else:
        d = os.path.expanduser(f"~/Library/Application Support/{APP}")
    os.makedirs(d, exist_ok=True)
    return d


def bundled(name):
    """Path to a file shipped inside the app (rclone), or None."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, name)
    return p if os.path.exists(p) else None


def fetch(url, dest=None):
    """Download `url`: returns the bytes, or saves them to `dest`.
    The Mac uses curl, which trusts the system's certificates."""
    if E.MAC:
        cmd = ["/usr/bin/curl", "-fsSL", "--max-time", "300", "-H", "User-Agent: companion-sync", url]
        r = subprocess.run(cmd + (["-o", dest] if dest else []), capture_output=True)
        if r.returncode:
            raise OSError(r.stderr.decode(errors="replace").strip() or f"curl failed ({r.returncode})")
        return r.stdout
    req = urllib.request.Request(url, headers={"User-Agent": "companion-sync"})
    with urllib.request.urlopen(req, timeout=300) as r:
        if not dest:
            return r.read()
        with open(dest, "wb") as f:
            shutil.copyfileobj(r, f)


def child_env():
    """Environment for starting another program from the built app (the
    installer, the new app): without PyInstaller's own settings."""
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    for k in [k for k in env if k.startswith(("_PYI", "_MEIPASS"))]:
        env.pop(k)
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
    return env


LOG_FILE = os.path.join(data_dir(), "companion-sync.log")
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(APP)


# ---- native dialogs (safe from any thread, unlike tkinter on the Mac) ------------
# Linux (the Pi desktop) uses zenity, installed by pi/install.sh.

def _zenity(*args):
    try:
        return subprocess.run(["zenity", f"--title={APP}", *args], capture_output=True, text=True)
    except OSError:
        log.info("zenity is missing: sudo apt install zenity")
        return subprocess.CompletedProcess([], 1, "", "")


def _osa(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def ask(msg, title=APP):
    if LINUX:
        return _zenity("--question", "--no-markup", "--width=460", f"--text={msg}").returncode == 0
    if E.WIN:
        import ctypes
        flags = 0x04 | 0x20 | 0x10000 | 0x40000          # yes/no, question, foreground, topmost
        return ctypes.windll.user32.MessageBoxW(0, msg, title, flags) == 6
    r = subprocess.run(["osascript", "-e",
                        f'display dialog {_osa(msg)} with title {_osa(title)} '
                        f'buttons {{"No", "Yes"}} default button "No" with icon caution'],
                       capture_output=True, text=True)
    return "button returned:Yes" in r.stdout


def tell(msg, title=APP, problem=False):
    if LINUX:
        _zenity("--error" if problem else "--info", "--no-markup", "--width=460", f"--text={msg}")
        return
    if E.WIN:
        import ctypes
        flags = (0x10 if problem else 0x40) | 0x10000 | 0x40000
        ctypes.windll.user32.MessageBoxW(0, msg, title, flags)
    else:
        subprocess.run(["osascript", "-e",
                        f'display dialog {_osa(msg)} with title {_osa(title)} buttons {{"OK"}} '
                        f'default button "OK"' + (" with icon stop" if problem else "")],
                       capture_output=True)


def choose(prompt, options, title=APP):
    """Pick one of `options` (strings). Returns it, or None if cancelled."""
    if not options:
        return None
    if LINUX:
        r = _zenity("--list", "--hide-header", "--column=Option", "--width=520",
                    f"--height={160 + 28 * len(options)}", f"--text={prompt}", *options)
        out = r.stdout.strip()
        return out if out in options else None
    if E.MAC:
        items = "{" + ", ".join(_osa(o) for o in options) + "}"
        r = subprocess.run(["osascript", "-e",
                            f"choose from list {items} with title {_osa(title)} "
                            f"with prompt {_osa(prompt)} default items {{{_osa(options[0])}}}"],
                           capture_output=True, text=True)
        out = r.stdout.strip()
        return out if out in options else None
    import tkinter as tk                          # Windows: a small window of its own
    picked = []
    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    tk.Label(root, text=prompt, justify="left", wraplength=460).pack(padx=16, pady=(14, 8), anchor="w")
    lb = tk.Listbox(root, height=len(options), width=60, activestyle="none", exportselection=False)
    for o in options:
        lb.insert("end", o)
    lb.selection_set(0)
    lb.pack(padx=16)

    def ok(*_):
        sel = lb.curselection()
        if sel:
            picked.append(options[sel[0]])
        root.destroy()
    lb.bind("<Double-Button-1>", ok)
    root.bind("<Return>", ok)
    root.bind("<Escape>", lambda *_: root.destroy())
    row = tk.Frame(root)
    row.pack(pady=12, padx=16, anchor="e")
    tk.Button(row, text="Cancel", width=10, command=root.destroy).pack(side="right", padx=(6, 0))
    tk.Button(row, text="OK", width=10, command=ok, default="active").pack(side="right")
    root.lift()
    root.focus_force()
    root.mainloop()
    return picked[0] if picked else None


def ask_secret(prompt, title=APP):
    """Type a passphrase (hidden). Returns it, or None if cancelled."""
    if LINUX:
        r = _zenity("--entry", "--hide-text", "--width=460", f"--text={prompt}")
        return r.stdout.rstrip("\n") if r.returncode == 0 else None
    if E.MAC:
        r = subprocess.run(["osascript", "-e",
                            f'display dialog {_osa(prompt)} with title {_osa(title)} default answer "" '
                            f'with hidden answer buttons {{"Cancel", "OK"}} default button "OK"'],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
        out = r.stdout.strip()
        return out.split("text returned:", 1)[1] if "text returned:" in out else None
    import tkinter as tk
    got = []
    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    tk.Label(root, text=prompt, justify="left", wraplength=420).pack(padx=16, pady=(14, 8), anchor="w")
    entry = tk.Entry(root, show="\u2022", width=40)
    entry.pack(padx=16)

    def ok(*_):
        got.append(entry.get())
        root.destroy()
    root.bind("<Return>", ok)
    root.bind("<Escape>", lambda *_: root.destroy())
    row = tk.Frame(root)
    row.pack(pady=12, padx=16, anchor="e")
    tk.Button(row, text="Cancel", width=10, command=root.destroy).pack(side="right", padx=(6, 0))
    tk.Button(row, text="OK", width=10, command=ok, default="active").pack(side="right")
    root.lift()
    root.focus_force()
    entry.focus_set()
    root.mainloop()
    return got[0] if got else None


def pick_folder(prompt, title=APP):
    """Choose a folder. Returns its path, or None if cancelled."""
    if LINUX:
        r = _zenity("--file-selection", "--directory")
        return r.stdout.strip().rstrip("/") or None if r.returncode == 0 else None
    if E.MAC:
        r = subprocess.run(["osascript", "-e", f"POSIX path of (choose folder with prompt {_osa(prompt)})"],
                           capture_output=True, text=True)
        return r.stdout.strip().rstrip("/") or None
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    p = filedialog.askdirectory(title=prompt, mustexist=True, parent=root)
    root.destroy()
    return os.path.normpath(p) if p else None


def copy_text(text):
    if LINUX:
        cmd = (["wl-copy"] if shutil.which("wl-copy") else
               ["xclip", "-selection", "clipboard"] if shutil.which("xclip") else None)
    else:
        cmd = ["clip"] if E.WIN else ["pbcopy"]
    if not cmd:
        return
    try:
        subprocess.run(cmd, input=text, text=True, creationflags=E.NO_WINDOW, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def show_github_code(code, url):
    """GitHub's code sign-in: copy the code, open the page, and say what to do.
    The message is shown on its own thread so the sign-in keeps waiting meanwhile."""
    copy_text(code)
    webbrowser.open(url)
    threading.Thread(target=tell, daemon=True, args=(
        f"Your browser has opened GitHub's sign-in page:\n  {url}\n\n"
        f"Enter this code (it's already copied, so you can paste it):\n\n      {code}\n\n"
        f"Then click Authorize. Companion Sync carries on by itself once you have.",)).start()


# ---- start at login ------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PLIST = os.path.expanduser("~/Library/LaunchAgents/uk.co.nigelisted.companionsync.plist")
AUTOSTART = os.path.expanduser("~/.config/autostart/companion-sync-tray.desktop")


def linux_autostart_entry():
    return ("[Desktop Entry]\nType=Application\nName=Companion Sync\n"
            "Comment=Share one Companion config between rigs\n"
            f"Exec={sys.executable} {os.path.abspath(__file__)} --login\n"
            "Icon=/opt/companion-sync/icon.png\nTerminal=false\nX-GNOME-Autostart-enabled=true\n")


def _mac_app_path():
    """Where the app really is. .../Companion Sync.app/Contents/MacOS/Companion Sync
    -> .../Companion Sync.app. A downloaded app that still has the 'from the
    internet' flag is run by macOS from a hidden read-only copy (App
    Translocation); then this is the real one, e.g. in Applications."""
    here = os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", ".."))
    if "/AppTranslocation/" not in here:
        return here
    return _mac_untranslocated(here) or here


def _mac_untranslocated(path):
    """The original location of a translocated app (macOS Security framework), or
    failing that a copy in Applications. None if it can't be found."""
    try:
        import ctypes
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        sec = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Security.framework/Security")
        cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
        cf.CFURLCreateFromFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                               ctypes.c_long, ctypes.c_bool]
        cf.CFURLGetFileSystemRepresentation.restype = ctypes.c_bool
        cf.CFURLGetFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                                        ctypes.c_char_p, ctypes.c_long]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        sec.SecTranslocateCreateOriginalPathForURL.restype = ctypes.c_void_p
        sec.SecTranslocateCreateOriginalPathForURL.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        raw = path.encode()
        url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), True)
        orig = sec.SecTranslocateCreateOriginalPathForURL(url, None) if url else None
        if url:
            cf.CFRelease(url)
        if orig:
            buf = ctypes.create_string_buffer(4096)
            ok = cf.CFURLGetFileSystemRepresentation(orig, True, buf, 4096)
            cf.CFRelease(orig)
            if ok and buf.value:
                return buf.value.decode().rstrip("/")
    except Exception:
        log.exception("couldn't find the app's real location")
    for p in (f"/Applications/{APP}.app", os.path.expanduser(f"~/Applications/{APP}.app")):
        if os.path.isdir(p):
            return p
    return None


def login_enabled():
    if E.WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, APP)
                return True
        except OSError:
            return False
    if LINUX:
        return os.path.exists(AUTOSTART)
    return os.path.exists(PLIST)


def set_login(on):
    if LINUX:
        if on:
            os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
            with open(AUTOSTART, "w") as f:
                f.write(linux_autostart_entry())
        elif os.path.exists(AUTOSTART):
            os.remove(AUTOSTART)
        return
    if not getattr(sys, "frozen", False):
        return                                   # only for the built app
    if E.WIN:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, f'"{sys.executable}"')
            else:
                try:
                    winreg.DeleteValue(k, APP)
                except OSError:
                    pass
    else:
        if on:
            os.makedirs(os.path.dirname(PLIST), exist_ok=True)
            with open(PLIST, "w") as f:
                f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>uk.co.nigelisted.companionsync</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/open</string><string>-a</string><string>{_mac_app_path()}</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict></plist>
""")
        elif os.path.exists(PLIST):
            os.remove(PLIST)


# ---- icon -------------------------------------------------------------------------

COLOURS = {
    "mine": (46, 160, 67),       # green: this rig has the config
    "free": (140, 140, 140),     # grey: nobody has it
    "other": (230, 150, 20),     # amber: another rig has it
    "problem": (210, 50, 50),    # red: offline / not signed in / error
    "busy": (40, 110, 220),      # blue: working
}


def make_icon(state):
    return draw_cs(64, status=COLOURS[state])


# ---- the app ------------------------------------------------------------------------

class App:
    def __init__(self):
        self.s = E.default_settings(log=log.info)
        rclone = bundled("rclone.exe" if E.WIN else "rclone")
        if rclone:
            self.s.rclone = rclone
        self.state = "busy"
        self.line = "Starting..."
        self.mine = False
        self.last_save = None
        self.last_status = 0
        self.companion_was_running = False
        self.warned_not_holding = False
        self.work = threading.Lock()
        self.restart = False          # reopen after an update (Pi)
        self.st = {}                  # last status from the storage
        self.signed = None            # signed in to the storage (None: not checked yet)
        self.encrypted = None
        self.popup_visible = False
        self.popup_proc = None
        self.httpd = None
        self.latest = None            # (time checked, latest build) for the GUI's Update page
        # On the Pi, a systemd timer may already save every 15 minutes (headless
        # install); then the tray app leaves saving to it.
        self.timer_saves = LINUX and subprocess.run(
            ["systemctl", "is-enabled", "--quiet", "companion-sync-save.timer"],
            capture_output=True).returncode == 0
        self.icon = pystray.Icon(APP, make_icon("busy"), APP, menu=self.menu())

    # -- menu --
    def menu(self):
        """The mini tray menu, like Companion's. Everything else is in the
        pop-up window and the web GUI."""
        M = pystray.MenuItem
        return pystray.Menu(
            M(lambda i: self.line, None, enabled=False),
            pystray.Menu.SEPARATOR,
            M(lambda i: "Hide window" if self.popup_visible else "Show window",
              lambda: self.popup_toggle(), default=True),
            M("Open Companion", self.bg(self.open_companion)),
            M("Take config", self.bg(self.take_only), enabled=lambda i: not self.mine),
            M("Launch GUI", lambda: self.launch_gui()),
            pystray.Menu.SEPARATOR,
            M(f"Quit {APP}", self.quit),
        )

    def bg(self, fn):
        """Run a menu action in the background so the menu never freezes."""
        def go(*_):
            threading.Thread(target=self.guarded, args=(fn,), daemon=True).start()
        return go

    def guarded(self, fn):
        if not self.work.acquire(blocking=False):
            self.notify("Busy with another job. Try again in a moment.")
            return
        try:
            self.set_state("busy", "Working…")
            try:
                fn()
            except E.NeedsPassphrase as e:          # encrypted storage: get the passphrase, then try again
                if self.enter_passphrase(str(e)):
                    fn()
        except E.NeedsUpdate as e:
            log.info(f"problem: {e}")
            self.offer_download(str(e))
        except E.SyncError as e:
            log.info(f"problem: {e}")
            tell(str(e), problem=True)
        except Exception as e:                     # never let the tray app die
            log.exception("unexpected")
            tell(f"Something went wrong:\n{e}\n\nDetails are in the log.", problem=True)
        finally:
            self.work.release()
            self.refresh(force=True)

    # -- display --
    def set_state(self, state, line):
        self.state, self.line = state, line
        self.icon.icon = make_icon(state)
        self.icon.title = f"{APP}: {line}"
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def notify(self, msg):
        log.info(msg)
        try:
            if LINUX:
                subprocess.run(["notify-send", "--app-name", APP, APP, msg], capture_output=True, timeout=10)
            elif E.MAC:
                subprocess.run(["osascript", "-e",
                                f"display notification {_osa(msg)} with title {_osa(APP)}"])
            else:
                self.icon.notify(msg, APP)
        except Exception:
            pass

    def refresh(self, force=False):
        if not force and time.time() - self.last_status < STATUS_EVERY:
            return
        self.last_status = time.time()
        try:
            self.signed = E.signed_in(self.s)
        except Exception:
            self.signed = None
        try:
            st = E.status(self.s)
            self.st = st
            try:
                self.encrypted = E.encryption_on(self.s)
            except Exception:
                self.encrypted = None
        except E.Offline as e:
            self.mine = os.path.exists(self.s.holding_file)
            self.set_state("problem", str(e).splitlines()[0])
            return
        self.mine = st["mine"]
        if st.get("moved"):
            self.set_state("problem", f"Config has moved {st['moved'].split(' (')[0]}: see Storage…")
        elif st["mine"]:
            saved = f", saved {self.last_save:%H:%M}" if self.last_save else ""
            self.set_state("mine", f"This rig has the config{saved}")
        elif st["holder"]:
            self.set_state("other", f"Config is with {st['holder']}")
        else:
            self.set_state("free", "Config is free")

    # -- pop-up window and web GUI --
    def app_setting(self, key, default):
        return _rig_setting(key, default)

    def gui_port(self):
        try:
            return int(self.app_setting("gui_port", GUI_PORT))
        except (TypeError, ValueError):
            return GUI_PORT

    def gui_password(self):
        return self.app_setting("gui_password", "") or ""

    def gui_url(self):
        return f"http://127.0.0.1:{self.gui_port()}/"

    def launch_gui(self):
        webbrowser.open(self.gui_url())

    def logo_path(self):
        return bundled("logo-source.png")

    def popup_cmd(self):
        me = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]
        return me + ["--popup", str(self.gui_port())]

    def popup_start(self):
        """Start the pop-up's own process (hidden) if it isn't running."""
        if self.popup_proc and self.popup_proc.poll() is None:
            return
        try:
            self.popup_proc = subprocess.Popen(self.popup_cmd(), env=child_env(), close_fds=True,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            log.exception("pop-up didn't start")

    def popup_restart(self):
        """Start a fresh pop-up (e.g. its size changed), keeping it shown or hidden."""
        if self.popup_proc and self.popup_proc.poll() is None:
            self.popup_proc.terminate()
        self.popup_proc = None
        self.popup_start()

    def popup_set(self, show):
        self.popup_visible = bool(show)
        if show:
            self.popup_start()
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def popup_toggle(self):
        self.popup_set(not self.popup_visible)

    def snapshot(self):
        """Everything the pop-up and the web GUI show, from what's already known
        (nothing here waits on the storage)."""
        st = self.st or {}
        last = self.last_save.strftime("%H:%M") if self.last_save else None
        if not last:
            ls = E.read_state(self.s).get("last_save")
            try:
                t = datetime.datetime.fromisoformat(ls)
                last = t.strftime("%H:%M" if t.date() == datetime.date.today() else "%d %b %H:%M")
            except (TypeError, ValueError):
                last = None
        if last and self.last_save:
            mins = int((datetime.datetime.now() - self.last_save).total_seconds() // 60)
            last += f" ({mins} min ago)" if mins else " (just now)"
        running = self.companion_was_running
        every = self.save_every() // 60
        if self.state == "mine":
            detail = f"Saving every {every} min · Companion {'running' if running else 'not running'}"
        elif self.state == "other":
            age = st.get("age_hours")
            detail = f"Last seen {age:.1f} hours ago" if isinstance(age, (int, float)) else ""
        elif self.state == "free":
            detail = "Take it here, or open Companion to take it and start"
        else:
            detail = ""
        try:
            lv = E.vtext(E.local_version(self.s))
        except Exception:
            lv = None
        where = (self.s.folder_path if self.s.storage == "folder" else
                 self.s.github_repo if self.s.storage == "github" else f"{self.s.place}: {self.s.folder}")
        return {
            "state": self.state, "line": self.line, "detail": detail, "mine": self.mine,
            "holder": st.get("holder"), "age_hours": st.get("age_hours"), "rig": self.s.rig,
            "build": BUILD, "platform": self.s.platform,
            "place": self.s.place.removeprefix("the ")[:1].upper() + self.s.place.removeprefix("the ")[1:], "storage_where": where,
            "signed_in": self.signed, "encrypted": self.encrypted, "last_save": last,
            "companion_running": running, "local_version": lv,
            "drive_version": E.vtext(st["drive_version"]) if st.get("drive_version") else None,
            "companion_desc": E.describe_companion(self.s), "start_at_login": login_enabled(),
            "popup_visible": self.popup_visible, "busy": self.work.locked(), "gui_url": self.gui_url(),
        }

    def save_every(self):
        try:
            return max(60, int(float(self.app_setting("save_every_min", SAVE_EVERY // 60)) * 60))
        except (TypeError, ValueError):
            return SAVE_EVERY

    WEB_ACTIONS = ("open", "take", "save", "return", "storage", "sign_in", "encryption", "update",
                   "get_companion", "gui", "toggle_login", "status")

    def web_action(self, name):
        """A button in the pop-up or the web GUI. Long jobs run in the background;
        their questions appear on this rig's screen."""
        if name not in self.WEB_ACTIONS:
            return False, "Unknown action."
        if name == "gui":
            self.launch_gui()
            return True, ""
        if name == "toggle_login":
            set_login(not login_enabled())
            return True, "Start at login is " + ("on." if login_enabled() else "off.")
        if name == "get_companion":
            webbrowser.open(E.COMPANION_DOWNLOAD)
            return True, "The Companion download page opened on this rig."
        if self.work.locked():
            return False, "Busy with another job. Try again in a moment."
        fn = {"open": self.open_companion, "take": self.take_only, "save": self.save_now,
              "return": self.return_now, "storage": self.choose_storage, "sign_in": self.sign_in,
              "encryption": self.encryption, "update": self.update_app, "status": self.show_status}[name]
        self.bg(fn)()
        return True, {"open": "Opening Companion…", "take": "Taking the config…", "save": "Saving…",
                      "return": "Returning the config…", "update": "Checking for an update…"}.get(
                          name, "Carry on on this rig's screen.")

    def check_update(self):
        """Latest release, checked at most every 10 minutes."""
        if not self.latest or time.time() - self.latest[0] > 600:
            try:
                rel = json.loads(fetch(LATEST_API))
                self.latest = (time.time(), rel.get("tag_name", "").removeprefix("build-"))
            except Exception:
                self.latest = (time.time() - 540, None)       # try again in a minute
        new = self.latest[1]
        newer = bool(new and new.isdigit() and (BUILD == "dev" or not str(BUILD).isdigit()
                                                or int(new) > int(BUILD)))
        return {"current": BUILD, "latest": new, "newer": newer}

    def log_tail(self, n):
        try:
            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                return f.read().splitlines()[-n:]
        except OSError:
            return []

    # settings.json keys: the engine's own, and the app's
    APP_KEYS = ("save_every_min", "gui_port", "gui_network", "gui_password", "tray_left_click", "popup_size")
    ENGINE_KEYS = ("login_action", "keep_history", "keep_local", "stale_hours", "companion_mode")

    def get_settings(self):
        return {
            "platform": self.s.platform, "rig": self.s.rig, "holding": bool(self.mine),
            "start_at_login": login_enabled(), "login_action": self.s.login_action,
            "save_every_min": self.save_every() // 60, "keep_history": self.s.keep_history,
            "keep_local": self.s.keep_local, "stale_hours": self.s.stale_hours,
            "companion_mode": _rig_setting("companion_mode", "auto") or "auto",
            "gui_port": self.gui_port(), "gui_network": bool(self.app_setting("gui_network", True)),
            "has_password": bool(self.gui_password()),
            "tray_left_click": bool(self.app_setting("tray_left_click", False)),
            "popup_size": self.app_setting("popup_size", 100),
        }

    def set_settings(self, b):
        """Save settings from the web GUI into settings.json (and the rig-name file)."""
        try:
            with open(self.s.settings_file) as f:
                saved = json.load(f)
            if not isinstance(saved, dict):
                saved = {}
        except (OSError, ValueError):
            saved = {}
        before_size = saved.get("popup_size", 100)
        restart_keys = ("gui_port", "gui_network", "tray_left_click", "companion_mode")
        before = {k: saved.get(k) for k in restart_keys}
        nums = {"save_every_min": (1, 24 * 60), "keep_history": (1, 1000), "keep_local": (1, 1000),
                "stale_hours": (1, 24 * 365), "gui_port": (1024, 65535), "popup_size": (50, 300)}
        for k, (lo, hi) in nums.items():
            if k in b and b[k] is not None:
                try:
                    v = float(b[k]) if k == "stale_hours" else int(b[k])
                except (TypeError, ValueError):
                    return False, f"{k.replace('_', ' ')} must be a number."
                if not lo <= v <= hi:
                    return False, f"{k.replace('_', ' ')} must be between {lo} and {hi}."
                saved[k] = v
        if b.get("login_action") in E.LOGIN_ACTIONS:
            saved["login_action"] = b["login_action"]
        if self.s.platform == "pi" and b.get("companion_mode") in ("auto",) + E.COMPANION_MODES:
            saved["companion_mode"] = b["companion_mode"]
        for k in ("gui_network", "tray_left_click"):
            if isinstance(b.get(k), bool):
                saved[k] = b[k]
        if "gui_password" in b and isinstance(b["gui_password"], str):
            saved["gui_password"] = b["gui_password"]
        rig = (b.get("rig") or "").strip()
        msg = []
        if rig and rig != self.s.rig:
            if self.mine:
                return False, "Return the config before renaming this rig."
            import re
            rig = re.sub(r"[^a-z0-9-]+", "-", rig.lower()).strip("-")
            if not rig:
                return False, "That rig name has no letters or numbers in it."
            with open(os.path.join(os.path.expanduser("~"), ".companion-sync-rig-name"), "w") as f:
                f.write(rig + "\n")
            self.s.rig = rig
            msg.append(f"This rig is now called {rig}.")
        os.makedirs(os.path.dirname(self.s.settings_file), exist_ok=True)
        tmp = self.s.settings_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(saved, f, indent=2)
        os.replace(tmp, self.s.settings_file)
        for k in self.ENGINE_KEYS:                         # take effect now
            if k in saved and k != "companion_mode":
                setattr(self.s, k, saved[k])
        if isinstance(b.get("start_at_login"), bool) and b["start_at_login"] != login_enabled():
            set_login(b["start_at_login"])
        if "popup_size" in b and saved.get("popup_size") != before_size:
            self.popup_restart()
        msg.append("Settings saved.")
        if any(saved.get(k) != before[k] for k in restart_keys):
            msg.append(f"Quit and reopen {APP} for the port, network, tray or Companion "
                       f"settings to take effect.")
        return True, " ".join(msg)

    # -- actions --
    def take_only(self):
        """Take the config without opening Companion."""
        managed = E.companion_managed(self.s)          # a service / container is stopped and restarted
        if not managed and E.companion_running(self.s):
            tell("Companion is open. Quit it first, then take the config.\n\n"
                 "(Or use Open Companion, which takes it and opens Companion.)")
            return
        try:
            msg = E.take(self.s, confirm_takeover=ask, manage_companion=managed)
        except E.NoConfigYet as e:
            if ask(f"{e}\n\nPut this rig's Companion config there and share it from now on?\n"
                   f"(Do this on the rig whose config you want to keep.)"):
                self.notify(E.start_sharing(self.s))
                self.mine = True
                self.last_save = datetime.datetime.now()
            return
        self.mine = True
        self.last_save = None
        self.notify(msg)

    def open_companion(self, auto=False):
        """Take the config and start Companion. auto=True: run at login (see
        login_action), so a service's admin page isn't opened in the browser."""
        managed = E.companion_managed(self.s)     # Pi: service or container, started by us
        running = E.companion_running(self.s)
        if running and not managed:
            if self.mine:
                self.notify("Companion is already open, and this rig has the config.")
            else:
                tell("Companion is already open, but this rig doesn't have the config, "
                     "so changes won't be kept.\n\nQuit Companion, then choose Open Companion again.")
            return
        try:
            msg = E.take(self.s, confirm_takeover=ask)
            self.mine = True
            self.last_save = None
            self.notify(msg)
            self.show_admin(managed, auto)
        except E.NeedsPassphrase:
            raise
        except E.NeedsUpdate as e:
            self.offer_download(str(e))
        except E.NoConfigYet as e:
            if ask(f"{e}\n\nPut this rig's Companion config there and share it from now on?\n"
                   f"(Do this on the rig whose config you want to keep.)"):
                self.notify(E.start_sharing(self.s))
                self.mine = True
                self.last_save = datetime.datetime.now()
            if (managed and running) or ask("Open Companion now?"):
                if not E.companion_running(self.s):
                    E.start_companion(self.s)
                self.show_admin(managed, auto)
        except E.SyncError as e:
            if managed and running:
                tell(f"{e}\n\nCompanion keeps running with this rig's own copy. Changes won't be kept.")
            elif ask(f"{e}\n\nOpen Companion anyway, with this rig's own copy?\n"
                     f"(Changes won't be kept.)"):
                E.start_companion(self.s)
                self.show_admin(managed, auto)

    def show_admin(self, managed, auto):
        """A service or container has no window: open its admin page instead."""
        if managed and not auto and self.s.admin_url:
            webbrowser.open(self.s.admin_url)

    def save_now(self):
        msg = E.save(self.s)
        self.last_save = datetime.datetime.now()
        self.notify(msg)

    def return_now(self):
        msg = E.return_(self.s)
        self.mine = False
        self.notify(msg)

    def show_status(self):
        st = E.status(self.s)
        lv = E.local_version(self.s)
        if st["mine"]:
            who = "This rig has the config."
        elif st["holder"]:
            who = f"The config is with {st['holder']} (last seen {st['age_hours']:.1f} hours ago)."
        else:
            who = "The config is free."
        extra = "\n\nThis rig's lock was taken over; its edits will go to 'stranded'." if st["stranded"] else ""
        saved = f"\nLast saved: {self.last_save:%H:%M}" if self.last_save else ""
        text = (f"{who}{saved}\n\nThis rig: {self.s.rig}, Companion {E.vtext(lv)}\n"
                f"{self.s.place} copy: Companion {E.vtext(st['drive_version'])}{extra}")
        dv = st["drive_version"]
        if dv and E.vkey(dv) > E.vkey(lv):
            self.offer_download(text + f"\n\nThis rig needs Companion {E.vtext(dv)} to use the config.")
        else:
            tell(text)

    def enter_passphrase(self, why):
        """Ask for the encryption passphrase until it's right (or cancelled). True when unlocked."""
        msg = f"{why}\n\nPassphrase:"
        while True:
            p = ask_secret(msg)
            if p is None:
                return False
            try:
                E.unlock(self.s, p)
                self.notify("Passphrase accepted. This rig will remember it.")
                return True
            except E.SyncError as e:
                msg = f"{e}\n\nTry again:"

    def encryption(self):
        """Turn encryption of the shared config on or off (from the rig that has it),
        or give this rig the passphrase."""
        if not E.encryption_on(self.s):
            if not self.mine:
                tell(f"The config in {self.s.place} isn't encrypted.\n\nTo turn encryption on, do it "
                     f"on the rig that has the config (or take it here first).")
                return
            if not ask(f"Encrypt the shared config?\n\nEverything Companion Sync puts in {self.s.place} "
                       f"is then scrambled with a passphrase you choose. Each rig asks for the passphrase "
                       f"once and remembers it.\n\nIf the passphrase is lost, the copy in {self.s.place} "
                       f"can't be read (each rig still has its own copy). Carry on?"):
                return
            while True:
                p1 = ask_secret(f"Choose a passphrase (at least {E.ENC_MIN_PASSPHRASE} characters):")
                if p1 is None:
                    return
                if len(p1) < E.ENC_MIN_PASSPHRASE:
                    tell(f"That's too short. Use at least {E.ENC_MIN_PASSPHRASE} characters.")
                    continue
                p2 = ask_secret("Type the passphrase again:")
                if p2 is None:
                    return
                if p1 == p2:
                    break
                tell("Those didn't match. Try again.")
            self.set_state("busy", "Encrypting…")
            tell(E.turn_on_encryption(self.s, p1))
            return
        if not self.mine:
            if self.enter_passphrase(f"The config in {self.s.place} is encrypted."):
                tell(f"This rig has the passphrase now.\n\nTo turn encryption off, do it on the rig "
                     f"that has the config.")
            return
        if ask(f"The config in {self.s.place} is encrypted.\n\nTurn encryption off? "
               f"It'll be stored unscrambled again."):
            self.set_state("busy", "Decrypting…")
            tell(E.turn_off_encryption(self.s))

    # -- updating: download the latest release and install it, then reopen --
    def update_app(self):
        if not getattr(sys, "frozen", False) and not LINUX:
            tell(f"This {APP} is running from source. Update it with git.")
            return
        self.set_state("busy", "Checking for an update…")
        try:
            rel = json.loads(fetch(LATEST_API))
        except Exception as e:
            raise E.SyncError(f"Can't reach GitHub to check for an update. Are you online?\n{e}")
        new = rel.get("tag_name", "").removeprefix("build-")
        if BUILD != "dev" and new.isdigit() and str(BUILD).isdigit() and int(new) <= int(BUILD):
            tell(f"You have the latest build ({BUILD}).")
            return
        asset = next((a for a in rel.get("assets", []) if a.get("name") == UPDATE_ASSET), None)
        if not asset:
            tell(f"Build {new} has no {UPDATE_ASSET}. Your browser will open the download page.")
            webbrowser.open(RELEASES)
            return
        if not ask(f"Update {APP} from build {BUILD} to build {new}?\n\n"
                   f"{APP} closes for a moment and opens again. Companion keeps running."):
            return
        self.set_state("busy", f"Downloading build {new}…")
        tmp = tempfile.mkdtemp(prefix="companion-sync-update-")
        dest = os.path.join(tmp, UPDATE_ASSET)
        try:
            fetch(asset["browser_download_url"], dest)
        except Exception as e:
            raise E.SyncError(f"The download didn't finish. Try again.\n{e}")
        log.info("update: build %s -> %s", BUILD, new)
        self.set_state("busy", f"Installing build {new}…")
        if E.WIN:
            self._update_windows(dest)
        elif E.MAC:
            self._update_mac(dest, tmp)
        else:
            self._update_pi(dest, tmp, new)

    def _update_windows(self, setup):
        # The installer waits for this app to close, installs, then opens it again.
        subprocess.Popen([setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                         env=child_env(), close_fds=True,
                         creationflags=0x00000008 | 0x00000200)   # detached, own process group
        self.quit()

    def _update_mac(self, zip_path, tmp):
        app = _mac_app_path()
        if "/AppTranslocation/" in app:
            raise E.SyncError(
                f"macOS is running {APP} from a hidden copy, so it can't update itself.\n\n"
                f"Quit {APP}, then in Terminal run:\n"
                f"  xattr -dr com.apple.quarantine \"/Applications/{APP}.app\"\n"
                f"(or wherever the app is), open it again and update.")
        if not app.endswith(".app") or not os.access(os.path.dirname(app), os.W_OK):
            raise E.SyncError(f"Can't replace {app}: that folder can't be written to.\n"
                              f"Move {APP} into Applications, then try again.")
        unpacked = os.path.join(tmp, "new")
        r = subprocess.run(["ditto", "-x", "-k", zip_path, unpacked], capture_output=True, text=True)
        new_app = os.path.join(unpacked, f"{APP}.app")
        if r.returncode or not os.path.isdir(new_app):
            raise E.SyncError(f"The download couldn't be unpacked.\n{r.stderr.strip()}")
        # swap the app over once this one has quit, then open it
        script = os.path.join(tmp, "swap.sh")
        with open(script, "w") as f:
            f.write('while kill -0 "$1" 2>/dev/null; do sleep 0.5; done\n'
                    'rm -rf "$2" && mv "$3" "$2"\n'
                    'xattr -dr com.apple.quarantine "$2" 2>/dev/null\n'
                    'open "$2"\n')
        subprocess.Popen(["/bin/sh", script, str(os.getpid()), app, new_app],
                         env=child_env(), start_new_session=True, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.quit()

    def _update_pi(self, tgz, tmp, new):
        with tarfile.open(tgz) as t:
            t.extractall(tmp, filter="data")
        installer = os.path.join(tmp, "companion-sync", "pi", "install.sh")
        if not os.path.exists(installer):
            raise E.SyncError("The download doesn't contain the installer.")
        cmd = ["/bin/bash", installer, "--from-update"]
        r = subprocess.run(["sudo", "-n", *cmd], capture_output=True, text=True)
        if r.returncode and "password" in (r.stderr or "").lower():
            r = subprocess.run(["pkexec", *cmd], capture_output=True, text=True)   # asks for the password
        log.info("install.sh:\n%s%s", r.stdout, r.stderr)
        if r.returncode:
            out = (r.stdout + r.stderr).strip().splitlines()[-6:]
            raise E.SyncError(f"Build {new} didn't install:\n" + "\n".join(out))
        self.restart = True
        self.quit()

    def offer_download(self, msg):
        if ask(f"{msg}\n\nOpen the Companion download page?"):
            webbrowser.open(E.COMPANION_DOWNLOAD)

    def sign_in(self):
        """Sign in again to the storage already chosen."""
        k = self.s.storage_kind
        if k == "gdrive":
            if not CLIENT_ID:
                raise E.SyncError("This build has no Google sign-in details.")
            tell("Your browser will open to sign in to Google.\n\n"
                 "Sign in with the account the configs are stored in, then click Allow.")
            E.sign_in(self.s, CLIENT_ID, CLIENT_SECRET)
        elif k in ("dropbox", "onedrive"):
            tell(f"Your browser will open to sign in to {self.s.place}.\n\n"
                 f"Sign in with the account the configs are stored in, then allow access.")
            E.connect_rclone(self.s, k)
        elif k == "github":
            E.github_sign_in(self.s, show_github_code)
        else:
            raise E.SyncError(f"Can't reach {self.s.place}:\n  {self.s.folder_path}\n\n"
                              f"Check it's connected, or choose it again from Storage….")
        self.notify(f"Signed in to {self.s.place}.")

    STORAGE_OPTIONS = [
        ("gdrive", "Google Drive"),
        ("github-quick", "GitHub: quick (makes a private repository for you)"),
        ("github-limited", "GitHub: limited (only the one repository you pick)"),
        ("dropbox", "Dropbox"),
        ("onedrive", "OneDrive"),
        ("nas", "Network folder / NAS"),
        ("icloud", "iCloud Drive"),
    ]

    def choose_storage(self):
        """Choose where the shared config lives, sign in, and check the connection.
        Every rig sharing a config must choose the same storage and account."""
        moving = self.mine
        if not moving and E.was_holding(self.s):
            tell("This rig's lock was taken over by another rig. Save or return the config first "
                 "(its changes go to 'stranded'), then come back to Storage….")
            return
        labels = [label for _, label in self.STORAGE_OPTIONS]
        intro = (f"This rig has the config, so the config will MOVE to the storage you pick "
                 f"(it must be empty). Every other rig then needs to choose it too.\n\n"
                 if moving else "")
        picked = choose(f"{intro}Where should the shared Companion config live?\n\n"
                        f"Now: {self.s.place}. Every rig must use the same storage and account.", labels)
        if not picked:
            return
        key = dict((label, k) for k, label in self.STORAGE_OPTIONS)[picked]
        s = dataclasses.replace(self.s)                # try it out on a copy; keep the old choice if it fails
        if key == "gdrive":
            if not CLIENT_ID:
                raise E.SyncError("This build has no Google sign-in details.")
            E.use_rclone(s, "gdrive")
            if not E.signed_in(s):
                tell("Your browser will open to sign in to Google.\n\n"
                     "Sign in with the account the configs are stored in, then click Allow.")
                E.sign_in(s, CLIENT_ID, CLIENT_SECRET)
        elif key.startswith("github"):
            mode = key.split("-")[1]
            if mode == "limited" and not ask(
                    "GitHub, limited: Companion Sync only gets the one repository you pick.\n\n"
                    "1. Make a PRIVATE repository on GitHub (for example companion-sync-config).\n"
                    "2. Install Companion Sync on it: your browser will open the page. Choose "
                    "'Only select repositories' and tick just that one.\n"
                    "3. Then sign in with the code shown next.\n\nCarry on?"):
                return
            if mode == "limited":
                E.use_github(s, "limited")
                webbrowser.open(E.github_install_link(s))
                if not ask("Have you installed Companion Sync on your private repository?"):
                    return
            E.connect_github(s, mode, show_github_code,
                             lambda repos: choose("Which repository should hold the config?", repos))
        elif key in ("dropbox", "onedrive"):
            tell(f"Your browser will open to sign in to {E.STORAGE_KINDS[key]}.\n\n"
                 f"Sign in with the account the configs are stored in, then allow access.")
            E.connect_rclone(s, key)
        elif key == "nas":
            path = pick_folder("Choose the shared folder on your network drive or NAS. "
                               "A CompanionSync folder is made inside it.")
            if not path:
                return
            E.use_folder(s, "nas", path)
        elif key == "icloud":
            root = E.icloud_folder()
            if not root:
                raise E.SyncError("Can't find iCloud Drive on this computer.\n\n"
                                  + ("Turn on iCloud Drive in System Settings > Apple Account > iCloud."
                                     if E.MAC else "Install iCloud for Windows and turn on iCloud Drive."))
            E.use_folder(s, "icloud", root)
        self.set_state("busy", f"Checking {s.place}…")
        result = E.check_storage(s)
        if moving:
            if s.storage_kind == self.s.storage_kind and s.store.describe() == self.s.store.describe():
                tell(f"That's the storage the config is already in ({self.s.place}).")
                return
            if not ask(f"{result}\n\nMove the config from {self.s.place} to {s.place} now?"):
                return
            self.set_state("busy", f"Moving the config to {s.place}…")
            result = E.move_storage(self.s, s)
        for k in E.SAVED_CHOICE:                      # it works: switch over and remember it
            setattr(self.s, k, getattr(s, k))
        E.save_storage_choice(self.s)
        note = ("\n\niCloud Drive can take a minute or two to pass changes between computers, "
                "so wait a moment after returning the config on one rig before opening it on another."
                if key == "icloud" else "")
        tell(f"{result}\n\nThis rig now uses {self.s.place}.{note}")
        self.mine = moving

    def open_path(self, p):
        if E.WIN:
            os.startfile(p)
        else:
            subprocess.run(["xdg-open" if LINUX else "open", p])

    def quit(self):
        if self.popup_proc and self.popup_proc.poll() is None:
            self.popup_proc.terminate()
        self.icon.stop()

    # -- background loop: watch Companion, save every 15 minutes --
    def loop(self):
        next_save = time.time() + 60
        while True:
            try:
                self.tick(next_save)
                if time.time() >= next_save:
                    next_save = time.time() + self.save_every()
            except Exception:
                log.exception("background loop")
            time.sleep(CHECK_EVERY)

    def tick(self, next_save):
        running = E.companion_running(self.s)
        # A service or container is stopped by takes and updates, not by quitting:
        # only a desktop app being quit returns the config.
        quit_now = self.companion_was_running and not running and not E.companion_managed(self.s)
        self.companion_was_running = running
        if not self.work.acquire(blocking=False):
            return
        try:
            if quit_now and self.mine:
                self.set_state("busy", "Returning config…")
                try:
                    self.notify(E.return_(self.s))
                    self.mine = False
                except E.SyncError as e:
                    self.notify(f"Couldn't return the config: {str(e).splitlines()[0]}\n"
                                f"It stays with this rig; it'll save when back online.")
                self.refresh(force=True)
            elif running and not self.mine and not self.warned_not_holding:
                self.warned_not_holding = True
                self.notify("Companion is open but this rig doesn't have the config. "
                            "Changes won't be kept.")
            elif (time.time() >= next_save and not self.timer_saves
                  and (self.mine or os.path.exists(self.s.holding_file))):
                if E.save(self.s, quiet=True):
                    self.last_save = datetime.datetime.now()
                self.refresh(force=True)
            else:
                self.refresh()
            if not running:
                self.warned_not_holding = False
        finally:
            self.work.release()

    def run(self):
        if not login_enabled() and not os.path.exists(os.path.join(data_dir(), "first-run-done")):
            set_login(True)                         # start at login by default
            open(os.path.join(data_dir(), "first-run-done"), "w").close()
        elif login_enabled() and not LINUX:
            set_login(True)                         # point it at this copy (e.g. now installed elsewhere)

        try:
            import web
            self.httpd = web.serve(self, self.gui_port(), bool(self.app_setting("gui_network", True)))
            log.info("web GUI on port %s", self.gui_port())
        except OSError as e:
            log.info("web GUI didn't start on port %s: %s", self.gui_port(), e)
        self.popup_start()                          # hidden until the icon is clicked

        def setup(icon):
            icon.visible = True
            self.refresh(force=True)
            if not E.signed_in(self.s):
                self.notify(f"Not signed in to {self.s.place}. Use Storage & sign-in in the GUI.")
            elif self.s.login_action == "take_and_open" and LOGGED_IN_START:
                self.bg(lambda: self.open_companion(auto=True))()
            threading.Thread(target=self.loop, daemon=True).start()
        self.icon.run(setup=setup)


def main():
    if "--popup" in sys.argv:                  # the pop-up window's own process
        import popup
        i = sys.argv.index("--popup")
        port = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else GUI_PORT
        popup.main(port, bundled("logo-source.png"))
        return
    # only one copy at a time (e.g. started at login, then opened by hand)
    guard = socket.socket()
    try:
        guard.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    except OSError:
        tell(f"{APP} is already running. Look for its icon by the clock "
             f"(Windows, Pi) or in the menu bar (Mac).")
        return
    if E.WIN:                       # lets the installer see that the app is open
        import ctypes
        global _mutex
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, WIN_MUTEX)
    log.info("started")
    app = App()
    app.run()
    if app.restart:                 # Pi, after an update: open the new copy
        guard.close()
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])


if __name__ == "__main__":
    main()
