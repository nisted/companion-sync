"""The panel's REBELDECK page: one RebelDeck deck on BD-1's touchscreen.

RebelDeck (https://github.com/nisted/RebelDeck) runs on the Pi and keeps the
deck connected to Companion; this only draws its buttons and passes taps on,
through RebelDeck's local API. Which deck: panel.json "rebeldeck_deck" (a deck
id or name). Not set: a deck called "BD-1 Panel" sized to fit this screen,
made in RebelDeck the first time it's needed.
"""
import base64
import io
import json
import logging
import threading
import time
import urllib.request

from PIL import Image

import rebeldeck_link as RD

log = logging.getLogger("panel.rebeldeck")
PANEL_DECK = "BD-1 Panel"


def fit(w, h, k):
    """Columns x rows that give finger-sized buttons on a w x h area."""
    side = 70 * k / 1.33 if k < 1.2 else 72        # about 70 px on either screen
    return max(1, int(w // side)), max(1, int(h // side))


class DeckView:
    def __init__(self, cfg):
        self.cfg = cfg
        self.deck_id = None
        self.name = ""
        self.cols, self.rows = 0, 0
        self.keys = {}               # index -> {"image": PIL or None, "color": (r,g,b), "text": str}
        self.status = "starting"     # starting | not running | no deck | connecting | connected | ...
        self.problem = ""
        self.changed = threading.Event()
        self.lock = threading.Lock()
        self.size_wanted = (4, 3)
        self.held = None
        self.active = False
        self._cache = {}
        threading.Thread(target=self._run, daemon=True).start()

    # ---- from the panel ------------------------------------------------------------
    def show(self, on, size_wanted=None):
        """The panel is showing the page (or not). size_wanted: (cols, rows)
        for a deck made for the panel."""
        if size_wanted:
            self.size_wanted = size_wanted
        self.active = on
        if on:
            self.changed.set()

    def press(self, key, down):
        if not self.deck_id:
            return
        if down:
            self.held = key
        elif self.held is None:
            return
        else:
            key, self.held = self.held, None
        threading.Thread(target=self._post, args=(f"/api/decks/{self.deck_id}/press",
                                                  {"key": key, "pressed": down}), daemon=True).start()

    def image(self, key, size):
        """Key picture scaled to size, cached."""
        with self.lock:
            k = self.keys.get(key)
            im = k and k.get("image")
        if im is None:
            return None
        c = self._cache.get(key)
        if c and c[0] is im and c[1] == size:
            return c[2]
        out = im.resize(size, Image.LANCZOS)
        self._cache[key] = (im, size, out)
        return out

    # ---- RebelDeck ------------------------------------------------------------------
    def _post(self, path, body, method="POST"):
        try:
            req = urllib.request.Request(RD.base() + path, method=method, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.load(r)
        except Exception as e:
            log.info("rebeldeck %s: %s", path, e)
            return None

    def _find_deck(self):
        st = RD.status(max_age=0)
        if not st:
            self.status = "not running" if RD.installed() else "not installed"
            return None
        want = str(self.cfg.get("rebeldeck_deck") or "").strip()
        decks = st.get("decks", [])
        for d in decks:
            if want and want in (d["id"], d["name"]) or not want and d["name"] == PANEL_DECK:
                return d
        if want:
            self.status, self.problem = "no deck", want
            return None
        c, r = self.size_wanted
        d = self._post("/api/decks", {"name": PANEL_DECK, "columns": c, "rows": r})
        if d:
            log.info("made RebelDeck deck %r (%dx%d) for the panel", PANEL_DECK, c, r)
            self._post(f"/api/decks/{d['id']}", {"showing": False}, "PUT")    # no window for it on the Pi
        return d

    def _run(self):
        while True:
            if not self.active:
                time.sleep(0.3)
                continue
            try:
                d = self._find_deck()
                if d:
                    self._listen(d["id"])
            except Exception as e:
                log.info("rebeldeck: %s", e)
                self.status = "lost"
            self.changed.set()
            time.sleep(3)

    def _listen(self, deck_id):
        """Follow the deck's live updates until RebelDeck goes away or the
        panel stops showing it."""
        self.deck_id = deck_id
        with urllib.request.urlopen(f"{RD.base()}/api/decks/{deck_id}/events", timeout=20) as r:
            kind = None
            for raw in r:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event: "):
                    kind = line[7:]
                elif line.startswith("data: ") and kind:
                    self._event(kind, json.loads(line[6:]))
                    kind = None
                if not self.active:
                    return
        self.status = "lost"

    def _event(self, kind, data):
        if kind == "state":
            if data.get("gone"):
                self.deck_id = None
                self.status = "no deck"
                raise ConnectionError("deck removed")
            keys = {k["i"]: self._key(k) for k in data.get("keys", [])}
            with self.lock:
                self.cols, self.rows = data["cols"], data["rows"]
                self.name = data.get("name", "")
                self.status = data.get("status", "?")
                self.keys = keys
        elif kind == "key":
            k = self._key(data)
            with self.lock:
                self.keys[data["i"]] = k
        elif kind == "status":
            self.status = data.get("status", "?")
        self.changed.set()

    @staticmethod
    def _key(k):
        im = None
        src = k.get("image") or ""
        if src.startswith("data:image/"):
            try:
                im = Image.open(io.BytesIO(base64.b64decode(src.split(",", 1)[1]))).convert("RGB")
            except Exception:
                im = None
        col = k.get("color") or "#000000"
        try:
            rgb = tuple(int(col.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            rgb = (0, 0, 0)
        tc = k.get("textColor") or "#ffffff"
        try:
            trgb = tuple(int(tc.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            trgb = (255, 255, 255)
        return {"image": im, "color": rgb, "text": k.get("text") or "", "text_color": trgb}
