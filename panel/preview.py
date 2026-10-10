"""Render the panel's screens with sample data, for checking without the Pi."""
import os
import time

from hw import Preview

SAMPLE = {
    "system": {"temp": 52.4, "undervolt_now": False, "throttled_now": False, "undervolt_since_boot": False,
               "uptime": 187020, "cpu": 0.18, "mem": 0.41, "disk": 0.27},
    "network": {"hostname": "bd-1", "ip": "10.66.100.20", "iface": "eth0", "link": "ethernet",
                "speed": 1000, "gateway": "10.66.100.1", "wifi": "off", "internet": True, "drive": True},
    "companion": {"running": True, "running_for": 7440, "version": "5.0.5"},
    "latest": "5.0.6",
    "sync": {"signed_in": True, "holder": "bd-1", "mine": True, "age_hours": 0.2, "drive_version": "v5.0",
             "stranded": False, "offline": None, "last_save": None, "last_error": None},
    "boot": {"result": "taken", "holder": "bd-1", "message": "bd-1 now has the config."},
}


def render_samples(Panel, cfg, out):
    os.makedirs(out, exist_ok=True)
    p = Panel(cfg, Preview(out), settings=None, live=False)
    p.data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in SAMPLE.items()}
    p.data["sync"]["last_save"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 660))

    def shot(name):
        p.render().save(os.path.join(out, name + ".png"))

    for t in ("SYSTEM", "NETWORK", "COMPANION", "SYNC"):
        p.tab = t
        shot(t.lower())
    # tap-again confirm on SHUTDOWN and RETURN
    p.tab = "SYSTEM"
    p.confirm = ("shutdown", time.monotonic() + 5)
    shot("system-confirm")
    p.tab = "SYNC"
    p.confirm = ("return", time.monotonic() + 5)
    shot("sync-confirm")
    p.confirm = None
    # another rig holds the config: SYNC flashes (both phases)
    p.data["sync"].update(holder="k-2so", mine=False)
    p.tab = "SYSTEM"
    cfg_flash = cfg["flash_s"]
    for phase in (0, 1):
        t0 = time.time()
        while int(time.time() / cfg_flash) % 2 != phase and time.time() - t0 < 2:
            time.sleep(0.05)
        shot(f"flash-{phase}")
    p.tab = "SYNC"
    shot("sync-held")
    # overlays
    p.busy("UPDATING COMPANION", "Installing v5.0.6. This takes a few minutes;",
           "Companion is stopped until it's done.")
    shot("overlay-busy")
    p.message("TAKE OVER?", "K-2SO hasn't been seen for 31 hours.", "Anything it hasn't saved stays on it.",
              buttons=[("CANCEL", "close", "cyan"), ("TAKE OVER", "takeover", "red")], colour=p.C["amber"])
    shot("overlay-takeover")
    p.overlay = None
    # start-up screen, part way and done
    p.started = False
    p.start_time = time.monotonic()
    p.data["boot"] = None
    p.data["companion"] = {}
    img, _ = p.draw_startup()
    img.save(os.path.join(out, "startup-early.png"))
    p.data["boot"] = SAMPLE["boot"]
    p.data["companion"] = SAMPLE["companion"]
    img, done = p.draw_startup()
    img.save(os.path.join(out, "startup-done.png"))
    print("rendered to", out, "startup done:", done)
