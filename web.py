"""Companion Sync's web GUI and the local API the pop-up window uses.

Served by the tray app on gui_port (default 8090). Open to the network by
default, like Companion's own GUI; an optional password (gui_password) asks
for it on every device except this computer. Questions and sign-ins that
need a dialog still appear on this rig's own screen.
"""
import base64
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


class Handler(BaseHTTPRequestHandler):
    app = None                      # set by serve()
    server_version = "CompanionSync"

    def log_message(self, *a):      # quiet: the app has its own log
        pass

    # -- plumbing --
    def _local(self):
        return self.client_address[0] in LOOPBACK

    def _allowed(self):
        pw = self.app.gui_password()
        if not pw or self._local():
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(auth[6:]).decode().partition(":")
                return hmac.compare_digest(given, pw)
            except Exception:
                return False
        return False

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (
            json.dumps(body).encode() if ctype == "application/json" else body.encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Companion Sync"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except ValueError:
            return {}

    # -- routes --
    def do_GET(self):
        if not self._allowed():
            return self._deny()
        u = urlparse(self.path)
        a = self.app
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html")
        if u.path == "/logo.png":
            p = a.logo_path()
            if p:
                with open(p, "rb") as f:
                    return self._send(200, f.read(), "image/png")
            return self._send(404, {"error": "no logo"})
        if u.path == "/api/status":
            return self._send(200, a.snapshot())
        if u.path == "/api/settings":
            return self._send(200, a.get_settings())
        if u.path == "/api/log":
            n = int(parse_qs(u.query).get("lines", ["300"])[0])
            return self._send(200, {"lines": a.log_tail(min(n, 5000))})
        if u.path == "/api/update":
            return self._send(200, a.check_update())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed():
            return self._deny()
        u = urlparse(self.path)
        a = self.app
        if u.path.startswith("/api/do/"):
            ok, msg = a.web_action(u.path[len("/api/do/"):])
            return self._send(200 if ok else 400, {"ok": ok, "message": msg})
        if u.path == "/api/settings":
            ok, msg = a.set_settings(self._body())
            return self._send(200 if ok else 400, {"ok": ok, "message": msg})
        if u.path.startswith("/api/popup/") and self._local():
            a.popup_set(u.path.rsplit("/", 1)[1] == "show")
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def serve(app, port, network=True):
    """Start the web GUI in the background. Returns the server, or raises OSError
    (e.g. the port is in use)."""
    Handler.app = app
    httpd = ThreadingHTTPServer(("0.0.0.0" if network else "127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Companion Sync</title>
<link rel="icon" href="/logo.png">
<style>
:root{--bg:#100e16;--panel:#17141f;--line:#2c2440;--top:#221a33;--text:#e9e2ff;--dim:#9d8ccc;
--purple:#5500AA;--purple2:#7b3ae0;--btn:#2a1c48;--green:#2ea043;--amber:#e6961a;--red:#d23232;--blue:#286edc;--grey:#8c8c8c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 "Segoe UI",system-ui,-apple-system,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:14px 22px;background:var(--top);border-bottom:1px solid var(--line)}
header img{width:40px;height:40px;border-radius:9px}
header h1{font-size:19px;margin:0;font-weight:600}
header .sub{font-size:12px;color:var(--dim)}
header .pill{margin-left:auto;font-size:13px;padding:6px 12px;border-radius:20px;border:1px solid var(--line);display:flex;gap:8px;align-items:center}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;flex:none}
.wrap{display:flex;min-height:calc(100vh - 69px)}
nav{width:200px;flex:none;background:var(--panel);border-right:1px solid var(--line);padding:14px 10px}
nav a{display:block;padding:10px 14px;border-radius:7px;color:var(--dim);text-decoration:none;margin-bottom:3px;cursor:pointer}
nav a.on{background:var(--purple);color:#fff}
nav a:hover:not(.on){background:var(--btn);color:var(--text)}
main{flex:1;padding:24px 28px;max-width:860px}
h2{font-size:18px;margin:0 0 16px;font-weight:600}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:18px}
.state{display:flex;gap:12px;align-items:center;border-radius:9px;padding:14px 16px;margin-bottom:18px;border:1px solid}
.state b{font-size:16px}.state span{display:block;font-size:13px;opacity:.8}
.row{display:flex;justify-content:space-between;gap:16px;padding:9px 0;border-bottom:1px solid var(--line);font-size:14px}
.row:last-child{border-bottom:0}.row .k{color:var(--dim);flex:none}
.row span:last-child{text-align:right;overflow-wrap:anywhere;min-width:0}
[hidden]{display:none!important}
.btns{display:flex;flex-wrap:wrap;gap:10px;margin-top:4px}
button{font:inherit;font-size:14px;padding:10px 16px;border-radius:7px;border:1px solid var(--purple2);background:var(--btn);color:var(--text);cursor:pointer}
button.p{background:var(--purple);font-weight:600}
button:disabled{opacity:.45;cursor:default}
button:hover:not(:disabled){filter:brightness(1.2)}
.note{font-size:13px;color:var(--dim);margin:8px 0 0}
.ok{color:#4cc76a}.bad{color:#ff6b6b}.warn{color:#f0b040}
label.f{display:block;margin:0 0 14px}
label.f span{display:block;font-size:13px;color:var(--dim);margin-bottom:5px}
input,select{font:inherit;width:100%;max-width:340px;padding:8px 10px;border-radius:6px;border:1px solid var(--line);background:#0c0a12;color:var(--text)}
input[type=checkbox]{width:auto;accent-color:var(--purple)}
.chk{display:flex;gap:9px;align-items:center;margin:0 0 14px}
pre{background:#0c0a12;border:1px solid var(--line);border-radius:8px;padding:12px;font:12px/1.5 ui-monospace,Consolas,monospace;max-height:65vh;overflow:auto;white-space:pre-wrap;margin:0}
.pbar{height:10px;border-radius:5px;background:#0c0a12;overflow:hidden;margin:10px 0 6px}
.pbar i{display:block;height:100%;background:var(--purple2);transition:width .4s}
.prow{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:13px;opacity:.9}
.prow button{padding:5px 12px;font-size:13px}
#lost{position:fixed;inset:0;background:rgba(8,6,12,.93);display:none;flex-direction:column;align-items:center;justify-content:center;text-align:center;z-index:9;padding:24px}
#lost h1{font-size:34px;margin:0 0 12px;color:#c8b4ff;font-weight:600}
#lost p{color:var(--dim);margin:0}
#toast{position:fixed;right:20px;bottom:20px;background:var(--top);border:1px solid var(--purple2);border-radius:8px;padding:12px 16px;max-width:420px;display:none;white-space:pre-wrap}
@media(max-width:700px){.wrap{flex-direction:column}nav{width:auto;display:flex;flex-wrap:wrap;gap:4px;border-right:0;border-bottom:1px solid var(--line)}nav a{margin:0}main{padding:18px 16px}header .pill{display:none}}
</style></head><body>
<header><img src="/logo.png" alt=""><div><h1>Companion Sync</h1><div class="sub" id="sub">&nbsp;</div></div>
<div class="pill"><span class="dot" id="pdot"></span><span id="pline">…</span></div></header>
<div class="wrap"><nav id="nav"></nav><main id="main"></main></div>
<div id="toast"></div>
<div id="lost"><h1>I've got a bad feeling about this...</h1><p id="lostp">Lost contact with Companion Sync. Trying again…</p></div>
<script>
const PAGES=[["status","Status"],["storage","Storage & sign-in"],["encryption","Encryption"],["settings","Settings"],["log","Log"],["update","Update"]];
const COL={mine:"var(--green)",free:"var(--grey)",other:"var(--amber)",problem:"var(--red)",busy:"var(--blue)"};
let page=(location.hash||"#status").slice(1),S={};
const $=id=>document.getElementById(id);
const esc=t=>String(t??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function toast(m){const t=$("toast");t.textContent=m;t.style.display="block";clearTimeout(t._h);t._h=setTimeout(()=>t.style.display="none",6000)}
async function api(p,body){const r=await fetch(p,body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});return r.json()}
async function act(a){const r=await api("/api/do/"+a,{});toast(r.message||"");setTimeout(load,800)}
function nav(){$("nav").innerHTML=PAGES.map(([k,l])=>`<a class="${k==page?"on":""}" onclick="go('${k}')">${l}</a>`).join("")}
function go(k){page=k;location.hash=k;nav();render()}
function row(k,v,c){return `<div class="row"><span class="k">${k}</span><span class="${c||""}">${v}</span></div>`}
const mmss=t=>t==null?"":Math.floor(t/60)+":"+String(t%60).padStart(2,"0");
function progBox(){const p=S.progress||{};if(!p.active)return "";
const w=p.pct!=null?p.pct:(p.steps?Math.round(100*(p.step-1)/p.steps):0);
const extra=[p.pct!=null?p.pct+"%":"",p.speed||"",p.detail||"",mmss(p.elapsed)].filter(x=>x).join(" · ");
return `<div class="pbar"><i style="width:${w}%"></i></div><div class="prow"><span>${esc(p.action)} ${p.steps?p.step+"/"+p.steps:""} · ${esc(p.text)}${extra?"<br>"+esc(extra):""}</span><button onclick="act('cancel')">Cancel</button></div>`}
function stateBox(){const c=COL[S.state]||"var(--grey)";return `<div class="state" style="border-color:${c};background:color-mix(in srgb,${c} 16%,transparent);flex-wrap:wrap"><span class="dot" style="background:${c};box-shadow:0 0 8px ${c}"></span><div style="flex:1"><b>${esc(S.line)}</b><span>${esc(S.detail||"")}</span>${progBox()}</div></div>`}
const ASK='<p class="note">Questions and sign-in windows appear on this rig\'s own screen.</p>';
const R={
status(){return `<h2>Status</h2>${stateBox()}<div class="card">
${row("This rig",esc(S.rig))}${row("Holder",esc(S.holder||(S.mine?S.rig:"nobody")))}
${row("Last save",esc(S.last_save||"never"))}
${row("Companion",esc((S.companion_running?"Running":"Not running")+(S.local_version?" · "+S.local_version:"")),S.companion_running?"ok":"")}
${row("Config in storage",esc(S.drive_version||"—"))}${row("How Companion runs",esc(S.companion_desc||"—"))}
${row("Storage",esc(S.place))}${row("Version",esc(S.label||S.build))}</div>
<div class="btns"><button class="p" onclick="act('open')">Open Companion</button><button onclick="act('take')">Take config</button>
<button onclick="act('save')" ${S.mine?"":"disabled"}>Save now</button><button onclick="act('return')" ${S.mine?"":"disabled"}>Return config</button>
<button onclick="act('get_companion')">Get Companion…</button></div>${ASK}`},
storage(){return `<h2>Storage & sign-in</h2><div class="card">${row("Storage",esc(S.place))}${row("Where",esc(S.storage_where||"—"))}
${row("Signed in",S.signed_in===null?"—":S.signed_in?"Yes":"No",S.signed_in?"ok":"bad")}</div>
<div class="btns"><button class="p" onclick="act('storage')">Change storage…</button><button onclick="act('sign_in')">Sign in again…</button></div>
<p class="note">Every rig sharing a config must use the same storage and account. On the rig that has the config, changing storage moves the config.</p>${ASK}`},
encryption(){return `<h2>Encryption</h2><div class="card">${row("Encryption",S.encrypted===null?"—":S.encrypted?"On":"Off",S.encrypted?"ok":"")}</div>
<div class="btns"><button class="p" onclick="act('encryption')">${S.encrypted?"Encryption options…":"Turn encryption on…"}</button></div>
<p class="note">Off by default. Turn it on or off from the rig that has the config. If the passphrase is lost, the copy in storage can't be read.</p>${ASK}`},
async settings(){const g=await api("/api/settings");const pi=g.platform=="pi";return `<h2>Settings</h2><div class="card" id="setf">
<label class="f"><span>Rig name${g.holding?" (return the config before renaming)":""}</span><input id="rig" value="${esc(g.rig)}" ${g.holding?"disabled":""}></label>
<div class="chk"><input type="checkbox" id="login" ${g.start_at_login?"checked":""}><label for="login">Start at login</label></div>
<label class="f"><span>At login</span><select id="login_action"><option value="tray_only">Just show the icon</option><option value="take_and_open">Take the config and open Companion</option></select></label>
<label class="f"><span>Save every (minutes)</span><input id="save_every_min" type="number" min="1" value="${g.save_every_min}"></label>
<label class="f"><span>Saves kept in history</span><input id="keep_history" type="number" min="1" value="${g.keep_history}"></label>
<label class="f"><span>Safety copies kept on this rig</span><input id="keep_local" type="number" min="1" value="${g.keep_local}"></label>
<label class="f"><span>Hours before a lost rig's lock can be taken over</span><input id="stale_hours" type="number" min="1" value="${g.stale_hours}"></label>
${pi?`<label class="f"><span>Companion on this Pi</span><select id="companion_mode"><option value="auto">Detect</option><option value="service">Service (CompanionPi)</option><option value="desktop">Desktop app</option><option value="docker">Docker container</option></select></label>
<div class="chk"><input type="checkbox" id="tray_left_click" ${g.tray_left_click?"checked":""}><label for="tray_left_click">Left click on the tray icon shows the window (experimental; restart needed)</label></div>`:""}
<label class="f"><span>Pop-up window size (%)</span><input id="popup_size" type="number" min="50" max="300" step="5" value="${g.popup_size}"></label>
<label class="f"><span>Web GUI port (restart needed)</span><input id="gui_port" type="number" min="1024" max="65535" value="${g.gui_port}"></label>
<div class="chk"><input type="checkbox" id="gui_network" ${g.gui_network?"checked":""}><label for="gui_network">Other devices on the network can open this page (restart needed)</label></div>
<label class="f"><span>Password for other devices (blank = none)${g.has_password?" · one is set":""}</span><input id="gui_password" type="password" placeholder="${g.has_password?"unchanged":""}"></label>
<div class="chk" ${g.has_password?"":"hidden"}><input type="checkbox" id="clear_pw"><label for="clear_pw">Remove the password</label></div>
<div class="btns"><button class="p" onclick="saveSettings()">Save settings</button></div></div>`},
async log(){const r=await api("/api/log?lines=400");return `<h2>Log</h2><pre id="logp">${esc(r.lines.join("\n"))}</pre><div class="btns" style="margin-top:12px"><button onclick="render()">Refresh</button></div>`},
async update(){const u=await api("/api/update");const newer=u.newer;return `<h2>Update</h2><div class="card">${row("This version",esc(u.current))}${row("Latest release",esc(u.latest||"couldn't check"),newer?"warn":"ok")}</div>
<div class="btns"><button class="p" onclick="act('update')" ${newer?"":"disabled"}>${newer?"Update to "+esc(u.latest):"Up to date"}</button></div>
<p class="note">Downloads the latest build, installs it and reopens Companion Sync. Companion keeps running.</p>`}
};
async function render(){const h=await R[page]();$("main").innerHTML=h;if(page=="settings"){const g=await api("/api/settings");$("login_action").value=g.login_action;if($("companion_mode"))$("companion_mode").value=g.companion_mode||"auto"}if(page=="log"){const p=$("logp");p.scrollTop=p.scrollHeight}}
async function saveSettings(){const v=id=>$(id)?$(id).value:undefined,c=id=>$(id)?$(id).checked:undefined;
const b={rig:v("rig"),start_at_login:c("login"),login_action:v("login_action"),save_every_min:+v("save_every_min"),keep_history:+v("keep_history"),keep_local:+v("keep_local"),stale_hours:+v("stale_hours"),gui_port:+v("gui_port"),gui_network:c("gui_network"),companion_mode:v("companion_mode"),tray_left_click:c("tray_left_click"),popup_size:+v("popup_size")};
if(v("gui_password"))b.gui_password=v("gui_password");if(c("clear_pw"))b.gui_password="";
const r=await api("/api/settings",b);toast(r.message);render()}
let misses=0;
async function load(){try{S=await api("/api/status");misses=0;$("lost").style.display="none"}catch(e){
if(++misses>=2){$("lostp").textContent="Lost contact with Companion Sync"+(S.rig?" on "+S.rig:"")+". Trying again…";$("lost").style.display="flex"}
$("pline").textContent="Can't reach Companion Sync";$("pdot").style.background="var(--red)";return}
$("sub").textContent=S.rig+" · "+(S.label||"build "+S.build);$("pline").textContent=S.line;$("pdot").style.background=COL[S.state]||"var(--grey)";
if(["status","storage","encryption"].includes(page))render()}
nav();load().then(()=>{if(!["status","storage","encryption"].includes(page))render()});(function tick(){setTimeout(()=>load().finally(tick),(S.progress&&S.progress.active)?1000:(misses?2000:5000))})();
</script></body></html>
"""
