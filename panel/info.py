"""Facts for the BD-1 panel: Pi health, network, Companion and sync.

Each function returns a plain dict and never raises: anything it can't find
comes back as None, so the screens can show a dash.
"""
import datetime
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request


def sh(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# ---- system ------------------------------------------------------------------

_cpu_prev = None


def _cpu_percent():
    global _cpu_prev
    line = _read("/proc/stat")
    if not line:
        return None
    v = [int(x) for x in line.splitlines()[0].split()[1:]]
    idle, total = v[3] + v[4], sum(v)
    prev, _cpu_prev = _cpu_prev, (idle, total)
    if not prev or total == prev[1]:
        return None
    return max(0.0, min(1.0, 1 - (idle - prev[0]) / (total - prev[1])))


def system():
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    thr = sh(["vcgencmd", "get_throttled"])
    flags = int(thr.split("=")[1], 16) if thr and "=" in thr else None
    mem = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        mem[k] = int(v.split()[0]) if v.split() else 0
    du = shutil.disk_usage("/")
    up = _read("/proc/uptime")
    return {
        "temp": int(temp) / 1000 if temp else None,
        "undervolt_now": bool(flags & 0x1) if flags is not None else None,
        "throttled_now": bool(flags & 0x6) if flags is not None else None,
        "undervolt_since_boot": bool(flags & 0x10000) if flags is not None else None,
        "uptime": float(up.split()[0]) if up else None,
        "cpu": _cpu_percent(),
        "mem": 1 - mem["MemAvailable"] / mem["MemTotal"] if mem.get("MemTotal") else None,
        "disk": du.used / du.total if du.total else None,
    }


# ---- network -----------------------------------------------------------------

def _reachable(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def network():
    out = {"hostname": socket.gethostname(), "ip": None, "iface": None, "link": None,
           "speed": None, "gateway": None, "wifi": None, "internet": False, "drive": False}
    routes = sh(["ip", "-j", "route", "show", "default"])
    try:
        r = json.loads(routes)[0] if routes else None
    except (ValueError, IndexError):
        r = None
    if r:
        out["gateway"], out["iface"] = r.get("gateway"), r.get("dev")
    addrs = sh(["ip", "-j", "-4", "addr", "show"]) or "[]"
    try:
        for a in json.loads(addrs):
            if a.get("ifname") == "lo":
                continue
            for ai in a.get("addr_info", []):
                if a.get("ifname") == out["iface"] or not out["ip"]:
                    out["ip"] = ai.get("local")
    except ValueError:
        pass
    if out["iface"]:
        wireless = os.path.isdir(f"/sys/class/net/{out['iface']}/wireless")
        out["link"] = "wifi" if wireless else "ethernet"
        sp = _read(f"/sys/class/net/{out['iface']}/speed")
        out["speed"] = int(sp) if sp and sp.lstrip("-").isdigit() and int(sp) > 0 else None
    wl = [n for n in os.listdir("/sys/class/net") if os.path.isdir(f"/sys/class/net/{n}/wireless")] \
        if os.path.isdir("/sys/class/net") else []
    if wl:
        state = _read(f"/sys/class/net/{wl[0]}/operstate")
        ssid = sh(["iwgetid", "-r"]) if state == "up" else None
        out["wifi"] = ssid or ("connected" if state == "up" else "off")
    if out["iface"]:
        out["internet"] = _reachable("1.1.1.1", 53) or _reachable("8.8.8.8", 53)
        out["drive"] = _reachable("www.googleapis.com", 443)
    return out


# ---- Companion ---------------------------------------------------------------

def companion(cfg):
    running = sh(["systemctl", "is-active", "companion"]) == "active"
    since = None
    if running:
        t = sh(["systemctl", "show", "companion", "-p", "ActiveEnterTimestampMonotonic", "--value"])
        if t and t.isdigit() and int(t):
            since = time.monotonic() - int(t) / 1e6
    version = None
    try:
        with open(cfg["companion_package_json"]) as f:
            version = json.load(f).get("version")
    except (OSError, ValueError):
        pass
    return {"running": running, "running_for": since, "version": version}


def latest_stable(cfg):
    """Newest stable Companion build for this Pi, as CompanionPi's updater would pick it."""
    machine = os.uname().machine
    arch = {"x86_64": "x64", "aarch64": "arm64"}.get(machine, machine)
    target = "linux-tgz" if arch == "x64" else f"linux-{arch}-tgz"
    url = f"{cfg['companion_api']}?branch=stable&limit=5&target={target}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return None
    for pkg in data.get("packages", []):
        v = str(pkg.get("version", "")).lstrip("vV")
        if pkg.get("target") == target and v.split(".")[0].isdigit() \
                and int(v.split(".")[0]) in cfg["companion_majors"]:
            return v
    return None


def version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).lstrip("vV").split("+")[0].split("-")[0].split("."))
    except ValueError:
        return ()


# ---- sync --------------------------------------------------------------------

def sync(E, s):
    """Lock state from Google Drive, plus the local record of saves."""
    out = {"signed_in": None, "holder": None, "mine": False, "age_hours": None,
           "drive_version": None, "stranded": False, "offline": None, "checked": time.time()}
    try:
        out["signed_in"] = E.signed_in(s)
        if out["signed_in"]:
            out.update(E.status(s))
    except E.Offline as e:
        out["offline"] = str(e).splitlines()[0]
    except Exception as e:
        out["offline"] = f"{type(e).__name__}: {e}"
    st = E.read_state(s)
    out["last_save"] = st.get("last_save")
    out["last_error"] = st.get("last_error")
    out["last_error_time"] = st.get("last_error_time")
    return out


def boot_report(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def ago(iso):
    """'14:30 (11 min)' for a saved ISO time."""
    try:
        t = datetime.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    mins = int((datetime.datetime.now() - t).total_seconds() // 60)
    when = t.strftime("%H:%M") if t.date() == datetime.date.today() else t.strftime("%d %b")
    span = f"{mins} min" if mins < 120 else f"{mins // 60} h" if mins < 2880 else f"{mins // 1440} d"
    return f"{when}  ({span})"


def duration(secs):
    if secs is None:
        return None
    secs = int(secs)
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    return f"{d}d {h:02d}:{m:02d}" if d else f"{h:02d}:{m:02d}"
