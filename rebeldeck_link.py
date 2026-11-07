"""Companion Sync's link to RebelDeck (https://github.com/nisted/RebelDeck).

Two separate apps that know about each other:
  - the tray menu gets a RebelDeck item: show/hide its decks, open its web GUI,
    and install a RebelDeck update (one update check covers both apps);
  - if RebelDeck's "Let my decks travel between rigs with Companion Sync" is on,
    deck layouts go to storage on Save and come back on Take (REBELDECK.json,
    next to the LOCK file; which deck windows are open stays each rig's choice).

Everything here is best-effort: if RebelDeck isn't installed or running,
Companion Sync carries on as if it didn't exist.
"""
import json
import os
import time
import urllib.request

DIR = os.path.join(os.path.expanduser("~"), ".config", "rebeldeck")
SETTINGS = os.path.join(DIR, "settings.json")
STORE_FILE = "REBELDECK.json"
DOWNLOAD = "https://github.com/nisted/RebelDeck/releases/latest"

_cache = {"at": 0.0, "status": None}


def installed():
    return os.path.exists(SETTINGS)


def _settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def base():
    try:
        port = int(_settings().get("web_port", 8091))
    except (TypeError, ValueError):
        port = 8091
    return f"http://127.0.0.1:{port}"


def _call(method, path, body=None, timeout=1.5):
    req = urllib.request.Request(base() + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def status(max_age=3):
    """RebelDeck's /api/status, or None if it isn't running. Cached briefly
    so building the tray menu never waits on it."""
    if time.time() - _cache["at"] > max_age:
        try:
            _cache["status"] = _call("GET", "/api/status", timeout=0.4)
        except Exception:
            _cache["status"] = None
        _cache["at"] = time.time()
    return _cache["status"]


def forget():
    _cache["at"] = 0.0


def set_showing(deck_id, on):
    _call("PUT", f"/api/decks/{deck_id}", {"showing": bool(on)})
    forget()


def gui_url():
    return base() + "/gui"


def update_info():
    """{"newer": bool, "latest": label} from RebelDeck's own check, or None."""
    st = status()
    return (st or {}).get("update")


def update_now():
    _call("POST", "/api/update", {})


# ---- decks travelling with the config -------------------------------------------
def syncing():
    return bool(_settings().get("sync_decks_with_companion_sync"))


def decks_to_store():
    """This rig's deck layouts as text for storage, or None if not syncing."""
    if not syncing():
        return None
    decks = [{k: d.get(k) for k in ("id", "device_id", "name", "columns", "rows")}
             for d in _settings().get("decks", []) if isinstance(d, dict)]
    return json.dumps({"decks": decks, "saved": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=1)


def apply_stored(text):
    """Use the deck layouts from storage on this rig. Returns a line for the log."""
    if not text or not syncing():
        return None
    decks = json.loads(text).get("decks")
    if not isinstance(decks, list):
        return None
    try:
        _call("POST", "/api/decks/sync", {"decks": decks}, timeout=5)      # RebelDeck is running
        forget()
        return f"RebelDeck: {len(decks)} deck(s) from storage"
    except Exception:
        pass
    # not running: change its settings file; it picks them up when it starts
    cfg = _settings()
    showing = {d.get("id"): d.get("showing", True) for d in cfg.get("decks", []) if isinstance(d, dict)}
    cfg["decks"] = [dict(d, showing=showing.get(d.get("id"), True)) for d in decks]
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, SETTINGS)
    return f"RebelDeck: {len(decks)} deck(s) from storage (used when it next starts)"
