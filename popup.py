"""Companion Sync's pop-up window (the approved mockup).

Runs as its own small process ('--popup <port>'), so it has its own main
thread for tkinter on every platform (the Mac needs that, as the tray icon
owns the app's main thread). It talks to the tray app over the local API:
reads /api/status every second, sends button presses to /api/do/<action>.
It's started hidden and shown or hidden by the tray app; closing the window
only hides it. It quits by itself when the tray app has gone.
It has no title bar: drag it from anywhere that isn't a button; Hide or Esc closes it.
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


def _scale():
    """Size of the pop-up. Bigger than the mockup's pixels by default (Tk's
    fonts come out smallest on the Mac), times popup_size % from Settings."""
    base = 1.45 if sys.platform == "darwin" else 1.2
    try:
        with open(os.path.expanduser("~/.config/companion-sync/settings.json")) as f:
            pct = float(json.load(f).get("popup_size", 100))
    except (OSError, ValueError, TypeError, AttributeError):
        pct = 100
    return base * min(max(pct, 50), 300) / 100


S = _scale()


def z(n):
    """Scaled size: pixels and font points."""
    return max(1, round(n * S))


W = z(380)
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


def _mmss(sec):
    if not isinstance(sec, (int, float)):
        return ""
    return f"{int(sec) // 60}:{int(sec) % 60:02d}"


class Popup:
    def __init__(self, port, logo=None):
        self.base = f"http://127.0.0.1:{port}"
        self.misses = 0
        self.visible = None
        r = self.root = tk.Tk()
        r.title("Companion Sync")
        r.configure(bg=BG, highlightthickness=1, highlightbackground=PURPLE2, highlightcolor=PURPLE2)
        r.resizable(False, False)
        # No title bar and no taskbar button, like Companion's launcher window.
        self.borderless()
        r.protocol("WM_DELETE_WINDOW", lambda: self.post("/api/popup/hide"))
        r.withdraw()
        self.moved_by_user = False
        self.drag = None
        r.bind_all("<Escape>", lambda e: self.post("/api/popup/hide"))
        r.bind_all("<ButtonPress-1>", self.drag_start, add="+")
        r.bind_all("<B1-Motion>", self.drag_move, add="+")
        r.bind_all("<ButtonRelease-1>", self.drag_end, add="+")
        self.mac_background()
        self.win_icon = None                 # the CS logo on the taskbar button, not Tk's quill
        if logo:
            try:
                from PIL import Image, ImageTk
                self.win_icon = ImageTk.PhotoImage(Image.open(logo).convert("RGBA").resize((64, 64)))
            except Exception:
                try:
                    self.win_icon = tk.PhotoImage(file=logo)
                except Exception:
                    pass
            if self.win_icon:
                try:
                    r.iconphoto(True, self.win_icon)
                except Exception:
                    pass

        head = tk.Frame(r, bg=BG)
        head.pack(fill="x", padx=z(20), pady=(z(18), z(10)))
        self.logo_img = None
        if logo:
            try:
                from PIL import Image, ImageTk
                self.logo_img = ImageTk.PhotoImage(Image.open(logo).convert("RGBA").resize((z(54), z(54))))
                tk.Label(head, image=self.logo_img, bg=BG).pack(side="left", padx=(0, z(14)))
            except Exception:
                pass
        t = tk.Frame(head, bg=BG)
        t.pack(side="left")
        tk.Label(t, text="Companion Sync", bg=BG, fg=TEXT, font=(FONT, z(16), "bold")).pack(anchor="w")
        self.sub = tk.Label(t, text="", bg=BG, fg=DIM, font=(FONT, z(10)))
        self.sub.pack(anchor="w")

        self.box = tk.Frame(r, bg=BG, highlightthickness=1)
        self.box.pack(fill="x", padx=z(20), pady=(z(4), z(12)))
        # "Update available" / "Updated to ..." (click to act on it)
        self.note = tk.Label(r, text="", bg=PURPLE, fg="white", font=(FONT, z(10)), cursor="hand2",
                             padx=z(10), pady=z(6), wraplength=W - z(70), justify="left")
        self.note_action = None
        self.note_shown = False
        self.note.bind("<Button-1>", lambda e: self.note_action and self.post(self.note_action))
        inner = tk.Frame(self.box, bg=BG)
        inner.pack(fill="x", padx=z(12), pady=z(10))
        self.dot = tk.Canvas(inner, width=z(14), height=z(14), highlightthickness=0, bg=BG)
        self.dot.pack(side="left", padx=(0, z(10)))
        tt = tk.Frame(inner, bg=BG)
        tt.pack(side="left", fill="x")
        self.line = tk.Label(tt, text="Starting…", bg=BG, fg=TEXT, font=(FONT, z(12), "bold"), anchor="w")
        self.line.pack(anchor="w")
        self.detail = tk.Label(tt, text="", bg=BG, fg=DIM, font=(FONT, z(9)), anchor="w",
                               justify="left", wraplength=W - z(100))
        self.detail.pack(anchor="w")
        # Progress of a take / save / return: bar, step and speed, Cancel.
        self.prog = tk.Frame(self.box, bg=BG)
        self.bar = tk.Canvas(self.prog, height=z(10), highlightthickness=0, bg=BG)
        self.bar.pack(fill="x", padx=z(12), pady=(0, z(6)))
        prow = tk.Frame(self.prog, bg=BG)
        prow.pack(fill="x", padx=z(12), pady=(0, z(10)))
        self.ptext = tk.Label(prow, text="", bg=BG, fg=DIM, font=(FONT, z(9)), anchor="w")
        self.ptext.pack(side="left")
        self.cancel = tk.Label(prow, text="Cancel", bg=BTN, fg=TEXT, font=(FONT, z(9)), padx=z(10),
                               pady=z(2), highlightthickness=1, highlightbackground=PURPLE2, cursor="hand2")
        self.cancel.pack(side="right")
        self.cancel.bind("<Button-1>", lambda e: self.post("/api/do/cancel"))
        self.prog_shown = False
        self.box_parts = [self.box, inner, self.dot, tt, self.line, self.detail, self.prog, prow,
                          self.ptext, self.bar]

        rows = tk.Frame(r, bg=BG)
        rows.pack(fill="x", padx=z(20))
        self.vals = {}
        for key, label in (("last_save", "Last save"), ("place", "Storage"),
                           ("signed", "Sign-in"), ("enc", "Encryption")):
            tk.Frame(rows, bg=LINE, height=1).pack(fill="x")
            f = tk.Frame(rows, bg=BG)
            f.pack(fill="x", pady=z(6))
            tk.Label(f, text=label, bg=BG, fg=DIM, font=(FONT, z(10))).pack(side="left")
            v = tk.Label(f, text="—", bg=BG, fg=TEXT, font=(FONT, z(10)))
            v.pack(side="right")
            self.vals[key] = v
        tk.Frame(rows, bg=LINE, height=1).pack(fill="x")

        grid = tk.Frame(r, bg=BG)
        grid.pack(fill="x", padx=z(20), pady=(z(16), z(10)))
        grid.columnconfigure(0, weight=1, uniform="b")
        grid.columnconfigure(1, weight=1, uniform="b")
        self.buttons = {}
        for i, (key, label, primary) in enumerate((
                ("open", "Open Companion", True), ("take", "Take config", False),
                ("save", "Save now", False), ("return", "Return config", False),
                ("gui", "Launch GUI ↗", False))):
            b = self.button(grid, label, key, primary)
            b.grid(row=i // 2, column=i % 2, columnspan=2 if key == "gui" else 1,
                   sticky="ew", padx=0 if key == "gui" else (0 if i % 2 == 0 else z(5), 0 if i % 2 else z(5)),
                   pady=z(5))
            self.buttons[key] = b

        foot = tk.Frame(r, bg=BG)
        foot.pack(fill="x", padx=z(20), pady=(z(4), z(16)))
        self.chk = tk.Label(foot, text=" ", bg=PURPLE, fg="white", width=2, font=(FONT, z(9), "bold"),
                            cursor="hand2")
        self.chk.pack(side="left")
        lab = tk.Label(foot, text="Start at login", bg=BG, fg=DIM, font=(FONT, z(10)), cursor="hand2")
        lab.pack(side="left", padx=z(8))
        for w in (self.chk, lab):
            w.bind("<Button-1>", lambda e: self.post("/api/do/toggle_login"))
        hide = tk.Label(foot, text="Hide", bg=BTN, fg=TEXT, font=(FONT, z(10)), padx=z(14), pady=z(4),
                        highlightthickness=1, highlightbackground=PURPLE2, cursor="hand2")
        hide.pack(side="right")
        hide.bind("<Button-1>", lambda e: self.post("/api/popup/hide"))
        hide.bind("<Enter>", lambda e: hide.configure(bg=PURPLE2))
        hide.bind("<Leave>", lambda e: hide.configure(bg=BTN))

        # Its size comes from what's in it. Placed (centred the first time, then wherever
        # it was last left) each time it is shown, once its content is in, so it is
        # centred on its real size and not on an empty window's.
        r.minsize(W, 1)

    def borderless(self):
        r = self.root
        try:
            if sys.platform == "darwin":
                r.tk.call("::tk::unsupported::MacWindowStyle", "style", r._w, "plain", "none")
            else:
                r.overrideredirect(True)
        except tk.TclError:
            pass

    def mac_background(self):
        """Tk makes its process a normal Mac app (Dock icon); turn that back into a background one."""
        if sys.platform != "darwin":
            return
        try:
            import ctypes

            class PSN(ctypes.Structure):
                _fields_ = [("hi", ctypes.c_uint32), ("lo", ctypes.c_uint32)]
            lib = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            lib.TransformProcessType(ctypes.byref(PSN(0, 2)), 4)    # kCurrentProcess -> UIElement
        except Exception:
            pass

    def place(self):
        """Put the window where it belongs: the saved spot, else the middle of the screen."""
        r = self.root
        r.update_idletasks()
        w, h = max(W, r.winfo_reqwidth()), r.winfo_reqheight()
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 2)
        pos = load_pos()
        if pos and -w // 2 < pos[0] < sw - 40 and 0 <= pos[1] < sh - 40:
            x, y = pos
            self.moved_by_user = True
        if (x, y) != getattr(self, "at", None):
            r.geometry(f"{w}x{h}+{x}+{y}")
            self.at = (x, y)
        else:
            r.geometry(f"{w}x{h}")

    # -- dragging: anywhere that isn't a button --
    def drag_start(self, e):
        try:
            if str(e.widget.cget("cursor")) == "hand2":
                return
        except tk.TclError:
            pass
        self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        if self.drag:
            x, y = e.x_root - self.drag[0], e.y_root - self.drag[1]
            self.root.geometry(f"+{x}+{y}")
            self.at = (x, y)
            self.moved_by_user = True

    def drag_end(self, e):
        if self.drag and self.moved_by_user:
            save_pos(self.root.winfo_x(), self.root.winfo_y())
        self.drag = None

    def button(self, parent, text, key, primary):
        # Labels, not tk.Buttons: the Mac ignores tk.Button colours.
        bg = PURPLE if primary else BTN
        b = tk.Label(parent, text=text, bg=bg, fg=TEXT, font=(FONT, z(11), "bold" if primary else "normal"),
                     pady=z(9), highlightthickness=1, highlightbackground=PURPLE2, highlightcolor=PURPLE2,
                     cursor="hand2")
        b.enabled = True
        b.base_bg = bg

        def press(_):
            if b.enabled:
                self.post(f"/api/do/{key}")
                if key == "gui":                  # the GUI takes over: get out of the way
                    self.post("/api/popup/hide")
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
        with urllib.request.urlopen(self.base + path, timeout=1.5) as r:
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
            if self.misses > 2:               # the tray app has gone
                self.root.destroy()
            return
        if st.get("quitting"):                # the app is closing (e.g. for an update)
            self.root.destroy()
            return
        self.show(st)

    def poll(self):
        self.poll_once()
        self.root.after(1000, self.poll)

    def show(self, st):
        border, fill, subcol = STATE.get(st.get("state"), STATE["free"])
        for w in self.box_parts:
            w.configure(bg=fill)
        self.box.configure(highlightbackground=border, highlightcolor=border)
        self.dot.delete("all")
        self.dot.create_oval(1, 1, z(14) - 1, z(14) - 1, fill=border, outline=border)
        self.line.configure(text=st.get("line", ""))
        self.detail.configure(text=st.get("detail", ""), fg=subcol)
        self.sub.configure(text=f"{st.get('rig', '')} · {st.get('label') or 'build ' + str(st.get('build', ''))}")
        self.show_progress(st.get("progress") or {}, border, fill)
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
        self.notice(st)
        want = bool(st.get("popup_visible"))
        if want != self.visible:
            self.visible = want
            if want:
                self.place()                      # content is in: centre on the real size
                self.root.deiconify()
                self.root.lift()
                self.root.attributes("-topmost", True)
                self.root.after(300, lambda: self.root.attributes("-topmost", False))
                self.root.focus_force()
                self.mac_background()
            else:
                self.root.withdraw()
        elif want and not self.drag:
            self.place()                          # it grew (progress bar, longer line): stay centred

    def notice(self, st):
        up, done = st.get("update") or {}, st.get("updated_note")
        text = action = None
        if done:
            text, action = f"{done}  (click to dismiss)", "/api/do/dismiss_updated"
        elif up.get("newer"):
            text, action = f"Update available: {up.get('latest')}  (click to update)", "/api/do/update"
        self.note_action = action
        if text:
            self.note.configure(text=text)
            if not self.note_shown:
                self.note.pack(fill="x", padx=z(20), pady=(0, z(8)), before=self.box)
                self.note_shown = True
        elif self.note_shown:
            self.note.pack_forget()
            self.note_shown = False

    def show_progress(self, pr, colour, fill):
        if not pr.get("active"):
            if self.prog_shown:
                self.prog.pack_forget()
                self.prog_shown = False
            return
        if not self.prog_shown:
            self.prog.pack(fill="x")
            self.prog_shown = True
        self.bar.configure(bg=fill)
        self.bar.update_idletasks()
        w, h = max(self.bar.winfo_width(), 10), z(10)
        self.bar.delete("all")
        self.bar.create_rectangle(0, 0, w, h, fill="#0c0a12", outline="")
        pct = pr.get("pct")
        if pct is not None:
            self.bar.create_rectangle(0, 0, int(w * min(pct, 100) / 100), h, fill=PURPLE2, outline="")
        elif pr.get("steps"):                  # no % yet: show how far through the steps
            self.bar.create_rectangle(0, 0, int(w * (pr["step"] - 1) / pr["steps"]), h, fill=PURPLE, outline="")
        step = f"{pr.get('step')}/{pr.get('steps')} " if pr.get("steps") else ""
        extra = " · ".join(x for x in (f"{pct}%" if pct is not None else "", pr.get("speed", ""),
                                        _mmss(pr.get("elapsed"))) if x)
        self.ptext.configure(text=f"{pr.get('action', '')} {step}· {pr.get('text', '')}"
                                  + (f"\n{extra}" if extra else ""), justify="left", wraplength=W - z(140))

    def run(self):
        self.root.after(200, self.poll)
        self.root.mainloop()


def main(port, logo=None):
    if sys.platform == "win32":
        # Same name as the tray app's: the installer waits until neither is open,
        # as this window is a copy of the same .exe and keeps it locked.
        import ctypes
        global _mutex
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "CompanionSyncRunning")
    Popup(port, logo).run()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8090,
         os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo-source.png"))
