"""Rebel Sync's web GUI and the local API the pop-up window uses.

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
    server_version = "RebelSync"

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
        self.send_header("WWW-Authenticate", 'Basic realm="Rebel Sync"')
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
        if u.path == "/api/files":
            return self._send(200, a.files_overview())
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
        if u.path == "/api/delete":
            b = self._body()
            ok, msg = a.delete_item(b.get("kind"), b.get("name"))
            return self._send(200 if ok else 400, {"ok": ok, "message": msg})
        if u.path == "/api/answer":
            b = self._body()
            ok = a.answer_prompt(b.get("id"), b.get("value"))
            return self._send(200, {"ok": ok})
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
<title>Rebel Sync</title>
<link rel="icon" href="/logo.png">
<style>
:root{--bar:#5a12b4;--hi:#7a2fe0;--on:#b98cff;--line:#2a2340;--ink:#120a22;--val:#5cd8f5;
--bg:#06070b;--panel:#0b0c12;--text:#dfe2ee;--dim:#9aa0b4;--btn:#17102b;
--green:#4fe07a;--amber:#ffa23e;--red:#ff5a4a;--blue:#5aa8ff;--grey:#8d92a3;
--head:"Archivo","Arial Narrow","Helvetica Neue",system-ui,sans-serif;--mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;--bh:52px;--nw:230px}
*{box-sizing:border-box;border-radius:0}
html{scroll-padding-top:calc(var(--bh) + 12px)}
body{margin:0;background:var(--bg);background-image:repeating-linear-gradient(0deg,transparent 0 3px,rgb(255 255 255/.018) 3px 4px);color:var(--text);font:14px/1.5 var(--mono)}
h2,h3,button,nav a,.title{font-family:var(--head)}
#bar{position:fixed;top:0;left:0;right:0;height:var(--bh);z-index:20;display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:stretch;background:var(--bg);border-bottom:2px solid var(--hi)}
#navtog{display:flex;align-items:center;gap:10px;min-width:0;padding:0 38px 0 14px;background:var(--bar);color:#f3ecff;border:0;cursor:pointer;font-weight:800;font-size:15px;letter-spacing:.04em;clip-path:polygon(0 0,100% 0,calc(100% - 26px) 100%,0 100%);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#navtog .bg{display:inline-block;transition:transform .2s}
body.nav-open #navtog .bg{transform:rotate(90deg)}
#navtog img{width:30px;height:30px;display:block;background:#f3ecff;padding:2px}
#tp{display:grid;align-content:center;gap:3px;padding:0 14px;min-width:0}
#tp .tpt{font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#tp .tpt.dim{color:var(--dim)}
#tp .tpr{display:flex;align-items:center;gap:10px}
#tp .tpr .pbar{flex:1}
#tp .tps{font-size:11.5px;color:var(--dim);white-space:nowrap}
#tp button{padding:2px 12px;font-size:12px}
.pbar{height:8px;background:#0c0a12;border:1px solid var(--line);overflow:hidden;position:relative}
.pbar i{display:block;height:100%;background:var(--hi);transition:width .4s}
.pbar.ind i{width:35%;position:absolute;animation:sl 1.3s ease-in-out infinite}
@keyframes sl{0%{left:-35%}100%{left:100%}}
#rt{display:flex;align-items:center;gap:12px;padding:0 12px 0 4px;font-size:12.5px;color:var(--dim)}
#led{width:11px;height:11px;background:var(--grey);flex:none}
#nav{position:fixed;top:var(--bh);bottom:0;left:0;width:var(--nw);background:var(--panel);border-right:2px solid var(--hi);padding:12px 10px;overflow-y:auto;z-index:15;transform:translateX(-105%);visibility:hidden;transition:transform .2s ease,visibility 0s linear .2s}
body.nav-open #nav{transform:none;visibility:visible;transition:transform .2s ease}
#nav a{display:flex;align-items:center;min-height:44px;padding:0 16px;margin-bottom:6px;color:var(--on);text-decoration:none;cursor:pointer;font-weight:800;font-size:13px;letter-spacing:.03em;text-transform:uppercase;background:color-mix(in srgb,var(--hi) 50%,#000);position:relative;clip-path:polygon(12px 0,100% 0,100% calc(100% - 12px),calc(100% - 12px) 100%,0 100%,0 12px)}
#nav a::before{content:"";position:absolute;inset:1.5px;background:#0d0b16;clip-path:inherit}
#nav a span{position:relative}
#nav a.on{background:var(--on);color:var(--ink)}
#nav a.on::before{background:var(--on)}
#scrim{display:none}
main{margin-left:0;padding:calc(var(--bh) + 22px) 22px 40px;max-width:920px;transition:margin-left .2s}
body.nav-open main{margin-left:var(--nw)}
@media(max-width:900px){body.nav-open main{margin-left:0}body.nav-open #scrim{display:block;position:fixed;inset:var(--bh) 0 0 0;background:rgb(0 0 0/.55);z-index:14}#rt .hide-s{display:none}}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.14em;color:var(--dim);margin:0 0 14px;font-weight:700;border-bottom:1px solid var(--line);padding-bottom:5px}
h3{font-size:13px;margin:16px 0 6px;color:var(--on);text-transform:uppercase;letter-spacing:.08em}
.card{background:var(--panel);border:1px solid var(--line);padding:14px 16px;margin-bottom:16px}
.state{display:flex;gap:12px;align-items:center;padding:14px 16px;margin-bottom:16px;border:1px solid}
.state b{font-size:16px;font-family:var(--head)}.state span{display:block;font-size:12.5px;opacity:.8}
.row{display:flex;justify-content:space-between;gap:16px;padding:8px 0;border-bottom:1px solid var(--line);font-size:13.5px}
.row:last-child{border-bottom:0}.row .k{color:var(--dim);flex:none;text-transform:uppercase;letter-spacing:.06em;font-size:12px}
.row>span:last-child{text-align:right;overflow-wrap:anywhere;min-width:0;color:var(--val)}
[hidden]{display:none!important}
.btns{display:flex;flex-wrap:wrap;gap:10px;margin-top:6px}
button{font:inherit;font-family:var(--head);font-weight:700;letter-spacing:.04em;font-size:13px;padding:9px 16px;border:1px solid var(--hi);background:var(--btn);color:var(--text);cursor:pointer}
button.p{background:var(--bar);color:#f3ecff;clip-path:polygon(0 0,calc(100% - 9px) 0,100% 9px,100% 100%,9px 100%,0 calc(100% - 9px))}
button:disabled{opacity:.4;cursor:default}
button:hover:not(:disabled){filter:brightness(1.25)}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid var(--on);outline-offset:2px}
.note{font-size:12.5px;color:var(--dim);margin:8px 0 0}
.ok{color:var(--green)!important}.bad{color:var(--red)!important}.warn{color:var(--amber)!important}
label.f{display:block;margin:0 0 14px}
label.f span{display:block;font-size:11.5px;color:var(--dim);margin-bottom:5px;text-transform:uppercase;letter-spacing:.06em}
input,select{font:inherit;width:100%;max-width:340px;padding:8px 10px;border:1px solid var(--line);background:#0c0a12;color:var(--text)}
input[type=checkbox],input[type=radio]{width:auto;accent-color:var(--hi)}
.chk{display:flex;gap:9px;align-items:center;margin:0 0 14px}
pre{background:#0c0a12;border:1px solid var(--line);padding:12px;font:12px/1.5 var(--mono);max-height:calc(100vh - 220px);overflow:auto;white-space:pre-wrap;margin:0}
.banner{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap;border:1px solid var(--hi);background:color-mix(in srgb,var(--bar) 35%,transparent);padding:10px 14px;margin-bottom:16px}
.banner button{padding:5px 12px;font-size:12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
.tile{background:var(--panel);border:1px solid var(--line);padding:12px 14px}
.tile .n{font-size:24px;color:var(--val);line-height:1.1}.tile .l{font-size:11.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--dim)}
.tile.w .n{color:var(--amber)}
details{border-top:1px solid var(--line)}
details>summary{cursor:pointer;list-style:none;display:flex;justify-content:space-between;gap:10px;padding:10px 0;font-family:var(--head);font-weight:700;text-transform:uppercase;letter-spacing:.06em;font-size:12.5px;color:var(--on)}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"▸";margin-right:8px}details[open]>summary::before{content:"▾"}
details>summary .c{margin-left:auto;color:var(--dim);font-weight:400;font-family:var(--mono)}
.list .row{align-items:center}.list button{padding:3px 10px;font-size:11.5px}
.list{max-height:320px;overflow:auto;margin-bottom:8px}
.help h3{margin-top:22px}.help p,.help li{color:var(--text);max-width:70ch}.help li{margin-bottom:4px}.help kbd{border:1px solid var(--line);padding:0 6px;color:var(--val)}
#modal{position:fixed;inset:0;background:rgba(6,7,11,.82);display:none;align-items:center;justify-content:center;z-index:30;padding:20px}
#modal .box{background:var(--panel);border:1px solid var(--hi);padding:20px 22px;max-width:520px;width:100%;max-height:90vh;overflow:auto}
#modal .box.problem{border-color:var(--red)}
#modal p{white-space:pre-wrap;margin:0 0 14px}
#modal .opt{display:flex;gap:9px;align-items:center;padding:7px 0}#modal .opt input{width:auto}
#lost{position:fixed;inset:0;background:rgba(6,7,11,.94);display:none;flex-direction:column;align-items:center;justify-content:center;text-align:center;z-index:40;padding:24px}
#lost h1{font-size:32px;margin:0 0 12px;color:var(--on);font-family:var(--head)}#lost p{color:var(--dim);margin:0}
#toast{position:fixed;right:20px;bottom:20px;background:var(--panel);border:1px solid var(--hi);padding:12px 16px;max-width:420px;display:none;white-space:pre-wrap;z-index:25}
@media(max-width:560px){main{padding-inline:14px}#rt{display:none}#navtog{padding-right:30px}#ttl{display:none}}
</style></head><body>
<header id="bar">
<button id="navtog" type="button" title="Show or hide the menu" aria-controls="nav" aria-expanded="false"><span class="bg">☰</span><img src="/logo.png" alt=""><span id="ttl">COMPANION SYNC</span></button>
<div id="tp" aria-live="polite"></div>
<div id="rt"><span id="sub" class="hide-s"></span><span id="led" title="Status colour"></span></div>
</header>
<nav id="nav" aria-label="Pages"></nav><div id="scrim"></div>
<main id="main"></main>
<div id="modal"><div class="box" id="mbox"></div></div>
<div id="toast"></div>
<div id="lost"><h1>I've got a bad feeling about this...</h1><p id="lostp">Lost contact with Rebel Sync. Trying again…</p></div>
<script>
const PAGES=[["status","Status","Who has the config, and what's happening now"],["storage","Storage & sign-in","Where the shared config lives, and signing in to it"],["files","Files","What's stored, what's on this rig, and clean-up"],["encryption","Encryption","Scramble the stored copy with a passphrase"],["settings","Settings","Rig name, timing, pop-up size, network access"],["log","Log","What Rebel Sync has been doing"],["update","Update","Check for and install a newer version"],["help","Help","What everything does"]];
const COL={mine:"var(--green)",free:"var(--grey)",other:"var(--amber)",problem:"var(--red)",busy:"var(--blue)"};
let page=(location.hash||"#status").slice(1),S={};
const $=id=>document.getElementById(id);
const esc=t=>String(t??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function toast(m){const t=$("toast");t.textContent=m;t.style.display="block";clearTimeout(t._h);t._h=setTimeout(()=>t.style.display="none",6000)}
async function api(p,body){const r=await fetch(p,body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});return r.json()}
async function act(a){const r=await api("/api/do/"+a,{});toast(r.message||"");setTimeout(load,800)}
function nav(){$("nav").innerHTML=PAGES.map(([k,l,t])=>`<a class="${k==page?"on":""}" title="${esc(t)}" onclick="go('${k}')"><span>${l}</span></a>`).join("");
const p=PAGES.find(x=>x[0]==page);$("ttl").textContent="COMPANION SYNC // "+(p?p[1].toUpperCase():"")}
function ls(k,v){try{if(v===undefined)return localStorage.getItem(k);localStorage.setItem(k,v)}catch(e){return null}}
function setNav(o){document.body.classList.toggle("nav-open",o);$("navtog").setAttribute("aria-expanded",o);ls("cs_nav",o?"1":"0")}
$("navtog").onclick=()=>setNav(!document.body.classList.contains("nav-open"));$("scrim").onclick=()=>setNav(false);
setNav(ls("cs_nav")===null?innerWidth>900:ls("cs_nav")=="1");
async function del(kind,name){if(!confirm("Delete "+(name||"all of Companion's local backups")+"? This can't be undone."))return;
const r=await api("/api/delete",{kind,name});toast(r.message||"");render()}
function go(k){page=k;location.hash=k;nav();render();if(innerWidth<=900)setNav(false);scrollTo(0,0)}
function row(k,v,c){return `<div class="row"><span class="k">${k}</span><span class="${c||""}">${v}</span></div>`}
const mmss=t=>t==null?"":Math.floor(t/60)+":"+String(t%60).padStart(2,"0");
function progBox(){const p=S.progress||{};if(!p.active)return "";
const w=p.pct!=null?p.pct:(p.steps?Math.round(100*(p.step-1)/p.steps):0);
const extra=[p.pct!=null?p.pct+"%":"",p.speed||"",p.detail||"",mmss(p.elapsed)].filter(x=>x).join(" · ");
return `<div class="pbar"><i style="width:${w}%"></i></div><div class="prow"><span>${esc(p.action)} ${p.steps?p.step+"/"+p.steps:""} · ${esc(p.text)}${extra?"<br>"+esc(extra):""}</span><button onclick="act('cancel')">Cancel</button></div>`}
function banners(){let h="";
if(S.updated_note)h+=`<div class="banner"><span>${esc(S.updated_note)}</span><button onclick="act('dismiss_updated')">OK</button></div>`;
if(S.update&&S.update.newer)h+=`<div class="banner"><span>Update available: <b>${esc(S.update.latest)}</b> (you have ${esc(S.label||S.build)})</span><button class="p" onclick="act('update')">Update now</button></div>`;
return h}
function stateBox(){const c=COL[S.state]||"var(--grey)";return `<div class="state" style="border-color:${c};background:color-mix(in srgb,${c} 16%,transparent);flex-wrap:wrap"><span class="dot" style="background:${c};box-shadow:0 0 8px ${c}"></span><div style="flex:1"><b>${esc(S.line)}</b><span>${esc(S.detail||"")}</span></div></div>`}
function topBar(){const p=S.progress||{},el=$("tp");
if(p.active){const w=p.pct!=null?p.pct:(p.steps?Math.round(100*(p.step-1)/p.steps):0);
const extra=[p.pct!=null?p.pct+"%":"",p.speed||"",p.detail||"",mmss(p.elapsed)].filter(x=>x).join(" · ");
el.innerHTML=`<div class="tpt">${esc(p.action)} ${p.steps?p.step+"/"+p.steps:""} · ${esc(p.text)}</div><div class="tpr"><div class="pbar"><i style="width:${w}%"></i></div><span class="tps">${esc(extra)}</span><button title="Stop this job" onclick="act('cancel')">Cancel</button></div>`}
else if(S.state=="busy")el.innerHTML=`<div class="tpt">${esc(S.line)}</div><div class="tpr"><div class="pbar ind"><i></i></div></div>`;
else el.innerHTML=`<div class="tpt dim">${esc(S.line||"")}</div>`}
const ASK='<p class="note">Questions and sign-in windows appear on this rig\'s own screen.</p>';
const R={
status(){return `<h2>Status</h2>${banners()}${stateBox()}<div class="card">
${row("This rig",esc(S.rig))}${row("Holder",esc(S.holder||(S.mine?S.rig:"nobody")))}
${row("Last save",esc(S.last_save||"never"))}
${row("Companion",esc((S.companion_running?"Running":"Not running")+(S.local_version?" · "+S.local_version:"")),S.companion_running?"ok":"")}
${row("Config in storage",esc(S.drive_version||"—"))}${row("How Companion runs",esc(S.companion_desc||"—"))}
${row("Storage",esc(S.place))}${row("Version",esc(S.label||S.build))}</div>
<div class="btns"><button class="p" title="Take the config if it's free, then open Companion" onclick="act('open')">Open Companion</button><button title="Take the config onto this rig without opening Companion" onclick="act('take')">Take config</button>
<button title="Save this rig's config to storage now" onclick="act('save')" ${S.mine?"":"disabled"}>Save now</button><button title="Save, then give the config back so another rig can take it" onclick="act('return')" ${S.mine?"":"disabled"}>Return config</button>
<button title="Open Companion's download page on this rig" onclick="act('get_companion')">Get Companion…</button></div>${ASK}`},
storage(){return `<h2>Storage & sign-in</h2><div class="card">${row("Storage",esc(S.place))}${row("Where",esc(S.storage_where||"—"))}
${row("Signed in",S.signed_in===null?"—":S.signed_in?"Yes":"No",S.signed_in?"ok":"bad")}</div>
<div class="btns"><button class="p" title="Choose where the config lives. On the rig that holds the config, this moves it" onclick="act('storage')">Change storage…</button><button title="Sign in again to the storage you already use" onclick="act('sign_in')">Sign in again…</button></div>
<p class="note">Every rig sharing a config must use the same storage and account. On the rig that has the config, changing storage moves the config.</p>${ASK}`},
async files(){const f=await api("/api/files");const st=f.storage||{},c=f.companion||{};
if(f.error)return `<h2>Files</h2><div class="card bad">${esc(f.error)}</div>`;
const mb=b=>b==null?"":(b/1048576).toFixed(1)+" MB";
const m=st.modules||{};const dups=Object.values(m).reduce((a,x)=>a+x.duplicates,0);
const nm=n=>typeof n=="string"?n:n.name;
const list=(kind,items)=>`<div class="list">${items.map(n=>`<div class="row"><span>${esc(nm(n))}${n.bytes!=null?` <span class="k">${mb(n.bytes)}</span>`:""}</span><button title="Delete this one" onclick="del('${kind}','${esc(nm(n))}')">Delete</button></div>`).join("")}</div>`;
const grp=(kind,items,title,open,tip)=>items&&items.length?`<details ${open?"open":""} title="${tip}"><summary>${title}<span class="c">${items.length}</span></summary>${list(kind,items)}</details>`:"";
const bk=c.backups||{};const safety=c.safety||[];
return `<h2>Files</h2>
<div class="tiles">
<div class="tile ${dups?"w":""}" title="Same-named module folders on Google Drive stop a take"><div class="n">${dups}</div><div class="l">Duplicate module folders</div></div>
<div class="tile" title="Earlier saves kept in storage"><div class="n">${(st.history||[]).length}</div><div class="l">History saves</div></div>
<div class="tile ${(st.stranded||[]).length?"w":""}" title="Edits from a rig whose lock was taken over"><div class="n">${(st.stranded||[]).length}</div><div class="l">Stranded edits</div></div>
<div class="tile" title="Copies of this rig's own config made before each take"><div class="n">${safety.length}</div><div class="l">Safety copies</div></div>
<div class="tile" title="Companion's own local backups on this rig"><div class="n">${bk.count??"—"}</div><div class="l">Local backups${bk.bytes!=null?" · "+mb(bk.bytes):""}</div></div></div>
<div class="btns" style="margin:0 0 18px"><button class="${dups?"p":""}" title="Merge same-named module folders in ${esc(st.where||"storage")}. Nothing is lost. Safe to run again if it stops." onclick="act('repair_modules')">Repair modules</button>
${bk.count?`<button title="Delete Companion's local backups on this rig" onclick="del('backups','')">Delete local backups</button>`:""}</div>
<div class="card"><b>In ${esc(st.where||"storage")}</b>${st.error?`<p class="bad">${esc(st.error)}</p>`:`
${row("Connection modules",(m.modules||{}).count??"—")}${row("Surface modules",(m.surfaces||{}).count??"—")}
${grp("stranded",st.stranded,"Stranded edits",true,"Edits saved by a rig that lost the lock; nothing overwrote the main copy")}
${grp("history",st.history,"History",false,"Earlier saves; the newest is first")}`}</div>
<div class="card"><b>On this rig</b>
${row("Config folder",esc(c.config_folder||c.config_dir||"—"))}${row("Modules here",c.modules?esc(Object.entries(c.modules).map(([k,v])=>v+" "+k).join(", ")):"—")}${row("Safety copies in",esc(c.safety_dir||"—"))}
${grp("safety",safety,"Safety copies",false,"Made before each take; the newest is first")}</div>`},
help(){return `<h2>Help</h2><div class="help">
<h3>The idea</h3><p>One Companion config is shared between your rigs through cloud storage. Only one rig holds it at a time. A rig <b>takes</b> it, works on it, <b>saves</b> it every few minutes, and <b>returns</b> it when finished.</p>
<h3>Buttons</h3><ul>
<li><b>Open Companion</b>: takes the config if it's free, then opens Companion.</li>
<li><b>Take config</b>: takes it without opening Companion.</li>
<li><b>Save now</b> / <b>Return config</b>: only for the rig that holds it. Quitting the Companion desktop app returns it automatically.</li></ul>
<h3>Colours</h3><ul><li><b class="ok">Green</b>: this rig has the config.</li><li>Grey: nobody has it.</li><li><b class="warn">Amber</b>: another rig has it.</li><li><b class="bad">Red</b>: a problem. Read the message.</li><li>Blue: working. The top bar shows progress, and Cancel.</li></ul>
<h3>Files</h3><p><b>Stranded edits</b> are changes from a rig whose lock was taken over; they're kept, never merged over the main copy. <b>Safety copies</b> are made on this rig before each take. <b>Repair modules</b> merges same-named module folders on Google Drive; a take can fail without it. It may take a long time and can be run again.</p>
<h3>Storage and sign-in</h3><p>Every rig must use the same storage and account. On the rig that holds the config, changing storage moves it. Sign-in for Google, Dropbox, OneDrive and GitHub opens a browser on the rig itself.</p>
<h3>Updating</h3><p>Update installs the newest release and reopens the app. Companion keeps running. A banner appears here and in the pop-up when a newer version is out, and says "Updated to…" afterwards.</p>
<h3>Pop-up window</h3><p>Click the tray icon. Drag it from anywhere that isn't a button; <kbd>Esc</kbd> or Hide closes it.</p>
<h3>Lost contact</h3><p>If this page can't reach Rebel Sync on the rig, a full-screen message appears and it keeps trying.</p>
<h3>Trouble</h3><ul><li>Check the <b>Log</b> page first.</li><li>A take that stops on a module: run <b>Files → Repair modules</b>, then take again.</li><li>Locked by a rig that's gone: after the lost-rig hours (Settings) you'll be asked to take over.</li></ul></div>`},
encryption(){return `<h2>Encryption</h2><div class="card">${row("Encryption",S.encrypted===null?"—":S.encrypted?"On":"Off",S.encrypted?"ok":"")}</div>
<div class="btns"><button class="p" title="Turn encryption on or off, or change options" onclick="act('encryption')">${S.encrypted?"Encryption options…":"Turn encryption on…"}</button></div>
<p class="note">Off by default. Turn it on or off from the rig that has the config. If the passphrase is lost, the copy in storage can't be read.</p>${ASK}`},
async settings(){const g=await api("/api/settings");const pi=g.platform=="pi";return `<h2>Settings</h2><div class="card" id="setf">
<label class="f"><span>Rig name${g.holding?" (return the config before renaming)":""}</span><input id="rig" value="${esc(g.rig)}" ${g.holding?"disabled":""}></label>
<div class="chk"><input type="checkbox" id="login" ${g.start_at_login?"checked":""}><label title="Open Rebel Sync when you log in" for="login">Start at login</label></div>
<label class="f"><span title="What to do when the app starts at login">At login</span><select id="login_action"><option value="tray_only">Just show the icon</option><option value="take_and_open">Take the config and open Companion</option></select></label>
<label class="f"><span title="How often a rig holding the config saves it">Save every (minutes)</span><input id="save_every_min" type="number" min="1" value="${g.save_every_min}"></label>
<label class="f"><span title="How many earlier saves are kept in storage">Saves kept in history</span><input id="keep_history" type="number" min="1" value="${g.keep_history}"></label>
<label class="f"><span title="Copies of this rig's own config made before each take">Safety copies kept on this rig</span><input id="keep_local" type="number" min="1" value="${g.keep_local}"></label>
<label class="f"><span title="If a rig stops saving for this long, another may take the config">Hours before a lost rig's lock can be taken over</span><input id="stale_hours" type="number" min="1" value="${g.stale_hours}"></label>
${pi?`<label class="f"><span>Companion on this Pi</span><select id="companion_mode"><option value="auto">Detect</option><option value="service">Service (CompanionPi)</option><option value="desktop">Desktop app</option><option value="docker">Docker container</option></select></label>
<div class="chk"><input type="checkbox" id="tray_left_click" ${g.tray_left_click?"checked":""}><label for="tray_left_click">Left click on the tray icon shows the window (experimental; restart needed)</label></div>`:""}
<label class="f"><span title="Scale of the small pop-up window (50 to 300)">Pop-up window size (%)</span><input id="popup_size" type="number" min="50" max="300" step="5" value="${g.popup_size}"></label>
<label class="f"><span title="The port this page is served on">Web GUI port (restart needed)</span><input id="gui_port" type="number" min="1024" max="65535" value="${g.gui_port}"></label>
<div class="chk"><input type="checkbox" id="gui_network" ${g.gui_network?"checked":""}><label for="gui_network">Other devices on the network can open this page (restart needed)</label></div>
<label class="f"><span title="Asked for on other devices; this computer never needs it">Password for other devices (blank = none)${g.has_password?" · one is set":""}</span><input id="gui_password" type="password" placeholder="${g.has_password?"unchanged":""}"></label>
<div class="chk" ${g.has_password?"":"hidden"}><input type="checkbox" id="clear_pw"><label for="clear_pw">Remove the password</label></div>
<div class="btns"><button class="p" title="Apply these settings" onclick="saveSettings()">Save settings</button></div></div>`},
async log(){const r=await api("/api/log?lines=400");return `<h2>Log</h2><pre id="logp">${esc(r.lines.join("\n"))}</pre><div class="btns" style="margin-top:12px"><button title="Load the latest log lines" onclick="render()">Refresh</button></div>`},
async update(){const u=await api("/api/update");const newer=u.newer;return `<h2>Update</h2><div class="card">${row("This version",esc(u.current))}${row("Latest release",esc(u.latest||"couldn't check"),newer?"warn":"ok")}</div>
<div class="btns"><button class="p" onclick="act('update')" ${newer?"":"disabled"}>${newer?"Update to "+esc(u.latest):"Up to date"}</button></div>
<p class="note">Downloads the latest build, installs it and reopens Rebel Sync. Companion keeps running.</p>`}
};
async function render(){const h=await R[page]();$("main").innerHTML=h;if(page=="settings"){const g=await api("/api/settings");$("login_action").value=g.login_action;if($("companion_mode"))$("companion_mode").value=g.companion_mode||"auto"}if(page=="log"){const p=$("logp");p.scrollTop=p.scrollHeight}}
async function saveSettings(){const v=id=>$(id)?$(id).value:undefined,c=id=>$(id)?$(id).checked:undefined;
const b={rig:v("rig"),start_at_login:c("login"),login_action:v("login_action"),save_every_min:+v("save_every_min"),keep_history:+v("keep_history"),keep_local:+v("keep_local"),stale_hours:+v("stale_hours"),gui_port:+v("gui_port"),gui_network:c("gui_network"),companion_mode:v("companion_mode"),tray_left_click:c("tray_left_click"),popup_size:+v("popup_size")};
if(v("gui_password"))b.gui_password=v("gui_password");if(c("clear_pw"))b.gui_password="";
const r=await api("/api/settings",b);toast(r.message);render()}
let shownPrompt=null;
function prompt(){const p=S.prompt,m=$("modal");if(!p){m.style.display="none";shownPrompt=null;return}
if(shownPrompt===p.id)return;shownPrompt=p.id;
let body=`<p>${esc(p.text)}</p>`,btns="";
if(p.kind=="choose")body+=p.options.map((o,i)=>`<label class="opt"><input type="radio" name="ch" value="${esc(o)}" ${i?"":"checked"}>${esc(o)}</label>`).join("");
if(p.kind=="secret")body+=`<input id="pin" type="password" autofocus>`;
if(p.kind=="folder")body+=`<input id="pin" type="text" style="max-width:none" autofocus>`;
if(p.kind=="ask")btns=`<button class="p" onclick="ans(true)">Yes</button><button onclick="ans(false)">No</button>`;
else if(p.kind=="tell")btns=`<button class="p" onclick="ans(true)">OK</button>`;
else btns=`<button class="p" onclick="ansv()">OK</button><button onclick="ans(null)">Cancel</button>`;
$("mbox").className="box"+(p.problem?" problem":"");$("mbox").innerHTML=body+`<div class="btns">${btns}</div>`;m.style.display="flex";
const i=$("pin");if(i){i.focus();i.onkeydown=e=>{if(e.key=="Enter")ansv()}}}
function ansv(){const p=S.prompt;if(!p)return;let v=null;
if(p.kind=="choose"){const c=document.querySelector("input[name=ch]:checked");v=c?c.value:null}else v=$("pin").value;ans(v)}
async function ans(v){const p=S.prompt;if(!p)return;$("modal").style.display="none";await api("/api/answer",{id:p.id,value:v});setTimeout(load,300)}
let misses=0;
async function load(){try{S=await api("/api/status");misses=0;$("lost").style.display="none"}catch(e){
if(++misses>=2){$("lostp").textContent="Lost contact with Rebel Sync"+(S.rig?" on "+S.rig:"")+". Trying again…";$("lost").style.display="flex"}
$("tp").innerHTML='<div class="tpt bad">Can\'t reach Rebel Sync</div>';$("led").style.background="var(--red)";return}
$("sub").textContent=S.rig+" · "+(S.label||"build "+S.build);$("led").style.background=COL[S.state]||"var(--grey)";topBar();
prompt();
if(["status","storage","encryption"].includes(page))render()}
nav();load().then(()=>{if(!["status","storage","encryption"].includes(page))render()});(function tick(){setTimeout(()=>load().finally(tick),(S.progress&&S.progress.active)||S.prompt||S.busy?1000:(misses?2000:5000))})();
</script></body></html>
"""
