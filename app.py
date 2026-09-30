"""Companion Sync: system-tray (Windows) / menu-bar (Mac) app.

Sits in the tray, shows who has the Companion config, and does the sync:
  - Open Companion: takes the config (if it can), then launches Companion
  - saves to Google Drive every 15 minutes while this rig has the config
  - returns the config automatically when Companion is quit
All the real work is in engine.py, shared with the Pi.
"""
import datetime
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

import pystray
from PIL import Image, ImageDraw

import engine as E
from icon import draw_cs

APP = "Companion Sync"
SAVE_EVERY = 15 * 60        # seconds
CHECK_EVERY = 30            # seconds between Companion / status checks
STATUS_EVERY = 5 * 60       # seconds between lock checks on Drive when idle
SINGLE_INSTANCE_PORT = 47823

try:                          # written by the build from GitHub secrets
    from _client import CLIENT_ID, CLIENT_SECRET
except ImportError:
    CLIENT_ID = os.environ.get("CS_CLIENT_ID", "")
    CLIENT_SECRET = os.environ.get("CS_CLIENT_SECRET", "")


# ---- places ------------------------------------------------------------------

def data_dir():
    if E.WIN:
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
    else:
        d = os.path.expanduser(f"~/Library/Application Support/{APP}")
    os.makedirs(d, exist_ok=True)
    return d


def bundled(name):
    """Path to a file shipped inside the app (rclone), or None."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, name)
    return p if os.path.exists(p) else None


LOG_FILE = os.path.join(data_dir(), "companion-sync.log")
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(APP)


# ---- native dialogs (safe from any thread, unlike tkinter on the Mac) ------------

def _osa(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def ask(msg, title=APP):
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
    if E.WIN:
        import ctypes
        flags = (0x10 if problem else 0x40) | 0x10000 | 0x40000
        ctypes.windll.user32.MessageBoxW(0, msg, title, flags)
    else:
        subprocess.run(["osascript", "-e",
                        f'display dialog {_osa(msg)} with title {_osa(title)} buttons {{"OK"}} '
                        f'default button "OK"' + (" with icon stop" if problem else "")],
                       capture_output=True)


# ---- start at login ------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PLIST = os.path.expanduser("~/Library/LaunchAgents/uk.co.nigelisted.companionsync.plist")


def _mac_app_path():
    # .../Companion Sync.app/Contents/MacOS/Companion Sync  ->  .../Companion Sync.app
    return os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", ".."))


def login_enabled():
    if E.WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, APP)
                return True
        except OSError:
            return False
    return os.path.exists(PLIST)


def set_login(on):
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
        self.icon = pystray.Icon(APP, make_icon("busy"), APP, menu=self.menu())

    # -- menu --
    def menu(self):
        M = pystray.MenuItem
        return pystray.Menu(
            M(lambda i: self.line, None, enabled=False),
            pystray.Menu.SEPARATOR,
            M("Open Companion", self.bg(self.open_companion), default=True),
            M("Save now", self.bg(self.save_now), enabled=lambda i: self.mine),
            M("Return config", self.bg(self.return_now), enabled=lambda i: self.mine),
            M("Status…", self.bg(self.show_status)),
            pystray.Menu.SEPARATOR,
            M("Sign in to Google Drive…", self.bg(self.sign_in),
              visible=lambda i: not E.signed_in(self.s)),
            M("Start at login", lambda: set_login(not login_enabled()),
              checked=lambda i: login_enabled()),
            M("Open log", lambda: self.open_path(LOG_FILE)),
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
            if E.MAC:
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
            st = E.status(self.s)
        except E.Offline as e:
            self.mine = os.path.exists(self.s.holding_file)
            self.set_state("problem", str(e).splitlines()[0])
            return
        self.mine = st["mine"]
        if st["mine"]:
            saved = f", saved {self.last_save:%H:%M}" if self.last_save else ""
            self.set_state("mine", f"This rig has the config{saved}")
        elif st["holder"]:
            self.set_state("other", f"Config is with {st['holder']}")
        else:
            self.set_state("free", "Config is free")

    # -- actions --
    def open_companion(self):
        if E.companion_running(self.s):
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
        except E.NeedsUpdate as e:
            self.offer_download(str(e))
        except E.SyncError as e:
            if ask(f"{e}\n\nOpen Companion anyway, with this rig's own copy?\n"
                   f"(Changes won't be kept.)"):
                E.start_companion(self.s)

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
                f"Google Drive copy: Companion {E.vtext(st['drive_version'])}{extra}")
        dv = st["drive_version"]
        if dv and E.vkey(dv) > E.vkey(lv):
            self.offer_download(text + f"\n\nThis rig needs Companion {E.vtext(dv)} to use the config.")
        else:
            tell(text)

    def offer_download(self, msg):
        if ask(f"{msg}\n\nOpen the Companion download page?"):
            webbrowser.open(E.COMPANION_DOWNLOAD)

    def sign_in(self):
        if not CLIENT_ID:
            raise E.SyncError("This build has no Google sign-in details.")
        tell("Your browser will open to sign in to Google.\n\n"
             "Sign in with the account the configs are stored in, then click Allow.")
        E.sign_in(self.s, CLIENT_ID, CLIENT_SECRET)
        self.notify("Signed in to Google Drive.")

    def open_path(self, p):
        if E.WIN:
            os.startfile(p)
        else:
            subprocess.run(["open", p])

    def quit(self):
        self.icon.stop()

    # -- background loop: watch Companion, save every 15 minutes --
    def loop(self):
        next_save = time.time() + 60
        while True:
            try:
                self.tick(next_save)
                if time.time() >= next_save:
                    next_save = time.time() + SAVE_EVERY
            except Exception:
                log.exception("background loop")
            time.sleep(CHECK_EVERY)

    def tick(self, next_save):
        running = E.companion_running(self.s)
        quit_now = self.companion_was_running and not running
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
            elif time.time() >= next_save and (self.mine or os.path.exists(self.s.holding_file)):
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

        def setup(icon):
            icon.visible = True
            self.refresh(force=True)
            if not E.signed_in(self.s):
                self.notify("Not signed in to Google Drive. Choose 'Sign in' from the menu.")
            threading.Thread(target=self.loop, daemon=True).start()
        self.icon.run(setup=setup)


def main():
    # only one copy at a time (e.g. started at login, then opened by hand)
    guard = socket.socket()
    try:
        guard.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    except OSError:
        tell(f"{APP} is already running. Look for its icon by the clock "
             f"(Windows) or in the menu bar (Mac).")
        return
    log.info("started")
    App().run()


if __name__ == "__main__":
    main()
