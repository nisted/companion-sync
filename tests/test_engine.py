"""Scenario tests for the sync engine, run against each kind of storage.

  python3 tests/test_engine.py

Each scenario uses two pretend rigs (alpha, bravo) with their own Companion
config folders, sharing one storage. Companion itself is never started:
starting/stopping it is replaced with no-ops. The 'rclone' run points rclone
at a local folder, so it exercises exactly the commands used for Google Drive.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import dataclasses
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import engine as E  # noqa: E402
from fake_github import FakeGitHub  # noqa: E402

E.stop_companion = lambda s: None
E.start_companion = lambda s: None
E.companion_running = lambda s: False


def make_config(base, version="v5.0", value="one"):
    d = os.path.join(base, version)
    os.makedirs(os.path.join(d, "backups"), exist_ok=True)
    db = sqlite3.connect(os.path.join(d, "db.sqlite"))
    db.execute("create table if not exists kv (k text primary key, v text)")
    db.execute("insert or replace into kv values ('setting', ?)", (value,))
    db.commit()
    db.close()
    with open(os.path.join(d, "backups", "local-only.txt"), "w") as f:
        f.write("stays local")


def read_value(base, version="v5.0"):
    db = sqlite3.connect(os.path.join(base, version, "db.sqlite"))
    v = db.execute("select v from kv where k='setting'").fetchone()[0]
    db.close()
    return v


def add_module(base, name):
    d = os.path.join(base, "modules", name, "companion")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        f.write('{"id": "%s"}' % name)


class World:
    def __init__(self, kind):
        self.tmp = tempfile.mkdtemp(prefix=f"cs-test-{kind}-")
        self.kind = kind
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(shared)
        extra = {}
        self.github = None
        if kind == "github":
            self.github = FakeGitHub()
            extra = dict(storage="github", storage_kind="github", github_repo=self.github.repo,
                         github_client_id="test-client", github_http=self.github)
        elif kind == "rclone":
            conf = os.path.join(self.tmp, "rclone.conf")
            with open(conf, "w") as f:
                f.write(f"[gdrive]\ntype = alias\nremote = {shared}\n")
            extra = dict(storage="rclone", storage_kind="gdrive", rclone_config=conf)
        else:
            extra = dict(storage="folder", storage_kind="nas", folder_path=os.path.join(shared, "RebelSync"))
            os.makedirs(extra["folder_path"])
        self.shared = shared
        self.rigs = {}
        for name in ("alpha", "bravo"):
            home = os.path.join(self.tmp, name)
            base = os.path.join(home, "companion")
            os.makedirs(base)
            s = E.Settings("mac", base, os.path.join(home, "safety"), os.path.join(home, "holding"),
                           rig=name, state_file=os.path.join(home, "state.json"), claim_wait=0.05,
                           key_file=os.path.join(home, "key.json"), log=lambda *a: None, **extra)
            if kind == "github":
                s.github_token_file = os.path.join(home, "github.json")
                shown = []
                E.github_sign_in(s, lambda code, url: shown.append((code, url)))
                assert shown == [("ABCD-1234", "https://github.com/login/device")], shown
            self.rigs[name] = s

    def store_has(self, rel):
        return self.rigs["alpha"].store.exists(rel)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def seed(w):
    """alpha has the first config and puts it on the storage."""
    a = w.rigs["alpha"]
    make_config(a.base, value="one")
    E.write_lock(a, E.now())
    E.mark_holding(a, True)
    E.save(a)


def expect_error(fn, words, cls=E.SyncError):
    try:
        fn()
    except cls as e:
        assert words.lower() in str(e).lower(), f"expected '{words}' in: {e}"
        return
    raise AssertionError(f"expected {cls.__name__} containing '{words}'")


# ---- scenarios ---------------------------------------------------------------

def t_first_save_and_status(w):
    seed(w)
    a = w.rigs["alpha"]
    st = E.status(a)
    assert st["holder"] == "alpha" and st["mine"] and st["drive_version"] == "v5.0", st
    assert w.store_has("v5.0/db.sqlite") and not w.store_has("v5.0/backups"), "backups/ must stay local"
    assert E.read_state(a).get("last_save"), "last save recorded"


def t_take_refused_while_held(w):
    seed(w)
    b = w.rigs["bravo"]
    make_config(b.base, value="bravo's own")
    expect_error(lambda: E.take(b), "being used by alpha")


def t_return_then_take(w):
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    E.return_(a)
    assert E.lock_holder(a) is None
    make_config(b.base, value="old bravo")
    E.take(b)
    assert read_value(b.base) == "one", "bravo got alpha's config"
    assert E.holder_name(E.lock_holder(b)) == "bravo"
    assert os.listdir(b.safety_dir), "safety copy kept"
    assert os.path.exists(os.path.join(b.base, "v5.0", "backups", "local-only.txt")), "own backups untouched"


def t_save_keeps_history(w):
    seed(w)
    a = w.rigs["alpha"]
    time.sleep(1.1)                           # history folders are named to the second
    make_config(a.base, value="two")
    E.save(a)
    if w.kind == "github":                    # history is the repository's own commits
        assert len(w.github.commits) >= 3
    else:
        assert a.store.dirs("history"), "history kept"
    b = w.rigs["bravo"]
    E.return_(a)
    make_config(b.base, value="x")
    E.take(b)
    assert read_value(b.base) == "two"


def t_stale_takeover_and_stranded(w):
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    a.stale_hours = b.stale_hours = 0         # alpha's lock counts as stale at once
    make_config(b.base, value="b")
    expect_error(lambda: E.take(b), "not taken")             # no one to ask: never taken over
    E.take(b, confirm_takeover=lambda m: True)
    make_config(a.base, value="alpha's late edit")
    msg = E.save(a)
    assert "stranded" in msg.lower(), msg
    assert w.store_has("stranded/alpha/db.sqlite")
    assert read_value(b.base) == "one", "bravo kept the main copy"


def t_modules_travel(w):
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    add_module(a.base, "custom-sq-3.1.1-grid.7")
    E.save(a)
    assert w.store_has("modules/modules.json")
    E.return_(a)
    make_config(b.base, value="b")
    E.take(b)
    assert os.path.exists(os.path.join(b.base, "modules", "custom-sq-3.1.1-grid.7", "companion", "manifest.json"))


def t_modules_duplicates_and_half_copies(w):
    """Drive can list one module folder several times, and a half-copy can be left locally:
    the take must still finish (the K-2SO WinError 183 failure)."""
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    add_module(a.base, "custom-sq-3.1.1-grid.7")
    E.save(a)
    E.return_(a)
    make_config(b.base, value="b")
    half = os.path.join(b.base, "modules", "custom-sq-3.1.1-grid.7")
    os.makedirs(os.path.join(half, "junk"))               # left by an earlier failed take, no manifest
    real_dirs = b.store.dirs
    def doubled(rel, strict=False):
        d = real_dirs(rel, strict)
        return d + d
    b.store.dirs = doubled
    try:
        E.take(b)
    finally:
        b.store.dirs = real_dirs
    assert os.path.exists(os.path.join(half, "companion", "manifest.json"))
    assert not os.path.exists(os.path.join(half, "junk")), "half-copy replaced"
    assert E.repair_modules(b) == 0


def t_older_companion_refused(w):
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    E.return_(a)
    make_config(b.base, version="v4.9", value="old")
    expect_error(lambda: E.take(b), "update companion", E.NeedsUpdate)


def t_boot(w):
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    make_config(b.base, value="b")
    assert E.boot(b, net_wait=0)["result"] == "held"
    E.return_(a)
    out = E.boot(b, net_wait=0)
    assert out["result"] == "taken" and read_value(b.base) == "one", out
    assert E.boot(b, net_wait=0)["result"] == "already"


def t_unreachable(w):
    seed(w)
    a = w.rigs["alpha"]
    if w.kind == "folder":
        a.folder_path = os.path.join(w.tmp, "unplugged-nas")
    elif w.kind == "github":
        os.remove(a.github_token_file)                       # signed out
    else:
        a.rclone_remote = "nosuchremote"
    expect_error(lambda: E.lock_holder(a), "", E.Offline)


def t_github_race(w):
    """Someone else commits while we save: the save is rebuilt on top, nothing lost."""
    if w.kind != "github":
        return
    seed(w)
    a = w.rigs["alpha"]
    make_config(a.base, value="three")
    w.github.race_next_update = True
    E.save(a)
    b = w.rigs["bravo"]
    E.return_(a)
    make_config(b.base, value="x")
    E.take(b)
    assert read_value(b.base) == "three"


def t_github_quick_setup(w):
    """Quick option: asks for repo access, makes the private repo once, then reuses it."""
    if w.kind != "github":
        return
    gh, a = w.github, w.rigs["alpha"]
    a.github_mode = "quick"
    E.github_sign_in(a, lambda c, u: None)
    assert gh.scopes_asked[-1] == "repo"
    gh.repo, gh.repo_exists = "nige/rebel-sync-config", False
    assert E.github_quick_setup(a) == "nige/rebel-sync-config" and gh.repo_exists and gh.repo_private
    assert E.github_quick_setup(a) == "nige/rebel-sync-config"           # already there: reused
    gh.repo_private = False
    expect_error(lambda: E.github_quick_setup(a), "public")                  # never use a public repo
    gh.repo_private = True
    seed(w)                                                                  # and it syncs
    assert E.status(a)["holder"] == "alpha"


def t_github_limited(w):
    """Limited option: no repo access asked for; explains what to do until a repo is ticked."""
    if w.kind != "github":
        return
    gh, a = w.github, w.rigs["alpha"]
    assert gh.scopes_asked[0] is None, "limited sign-in asks for no repo access"
    a.github_app_slug = "companion-sync"
    expect_error(lambda: E.github_granted_repos(a), "github.com/apps/companion-sync/installations/new")
    gh.installed = ["nige/companion-data"]
    assert E.github_granted_repos(a) == ["nige/companion-data"]


def t_first_rig_starts_sharing(w):
    """Empty storage: take says so; the first rig puts its config there; others can then take it."""
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    make_config(a.base, value="first")
    expect_error(lambda: E.take(a), "no config", E.NoConfigYet)
    E.start_sharing(a)
    assert E.status(a)["mine"]
    expect_error(lambda: E.start_sharing(b), "already a config")
    E.return_(a)
    make_config(b.base, value="b")
    E.take(b)
    assert read_value(b.base) == "first"


def t_check_storage(w):
    a = w.rigs["alpha"]
    out = E.check_storage(a)
    assert "Reading and writing both work" in out and "no config here yet" in out, out
    seed(w)
    out = E.check_storage(w.rigs["bravo"])
    assert "held by alpha" in out, out


def t_messages_name_storage(w):
    seed(w)
    a = w.rigs["alpha"]
    want = {"folder": "the network folder", "rclone": "Google Drive", "github": "GitHub"}[w.kind]
    msg = E.save(a)
    assert msg.startswith(f"Saved to {want}"), msg


def t_choice_saved(w):
    """The storage choice is saved per rig and comes back next time."""
    if w.kind != "folder":
        return
    a = w.rigs["alpha"]
    a.storage_file = os.path.join(w.tmp, "alpha-storage.json")
    nas = os.path.join(w.tmp, "nas-share")
    os.makedirs(nas)
    E.use_folder(a, "nas", nas)
    assert a.folder_path == os.path.join(nas, "RebelSync") and os.path.isdir(a.folder_path)
    E.save_storage_choice(a)
    fresh = E.Settings("mac", a.base, a.safety_dir, a.holding_file, rig="alpha", storage_file=a.storage_file)
    assert fresh.storage_kind == "gdrive"
    E.load_storage_choice(fresh)
    assert (fresh.storage_kind, fresh.storage, fresh.folder_path) == ("nas", "folder", a.folder_path)
    expect_error(lambda: E.use_folder(a, "nas", os.path.join(w.tmp, "missing")), "can't find")


def t_github_connect(w):
    """Choosing GitHub from the Storage window: both options end up pointed at a repository."""
    if w.kind != "github":
        return
    gh, a = w.github, w.rigs["alpha"]
    a.github_client_id = ""                                   # connect_github fills these in
    E.connect_github(a, "quick", lambda c, u: None, lambda r: None)
    assert (a.storage_kind, a.github_mode, a.github_repo) == ("github", "quick", "nige/rebel-sync-config")
    gh.repo = "nige/companion-data"
    gh.installed = ["nige/companion-data", "nige/other"]
    E.connect_github(a, "limited", lambda c, u: None, lambda repos: repos[0])
    assert (a.github_mode, a.github_repo, a.github_client_id) == ("limited", "nige/companion-data", "Iv23liZE4l3VNzqtCS2G")
    assert "Reading and writing both work" in E.check_storage(a)


def stored_bytes(w):
    """Every file's bytes as the storage holds them now (not history, for GitHub)."""
    a = w.rigs["alpha"]
    if w.kind == "github":
        files = a.store._tree()
        return {p: a.store._blob(sha) for p, sha in files.items()}
    out = {}
    for root, _, files in os.walk(w.shared):
        for f in files:
            with open(os.path.join(root, f), "rb") as fh:
                out[os.path.relpath(os.path.join(root, f), w.shared)] = fh.read()
    return out


SECRET = "secret-password-in-config"
PASS = "correct horse battery"


def t_encryption(w):
    """Turned on: nothing readable leaves the rig, other rigs need the passphrase, saves stay small."""
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    make_config(a.base, value=SECRET)
    E.write_lock(a, E.now()); E.mark_holding(a, True); E.save(a)
    add_module(a.base, "custom-thing-1.0.0")
    E.save(a)
    assert any(SECRET.encode() in v for v in stored_bytes(w).values()), "test can't see plain text"
    expect_error(lambda: E.turn_on_encryption(b, PASS), "only the rig")
    expect_error(lambda: E.turn_on_encryption(a, "short"), "at least")
    E.turn_on_encryption(a, PASS)
    stored = stored_bytes(w)
    assert not any(SECRET.encode() in v for v in stored.values()), "plain config left in the storage"
    assert any(p.endswith("db.sqlite.enc") for p in stored), sorted(stored)
    assert not any(p.endswith("db.sqlite") for p in stored), "an old build must not find a plain db.sqlite"
    before = dict(stored)
    E.save(a)                                     # nothing changed: same encrypted bytes
    after = stored_bytes(w)
    assert {p: v for p, v in after.items() if p.endswith(".enc")} == {p: v for p, v in before.items() if p.endswith(".enc")}
    E.return_(a)
    make_config(b.base, value="bravo old")
    expect_error(lambda: E.take(b), "passphrase", E.NeedsPassphrase)
    expect_error(lambda: E.unlock(b, "wrong passphrase!"), "isn't right")
    E.unlock(b, PASS)
    E.take(b)
    assert read_value(b.base) == SECRET
    assert os.path.exists(os.path.join(b.base, "modules", "custom-thing-1.0.0", "companion", "manifest.json"))
    assert "This rig has the passphrase" in E.check_storage(b)


def t_encryption_off(w):
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    make_config(a.base, value=SECRET)
    E.write_lock(a, E.now()); E.mark_holding(a, True); E.save(a)
    E.turn_on_encryption(a, PASS)
    E.turn_off_encryption(a)
    stored = stored_bytes(w)
    assert not any(p.endswith(".enc") for p in stored) and not E.encryption_on(a), sorted(stored)
    E.return_(a)
    make_config(b.base, value="x")
    E.take(b)                                     # no passphrase needed now
    assert read_value(b.base) == SECRET


def new_folder_storage(w, s, name="moved-here"):
    path = os.path.join(w.tmp, name)
    os.makedirs(path, exist_ok=True)
    n = dataclasses.replace(s)
    E.use_folder(n, "nas", path)
    return n


def t_move_storage(w):
    """The holder moves the config (and modules) to empty storage; rigs left on the old one are stopped."""
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    add_module(a.base, "custom-thing-1.0.0")
    make_config(a.base, value="latest")                         # unsaved: the move saves it first
    expect_error(lambda: E.move_storage(b, new_folder_storage(w, b)), "only the rig")
    full = new_folder_storage(w, a, "already-used")
    E.write_lock(full, E.now())
    expect_error(lambda: E.move_storage(a, full), "already has")
    a2 = new_folder_storage(w, a)
    E.move_storage(a, a2)
    assert E.status(a2)["mine"] and E.status(a)["moved"]
    make_config(b.base, value="b")
    expect_error(lambda: E.take(b), "has moved")                # bravo still on the old storage
    b2 = dataclasses.replace(b, storage="folder", storage_kind="nas", folder_path=a2.folder_path)
    expect_error(lambda: E.take(b2), "being used by alpha")
    E.return_(a2)
    E.take(b2)
    assert read_value(b2.base) == "latest"
    assert os.path.exists(os.path.join(b2.base, "modules", "custom-thing-1.0.0", "companion", "manifest.json"))
    expect_error(lambda: E.move_storage(a, new_folder_storage(w, a, "again")), "already moved")


def t_move_encrypted(w):
    """Encrypted storage moves encrypted, same passphrase; nothing readable lands in the new one."""
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    make_config(a.base, value=SECRET)
    E.write_lock(a, E.now()); E.mark_holding(a, True); E.save(a)
    E.turn_on_encryption(a, PASS)
    E.unlock(b, PASS)
    a2 = new_folder_storage(w, a)
    E.move_storage(a, a2)
    for root, _, files in os.walk(a2.folder_path):
        for f in files:
            with open(os.path.join(root, f), "rb") as fh:
                assert SECRET.encode() not in fh.read(), f
    E.return_(a2)
    b2 = dataclasses.replace(b, storage="folder", storage_kind="nas", folder_path=a2.folder_path)
    make_config(b2.base, value="x")
    E.take(b2)                                                  # bravo's passphrase still works
    assert read_value(b2.base) == SECRET


def t_sign_in_elsewhere(w):
    """Headless Pi: rclone's 'run this on a computer with a browser, paste the result' sign-in."""
    if w.kind != "rclone":
        return
    a = w.rigs["alpha"]
    shown = []
    token = '{"access_token":"x","token_type":"bearer","expiry":"0001-01-01T00:00:00Z"}'
    E.connect_rclone_elsewhere(a, "dropbox", lambda help: shown.append(help) or token)
    assert 'rclone authorize "dropbox"' in shown[0], shown
    assert (a.storage_kind, a.rclone_remote) == ("dropbox", "cs-dropbox")
    conf = open(a.rclone_config).read()
    assert "[cs-dropbox]" in conf and '"access_token":"x"' in conf, conf
    expect_error(lambda: E.connect_rclone_elsewhere(a, "onedrive", lambda help: ""), "nothing was pasted")


def t_linux_companion_modes(w):
    """Pi: Companion found as a Docker container, CompanionPi's service or the
    desktop app, and the settings file overrides what's found."""
    if w.kind != "folder":
        return
    import json
    home = os.path.join(w.tmp, "pi-home")
    mount = os.path.join(w.tmp, "docker-config")
    os.makedirs(os.path.join(home, ".config", "companion-nodejs", "v5.0"))
    os.makedirs(os.path.join(mount, "v5.0"))
    have = {"docker": False, "service": False}

    def fake_run(cmd, check=True, input=None):
        out, code = "", 1
        if cmd[:2] == ["docker", "inspect"] and have["docker"]:
            code = 0
            out = json.dumps([{"Source": mount, "Destination": "/companion"}]) if "-f" in cmd else "[]"
        elif cmd[:2] == ["systemctl", "list-unit-files"] and have["service"]:
            out, code = "companion.service enabled enabled\n", 0
        return subprocess.CompletedProcess(cmd, code, out, "")

    def settings(**saved):
        s = E.Settings("pi", "", "", os.path.join(home, "holding"))
        s.settings_file = os.path.join(home, "settings.json")
        with open(s.settings_file, "w") as f:
            json.dump(saved, f)
        E.load_rig_settings(s)
        E.configure_linux(s)
        return s

    old = (E.run, E.shutil.which, os.environ.get("HOME"))
    E.run, E.shutil.which = fake_run, (lambda n: f"/usr/bin/{n}")
    os.environ["HOME"] = home
    try:
        s = settings()
        assert s.companion_mode == "desktop" and not E.companion_managed(s), s.companion_mode
        assert s.base == os.path.join(home, ".config", "companion-nodejs") and not s.sudo_files
        assert s.safety_dir == os.path.join(home, ".config", "rebel-sync-safety-copies")
        assert E.local_version(s) == "v5.0"

        have["service"] = True
        s = settings()
        assert s.companion_mode == "service" and s.sudo_files and E.companion_managed(s)
        assert s.base == "/home/companion/.config/companion-nodejs" and s.companion_owner == "companion:companion"

        have["docker"] = True                                    # a container wins over the service
        s = settings()
        assert s.companion_mode == "docker" and s.base == mount and E.companion_managed(s), (s.companion_mode, s.base)
        assert "Docker container 'companion'" in E.describe_companion(s)

        s = settings(companion_mode="desktop", companion_base=mount, login_action="nonsense")
        assert s.companion_mode == "desktop" and s.base == mount and s.login_action == "tray_only"
        s = settings(companion_mode="auto", docker_container="cs", admin_url="http://bd-1:8000")
        assert s.companion_mode == "docker" and s.docker_container == "cs" and s.admin_url == "http://bd-1:8000"
    finally:
        E.run, E.shutil.which = old[0], old[1]
        if old[2] is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old[2]


def t_progress_steps(w):
    """A take reports its steps (and, through rclone, a %) and finishes."""
    import threading
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    E.return_(a)
    make_config(b.base, value="bravo's own")
    seen = []

    class Rec(E.Progress):
        def step(self, i, n, text):
            seen.append((i, n, text))
            super().step(i, n, text)
    b.progress = Rec(os.path.join(w.tmp, "progress.json"))
    b.cancel = threading.Event()
    E.take(b)
    assert [x[0] for x in seen] == [1, 2, 3, 4, 5], seen
    st = E.read_progress(os.path.join(w.tmp, "progress.json"))
    assert st.get("active") is False and st.get("result") == "done", st


def t_cancel_take_restores(w):
    """Cancel after the config was installed: this rig's own config is put back, no lock."""
    import threading
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    E.return_(a)
    make_config(b.base, value="bravo's own")
    b.cancel = threading.Event()

    class CancelAt4(E.Progress):
        def step(self, i, n, text):
            super().step(i, n, text)
            if i == 4:
                b.cancel.set()                 # pressed Cancel while modules are copying
    b.progress = CancelAt4()
    expect_error(lambda: E.take(b), "cancelled", E.Cancelled)
    assert read_value(b.base) == "bravo's own", "own config put back"
    assert E.lock_holder(b) is None, "not locked"
    assert b.progress.snapshot().get("result") == "cancelled"


# ---- v2.0.0 (Rebel Sync) -----------------------------------------------------

def t_module_check_with_sudo(w):
    """Companion's folders belong to another user: the check goes through one sudo
    find, and modules already there are never downloaded again (BD-1, 8 Nov)."""
    a = w.rigs["alpha"]
    for n in ("aaa-1.0.0", "bbb-2.1.0"):
        add_module(a.base, n)
    os.makedirs(os.path.join(a.base, "modules", "half-1.0.0"))          # no manifest
    real_run = E.run
    seen = []

    def fake_run(cmd, *args, **kw):
        if cmd[:1] == ["sudo"]:
            seen.append(cmd[1])
            cmd = cmd[1:]
        return real_run(cmd, *args, **kw)
    E.run, a.sudo_files = fake_run, True
    try:
        assert E._complete_local_modules(a, "modules") == {"aaa-1.0.0", "bbb-2.1.0"}
        assert seen == ["find"], seen
    finally:
        E.run, a.sudo_files = real_run, False


def t_take_skips_modules_it_has(w):
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    add_module(a.base, "mod-1.0.0")
    add_module(a.base, "other-2.0.0")
    E.save(a)
    E.return_(a)
    make_config(b.base, value="b")
    add_module(b.base, "mod-1.0.0")                                     # bravo has one already
    fetched = []
    real = E.fetch_modules

    def counting(s, where):
        got = real(s, where)
        fetched.extend(got)
        return got
    E.fetch_modules = counting
    try:
        E.take(b)
    finally:
        E.fetch_modules = real
    assert fetched == ["other-2.0.0"], fetched
    assert os.path.exists(os.path.join(b.base, "modules", "other-2.0.0", "companion", "manifest.json"))


def t_claims_one_winner(w):
    """Two rigs take a free config at the same moment: exactly one gets it."""
    import threading
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    E.return_(a)
    make_config(b.base, value="b")
    a.claim_wait = b.claim_wait = 0.3
    results = {}

    def go(s):
        try:
            results[s.rig] = E.take(s)
        except E.SyncError as e:
            results[s.rig] = e
    ts = [threading.Thread(target=go, args=(s,)) for s in (a, b)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    won = [r for r, v in results.items() if isinstance(v, str)]
    assert len(won) == 1, results
    assert E.holder_name(E.lock_holder(a)) == won[0]
    assert not a.store.files("claims"), "claims cleared"


def t_stale_claim_ignored(w):
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    E.return_(a)
    make_config(b.base, value="b")
    b.store.write("claims/ghost", "ghost claims the config\n")
    real = b.store.__class__.age_hours
    b.store.__class__.age_hours = lambda self, rel: 1.0 if rel == "claims/ghost" else real(self, rel)
    try:
        E.take(b)
    finally:
        b.store.__class__.age_hours = real
    assert E.holder_name(E.lock_holder(b)) == "bravo"


def t_lost_claim_installs_nothing(w):
    """Another rig locks it while this one is claiming: nothing is installed."""
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    E.return_(a)
    make_config(b.base, value="b")
    real_sleep = E._claim_sleep
    E._claim_sleep = lambda t: E.write_lock(a, E.now())                # alpha locks it meanwhile
    try:
        expect_error(lambda: E.take(b), "took the config")
    finally:
        E._claim_sleep = real_sleep
    assert read_value(b.base) == "b"
    assert E.holder_name(E.lock_holder(b)) == "alpha"


def t_failed_take_releases_lock(w):
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    E.return_(a)
    make_config(b.base, value="b")
    real = E.install_config

    def boom(*args):
        raise E.SyncError("disk full")
    E.install_config = boom
    try:
        expect_error(lambda: E.take(b), "disk full")
    finally:
        E.install_config = real
    assert E.lock_holder(b) is None, "lock put back"
    assert not E.was_holding(b)


def t_prefetch_then_quick_take(w):
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    add_module(a.base, "mod-1.0.0")
    E.save(a)
    make_config(b.base, value="b")
    assert E.prefetch(b) is None, "held by alpha: nothing downloaded"
    E.return_(a)
    msg = E.prefetch(b)
    assert msg and "ready" in msg, msg
    assert read_value(b.base) == "b", "Companion's folder untouched"
    wa = E.waiting_area(b)
    assert os.path.exists(os.path.join(wa, "v5.0", "db.sqlite"))
    assert os.path.exists(os.path.join(wa, "modules", "modules", "mod-1.0.0", "companion", "manifest.json"))
    pulled = []
    real = b.store.__class__.copy_down
    b.store.__class__.copy_down = lambda self, rel, local: (pulled.append(rel), real(self, rel, local))
    try:
        E.take(b)
    finally:
        b.store.__class__.copy_down = real
    assert pulled == [], f"modules came from the waiting area: {pulled}"
    assert read_value(b.base) == "one"
    assert os.path.exists(os.path.join(b.base, "modules", "mod-1.0.0", "companion", "manifest.json"))
    assert E.tick(b) and E.holder_name(E.lock_holder(b)) == "bravo"   # holder's tick saves


def t_unfinished_take(w):
    """Locked to this rig but the take never finished: never saved over the stored
    copy; the next take completes it."""
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    E.return_(a)
    make_config(b.base, value="old on bravo")
    E.write_lock(b, E.now())                                             # killed mid-take
    assert E.save(b, quiet=True) is None
    expect_error(lambda: E.save(b), "didn't finish")
    assert E.take(b) == "bravo now has the config."
    assert read_value(b.base) == "one"


def t_storage_log(w):
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    E.return_(a)
    make_config(b.base, value="b")
    E.take(b)
    log = a.store.read("LOG.txt")
    assert "alpha: saved" in log and "alpha: returned" in log and "bravo: took" in log, log


def t_rename_local_files(w):
    home = os.path.join(w.tmp, "oldhome")
    os.makedirs(os.path.join(home, ".config", "companion-sync"))
    for n in ("holding", "rig-name", "storage.json"):
        with open(os.path.join(home, ".companion-sync-" + n), "w") as f:
            f.write(n)
    with open(os.path.join(home, ".config", "companion-sync", "settings.json"), "w") as f:
        f.write("{}")
    with open(os.path.join(home, ".rebel-sync-rig-name"), "w") as f:
        f.write("kept")                                                  # never overwritten
    E.migrate_local_names(home)
    assert open(os.path.join(home, ".rebel-sync-holding")).read() == "holding"
    assert open(os.path.join(home, ".rebel-sync-rig-name")).read() == "kept"
    assert os.path.exists(os.path.join(home, ".config", "rebel-sync", "settings.json"))
    assert not os.path.exists(os.path.join(home, ".companion-sync-holding"))


def t_rename_storage_folder(w):
    """Folder storage: CompanionSync is renamed to RebelSync once; the old place is
    marked as moved, so a rig still on Companion Sync is told and never uses it."""
    if w.kind == "rclone":
        seed(w)
        a, b = w.rigs["alpha"], w.rigs["bravo"]
        os.replace(os.path.join(w.shared, "RebelSync"), os.path.join(w.shared, "CompanionSync"))
        E._MIGRATED.clear()
        for s in (a, b):
            s._storage_migrated = False
        assert E.holder_name(E.lock_holder(a)) == "alpha"                # renamed by rclone moveto
        assert os.path.exists(os.path.join(w.shared, "RebelSync", "CURRENT"))
        assert "Rebel Sync" in open(os.path.join(w.shared, "CompanionSync", "MOVED")).read()
        assert E.holder_name(E.lock_holder(b)) == "alpha"
        oldrig = dataclasses.replace(b, folder="CompanionSync")
        oldrig._storage_migrated = True
        assert "Rebel Sync" in E.moved_to(oldrig)
        return
    if w.kind != "folder":
        return
    seed(w)
    a, b = w.rigs["alpha"], w.rigs["bravo"]
    old = os.path.join(w.shared, "CompanionSync")
    os.replace(a.folder_path, old)
    for s in (a, b):
        s.folder_path = old
        s.storage_file = os.path.join(os.path.dirname(s.holding_file), "storage.json")
        s._storage_migrated = False
    assert E.holder_name(E.lock_holder(a)) == "alpha"                    # first use moves it
    assert a.folder_path == os.path.join(w.shared, "RebelSync")
    assert "Rebel Sync" in open(os.path.join(old, "MOVED")).read()
    assert E.holder_name(E.lock_holder(b)) == "alpha"                    # second rig follows
    assert b.folder_path == a.folder_path
    oldrig = dataclasses.replace(b, folder_path=old)
    oldrig._storage_migrated = True                                      # behaves like v1
    assert "Rebel Sync" in E.moved_to(oldrig)


SCENARIOS = [t_progress_steps, t_cancel_take_restores, t_linux_companion_modes, t_sign_in_elsewhere, t_move_storage, t_move_encrypted, t_encryption, t_encryption_off, t_github_connect, t_first_rig_starts_sharing, t_check_storage, t_messages_name_storage, t_choice_saved, t_first_save_and_status, t_take_refused_while_held, t_return_then_take,
             t_save_keeps_history, t_stale_takeover_and_stranded, t_modules_travel, t_modules_duplicates_and_half_copies,
             t_older_companion_refused, t_boot, t_unreachable, t_github_race,
             t_github_quick_setup, t_github_limited,
             t_module_check_with_sudo, t_take_skips_modules_it_has, t_claims_one_winner, t_stale_claim_ignored,
             t_lost_claim_installs_nothing, t_failed_take_releases_lock, t_prefetch_then_quick_take,
             t_unfinished_take, t_storage_log, t_rename_local_files, t_rename_storage_folder]


def main():
    kinds = sys.argv[1:] or ["folder", "rclone", "github"]
    failed = 0
    for kind in kinds:
        if kind == "rclone" and not shutil.which("rclone"):
            print("rclone not installed: skipping rclone scenarios")
            continue
        for t in SCENARIOS:
            w = World(kind)
            try:
                t(w)
                print(f"  ok    {kind:6} {t.__name__}")
            except Exception:
                failed += 1
                print(f"  FAIL  {kind:6} {t.__name__}")
                traceback.print_exc()
            finally:
                w.close()
    print("all passed" if not failed else f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
