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
            extra = dict(storage="folder", storage_kind="nas", folder_path=os.path.join(shared, "CompanionSync"))
            os.makedirs(extra["folder_path"])
        self.shared = shared
        self.rigs = {}
        for name in ("alpha", "bravo"):
            home = os.path.join(self.tmp, name)
            base = os.path.join(home, "companion")
            os.makedirs(base)
            s = E.Settings("mac", base, os.path.join(home, "safety"), os.path.join(home, "holding"),
                           rig=name, state_file=os.path.join(home, "state.json"), log=lambda *a: None, **extra)
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
    gh.repo, gh.repo_exists = "nige/companion-sync-config", False
    assert E.github_quick_setup(a) == "nige/companion-sync-config" and gh.repo_exists and gh.repo_private
    assert E.github_quick_setup(a) == "nige/companion-sync-config"           # already there: reused
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
    assert a.folder_path == os.path.join(nas, "CompanionSync") and os.path.isdir(a.folder_path)
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
    assert (a.storage_kind, a.github_mode, a.github_repo) == ("github", "quick", "nige/companion-sync-config")
    gh.repo = "nige/companion-data"
    gh.installed = ["nige/companion-data", "nige/other"]
    E.connect_github(a, "limited", lambda c, u: None, lambda repos: repos[0])
    assert (a.github_mode, a.github_repo, a.github_client_id) == ("limited", "nige/companion-data", "Iv23liZE4l3VNzqtCS2G")
    assert "Reading and writing both work" in E.check_storage(a)


SCENARIOS = [t_github_connect, t_first_rig_starts_sharing, t_check_storage, t_messages_name_storage, t_choice_saved, t_first_save_and_status, t_take_refused_while_held, t_return_then_take,
             t_save_keeps_history, t_stale_takeover_and_stranded, t_modules_travel,
             t_older_companion_refused, t_boot, t_unreachable, t_github_race,
             t_github_quick_setup, t_github_limited]


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
