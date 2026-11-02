"""Companion Sync's pop-up window (the approved mockup).

Runs as its own small process ('--popup <port>'), so it has its own main
thread for tkinter on every platform (the Mac needs that, as the tray icon
owns the app's main thread). It talks to the tray app over the local API:
reads /api/status every second, sends button presses to /api/do/<action>.
It's started hidden and shown or hidden by the tray app; closing the window
only hides it. It quits by itself when the tray app has gone.
"""
import json
import os
import sys
import tkinter as tk
import urllib.request

BG, TOP, TEXT, DIM, LINE = "#17141f", "#221a33", "#e9e2ff", "#9d8ccc", "#2c2440"
PURPLE, PURPLE2, BTN = "#5500AA", "#7b3ae0", "#2a1c48"
STATE = {   # tray colours: border, fill, sub-text
    "mine": ("#2ea043", "#1f3a26", "#a9d9b4"),
    "free": ("#8c8c8c", "#26242b", "#c4c4c4"),
    "other": ("#e6961a", "#3d2d14", "#f0cf99"),
    "problem": ("#d23232", "#3d1a1a", "#f0a6a6"),
    "busy": ("#286edc", "#172a45", "#a8c4f0"),
}
W, H = 380, 470
FONT = "Segoe UI" if sys.platform == "win32" else ("Helvetica Neue" if sys.platform == "darwin" else "DejaVu Sans")


POS_FILE = os.path.expanduser("~/.config/companion-sync/popup-position.json")


def load_pos():
    try:
        with open(POS_FILE) as f:
            d = json.load(f)
        return int(d["x"]), int(d["y"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_pos(x, y):
    try:
        os.makedirs(os.path.dirname(POS_FILE), exist_ok=True)
        with open(POS_FILE, "w") as f:
            json.dump({"x": x, "y": y}, f)
    except OSError:
        pass


class Popup:
    def __init__(self, port, logo=None):
        self.base = f"http://127.0.0.1:{port}"
        self.misses = 0
        self.visible = None
        r = self.root = tk.Tk()
        r.title("Companion Sync")
        r.configure(bg=BG)
        r.resizable(False, False)
        r.protocol("WM_DELETE_WINDOW", lambda: self.post("/api/popup/hide"))
        # Centred the first time, then wherever it was last left.
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        x, y = (sw - W) // 2, (sh - H) // 2
        pos = load_pos()
        if pos and -W // 2 < pos[0] < sw - 40 and 0 <= pos[1] < sh - 40:
            x, y = pos
        r.geometry(f"{W}x{H}+{x}+{y}")
        self.last_pos = (x, y)
        r.bind("<Configure>", self.moved)
        r.withdraw()

        head = tk.Frame(r, bg=BG)
        head.pack(fill="x", padx=20, pady=(18, 10))
        self.logo_img = None
        if logo:
            try:
                from PIL import Image, ImageTk
                self.logo_img = ImageTk.PhotoImage(Image.open(logo).convert("RGBA").resize((54, 54)))
                tk.Label(head, image=self.logo_img, bg=BG).pack(side="left", padx=(0, 14))
            except Exception:
                pass
        t = tk.Frame(head, bg=BG)
        t.pack(side="left")
        tk.Label(t, text="Companion Sync", bg=BG, fg=TEXT, font=(FONT, 16, "bold")).pack(anchor="w")
        self.sub = tk.Label(t, text="", bg=BG, fg=DIM, font=(FONT, 10))
        self.sub.pack(anchor="w")

        self.box = tk.Frame(r, bg=BG, highlightthickness=1)
        self.box.pack(fill="x", padx=20, pady=(4, 12))
        inner = tk.Frame(self.box, bg=BG)
        inner.pack(fill="x", padx=12, pady=10)
        self.dot = tk.Canvas(inner, width=14, height=14, highlightthickness=0, bg=BG)
        self.dot.pack(side="left", padx=(0, 10))
        tt = tk.Frame(inner, bg=BG)
        tt.pack(side="left", fill="x")
        self.line = tk.Label(tt, text="Starting…", bg=BG, fg=TEXT, font=(FONT, 12, "bold"), anchor="w")
        self.line.pack(anchor="w")
        self.detail = tk.Label(tt, text="", bg=BG, fg=DIM, font=(FONT, 9), anchor="w",
                               justify="left", wraplength=W - 100)
        self.detail.pack(anchor="w")
        self.box_parts = [self.box, inner, self.dot, tt, self.line, self.detail]

        rows = tk.Frame(r, bg=BG)
        rows.pack(fill="x", padx=20)
        self.vals = {}
        for key, label in (("last_save", "Last save"), ("place", "Storage"),
                           ("signed", "Sign-in"), ("enc", "Encryption")):
            tk.Frame(rows, bg=LINE, height=1).pack(fill="x")
            f = tk.Frame(rows, bg=BG)
            f.pack(fill="x", pady=6)
            tk.Label(f, text=label, bg=BG, fg=DIM, font=(FONT, 10)).pack(side="left")
            v = tk.Label(f, text="—", bg=BG, fg=TEXT, font=(FONT, 10))
            v.pack(side="right")
            self.vals[key] = v
        tk.Frame(rows, bg=LINE, height=1).pack(fill="x")

        grid = tk.Frame(r, bg=BG)
        grid.pack(fill="x", padx=20, pady=(16, 10))
        grid.columnconfigure(0, weight=1, uniform="b")
        grid.columnconfigure(1, weight=1, uniform="b")
        self.buttons = {}
        for i, (key, label, primary) in enumerate((
                ("open", "Open Companion", True), ("take", "Take config", False),
                ("save", "Save now", False), ("return", "Return config", False),
                ("gui", "Launch GUI ↗", False))):
            b = self.button(grid, label, key, primary)
            b.grid(row=i // 2, column=i % 2, columnspan=2 if key == "gui" else 1,
                   sticky="ew", padx=0 if key == "gui" else (0 if i % 2 == 0 else 5, 0 if i % 2 else 5),
                   pady=5)
            self.buttons[key] = b

        foot = tk.Frame(r, bg=BG)
        foot.pack(fill="x", padx=20, pady=(4, 16))
        self.chk = tk.Label(foot, text=" ", bg=PURPLE, fg="white", width=2, font=(FONT, 9, "bold"),
                            cursor="hand2")
        self.chk.pack(side="left")
        lab = tk.Label(foot, text="Start at login", bg=BG, fg=DIM, font=(FONT, 10), cursor="hand2")
        lab.pack(side="left", padx=8)
        for w in (self.chk, lab):
            w.bind("<Button-1>", lambda e: self.post("/api/do/toggle_login"))

    def moved(self, event):
        if event.widget is not self.root or not self.visible:
            return
        pos = (self.root.winfo_x(), self.root.winfo_y())
        if pos != self.last_pos:
            self.last_pos = pos
            if getattr(self, "_save_job", None):
                self.root.after_cancel(self._save_job)
            self._save_job = self.root.after(800, lambda: save_pos(*self.last_pos))

    def button(self, parent, text, key, primary):
        # Labels, not tk.Buttons: the Mac ignores tk.Button colours.
        bg = PURPLE if primary else BTN
        b = tk.Label(parent, text=text, bg=bg, fg=TEXT, font=(FONT, 11, "bold" if primary else "normal"),
                     pady=9, highlightthickness=1, highlightbackground=PURPLE2, highlightcolor=PURPLE2,
                     cursor="hand2")
        b.enabled = True
        b.base_bg = bg

        def press(_):
            if b.enabled:
                self.post(f"/api/do/{key}")
        b.bind("<Button-1>", press)
        b.bind("<Enter>", lambda e: b.enabled and b.configure(bg=PURPLE2))
        b.bind("<Leave>", lambda e: b.configure(bg=b.base_bg))
        return b

    def set_enabled(self, key, on):
        b = self.buttons[key]
        b.enabled = on
        b.configure(fg=TEXT if on else "#6d6290", cursor="hand2" if on else "arrow")

    # -- talking to the tray app --
    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=3) as r:
            return json.loads(r.read().decode())

    def post(self, path):
        try:
            req = urllib.request.Request(self.base + path, data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass
        self.root.after(300, self.poll_once)

    def poll_once(self):
        try:
            st = self.get("/api/status")
            self.misses = 0
        except Exception:
            self.misses += 1
            if self.misses > 5:               # the tray app has gone
                self.root.destroy()
            return
        self.show(st)

    def poll(self):
        self.poll_once()
        self.root.after(1000, self.poll)

    def show(self, st):
        want = bool(st.get("popup_visible"))
        if want != self.visible:
            self.visible = want
            if want:
                self.root.deiconify()
                self.root.lift()
                self.root.attributes("-topmost", True)
                self.root.after(300, lambda: self.root.attributes("-topmost", False))
                self.root.focus_force()
            else:
                self.root.withdraw()
        border, fill, subcol = STATE.get(st.get("state"), STATE["free"])
        for w in self.box_parts:
            w.configure(bg=fill)
        self.box.configure(highlightbackground=border, highlightcolor=border)
        self.dot.delete("all")
        self.dot.create_oval(1, 1, 13, 13, fill=border, outline=border)
        self.line.configure(text=st.get("line", ""))
        self.detail.configure(text=st.get("detail", ""), fg=subcol)
        self.sub.configure(text=f"{st.get('rig', '')} · build {st.get('build', '')}")
        self.vals["last_save"].configure(text=st.get("last_save") or "never")
        self.vals["place"].configure(text=st.get("place") or "—")
        si = st.get("signed_in")
        self.vals["signed"].configure(text="—" if si is None else "Signed in" if si else "Not signed in",
                                      fg="#4cc76a" if si else ("#ff6b6b" if si is False else TEXT))
        enc = st.get("encrypted")
        self.vals["enc"].configure(text="—" if enc is None else "On" if enc else "Off")
        self.set_enabled("save", bool(st.get("mine")))
        self.set_enabled("return", bool(st.get("mine")))
        self.set_enabled("take", not st.get("mine"))
        self.chk.configure(text="✓" if st.get("start_at_login") else " ",
                           bg=PURPLE if st.get("start_at_login") else BTN)

    def run(self):
        self.root.after(200, self.poll)
        self.root.mainloop()


def main(port, logo=None):
    Popup(port, logo).run()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8090,
         os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo-source.png"))
