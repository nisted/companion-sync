#!/usr/bin/env python3
"""Companion Sync: share one Companion config between rigs via Google Drive.

  companion-sync status   who holds the config right now
  companion-sync take     lock the config to this rig and download it
  companion-sync save     upload this rig's current config, keep the lock
  companion-sync return   upload this rig's config and release the lock

Only one rig holds the config at a time. The holder saves every 15 minutes
(SQLite live backup, so Companion keeps running). A lock not refreshed for
24 hours can be taken over, after asking. Take replaces the config, so on a
desktop rig Companion must be quit first.

Companion keeps one config folder per version (v5.0, v5.1 ...). Drive records
which version its copy is; a rig with an older Companion refuses a newer
config, and a rig with a newer Companion lets Companion upgrade the old one.

This file is both the engine used by the desktop app and the command-line
tool installed on the Pi.
"""
import datetime
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

REMOTE_NAME = "gdrive"
EXCLUDE = "backups/**"            # Companion's own local backups stay local
VERSION_RE = re.compile(r"^v(\d+)\.(\d+)$")
WIN = sys.platform == "win32"
MAC = sys.platform == "darwin"
NO_WINDOW = 0x08000000 if WIN else 0   # stop console windows flashing up from the app


COMPANION_DOWNLOAD = "https://bitfocus.io/companion"


class SyncError(Exception):
    """A problem to show the user, in plain words."""


class NeedsUpdate(SyncError):
    """This rig's Companion is older than the config: offer the download page."""


class Offline(SyncError):
    pass


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")


def vkey(name):
    m = VERSION_RE.match(name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def vtext(name):
    return name[1:] if name else "?"


# ---- settings per rig ------------------------------------------------------

@dataclass
class Settings:
    platform: str                      # 'pi', 'win' or 'mac'
    base: str                          # Companion's config folder (holds v5.0 etc.)
    safety_dir: str                    # safety copies, OUTSIDE base
    holding_file: str                  # local note: this rig took the config
    rig: str = field(default_factory=lambda: socket.gethostname().split(".")[0].lower())
    folder: str = "CompanionSync"      # folder on Google Drive
    rclone: str = "rclone"
    rclone_config: str = ""            # blank = rclone's default
    companion_app: str = ""            # desktop: what to launch
    keep_local: int = 5
    keep_history: int = 30
    stale_hours: float = 24
    log: object = print

    @property
    def remote(self):
        return f"{REMOTE_NAME}:{self.folder}"

    @property
    def lock(self):
        return f"{self.remote}/LOCK"


def _pick_base(parent):
    """Companion's config folder. It's 'companion' or 'companion-nodejs' depending
    on platform and history; if both exist, use the one with the newest version."""
    best, best_v = os.path.join(parent, "companion"), (-1, -1)
    for d in ("companion", "companion-nodejs"):
        p = os.path.join(parent, d)
        if os.path.isdir(p):
            vs = [vkey(n) for n in os.listdir(p) if vkey(n)]
            v = max(vs) if vs else (-1, -1)
            if v > best_v:
                best, best_v = p, v
    return best


def stable_rig_name(path):
    """This rig's name, fixed the first time it's worked out.

    Macs rename themselves ('c3po' -> 'c3po (3)' / 'c3po-3.local') when they
    think there's a clash on the network. The lock depends on the name staying
    the same, so it's saved on first run and reused from then on.
    """
    try:
        with open(path) as f:
            saved = f.read().strip()
        if saved:
            return saved
    except OSError:
        pass
    if MAC:
        r = run(["scutil", "--get", "ComputerName"], check=False)
        name = r.stdout.strip() or socket.gethostname().split(".")[0]
        name = re.sub(r"\s*\(\d+\)$", "", name)            # 'c3po (3)'  -> 'c3po'
        name = re.sub(r"-\d+$", "", name)                  # 'c3po-3'    -> 'c3po'
    else:
        name = socket.gethostname().split(".")[0]
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "rig"
    with open(path, "w") as f:
        f.write(name + "\n")
    return name


def default_settings(**kw):
    home = os.path.expanduser("~")
    if WIN:
        appdata = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
        app = next((p for p in (
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Companion", "Companion.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "companion", "Companion.exe"),
        ) if os.path.exists(p)), "")
        s = Settings("win", _pick_base(appdata), os.path.join(appdata, "companion-sync-safety-copies"),
                     os.path.join(home, ".companion-sync-holding"), companion_app=app)
    elif MAC:
        sup = os.path.join(home, "Library", "Application Support")
        s = Settings("mac", _pick_base(sup), os.path.join(sup, "companion-sync-safety-copies"),
                     os.path.join(home, ".companion-sync-holding"), companion_app="Companion")
    else:
        s = Settings("pi", "/home/companion/.config/companion-nodejs",
                     "/home/companion/.config/companion-sync-safety-copies",
                     os.path.join(home, ".companion-sync-holding"))
    s.rig = stable_rig_name(os.path.join(home, ".companion-sync-rig-name"))
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ---- running things --------------------------------------------------------

def run(cmd, check=True, input=None):
    r = subprocess.run(cmd, text=True, capture_output=True, input=input, creationflags=NO_WINDOW)
    if check and r.returncode != 0:
        raise SyncError(f"Command failed: {' '.join(cmd)}\n{r.stderr.strip()}")
    return r


def rc(s, *args, check=True, input=None):
    cmd = [s.rclone] + (["--config", s.rclone_config] if s.rclone_config else []) + list(args)
    return run(cmd, check=check, input=input)


def signed_in(s):
    r = rc(s, "listremotes", check=False)
    return r.returncode == 0 and f"{REMOTE_NAME}:" in r.stdout.split()


def sign_in(s, client_id, client_secret):
    """Link Google Drive. Opens the browser for the Google sign-in."""
    rc(s, "config", "create", REMOTE_NAME, "drive", "scope", "drive.file",
       "client_id", client_id, "client_secret", client_secret)


def _sudo(s, cmd):
    return ["sudo"] + cmd if s.platform == "pi" else cmd


def listdir(s, path):
    if s.platform == "pi":
        r = run(["sudo", "ls", path], check=False)
        return r.stdout.split() if r.returncode == 0 else []
    return os.listdir(path) if os.path.isdir(path) else []


def isdir(s, path):
    if s.platform == "pi":
        return run(["sudo", "test", "-d", path], check=False).returncode == 0
    return os.path.isdir(path)


# ---- Companion versions ----------------------------------------------------

def local_version(s):
    """The newest version folder Companion has on this rig, e.g. 'v5.0'."""
    vs = [n for n in listdir(s, s.base) if vkey(n)]
    if not vs:
        raise SyncError(f"Can't find Companion's config in\n  {s.base}\n"
                        f"Has Companion been run on this rig at least once?")
    return max(vs, key=vkey)


def drive_version(s):
    r = rc(s, "cat", f"{s.remote}/CURRENT", check=False)
    if r.returncode == 0 and vkey(r.stdout.strip()):
        return r.stdout.strip()
    _raise_if_offline(r)
    r = rc(s, "lsf", "--dirs-only", s.remote, check=False)
    vs = [d.rstrip("/") for d in r.stdout.split() if vkey(d.rstrip("/"))]
    return max(vs, key=vkey) if vs else None


# ---- Companion start/stop --------------------------------------------------

def companion_running(s):
    if s.platform == "pi":
        return run(["systemctl", "is-active", "--quiet", "companion"], check=False).returncode == 0
    if s.platform == "win":
        out = run(["tasklist", "/FI", "IMAGENAME eq Companion.exe", "/NH"], check=False).stdout
        return "companion.exe" in out.lower()
    return run(["pgrep", "-x", "Companion"], check=False).returncode == 0


def stop_companion(s):
    if s.platform == "pi":
        s.log("Stopping Companion...")
        run(["sudo", "systemctl", "stop", "companion"])
    elif companion_running(s):
        raise SyncError("Companion is open. Quit it first, then try again.")


def start_companion(s):
    if s.platform == "pi":
        s.log("Starting Companion...")
        run(["sudo", "systemctl", "start", "companion"])
    elif s.platform == "win":
        if not s.companion_app:
            s.log("Couldn't find Companion.exe. Start Companion yourself.")
            return
        s.log("Starting Companion...")
        subprocess.Popen([s.companion_app], close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x00000008 | 0x00000200)   # detached, own process group
    else:
        s.log("Starting Companion...")
        run(["open", "-a", s.companion_app or "Companion"], check=False)


# ---- copying config --------------------------------------------------------

def _snapshot_local(src_dir, dest):
    """Consistent copy of a config folder WHILE Companion runs.

    Databases are copied with SQLite's live backup, which gives a clean
    snapshot even mid-write; their -wal/-shm side files aren't needed.
    """
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if not (root == src_dir and d == "backups")]
        out = os.path.join(dest, os.path.relpath(root, src_dir))
        os.makedirs(out, exist_ok=True)
        for f in files:
            a, b = os.path.join(root, f), os.path.join(out, f)
            if f.endswith((".sqlite-wal", ".sqlite-shm")):
                continue
            if f.endswith(".sqlite"):
                src = sqlite3.connect(f"file:{a}?mode=ro", uri=True)
                dst = sqlite3.connect(b)
                src.backup(dst)
                dst.close()
                src.close()
            else:
                shutil.copy2(a, b)


def snapshot(s, version, dest):
    src = os.path.join(s.base, version)
    if s.platform == "pi":
        # Companion's files belong to the 'companion' user, so snapshot as root
        run(["sudo", sys.executable, os.path.abspath(__file__), "_snapshot", src, dest])
        run(["sudo", "chown", "-R", os.environ.get("USER", "root"), dest])
    else:
        _snapshot_local(src, dest)


def tidy_safety_copies(s):
    """Move any safety copies out of Companion's folder, keep the newest few.

    They must live outside it: Companion treats any folder next to v5.0 as
    another version of itself and complains on start-up.
    """
    if s.platform == "pi":
        run(["sudo", "mkdir", "-p", s.safety_dir])
    else:
        os.makedirs(s.safety_dir, exist_ok=True)
    for n in listdir(s, s.base):
        if ".before-" in n and vkey(n.split(".before-")[0]):
            a, b = os.path.join(s.base, n), os.path.join(s.safety_dir, n)
            run(["sudo", "mv", a, b]) if s.platform == "pi" else shutil.move(a, b)
    copies = sorted((n for n in listdir(s, s.safety_dir) if ".before-" in n),
                    key=lambda n: n.split(".before-")[1][-17:])
    for n in copies[:-s.keep_local]:
        p = os.path.join(s.safety_dir, n)
        run(["sudo", "rm", "-rf", p]) if s.platform == "pi" else shutil.rmtree(p, ignore_errors=True)


def _move_to_safety(s, version, reason):
    src = os.path.join(s.base, version)
    if not isdir(s, src):
        return None
    dst = os.path.join(s.safety_dir, f"{version}.before-{reason}-{now()}")
    run(["sudo", "mv", src, dst]) if s.platform == "pi" else shutil.move(src, dst)
    return dst


def install_config(s, version, src):
    """Make `src` Companion's config for `version`, keeping a safety copy."""
    tidy_safety_copies(s)
    target = os.path.join(s.base, version)
    safety = os.path.join(s.safety_dir, f"{version}.before-take-{now()}")
    if s.platform == "pi":
        run(["sudo", "mkdir", "-p", target])
        run(["sudo", "cp", "-a", target, safety])
        run(["sudo", "rsync", "-a", "--delete", "--exclude", "backups/", f"{src}/", f"{target}/"])
        run(["sudo", "chown", "-R", "companion:companion", target])
    else:
        os.makedirs(target, exist_ok=True)
        shutil.copytree(target, safety)
        for name in os.listdir(target):          # clear all but Companion's own backups
            if name == "backups":
                continue
            p = os.path.join(target, name)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        shutil.copytree(src, target, dirs_exist_ok=True)
    tidy_safety_copies(s)
    s.log(f"Safety copy of the old config: {safety}")


# ---- lock ------------------------------------------------------------------
# The LOCK file on Drive says which rig holds the config. The holder refreshes
# it on every save, so its age = time since the holder was last seen online.
# The holding file is a local note that this rig took the config, so a rig
# whose lock was taken over knows its unsaved edits are stranded.

def _raise_if_offline(r):
    """rclone failed: a missing file is fine, anything else is a problem."""
    if r.returncode == 0:
        return
    err = r.stderr.lower()
    if "didn't find section" in err or "not configured" in err:
        raise Offline("Not signed in to Google Drive yet.")
    if not ("object not found" in err or "directory not found" in err or "doesn't exist" in err):
        raise Offline("Can't reach Google Drive. Are you online?\n" + r.stderr.strip()[-300:])


def lock_holder(s):
    r = rc(s, "cat", s.lock, check=False)
    _raise_if_offline(r)
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def holder_name(text):
    return text.split("|")[0].strip().lower() if text else None


def taken_time(text):
    for part in (text or "").split("|"):
        if part.strip().startswith("taken "):
            return part.strip()[6:]
    return now()


def lock_age_hours(s):
    r = rc(s, "lsjson", s.lock, check=False)
    try:
        t = json.loads(r.stdout)[0]["ModTime"]            # e.g. 2026-09-30T12:03:01.123456789+01:00
        t = re.sub(r"\.\d+", "", t).replace("Z", "+00:00")
        return (datetime.datetime.now(datetime.timezone.utc)
                - datetime.datetime.fromisoformat(t)).total_seconds() / 3600
    except Exception:
        return 0.0


def write_lock(s, taken):
    rc(s, "rcat", s.lock, input=f"{s.rig} | taken {taken} | last seen {now()}\n")


def mark_holding(s, on):
    if on:
        with open(s.holding_file, "w") as f:
            f.write(now())
    elif os.path.exists(s.holding_file):
        os.remove(s.holding_file)


def was_holding(s):
    return os.path.exists(s.holding_file)


# ---- upload ----------------------------------------------------------------

def upload(s, version, dest=None):
    """Snapshot `version` and upload it (to the main copy by default).

    Only changed files are sent (compared by checksum); anything replaced on
    Drive is kept in history/ so you can roll back.
    """
    main = f"{s.remote}/{version}"
    dest = dest or main
    with tempfile.TemporaryDirectory() as tmp:
        up = os.path.join(tmp, version)
        snapshot(s, version, up)
        cmd = ["sync", up, dest, "--checksum", "--exclude", EXCLUDE]
        if dest == main:
            cmd += ["--backup-dir", f"{s.remote}/history/{now()}_{s.rig}"]
        rc(s, *cmd)
        rc(s, "check", up, dest, "--exclude", EXCLUDE)
    if dest == main:
        rc(s, "rcat", f"{s.remote}/CURRENT", input=version + "\n")
    r = rc(s, "lsf", "--dirs-only", f"{s.remote}/history", check=False)
    for d in sorted(r.stdout.split())[:-s.keep_history]:
        rc(s, "purge", f"{s.remote}/history/{d.rstrip('/')}", check=False)


# ---- modules ---------------------------------------------------------------
# Companion keeps installed modules beside the version folders, not in them:
#   <base>/modules/<id>-<version>    connection modules
#   <base>/surfaces/<id>-<version>   surface integrations
# so the config alone doesn't carry them. Store modules get re-downloaded by
# Companion when it's online, but custom ones (imported packages) don't exist
# anywhere else. Each version folder never changes once installed, so on save
# any folder Drive doesn't have yet is uploaded to modules/ on Drive, and on
# take any folder this rig doesn't have is copied in before Companion starts.
# modules/modules.json lists what's on Drive. Nothing is ever deleted.

MODULE_KINDS = {"modules": "connection", "surfaces": "surface"}
MODULE_FOLDER_RE = re.compile(r"^(.+)-(\d+\.\d+\.\d+.*)$")     # 'allenheath-sq-3.1.1-grid.6'


def _local_module_folders(s, sub):
    d = os.path.join(s.base, sub)
    return sorted(n for n in listdir(s, d) if MODULE_FOLDER_RE.match(n) and isdir(s, os.path.join(d, n)))


def _drive_module_folders(s, sub):
    r = rc(s, "lsf", "--dirs-only", f"{s.remote}/modules/{sub}", check=False)
    _raise_if_offline(r)
    return sorted(d.rstrip("/") for d in r.stdout.split() if MODULE_FOLDER_RE.match(d.rstrip("/")))


def _copy_folder(s, src, dst):
    """Copy a module folder; on the Pi, Companion's folders belong to the 'companion' user."""
    if s.platform == "pi":
        run(["sudo", "mkdir", "-p", os.path.dirname(dst)])
        run(["sudo", "cp", "-a", src, dst])
    else:
        shutil.copytree(src, dst)


def upload_modules(s):
    """Upload installed module versions Drive doesn't have yet. Returns their names."""
    added = []
    with tempfile.TemporaryDirectory() as tmp:
        for sub in MODULE_KINDS:
            have = set(_drive_module_folders(s, sub))
            for name in _local_module_folders(s, sub):
                if name in have:
                    continue
                staged = os.path.join(tmp, sub, name)
                _copy_folder(s, os.path.join(s.base, sub, name), staged)
                if s.platform == "pi":
                    run(["sudo", "chown", "-R", os.environ.get("USER", "root"), os.path.join(tmp, sub)])
                rc(s, "copy", staged, f"{s.remote}/modules/{sub}/{name}")
                added.append(name)
    if added or rc(s, "lsf", f"{s.remote}/modules/modules.json", check=False).returncode != 0:
        _write_modules_list(s)
    return added


def _write_modules_list(s):
    listing = {"updated": now(), "by": s.rig, "modules": []}
    for sub, kind in MODULE_KINDS.items():
        for name in _drive_module_folders(s, sub):
            m = MODULE_FOLDER_RE.match(name)
            listing["modules"].append({"type": kind, "id": m.group(1), "version": m.group(2), "folder": f"{sub}/{name}"})
    rc(s, "rcat", f"{s.remote}/modules/modules.json", input=json.dumps(listing, indent=2) + "\n")


def download_modules(s):
    """Copy in module versions this rig is missing. Companion must be stopped. Returns their names."""
    added = []
    with tempfile.TemporaryDirectory() as tmp:
        for sub in MODULE_KINDS:
            have = set(_local_module_folders(s, sub))
            for name in _drive_module_folders(s, sub):
                if name in have:
                    continue
                dl = os.path.join(tmp, sub, name)
                rc(s, "copy", f"{s.remote}/modules/{sub}/{name}", dl)
                if not os.path.exists(os.path.join(dl, "companion", "manifest.json")):
                    s.log(f"Skipped module {name}: incomplete on Google Drive")
                    continue
                dst = os.path.join(s.base, sub, name)
                _copy_folder(s, dl, dst)
                if s.platform == "pi":
                    run(["sudo", "chown", "-R", "companion:companion", dst])
                added.append(name)
    return added


def _modules_step(s, fn, verb):
    """Module sync never stops a take/save/return: problems are reported, not raised."""
    try:
        added = fn(s)
        if added:
            s.log(f"Modules {verb}: {', '.join(added)}")
        return added
    except SyncError as e:
        s.log(f"Modules not synced: {e}")
        return []


def _save_stranded(s, holder):
    upload(s, local_version(s), f"{s.remote}/stranded/{s.rig}")
    return (f"NOT saved to the main copy: the config was taken over by {holder_name(holder) or 'nobody'}.\n"
            f"This rig's version is safe on Google Drive in {s.folder}/stranded/{s.rig}.")


# ---- commands --------------------------------------------------------------

def status(s):
    """Plain facts for display. Raises Offline if Drive can't be reached."""
    tidy_safety_copies(s)
    h = lock_holder(s)
    return {
        "holder": holder_name(h),
        "mine": holder_name(h) == s.rig,
        "age_hours": lock_age_hours(s) if h else None,
        "drive_version": drive_version(s),
        "stranded": was_holding(s) and holder_name(h) != s.rig,
    }


def take(s, confirm_takeover=None):
    """Lock the config to this rig and install Drive's copy.

    confirm_takeover(message) -> bool is asked before taking over a stale
    lock; without it, a stale lock is never taken over (e.g. at boot).
    """
    lv = local_version(s)
    h = lock_holder(s)
    if h and holder_name(h) == s.rig:
        mark_holding(s, True)
        return f"This rig ({s.rig}) already has the config."
    if h:
        age = lock_age_hours(s)
        if age < s.stale_hours:
            raise SyncError(f"The config is being used by {holder_name(h)} "
                            f"(last seen {age:.1f} hours ago). Return it on that rig first.")
        msg = (f"The config is held by {holder_name(h)}, but that rig hasn't been seen for "
               f"{age:.0f} hours.\n\nIf you take it, any changes that rig hasn't saved stay on it "
               f"(they go to 'stranded' on Google Drive when it next comes online).\n\nTake it anyway?")
        if not (confirm_takeover and confirm_takeover(msg)):
            raise SyncError("Not taken.")
    dv = drive_version(s)
    if not dv:
        raise SyncError("There's no config on Google Drive yet.")
    if vkey(dv) > vkey(lv):
        raise NeedsUpdate(f"The config on Google Drive is from Companion {vtext(dv)}, but this rig "
                        f"has Companion {vtext(lv)}.\nUpdate Companion on this rig first.")
    stop_companion(s)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dl = os.path.join(tmp, dv)
            s.log("Downloading config from Google Drive...")
            rc(s, "copy", f"{s.remote}/{dv}", dl, "--exclude", EXCLUDE)
            if not os.path.exists(os.path.join(dl, "db.sqlite")):
                raise SyncError("The config on Google Drive looks incomplete (no db.sqlite).")
            install_config(s, dv, dl)
            if vkey(dv) < vkey(lv):
                # This rig has a newer Companion. Move its own newer folder aside so
                # Companion upgrades the downloaded config when it starts.
                moved = _move_to_safety(s, lv, "upgrade")
                s.log(f"Companion {vtext(lv)} will upgrade the {vtext(dv)} config when it starts. "
                      f"Old {lv} folder kept at {moved}")
        _modules_step(s, download_modules, "added to this rig")
    finally:
        start_companion(s)
    write_lock(s, now())
    mark_holding(s, True)
    return f"{s.rig} now has the config."


def save(s, quiet=False):
    """Upload the current config and keep the lock.

    quiet=True is for the 15-minute timer: returns None without error if this
    rig doesn't hold the config or Drive can't be reached.
    """
    try:
        h = lock_holder(s)
    except Offline:
        if quiet:
            return None
        raise
    if holder_name(h) != s.rig:
        if was_holding(s):
            return _save_stranded(s, h)
        if quiet:
            return None
        raise SyncError("This rig doesn't have the config, so there's nothing to save.")
    lv = local_version(s)
    dv = drive_version(s)
    if dv and vkey(lv) < vkey(dv):
        raise NeedsUpdate(f"This rig's Companion ({vtext(lv)}) is older than the config on "
                        f"Google Drive ({vtext(dv)}). Not saved.")
    upload(s, lv)
    _modules_step(s, upload_modules, "uploaded")
    write_lock(s, taken_time(h))
    mark_holding(s, True)
    note = f" (config upgraded to Companion {vtext(lv)})" if dv and lv != dv else ""
    return f"Saved to Google Drive{note}. {s.rig} still has the config."


def return_(s):
    h = lock_holder(s)
    if holder_name(h) != s.rig:
        if was_holding(s):
            msg = _save_stranded(s, h)
            mark_holding(s, False)
            return msg
        raise SyncError("This rig doesn't have the config, so there's nothing to return.")
    s.log("Uploading config to Google Drive...")
    lv = local_version(s)
    upload(s, lv)
    _modules_step(s, upload_modules, "uploaded")
    rc(s, "deletefile", s.lock)
    mark_holding(s, False)
    return f"Config returned and unlocked. Changes made on {s.rig} from now on won't be kept."


# ---- command line (the Pi) -------------------------------------------------

def _cli_confirm(msg):
    if not sys.stdin.isatty():
        return False
    print(msg)
    return input("(y/N) ").strip().lower() == "y"


def main(argv):
    if argv[:1] == ["_snapshot"] and len(argv) == 3:     # internal, run via sudo on the Pi
        _snapshot_local(argv[1], argv[2])
        return 0
    s = default_settings()
    cmd = argv[0] if argv else ""
    quiet = "--quiet" in argv
    try:
        if cmd == "status":
            st = status(s)
            if st["holder"]:
                print(f"Config is TAKEN by: {st['holder']}  "
                      f"(last seen {st['age_hours']:.1f} hours ago)")
            else:
                print("Config is free.")
            print(f"Google Drive has Companion {vtext(st['drive_version'])} config; "
                  f"this rig has {vtext(local_version(s))}.")
            if st["stranded"]:
                print("This rig's lock was taken over; its next save goes to 'stranded'.")
        elif cmd == "take":
            print("\nDONE. " + take(s, _cli_confirm))
        elif cmd == "save":
            msg = save(s, quiet)
            if msg and not quiet:
                print(msg)
        elif cmd == "return":
            print("\nDONE. " + return_(s))
        else:
            print(__doc__)
            return 1
    except SyncError as e:
        if quiet:
            return 0
        print(f"\n{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
