"""Companion Sync: system-tray (Windows) / menu-bar (Mac) app.

Sits in the tray, shows who has the Companion config, and does the sync:
  - Open Companion: takes the config (if it can), then launches Companion
  - saves to the chosen storage (Google Drive by default; see Storage…) every
    15 minutes while this rig has the config
  - returns the config automatically when Companion is quit
All the real work is in engine.py, shared with the Pi.
"""
import dataclasses
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
STATUS_EVERY = 5 * 60       # seconds between lock checks on the storage when idle
SINGLE_INSTANCE_PORT = 47823

try:                          # written by the build from GitHub secrets
    from _client import CLIENT_ID, CLIENT_SECRET, BUILD
except ImportError:
    CLIENT_ID = os.environ.get("CS_CLIENT_ID", "")
    CLIENT_SECRET = os.environ.get("CS_CLIENT_SECRET", "")
    BUILD = "dev"

RELEASES = "https://github.com/nisted/companion-sync/releases/latest"
UPDATE_URL = RELEASES + ("/download/Companion-Sync-Windows.exe" if E.WIN else "/download/Companion-Sync-Mac.zip")


# Windows: a left-click on the tray icon opens the menu, like a right-click,
# instead of running an action.
if E.WIN:
    from pystray import _win32 as _pw

    _orig_notify = _pw.Icon._on_notify

    def _left_click_opens_menu(self, wparam, lparam):
        if lparam == 0x0202:           # WM_LBUTTONUP -> treat as WM_RBUTTONUP
            lparam = 0x0205
        return _orig_notify(self, wparam, lparam)

    _pw.Icon._on_notify = _left_click_opens_menu


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


def choose(prompt, options, title=APP):
    """Pick one of `options` (strings). Returns it, or None if cancelled."""
    if not options:
        return None
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
    try:
        subprocess.run(["clip"] if E.WIN else ["pbcopy"], input=text, text=True,
                       creationflags=E.NO_WINDOW, check=False)
    except OSError:
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
            M("Open Companion", self.bg(self.open_companion)),
            M("Save now", self.bg(self.save_now), enabled=lambda i: self.mine),
            M("Return config", self.bg(self.return_now), enabled=lambda i: self.mine),
            M("Status…", self.bg(self.show_status)),
            pystray.Menu.SEPARATOR,
            M(lambda i: f"Sign in to {self.s.place}…", self.bg(self.sign_in),
              visible=lambda i: not E.signed_in(self.s)),
            M(lambda i: f"Storage: {self.s.place.removeprefix('the ').capitalize()}…", self.bg(self.choose_storage)),
            M("Encryption…", self.bg(self.encryption)),
            M("Start at login", lambda: set_login(not login_enabled()),
              checked=lambda i: login_enabled()),
            M("Open log", lambda: self.open_path(LOG_FILE)),
            pystray.Menu.SEPARATOR,
            M(f"Update {APP}…", lambda: threading.Thread(target=self.update_app, daemon=True).start()),
            M("Get Companion…", lambda: webbrowser.open(E.COMPANION_DOWNLOAD)),
            M(f"{APP} build {BUILD}", None, enabled=False),
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
        if st.get("moved"):
            self.set_state("problem", f"Config has moved {st['moved'].split(' (')[0]}: see Storage…")
        elif st["mine"]:
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
            if ask("Open Companion now?"):
                E.start_companion(self.s)
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

    def update_app(self):
        tell(f"Your browser will download the latest {APP}.\n\n"
             f"When it's downloaded: quit {APP} from this menu, replace the old "
             f"{'Companion Sync.exe' if E.WIN else 'Companion Sync app in Applications'} "
             f"with the new one, and open it again.")
        webbrowser.open(UPDATE_URL)

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
                self.notify(f"Not signed in to {self.s.place}. Choose 'Sign in' from the menu.")
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
