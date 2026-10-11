#!/usr/bin/env python3
"""Companion Sync: share one Companion config between rigs via Google Drive.

  companion-sync status   who holds the config right now
  companion-sync take     lock the config to this rig and download it
  companion-sync save     upload this rig's current config, keep the lock
  companion-sync return   upload this rig's config and release the lock
  companion-sync boot --report FILE
                          at start-up, before Companion runs: take the config
                          if it's free or already this rig's, never ask, never
                          wait long; the outcome is written to FILE as JSON

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
import time
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
    rclone_flags: list = field(default_factory=list)   # extra flags on every rclone call
    state_file: str = ""               # last save / last problem, for the Pi panel
    storage: str = "rclone"            # 'rclone' (Google Drive etc.) or 'folder'
    rclone_remote: str = REMOTE_NAME   # which rclone remote, when storage is 'rclone'
    folder_path: str = ""              # the shared folder, when storage is 'folder'
    companion_app: str = ""            # desktop: what to launch
    keep_local: int = 5
    keep_history: int = 30
    stale_hours: float = 24
    log: object = print

    @property
    def remote(self):
        return f"{self.rclone_remote}:{self.folder}"

    @property
    def store(self):
        """Where the shared config lives. Made fresh each time, so a copy of the
        settings with different rclone flags (the Pi panel) behaves accordingly."""
        if self.storage == "folder":
            return FolderStorage(self.folder_path)
        return RcloneStorage(self)


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
    s.state_file = os.path.join(home, ".companion-sync-state.json")
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
    cmd = [s.rclone] + (["--config", s.rclone_config] if s.rclone_config else []) + list(s.rclone_flags) + list(args)
    return run(cmd, check=check, input=input)


# ---- local record of how syncing is going (shown on the Pi panel) ----------

def read_state(s):
    try:
        with open(s.state_file) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def record(s, **changes):
    """Merge changes into the state file. Never lets a write problem stop a sync."""
    if not s.state_file:
        return
    st = read_state(s)
    st.update(changes)
    try:
        tmp = s.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=1)
        os.replace(tmp, s.state_file)
    except OSError:
        pass


def record_ok(s, action):
    t = datetime.datetime.now().isoformat(timespec="seconds")
    record(s, last_save=t, last_action=action, last_error=None, last_error_time=None)


def record_error(s, action, message):
    record(s, last_error=f"{action}: {message}".strip(),
           last_error_time=datetime.datetime.now().isoformat(timespec="seconds"))


def signed_in(s):
    return s.store.signed_in()


# ---- storage ---------------------------------------------------------------
# Everything the sync needs from wherever the shared config lives. Paths are
# relative to the Companion Sync folder there ('' is the folder itself), with
# '/' as the separator. Reads raise Offline when the storage can't be reached;
# a missing file or folder is not an error.

class RcloneStorage:
    """Google Drive (and anything else rclone can reach)."""

    def __init__(self, s):
        self.s = s

    def _p(self, rel):
        return f"{self.s.remote}/{rel}" if rel else self.s.remote

    def describe(self):
        return f"{self.s.rclone_remote}:{self.s.folder}"

    def signed_in(self):
        r = rc(self.s, "listremotes", check=False)
        return r.returncode == 0 and f"{self.s.rclone_remote}:" in r.stdout.split()

    def read(self, rel):
        r = rc(self.s, "cat", self._p(rel), check=False)
        _raise_if_offline(r)
        return r.stdout if r.returncode == 0 else None

    def write(self, rel, text):
        rc(self.s, "rcat", self._p(rel), input=text)

    def delete(self, rel):
        rc(self.s, "deletefile", self._p(rel))

    def age_hours(self, rel):
        r = rc(self.s, "lsjson", self._p(rel), check=False)
        try:
            t = json.loads(r.stdout)[0]["ModTime"]        # e.g. 2026-09-30T12:03:01.123456789+01:00
            t = re.sub(r"\.\d+", "", t).replace("Z", "+00:00")
            return (datetime.datetime.now(datetime.timezone.utc)
                    - datetime.datetime.fromisoformat(t)).total_seconds() / 3600
        except Exception:
            return 0.0

    def dirs(self, rel, strict=False):
        r = rc(self.s, "lsf", "--dirs-only", self._p(rel), check=False)
        if strict:
            _raise_if_offline(r)
        return [d.rstrip("/") for d in r.stdout.split()] if r.returncode == 0 else []

    def exists(self, rel):
        return rc(self.s, "lsf", self._p(rel), check=False).returncode == 0

    def push_tree(self, local, rel, backup_rel=None):
        """Make `rel` match the local folder (Companion's own backups excluded),
        keeping anything replaced or removed in `backup_rel`, then verify."""
        cmd = ["sync", local, self._p(rel), "--checksum", "--exclude", EXCLUDE]
        if backup_rel:
            cmd += ["--backup-dir", self._p(backup_rel)]
        rc(self.s, *cmd)
        rc(self.s, "check", local, self._p(rel), "--exclude", EXCLUDE)

    def pull_tree(self, rel, local):
        rc(self.s, "copy", self._p(rel), local, "--exclude", EXCLUDE)

    def copy_up(self, local, rel):
        rc(self.s, "copy", local, self._p(rel))

    def copy_down(self, rel, local):
        rc(self.s, "copy", self._p(rel), local)

    def purge(self, rel):
        rc(self.s, "purge", self._p(rel), check=False)


class FolderStorage:
    """A shared folder: network share / NAS, or a folder a sync app keeps up to
    date (iCloud Drive). The folder itself must exist; Companion Sync makes
    everything inside it."""

    def __init__(self, root):
        self.root = root

    def _p(self, rel):
        return os.path.join(self.root, *rel.split("/")) if rel else self.root

    def _check(self):
        if not self.root or not os.path.isdir(self.root):
            raise Offline(f"Can't reach the shared folder:\n  {self.root or '(none chosen)'}")

    def describe(self):
        return self.root

    def signed_in(self):
        return bool(self.root) and os.path.isdir(self.root)

    def read(self, rel):
        self._check()
        try:
            with open(self._p(rel), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise Offline(f"Can't read from the shared folder: {e}")

    def write(self, rel, text):
        self._check()
        p = self._p(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)

    def delete(self, rel):
        self._check()
        try:
            os.remove(self._p(rel))
        except FileNotFoundError:
            pass

    def age_hours(self, rel):
        try:
            return (time.time() - os.path.getmtime(self._p(rel))) / 3600
        except OSError:
            return 0.0

    def dirs(self, rel, strict=False):
        if strict:
            self._check()
        p = self._p(rel)
        if not os.path.isdir(p):
            return []
        return sorted(n for n in os.listdir(p) if os.path.isdir(os.path.join(p, n)))

    def exists(self, rel):
        return os.path.exists(self._p(rel))

    @staticmethod
    def _files(top):
        """Relative paths of files under top, without Companion's own backups."""
        out = {}
        for root, dirs, files in os.walk(top):
            if root == top:
                dirs[:] = [d for d in dirs if d != "backups"]
            for f in files:
                full = os.path.join(root, f)
                out[os.path.relpath(full, top)] = full
        return out

    @staticmethod
    def _same(a, b):
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                x, y = fa.read(1 << 20), fb.read(1 << 20)
                if x != y:
                    return False
                if not x:
                    return True

    def push_tree(self, local, rel, backup_rel=None):
        self._check()
        dest = self._p(rel)
        backup = self._p(backup_rel) if backup_rel else None
        os.makedirs(dest, exist_ok=True)
        have, want = self._files(dest), self._files(local)
        for r, full in have.items():                      # replaced or removed: keep a copy
            if r not in want or not self._same(want[r], full):
                if backup:
                    os.makedirs(os.path.dirname(os.path.join(backup, r)), exist_ok=True)
                    shutil.move(full, os.path.join(backup, r))
                else:
                    os.remove(full)
        for r, full in want.items():
            target = os.path.join(dest, r)
            if not os.path.exists(target):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(full, target)
        have = self._files(dest)                           # verify
        if set(have) != set(want) or any(not self._same(want[r], have[r]) for r in want):
            raise SyncError("The copy in the shared folder doesn't match after saving. Try again.")

    def pull_tree(self, rel, local):
        self._check()
        for r, full in self._files(self._p(rel)).items():
            target = os.path.join(local, r)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(full, target)

    def copy_up(self, local, rel):
        self._check()
        shutil.copytree(local, self._p(rel), dirs_exist_ok=True)

    def copy_down(self, rel, local):
        self._check()
        shutil.copytree(self._p(rel), local, dirs_exist_ok=True)

    def purge(self, rel):
        shutil.rmtree(self._p(rel), ignore_errors=True)


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
    cur = (s.store.read("CURRENT") or "").strip()
    if vkey(cur):
        return cur
    vs = [d for d in s.store.dirs("") if vkey(d)]
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
    text = s.store.read("LOCK")
    return (text.strip() or None) if text is not None else None


def holder_name(text):
    return text.split("|")[0].strip().lower() if text else None


def taken_time(text):
    for part in (text or "").split("|"):
        if part.strip().startswith("taken "):
            return part.strip()[6:]
    return now()


def lock_age_hours(s):
    return s.store.age_hours("LOCK")


def write_lock(s, taken):
    s.store.write("LOCK", f"{s.rig} | taken {taken} | last seen {now()}\n")


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

    `dest` is a path inside the Companion Sync folder. Only changed files are
    sent; anything replaced in the main copy is kept in history/ so you can
    roll back.
    """
    main = version
    dest = dest or main
    with tempfile.TemporaryDirectory() as tmp:
        up = os.path.join(tmp, version)
        snapshot(s, version, up)
        s.store.push_tree(up, dest, f"history/{now()}_{s.rig}" if dest == main else None)
    if dest == main:
        s.store.write("CURRENT", version + "\n")
    for d in sorted(s.store.dirs("history"))[:-s.keep_history]:
        s.store.purge(f"history/{d}")


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
    return sorted(d for d in s.store.dirs(f"modules/{sub}", strict=True) if MODULE_FOLDER_RE.match(d))


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
                s.store.copy_up(staged, f"modules/{sub}/{name}")
                added.append(name)
    if added or not s.store.exists("modules/modules.json"):
        _write_modules_list(s)
    return added


def _write_modules_list(s):
    listing = {"updated": now(), "by": s.rig, "modules": []}
    for sub, kind in MODULE_KINDS.items():
        for name in _drive_module_folders(s, sub):
            m = MODULE_FOLDER_RE.match(name)
            listing["modules"].append({"type": kind, "id": m.group(1), "version": m.group(2), "folder": f"{sub}/{name}"})
    s.store.write("modules/modules.json", json.dumps(listing, indent=2) + "\n")


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
                s.store.copy_down(f"modules/{sub}/{name}", dl)
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
    upload(s, local_version(s), f"stranded/{s.rig}")
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


def take(s, confirm_takeover=None, manage_companion=True):
    """Lock the config to this rig and install Drive's copy.

    confirm_takeover(message) -> bool is asked before taking over a stale
    lock; without it, a stale lock is never taken over (e.g. at boot).
    manage_companion=False: Companion isn't running and mustn't be started
    (the Pi's boot check runs before Companion's service).
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
    if manage_companion:
        stop_companion(s)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dl = os.path.join(tmp, dv)
            s.log("Downloading config from Google Drive...")
            s.store.pull_tree(dv, dl)
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
        if manage_companion:
            start_companion(s)
    write_lock(s, now())
    mark_holding(s, True)
    record_ok(s, "take")
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
    record_ok(s, "save")
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
    s.store.delete("LOCK")
    mark_holding(s, False)
    record_ok(s, "return")
    return f"Config returned and unlocked. Changes made on {s.rig} from now on won't be kept."


def boot(s, net_wait=20):
    """Start-up check on the Pi, before Companion starts. Never asks, never raises.

    Takes the config if it's free or already this rig's; otherwise leaves the
    local copy alone. Returns a dict for the panel:
      result: taken | already | held | offline | not_signed_in | needs_update | error
    """
    out = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "rig": s.rig,
           "holder": None, "message": ""}
    deadline = datetime.datetime.now() + datetime.timedelta(seconds=net_wait)
    while datetime.datetime.now() < deadline and not _have_default_route():
        time.sleep(1)
    try:
        if not signed_in(s):
            out.update(result="not_signed_in", message="Not signed in to Google Drive.")
            return out
        h = lock_holder(s)
        out["holder"] = holder_name(h)
        if h and holder_name(h) != s.rig:
            out.update(result="held", message=f"Config held by {holder_name(h)}. Using the local copy.")
            return out
        msg = take(s, None, manage_companion=False)
        out.update(result="already" if h else "taken", holder=s.rig, message=msg)
    except Offline as e:
        out.update(result="offline", message=str(e).splitlines()[0])
    except NeedsUpdate as e:
        out.update(result="needs_update", message=str(e).replace("\n", " "))
        record_error(s, "start-up", out["message"])
    except SyncError as e:
        out.update(result="error", message=str(e).replace("\n", " "))
        record_error(s, "start-up", out["message"])
    except Exception as e:                      # never stop Companion starting
        out.update(result="error", message=f"{type(e).__name__}: {e}")
    return out


def _have_default_route():
    if not os.path.exists("/proc/net/route"):
        return True
    with open("/proc/net/route") as f:
        return any(line.split()[1] == "00000000" for line in f.readlines()[1:] if line.split())


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
    if cmd == "boot":
        report = argv[argv.index("--report") + 1] if "--report" in argv else None
        # fail fast: Companion is waiting for this to finish
        s.rclone_flags = ["--contimeout", "10s", "--timeout", "20s", "--retries", "1", "--low-level-retries", "2"]
        out = boot(s)
        text = json.dumps(out, indent=1)
        print(text)
        if report:
            os.makedirs(os.path.dirname(report) or ".", exist_ok=True)
            with open(report + ".tmp", "w") as f:
                f.write(text + "\n")
            os.replace(report + ".tmp", report)
        return 0
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
        if cmd in ("take", "save", "return") and not isinstance(e, Offline):
            record_error(s, cmd, str(e).replace("\n", " "))
        if quiet:
            return 0
        print(f"\n{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
