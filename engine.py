#!/usr/bin/env python3
"""Companion Sync: share one Companion config between rigs, through a storage
of your choice (Google Drive, GitHub, Dropbox, OneDrive, a network folder or iCloud Drive).

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

Companion keeps one config folder per version (v5.0, v5.1 ...). The storage records
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


class NoConfigYet(SyncError):
    """The storage has no config yet: this rig can start sharing its own (start_sharing)."""


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
    folder: str = "CompanionSync"      # folder in the storage (Google Drive, Dropbox, OneDrive, GitHub)
    rclone: str = "rclone"
    rclone_config: str = ""            # blank = rclone's default
    rclone_flags: list = field(default_factory=list)   # extra flags on every rclone call
    state_file: str = ""               # last save / last problem, for the Pi panel
    storage_kind: str = "gdrive"       # which storage was chosen: see STORAGE_KINDS
    storage_file: str = ""             # where the choice is saved
    storage: str = "rclone"            # how it's reached: 'rclone' (Google Drive, Dropbox, OneDrive), 'folder' or 'github'
    rclone_remote: str = REMOTE_NAME   # which rclone remote, when storage is 'rclone'
    folder_path: str = ""              # the shared folder, when storage is 'folder'
    github_repo: str = ""              # 'owner/name', when storage is 'github'
    github_branch: str = "main"
    github_token_file: str = ""        # sign-in from GitHub's code sign-in (see github_sign_in)
    key_file: str = ""                 # this rig's copy of the encryption key, when encryption is on
    github_client_id: str = ""         # the registered GitHub app that does the sign-in
    github_mode: str = "limited"       # 'limited' (GitHub App, one chosen repo) or 'quick' (OAuth app, makes the repo)
    github_app_slug: str = ""          # the GitHub App's name in its web address, for the 'limited' install link
    github_http: object = None         # tests only: stands in for the GitHub API
    companion_app: str = ""            # desktop: what to launch
    keep_local: int = 5
    keep_history: int = 30
    stale_hours: float = 24
    log: object = print

    @property
    def place(self):
        """The storage's name, for messages: 'Google Drive', 'GitHub'..."""
        return STORAGE_KINDS.get(self.storage_kind, self.storage_kind)

    @property
    def remote(self):
        return f"{self.rclone_remote}:{self.folder}"

    @property
    def store(self):
        """Where the shared config lives. Made fresh each time, so a copy of the
        settings with different rclone flags (the Pi panel) behaves accordingly."""
        if self.storage == "folder":
            return FolderStorage(self.folder_path)
        if self.storage == "github":
            return GitHubStorage(self)
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
    s.storage_file = os.path.join(home, ".companion-sync-storage.json")
    s.github_token_file = os.path.join(home, ".companion-sync-github.json")
    s.key_file = os.path.join(home, ".companion-sync-key.json")
    for k, v in kw.items():
        setattr(s, k, v)
    load_storage_choice(s)
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
        return f"{self.s.folder} folder"

    def signed_in(self):
        r = rc(self.s, "listremotes", check=False)
        return r.returncode == 0 and f"{self.s.rclone_remote}:" in r.stdout.split()

    def read(self, rel):
        r = rc(self.s, "cat", self._p(rel), check=False)
        _raise_if_offline(r, self.s.place)
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
            _raise_if_offline(r, self.s.place)
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


# ---- encryption (optional, off by default) ----------------------------------
# When it's on, every config and module file is encrypted on the rig before it
# leaves, so the storage only ever holds scrambled copies, named '<file>.enc'.
# (An older Companion Sync then finds no db.sqlite and refuses, rather than
# installing scrambled files.) LOCK, CURRENT, checks/ and modules.json stay
# readable: they hold rig names and versions, nothing from the config.
#
# The key comes from a passphrase (scrypt), typed once on each rig and kept in
# s.key_file. The ENCRYPTED file in the storage holds the salt and a check
# value, so a wrong passphrase is caught straight away. The same file always
# encrypts to the same bytes (the nonce is derived from its content), so
# unchanged files aren't uploaded again on every save.

ENC_MARKER = "ENCRYPTED"
ENC_SUFFIX = ".enc"
ENC_MAGIC = b"CSENC1"
ENC_MIN_PASSPHRASE = 10


class NeedsPassphrase(SyncError):
    """The storage is encrypted and this rig hasn't been given the passphrase."""


def _b64(b):
    import base64
    return base64.b64encode(b).decode()


def _unb64(t):
    import base64
    return base64.b64decode(t)


def _derive_key(passphrase, salt, n=2 ** 15, r=8, p=1):
    import hashlib
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          maxmem=128 * 1024 * 1024, dklen=64)


class Cipher:
    def __init__(self, key):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise SyncError("This copy of Companion Sync can't do encryption (the 'cryptography' "
                            "package is missing).")
        self.aes, self.nonce_key = AESGCM(key[:32]), key[32:]

    def seal(self, name, data):
        import hashlib
        import hmac
        nonce = hmac.new(self.nonce_key, name.encode() + b"\0" + data, hashlib.sha256).digest()[:12]
        return ENC_MAGIC + nonce + self.aes.encrypt(nonce, data, name.encode())

    def open(self, name, blob):
        if not blob.startswith(ENC_MAGIC):
            raise SyncError(f"{name} isn't a Companion Sync encrypted file.")
        n = len(ENC_MAGIC)
        try:
            return self.aes.decrypt(blob[n:n + 12], blob[n + 12:], name.encode())
        except Exception:
            raise SyncError(f"Couldn't decrypt {name}: the passphrase is wrong or the file is damaged.")


def _walk_files(top):
    for root, dirs, files in os.walk(top):
        if root == top:
            dirs[:] = [d for d in dirs if d != "backups"]
        for f in files:
            full = os.path.join(root, f)
            yield os.path.relpath(full, top).replace(os.sep, "/"), full


def _seal_tree(cipher, src, dst):
    for rel, full in _walk_files(src):
        out = os.path.join(dst, *rel.split("/")) + ENC_SUFFIX
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(full, "rb") as f:
            data = f.read()
        with open(out, "wb") as f:
            f.write(cipher.seal(rel, data))


def _open_tree(cipher, src, dst):
    """Decrypt '.enc' files; anything else (from before encryption) is copied as it is."""
    for rel, full in _walk_files(src):
        plain = rel[:-len(ENC_SUFFIX)] if rel.endswith(ENC_SUFFIX) else rel
        out = os.path.join(dst, *plain.split("/"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(full, "rb") as f:
            data = f.read()
        with open(out, "wb") as f:
            f.write(cipher.open(plain, data) if plain != rel else data)


class EncryptedStorage:
    """Wraps a storage: folders of files go in and out encrypted; the small
    readable files (LOCK, CURRENT...) pass straight through."""

    def __init__(self, inner, cipher):
        self.inner, self.cipher = inner, cipher

    def __getattr__(self, name):                 # read, write, delete, dirs, exists, purge...
        return getattr(self.inner, name)

    def push_tree(self, local, rel, backup_rel=None):
        with tempfile.TemporaryDirectory() as tmp:
            _seal_tree(self.cipher, local, tmp)
            self.inner.push_tree(tmp, rel, backup_rel)

    def pull_tree(self, rel, local):
        with tempfile.TemporaryDirectory() as tmp:
            self.inner.pull_tree(rel, tmp)
            _open_tree(self.cipher, tmp, local)

    def copy_up(self, local, rel):
        with tempfile.TemporaryDirectory() as tmp:
            _seal_tree(self.cipher, local, tmp)
            self.inner.copy_up(tmp, rel)

    def copy_down(self, rel, local):
        with tempfile.TemporaryDirectory() as tmp:
            self.inner.copy_down(rel, tmp)
            _open_tree(self.cipher, tmp, local)


def encryption_marker(s):
    text = s.store.read(ENC_MARKER)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        raise SyncError(f"The {ENC_MARKER} file in {s.place} is damaged.")


def _saved_key(s, marker):
    try:
        with open(s.key_file) as f:
            k = json.load(f)
        if k.get("salt") == marker["salt"]:
            return _unb64(k["key"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _save_key(s, salt_b64, key):
    tmp = s.key_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"salt": salt_b64, "key": _b64(key)}, f)
    if not WIN:
        os.chmod(tmp, 0o600)
    os.replace(tmp, s.key_file)


def _check_key(marker, key):
    try:
        return Cipher(key).open("check", _unb64(marker["check"])) == b"companion-sync"
    except SyncError as e:
        if "can't do encryption" in str(e):
            raise
        return False


def data_store(s):
    """The storage to move config and module files through: encrypted if the
    storage says so (asking for the passphrase if this rig doesn't have it)."""
    store = s.store
    marker = encryption_marker(s)
    if not marker:
        return store
    key = _saved_key(s, marker)
    if key is None:
        raise NeedsPassphrase(f"The config in {s.place} is encrypted. "
                              f"Enter its passphrase on this rig to use it.")
    return EncryptedStorage(store, Cipher(key))


def encryption_on(s):
    return encryption_marker(s) is not None


def unlock(s, passphrase):
    """Give this rig the passphrase for encrypted storage. Raises if it's wrong."""
    marker = encryption_marker(s)
    if not marker:
        raise SyncError(f"The config in {s.place} isn't encrypted.")
    key = _derive_key(passphrase, _unb64(marker["salt"]), marker["n"], marker["r"], marker["p"])
    if not _check_key(marker, key):
        raise SyncError("That passphrase isn't right.")
    _save_key(s, marker["salt"], key)


def _recode_storage(s, old, new):
    """Rewrite everything in the storage from `old` to `new` (plain or encrypted):
    this rig's config is uploaded afresh, modules are re-encoded, and history
    and stranded copies (in the old form) are removed."""
    lv = local_version(s)
    for d in s.store.dirs(""):
        if vkey(d) or d in ("history", "stranded"):
            s.store.purge(d)
    with tempfile.TemporaryDirectory() as tmp:
        up = os.path.join(tmp, "config", lv)
        snapshot(s, lv, up)
        new.push_tree(up, lv)
        for sub in MODULE_KINDS:
            for name in _drive_module_folders(s, sub):
                here = os.path.join(tmp, "modules", sub, name)
                old.copy_down(f"modules/{sub}/{name}", here)
                s.store.purge(f"modules/{sub}/{name}")
                new.copy_up(here, f"modules/{sub}/{name}")
    s.store.write("CURRENT", lv + "\n")


def _must_hold(s, what):
    if holder_name(lock_holder(s)) != s.rig:
        raise SyncError(f"Only the rig that has the config can {what}. Take the config first.")


def turn_on_encryption(s, passphrase):
    """Encrypt the shared config from now on. This rig must have the config."""
    if len(passphrase) < ENC_MIN_PASSPHRASE:
        raise SyncError(f"Use a passphrase of at least {ENC_MIN_PASSPHRASE} characters.")
    if encryption_on(s):
        raise SyncError(f"The config in {s.place} is already encrypted.")
    _must_hold(s, "turn on encryption")
    Cipher(b"\0" * 64)                                       # fails early if encryption isn't available
    salt = os.urandom(16)
    key = _derive_key(passphrase, salt)
    marker = {"version": 1, "kdf": "scrypt", "n": 2 ** 15, "r": 8, "p": 1, "salt": _b64(salt),
              "check": _b64(Cipher(key).seal("check", b"companion-sync"))}
    _save_key(s, marker["salt"], key)
    plain, sealed = s.store, EncryptedStorage(s.store, Cipher(key))
    s.store.write(ENC_MARKER, json.dumps(marker, indent=1) + "\n")
    _recode_storage(s, plain, sealed)
    record_ok(s, "save")
    note = ("\n\nGitHub keeps every earlier version in the repository's history, including the "
            "unencrypted ones. To be rid of those, make a new private repository and choose it in Storage…."
            if s.storage == "github" else "")
    return (f"The config in {s.place} is now encrypted. Other rigs will ask for the passphrase "
            f"the first time they use it.\n\nKeep the passphrase safe: without it the config in "
            f"{s.place} can't be read (each rig still has its own copy).{note}")


def turn_off_encryption(s):
    """Store the shared config unencrypted again. This rig must have the config and the passphrase."""
    if not encryption_on(s):
        raise SyncError(f"The config in {s.place} isn't encrypted.")
    _must_hold(s, "turn off encryption")
    sealed = data_store(s)
    _recode_storage(s, sealed, s.store)
    s.store.delete(ENC_MARKER)
    record_ok(s, "save")
    return f"The config in {s.place} is no longer encrypted."


# ---- GitHub ----------------------------------------------------------------
# A private repository per user. Every change is one commit, so history comes
# from git itself (no history/ folder). Updates refuse to land if someone else
# committed in between; the change is then rebuilt on top and tried again.

GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"


def _git_blob_sha(data):
    import hashlib
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _http_json(method, url, body=None, headers=None, form=False):
    """(status, parsed body). Network trouble raises Offline."""
    import urllib.error
    import urllib.parse
    import urllib.request
    data = None
    hdrs = {"Accept": "application/vnd.github+json", "User-Agent": "companion-sync",
            "X-GitHub-Api-Version": "2022-11-28"}
    hdrs.update(headers or {})
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
            hdrs["Accept"] = "application/json"
        else:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"message": raw.decode(errors="replace")[:300]}
    except (urllib.error.URLError, OSError) as e:
        raise Offline(f"Can't reach GitHub. Are you online?\n{e}")


def _read_token(s):
    try:
        with open(s.github_token_file) as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return None


def _save_token(s, tok):
    if "expires_in" in tok:                      # GitHub App tokens expire; keep a refresh token
        tok["expires_at"] = time.time() + int(tok["expires_in"]) - 60
    os.makedirs(os.path.dirname(s.github_token_file) or ".", exist_ok=True)
    tmp = s.github_token_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tok, f)
    if not WIN:
        os.chmod(tmp, 0o600)
    os.replace(tmp, s.github_token_file)


def github_sign_in(s, show_code, http=None):
    """GitHub's code sign-in: show_code(code, address) is called once, for the
    person to open the address (phone or computer) and type the code. Blocks
    until they've approved it (or it times out) and saves the sign-in."""
    http = http or s.github_http or _http_json
    if not s.github_client_id:
        raise SyncError("This build has no GitHub sign-in details.")
    body = {"client_id": s.github_client_id}
    if s.github_mode == "quick":
        body["scope"] = "repo"                 # OAuth app: may create and use the user's repositories
    st, d = http("POST", f"{GITHUB_WEB}/login/device/code", body, form=True)
    if st != 200 or "device_code" not in (d or {}):
        raise SyncError(f"GitHub didn't start the sign-in: {(d or {}).get('error_description', st)}")
    show_code(d["user_code"], d["verification_uri"])
    interval, deadline = int(d.get("interval", 5)), time.time() + int(d.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        st, t = http("POST", f"{GITHUB_WEB}/login/oauth/access_token",
                     {"client_id": s.github_client_id, "device_code": d["device_code"],
                      "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, form=True)
        err = (t or {}).get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err:
            raise SyncError(f"GitHub sign-in didn't complete: {t.get('error_description', err)}")
        _save_token(s, t)
        return
    raise SyncError("The GitHub sign-in code expired. Try again.")


def _gh_user(s, method, path, body=None):
    """A GitHub API call that isn't about one repository (who am I, make a repo...)."""
    store = GitHubStorage(s)
    st, d = store.http(method, f"{GITHUB_API}{path}", body, {"Authorization": f"Bearer {store._token()}"})
    if st == 401:
        raise Offline("GitHub sign-in has expired or was removed. Sign in again.")
    return st, d


GITHUB_DEFAULT_REPO = "companion-sync-config"
GITHUB_README = ("# Companion Sync config\n\n"
                 "Bitfocus Companion settings kept in sync by Companion Sync.\n"
                 "Keep this repository private.\n")

# The two registered GitHub apps (Client IDs are public; no secrets are used)
GITHUB_APPS = {
    "limited": {"client_id": "Iv23liZE4l3VNzqtCS2G", "slug": "companion-sync"},   # GitHub App
    "quick": {"client_id": "Ov23liSTzcqDK8JcwVnn", "slug": ""},                   # OAuth App
}


def use_github(s, mode, repo=""):
    """Point settings at GitHub with the chosen sign-in option."""
    app = GITHUB_APPS[mode]
    s.storage, s.github_mode = "github", mode
    s.github_client_id, s.github_app_slug = app["client_id"], app["slug"]
    if repo:
        s.github_repo = repo


def github_quick_setup(s, name=GITHUB_DEFAULT_REPO):
    """'Quick' option: use (or make) a private repository called `name` in the
    signed-in account. Sets s.github_repo and returns it."""
    st, me = _gh_user(s, "GET", "/user")
    if st != 200:
        raise SyncError(f"GitHub didn't say who's signed in ({st}).")
    full = f"{me['login']}/{name}"
    st, repo = _gh_user(s, "GET", f"/repos/{full}")
    if st == 404:
        st, repo = _gh_user(s, "POST", "/user/repos",
                            {"name": name, "private": True,
                             "description": "Companion config shared by Companion Sync"})
        if st not in (200, 201):
            raise SyncError(f"Couldn't make the repository {full}: {(repo or {}).get('message', st)}")
    elif st != 200:
        raise SyncError(f"Couldn't check the repository {full} ({st}).")
    elif not repo.get("private", True):
        raise SyncError(f"{full} already exists and is public. Companion configs can hold passwords, "
                        f"so make it private on GitHub (or choose another name) and try again.")
    s.github_repo = repo.get("full_name", full)
    return s.github_repo


def github_install_link(s):
    return f"{GITHUB_WEB}/apps/{s.github_app_slug}/installations/new" if s.github_app_slug else GITHUB_WEB


def github_granted_repos(s):
    """'Limited' option: the repositories this sign-in was given, i.e. those the
    person ticked when installing the Companion Sync GitHub App."""
    st, inst = _gh_user(s, "GET", "/user/installations")
    if st != 200:
        raise SyncError(f"Couldn't ask GitHub which repositories Companion Sync can use ({st}).")
    repos = []
    for i in inst.get("installations", []):
        st, r = _gh_user(s, "GET", f"/user/installations/{i['id']}/repositories")
        if st == 200:
            repos += [x["full_name"] for x in r.get("repositories", [])]
    if not repos:
        raise SyncError("Companion Sync hasn't been given a repository yet.\n\n"
                        "Make a private repository on GitHub, then open this link and tick just that one:\n"
                        f"  {github_install_link(s)}")
    return sorted(set(repos))


class GitHubStorage:
    def __init__(self, s):
        self.s = s
        self.http = s.github_http or _http_json
        self.root = s.folder.strip("/")

    # -- plumbing --
    def describe(self):
        return f"github.com/{self.s.github_repo}"

    def _token(self):
        tok = _read_token(self.s)
        if not tok or "access_token" not in tok:
            raise Offline("Not signed in to GitHub yet.")
        if tok.get("expires_at") and time.time() > tok["expires_at"] and tok.get("refresh_token"):
            st, t = self.http("POST", f"{GITHUB_WEB}/login/oauth/access_token",
                              {"client_id": self.s.github_client_id, "grant_type": "refresh_token",
                               "refresh_token": tok["refresh_token"]}, form=True)
            if st != 200 or "access_token" not in (t or {}):
                raise Offline("GitHub sign-in has expired. Sign in again.")
            _save_token(self.s, t)
            tok = t
        return tok["access_token"]

    def _api(self, method, path, body=None, ok=(200, 201)):
        st, d = self.http(method, f"{GITHUB_API}/repos/{self.s.github_repo}{path}", body,
                          {"Authorization": f"Bearer {self._token()}"})
        if st == 401:
            raise Offline("GitHub sign-in has expired or was removed. Sign in again.")
        if st in ok:
            return st, d
        if st in (404, 409):
            return st, d
        if st >= 500:
            raise Offline(f"GitHub is having trouble ({st}). Try again shortly.")
        raise SyncError(f"GitHub said no ({st}): {(d or {}).get('message', '')}")

    def _full(self, rel):
        return "/".join(p for p in (self.root, rel.strip("/")) if p)

    def _head(self):
        """(commit sha, tree sha), or (None, None) for an empty repository."""
        st, ref = self._api("GET", f"/git/ref/heads/{self.s.github_branch}")
        if st in (404, 409):
            st2, _ = self._api("GET", "")
            if st2 == 404:
                raise Offline(f"Can't find the GitHub repository {self.s.github_repo} "
                              f"(or this sign-in can't see it).")
            return None, None
        sha = ref["object"]["sha"]
        _, c = self._api("GET", f"/git/commits/{sha}")
        return sha, c["tree"]["sha"]

    def _tree(self):
        """{full path: blob sha} for every file in the branch."""
        _, tree = self._head()
        if not tree:
            return {}
        _, t = self._api("GET", f"/git/trees/{tree}?recursive=1")
        return {e["path"]: e["sha"] for e in t.get("tree", []) if e["type"] == "blob"}

    def _blob(self, sha):
        import base64
        _, b = self._api("GET", f"/git/blobs/{sha}")
        return base64.b64decode(b["content"])

    def _commit(self, changes, message):
        """changes: {full path: bytes, or None to delete}. One commit; retried on a race."""
        import base64
        for attempt in range(4):
            head, base_tree = self._head()
            if not head:
                # GitHub won't take files the fast way into an empty repository, so start it
                # off with a README through the simple file API, then save as normal.
                self._api("PUT", "/contents/README.md",
                          {"message": "Start Companion Sync storage",
                           "content": base64.b64encode(GITHUB_README.encode("utf-8")).decode(),
                           "branch": self.s.github_branch}, ok=(200, 201, 422))
                head, base_tree = self._head()
                if not head:
                    raise SyncError(f"Couldn't start the GitHub repository {self.s.github_repo}.")
            existing = self._tree()
            entries = []
            for path, data in sorted(changes.items()):
                if data is None:
                    if path in existing:
                        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                    continue
                sha = _git_blob_sha(data)
                if existing.get(path) == sha:
                    continue
                st, b = self._api("POST", "/git/blobs",
                                  {"content": base64.b64encode(data).decode(), "encoding": "base64"})
                if st != 201:
                    raise SyncError(f"GitHub wouldn't take the file {path} ({st}): {(b or {}).get('message', '')}")
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": b["sha"]})
            if not entries:
                return
            tree_body = {"tree": entries}
            if base_tree:
                tree_body["base_tree"] = base_tree
            _, t = self._api("POST", "/git/trees", tree_body)
            _, c = self._api("POST", "/git/commits",
                             {"message": message, "tree": t["sha"], "parents": [head] if head else []})
            if head:
                st, _ = self._api("PATCH", f"/git/refs/heads/{self.s.github_branch}",
                                  {"sha": c["sha"], "force": False}, ok=(200, 422))
            else:
                st, _ = self._api("POST", "/git/refs",
                                  {"ref": f"refs/heads/{self.s.github_branch}", "sha": c["sha"]}, ok=(201, 422))
            if st in (200, 201):
                return
            time.sleep(1 + attempt)                     # someone else committed: rebuild on theirs
        raise SyncError("GitHub kept changing underneath this save. Try again.")

    # -- the storage interface --
    def signed_in(self):
        if not _read_token(self.s):
            return False
        try:
            st, _ = self._api("GET", "")
            return st == 200
        except (Offline, SyncError):
            return False

    def read(self, rel):
        sha = self._tree().get(self._full(rel))
        return self._blob(sha).decode("utf-8") if sha else None

    def write(self, rel, text):
        self._commit({self._full(rel): text.encode("utf-8")}, f"{self.s.rig}: {rel}")

    def delete(self, rel):
        self._commit({self._full(rel): None}, f"{self.s.rig}: remove {rel}")

    def age_hours(self, rel):
        try:
            st, cs = self._api("GET", f"/commits?path={self._full(rel)}&sha={self.s.github_branch}&per_page=1")
            t = cs[0]["commit"]["committer"]["date"].replace("Z", "+00:00")
            return (datetime.datetime.now(datetime.timezone.utc)
                    - datetime.datetime.fromisoformat(t)).total_seconds() / 3600
        except (Offline, SyncError, IndexError, KeyError, TypeError):
            return 0.0

    def dirs(self, rel, strict=False):
        try:
            tree = self._tree()
        except Offline:
            if strict:
                raise
            return []
        pre = self._full(rel)
        pre = pre + "/" if pre else ""
        return sorted({p[len(pre):].split("/")[0] for p in tree
                       if p.startswith(pre) and "/" in p[len(pre):]})

    def exists(self, rel):
        full = self._full(rel)
        return any(p == full or p.startswith(full + "/") for p in self._tree())

    def _local_files(self, local, skip_backups):
        files = {}
        for root, dirs, names in os.walk(local):
            if skip_backups and root == local:
                dirs[:] = [d for d in dirs if d != "backups"]
            for n in names:
                full = os.path.join(root, n)
                files[os.path.relpath(full, local).replace(os.sep, "/")] = full
        return files

    def push_tree(self, local, rel, backup_rel=None):
        # backup_rel isn't needed: the previous version stays in the repository's history
        pre = self._full(rel) + "/"
        want = self._local_files(local, skip_backups=True)
        changes = {}
        for r, full in want.items():
            with open(full, "rb") as f:
                changes[pre + r] = f.read()
        for p in self._tree():
            if p.startswith(pre) and p[len(pre):] not in want and not p[len(pre):].startswith("backups/"):
                changes[p] = None
        self._commit(changes, f"{self.s.rig}: save {rel}")
        have = self._tree()                                     # verify
        for r, data_path in want.items():
            with open(data_path, "rb") as f:
                if have.get(pre + r) != _git_blob_sha(f.read()):
                    raise SyncError("The copy on GitHub doesn't match after saving. Try again.")

    def _pull(self, rel, local, skip_backups):
        pre = self._full(rel) + "/"
        for p, sha in self._tree().items():
            if not p.startswith(pre):
                continue
            r = p[len(pre):]
            if skip_backups and r.startswith("backups/"):
                continue
            target = os.path.join(local, *r.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(self._blob(sha))

    def pull_tree(self, rel, local):
        self._pull(rel, local, skip_backups=True)

    def copy_up(self, local, rel):
        pre = self._full(rel) + "/"
        changes = {}
        for r, full in self._local_files(local, skip_backups=False).items():
            with open(full, "rb") as f:
                changes[pre + r] = f.read()
        self._commit(changes, f"{self.s.rig}: add {rel}")

    def copy_down(self, rel, local):
        self._pull(rel, local, skip_backups=False)

    def purge(self, rel):
        pre = self._full(rel) + "/"
        gone = {p: None for p in self._tree() if p.startswith(pre)}
        if gone:
            self._commit(gone, f"{self.s.rig}: remove {rel}")


def sign_in(s, client_id, client_secret):
    """Link Google Drive. Opens the browser for the Google sign-in."""
    rc(s, "config", "create", REMOTE_NAME, "drive", "scope", "drive.file",
       "client_id", client_id, "client_secret", client_secret)


# ---- choosing the storage --------------------------------------------------
# Each rig keeps its own choice in s.storage_file. Every rig sharing a config
# must choose the same storage (and the same account / repository / folder).

STORAGE_KINDS = {                     # kind: name shown to people
    "gdrive": "Google Drive",
    "github": "GitHub",
    "dropbox": "Dropbox",
    "onedrive": "OneDrive",
    "nas": "the network folder",
    "icloud": "iCloud Drive",
}
RCLONE_REMOTES = {"gdrive": REMOTE_NAME, "dropbox": "cs-dropbox", "onedrive": "cs-onedrive"}
SAVED_CHOICE = ("storage_kind", "storage", "rclone_remote", "folder_path", "github_repo",
                "github_branch", "github_mode", "github_client_id", "github_app_slug")


def load_storage_choice(s):
    """Apply the saved choice, if there is one (otherwise Google Drive, as before)."""
    try:
        with open(s.storage_file) as f:
            saved = json.load(f)
    except (OSError, ValueError, TypeError):
        return
    for k in SAVED_CHOICE:
        if k in saved:
            setattr(s, k, saved[k])


def save_storage_choice(s):
    tmp = s.storage_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: getattr(s, k) for k in SAVED_CHOICE}, f, indent=1)
    os.replace(tmp, s.storage_file)


def use_rclone(s, kind):
    s.storage_kind, s.storage, s.rclone_remote = kind, "rclone", RCLONE_REMOTES[kind]


def use_folder(s, kind, path):
    """kind 'nas' or 'icloud'. `path` is the folder chosen (or the iCloud Drive
    folder); the config goes in a CompanionSync folder inside it."""
    if not path or not os.path.isdir(path):
        raise SyncError(f"Can't find the folder:\n  {path or '(none chosen)'}")
    target = path if os.path.basename(path.rstrip("/\\")) == s.folder else os.path.join(path, s.folder)
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        raise SyncError(f"Can't make the {s.folder} folder there:\n  {target}\n{e}")
    s.storage_kind, s.storage, s.folder_path = kind, "folder", target


def icloud_folder():
    """This computer's iCloud Drive folder, or None if iCloud Drive isn't set up."""
    home = os.path.expanduser("~")
    if MAC:
        p = os.path.join(home, "Library", "Mobile Documents", "com~apple~CloudDocs")
    elif WIN:
        p = os.path.join(home, "iCloudDrive")
    else:
        return None
    return p if os.path.isdir(p) else None


def connect_rclone(s, kind):
    """Dropbox or OneDrive: rclone opens the browser to sign in."""
    remote = RCLONE_REMOTES[kind]
    rc(s, "config", "delete", remote, check=False)          # start clean if signing in again
    rc(s, "config", "create", remote, kind)
    use_rclone(s, kind)


def connect_github(s, mode, show_code, choose_repo):
    """Sign in to GitHub with the chosen option and pick the repository.
    show_code(code, address): show the sign-in code. choose_repo(list) -> one of them."""
    use_github(s, mode)
    s.storage_kind = "github"
    github_sign_in(s, show_code)
    if mode == "quick":
        github_quick_setup(s)
    else:
        repos = github_granted_repos(s)
        s.github_repo = repos[0] if len(repos) == 1 else choose_repo(repos)
        if not s.github_repo:
            raise SyncError("No repository chosen.")


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
# The LOCK file in the storage says which rig holds the config. The holder refreshes
# it on every save, so its age = time since the holder was last seen online.
# The holding file is a local note that this rig took the config, so a rig
# whose lock was taken over knows its unsaved edits are stranded.

def _raise_if_offline(r, place="Google Drive"):
    """rclone failed: a missing file is fine, anything else is a problem."""
    if r.returncode == 0:
        return
    err = r.stderr.lower()
    if "didn't find section" in err or "not configured" in err:
        raise Offline(f"Not signed in to {place} yet.")
    if not ("object not found" in err or "directory not found" in err or "doesn't exist" in err):
        raise Offline(f"Can't reach {place}. Are you online?\n" + r.stderr.strip()[-300:])


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
    store = data_store(s)
    with tempfile.TemporaryDirectory() as tmp:
        up = os.path.join(tmp, version)
        snapshot(s, version, up)
        store.push_tree(up, dest, f"history/{now()}_{s.rig}" if dest == main else None)
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
# any folder the storage doesn't have yet is uploaded to its modules/ folder, and
# on take any folder this rig doesn't have is copied in before Companion starts.
# modules/modules.json lists what's in the storage. Nothing is ever deleted.

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
    """Upload installed module versions the storage doesn't have yet. Returns their names."""
    added = []
    store = data_store(s)
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
                store.copy_up(staged, f"modules/{sub}/{name}")
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
    store = data_store(s)
    with tempfile.TemporaryDirectory() as tmp:
        for sub in MODULE_KINDS:
            have = set(_local_module_folders(s, sub))
            for name in _drive_module_folders(s, sub):
                if name in have:
                    continue
                dl = os.path.join(tmp, sub, name)
                store.copy_down(f"modules/{sub}/{name}", dl)
                if not os.path.exists(os.path.join(dl, "companion", "manifest.json")):
                    s.log(f"Skipped module {name}: incomplete in {s.place}")
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
            f"This rig's version is safe in {s.place}, in stranded/{s.rig}.")


# ---- commands --------------------------------------------------------------

def status(s):
    """Plain facts for display. Raises Offline if the storage can't be reached."""
    tidy_safety_copies(s)
    h = lock_holder(s)
    return {
        "moved": moved_to(s),
        "holder": holder_name(h),
        "mine": holder_name(h) == s.rig,
        "age_hours": lock_age_hours(s) if h else None,
        "drive_version": drive_version(s),
        "stranded": was_holding(s) and holder_name(h) != s.rig,
    }


def take(s, confirm_takeover=None, manage_companion=True):
    """Lock the config to this rig and install the stored copy.

    confirm_takeover(message) -> bool is asked before taking over a stale
    lock; without it, a stale lock is never taken over (e.g. at boot).
    manage_companion=False: Companion isn't running and mustn't be started
    (the Pi's boot check runs before Companion's service).
    """
    lv = local_version(s)
    m = moved_to(s)
    if m:
        raise SyncError(f"The config has moved {m}.\n\nChoose the new storage in Storage… on this rig.")
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
               f"(they go to 'stranded' in {s.place} when it next comes online).\n\nTake it anyway?")
        if not (confirm_takeover and confirm_takeover(msg)):
            raise SyncError("Not taken.")
    dv = drive_version(s)
    if not dv:
        raise NoConfigYet(f"There's no config in {s.place} yet.")
    if vkey(dv) > vkey(lv):
        raise NeedsUpdate(f"The config in {s.place} is from Companion {vtext(dv)}, but this rig "
                        f"has Companion {vtext(lv)}.\nUpdate Companion on this rig first.")
    store = data_store(s)                      # encrypted? needs the passphrase before anything stops
    if manage_companion:
        stop_companion(s)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dl = os.path.join(tmp, dv)
            s.log(f"Downloading config from {s.place}...")
            store.pull_tree(dv, dl)
            if not os.path.exists(os.path.join(dl, "db.sqlite")):
                raise SyncError(f"The config in {s.place} looks incomplete (no db.sqlite).")
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
    rig doesn't hold the config or the storage can't be reached.
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
                        f"{s.place} ({vtext(dv)}). Not saved.")
    upload(s, lv)
    _modules_step(s, upload_modules, "uploaded")
    write_lock(s, taken_time(h))
    mark_holding(s, True)
    record_ok(s, "save")
    note = f" (config upgraded to Companion {vtext(lv)})" if dv and lv != dv else ""
    return f"Saved to {s.place}{note}. {s.rig} still has the config."


def return_(s):
    h = lock_holder(s)
    if holder_name(h) != s.rig:
        if was_holding(s):
            msg = _save_stranded(s, h)
            mark_holding(s, False)
            return msg
        raise SyncError("This rig doesn't have the config, so there's nothing to return.")
    s.log(f"Uploading config to {s.place}...")
    lv = local_version(s)
    upload(s, lv)
    _modules_step(s, upload_modules, "uploaded")
    s.store.delete("LOCK")
    mark_holding(s, False)
    record_ok(s, "return")
    return f"Config returned and unlocked. Changes made on {s.rig} from now on won't be kept."


# ---- moving to another storage ---------------------------------------------
# The rig that has the config copies it (and every module) into the new, empty
# storage and keeps holding it there. The old storage keeps its copy and
# history as a backup, but gets a MOVED note and a lock that says where it
# went, so no rig can carry on with the old copy by mistake.

MOVED_FILE = "MOVED"


def moved_to(s):
    """Where the config went, if it has moved out of this storage, else None."""
    text = s.store.read(MOVED_FILE)
    return text.strip() if text and text.strip() else None


def move_storage(s, new):
    """Move the shared config from s's storage to new's (a copy of the settings
    pointed at the new storage). This rig must have the config."""
    if moved_to(s):
        raise SyncError(f"The config has already moved: {moved_to(s)}")
    if holder_name(lock_holder(s)) != s.rig:
        raise SyncError("Only the rig that has the config can move it. Take the config first.")
    if drive_version(new) or lock_holder(new):
        raise SyncError(f"{new.place} already has a Companion Sync config in it. "
                        f"Move into an empty one (or a different folder / repository).")
    save(s)                                                   # the latest goes along
    old_data = data_store(s)
    marker = s.store.read(ENC_MARKER)
    if marker:                                                # stays encrypted, same passphrase
        new.store.write(ENC_MARKER, marker)
        new.key_file = s.key_file
    new_data = data_store(new)
    write_lock(new, now())
    s.log(f"Copying the config to {new.place}...")
    upload(new, local_version(s))
    with tempfile.TemporaryDirectory() as tmp:
        for sub in MODULE_KINDS:
            for name in _drive_module_folders(s, sub):
                here = os.path.join(tmp, sub, name)
                old_data.copy_down(f"modules/{sub}/{name}", here)
                new_data.copy_up(here, f"modules/{sub}/{name}")
    _modules_step(new, upload_modules, "uploaded")            # and any only on this rig
    _write_modules_list(new)
    s.store.write(MOVED_FILE, f"to {new.place} ({new.store.describe()}), by {s.rig} on {now()}\n")
    s.store.write("LOCK", f"moved to {new.place} | taken {now()} | last seen {now()}\n")
    mark_holding(new, True)
    record_ok(new, "save")
    return (f"The config has moved to {new.place}, and {s.rig} still has it.\n\n"
            f"On every other rig, choose {new.place} in Storage… (same account). The old copy "
            f"stays in {s.place} as a backup, marked as moved.")


def start_sharing(s):
    """First rig: put this rig's config into empty storage and hold it."""
    if moved_to(s):
        raise SyncError(f"The config has moved {moved_to(s)}.")
    if drive_version(s):
        raise SyncError(f"There's already a config in {s.place}. Use Open Companion / take instead.")
    if lock_holder(s):
        raise SyncError(f"{s.place} is locked by {holder_name(lock_holder(s))}.")
    lv = local_version(s)
    write_lock(s, now())
    mark_holding(s, True)
    upload(s, lv)
    _modules_step(s, upload_modules, "uploaded")
    record_ok(s, "save")
    return f"{s.rig}'s config is now in {s.place}, and {s.rig} has it."


def check_storage(s):
    """Connection check: can this rig read and write the storage? Returns what's
    there, in plain words. Raises SyncError / Offline when it can't."""
    if not signed_in(s):
        raise Offline(f"Not signed in to {s.place}.")
    h = lock_holder(s)                                       # reading works
    dv = drive_version(s)
    stamp = f"{s.rig} checked {now()}\n"
    s.store.write(f"checks/{s.rig}", stamp)                  # writing works
    if (s.store.read(f"checks/{s.rig}") or "").strip() != stamp.strip():
        raise SyncError(f"Wrote a test file to {s.place} but couldn't read it back.")
    where = s.store.describe()
    if not dv:
        there = ("There's no config here yet. Open Companion on the rig whose config you want "
                 "to share, and it'll offer to put it here.")
    elif h:
        there = f"There's a Companion {vtext(dv)} config here, held by {holder_name(h)}."
    else:
        there = f"There's a Companion {vtext(dv)} config here, free to take."
    marker = encryption_marker(s)
    if marker:
        there += ("\n\nIt's encrypted. This rig has the passphrase." if _saved_key(s, marker)
                  else "\n\nIt's encrypted: this rig will ask for the passphrase before using it.")
    return f"Connected to {s.place} ({where}). Reading and writing both work.\n\n{there}"


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
            out.update(result="not_signed_in", message=f"Not signed in to {s.place}.")
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
            print(f"{s.place} has Companion {vtext(st['drive_version'])} config; "
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
