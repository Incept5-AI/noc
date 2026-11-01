#!/usr/bin/env python3
"""NOC collector + dashboard. Agents POST stats here; browsers view the dashboard.
Stdlib only. Runs anywhere reachable by the agents (e.g. a Spark, so it's Mac-independent).

Run:  python3 noc_server.py [--port 8787] [--token SECRET]
View: http://<this-host>:8787
"""
import argparse, datetime, json, os, re, time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HIST = 320          # samples retained per host; client shows a 300s window
WIN = 305           # seconds of history to send (client renders 300s)
STORE = {}          # name -> {"data":..., "ts":..., "hist":deque}
TOKEN = ""
# Queued power commands, handed back to agents on their next /report (see noc_web.py for the
# full rationale and the os/plug tiers). Local dev is unauthenticated — it is localhost — so
# there is no owner check and the UI treats the viewer as an admin.
PENDING_CMDS = {}   # agent name -> [command dicts]
CMD_RESULTS = {}    # target name -> {"action","ok","error","detail"?,"ts"}
CMD_AUDIT = {}      # command id -> {ip, machine, tier, action} for the outcome log
CMD_TTL = 25
RESULT_TTL = 120
# Append-only power audit (IP, time, action, outcome) — see noc_web.py for the full rationale.
AUDIT_LOG = os.getenv("NOC_AUDIT_LOG",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "power-audit.log"))


def _audit(event, **fields):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [ts, event]
    for k, v in fields.items():
        if v is None or v == "":
            continue
        s = str(v)
        if any(c in s for c in ' "\n\t'):
            s = '"' + s.replace('"', "'").replace("\n", " ").replace("\t", " ")[:200] + '"'
        parts.append(f"{k}={s}")
    line = " ".join(parts)
    print("AUDIT " + line, flush=True)
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _plug_relays(now):
    out = {}
    for name, s in STORE.items():
        if "data" not in s or now - s["ts"] > 20:
            continue
        pr = (s["data"] or {}).get("plug_relay") or {}
        if pr.get("plugs"):
            out[name] = {_norm(p): p for p in pr["plugs"]}
    return out


def _hw_for(name, relays):
    key = _norm(name)
    plug, cands = None, []
    for relay, plugs in relays.items():
        if key in plugs:
            plug = plugs[key]; cands.append(relay)
    return plug, cands

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        if path == "/api/power":
            ip = (self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                  or self.headers.get("X-Real-IP") or self.client_address[0])
            try:
                b = json.loads(raw or b"{}")
                name = (b.get("name") or "").strip()
                action = (b.get("action") or "").strip()
                tier = (b.get("tier") or "os").strip()
                pw = b.get("password") or ""
                def rej(reason, code=400):
                    _audit("power.request", ip=ip, machine=name, tier=tier, action=action,
                           result="REJECTED", reason=reason)
                    return self._send(code, json.dumps({"error": reason}).encode(), "application/json")
                if action not in ("reboot", "shutdown") or not pw or not name:
                    return rej("bad request")
                now = time.time()
                cid = "%x" % int(now * 1000)
                if tier == "hardware":
                    if _norm(b.get("confirm") or "") != _norm(name):
                        return rej("bad_confirm")
                    plug, cands = _hw_for(name, _plug_relays(now))
                    if not plug:
                        return rej("no_plug", 409)
                    op = "off" if action == "shutdown" else "cycle"
                    others = [r for r in cands if r != name]
                    relay = others[0] if others else (cands[0] if op == "off" else None)
                    if not relay:
                        return rej("no_relay", 409)
                    PENDING_CMDS.setdefault(relay, []).append(
                        {"kind": "plug", "op": op, "plug": plug, "pw": pw,
                         "target": name, "id": cid, "ts": now})
                    CMD_AUDIT[cid] = {"ip": ip, "machine": name, "tier": tier, "action": action}
                    _audit("power.request", ip=ip, machine=name, tier="hardware", action=action,
                           plug=plug, relay=relay, result="QUEUED", id=cid)
                    return self._send(200, json.dumps({"ok": True, "relay": relay}).encode(),
                                      "application/json")
                PENDING_CMDS.setdefault(name, []).append(
                    {"kind": "os", "action": action, "pw": pw, "id": cid, "ts": now})
                CMD_AUDIT[cid] = {"ip": ip, "machine": name, "tier": tier, "action": action}
                _audit("power.request", ip=ip, machine=name, tier="os", action=action,
                       result="QUEUED", id=cid)
                return self._send(200, b'{"ok":true}', "application/json")
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)[:160]}).encode(), "application/json")
        q = parse_qs(urlparse(self.path).query)
        if TOKEN and q.get("token", [""])[0] != TOKEN:
            return self._send(403, b"forbidden", "text/plain")
        try:
            d = json.loads(raw or b"{}")
            name = d.pop("name", None) or "unknown"
            now = time.time()
            cres = d.pop("cmd_result", None)             # route a command result to its target
            if isinstance(cres, dict):
                tgt = cres.get("target") or name
                CMD_RESULTS[tgt] = {**{k: cres[k] for k in ("action", "ok", "error", "detail")
                                       if k in cres}, "ts": now}
                info = CMD_AUDIT.pop(cres.get("id"), {}) if cres.get("id") else {}
                _audit("power.outcome", id=cres.get("id"), machine=tgt, tier=info.get("tier"),
                       action=cres.get("action"),
                       status="SUCCESS" if cres.get("ok") else "FAILED",
                       ip=info.get("ip"), error=cres.get("error"), detail=cres.get("detail"))
            s = STORE.setdefault(name, {"hist": deque(maxlen=HIST)})
            s["data"] = d; s["ts"] = now
            gu = d.get("gpu_util")                       # GPU-less hosts (e.g. Pi) track CPU instead
            hv = gu if gu is not None else d.get("cpu")
            s["hist"].append((now, round(hv) if hv is not None else 0))
            pend = PENDING_CMDS.pop(name, None)          # hand back queued commands (one-shot)
            if pend:
                fresh = [c for c in pend if now - c["ts"] <= CMD_TTL]
                if fresh:
                    keys = ("kind", "action", "op", "plug", "pw", "target", "id")
                    return self._send(200, json.dumps({"cmds": [
                        {k: c[k] for k in keys if k in c} for c in fresh]}).encode(),
                        "application/json")
            self._send(200, b"ok", "text/plain")
        except Exception as e:
            self._send(400, str(e).encode()[:200], "text/plain")

    def do_GET(self):
        if self.path.startswith("/metrics"):
            now = time.time()
            relays = _plug_relays(now)
            hosts = {}
            for name, s in STORE.items():
                if "data" not in s:
                    continue
                plug, _ = _hw_for(name, relays)
                host = {**s["data"], "age": round(now - s["ts"], 1), "hw_power": bool(plug),
                        "hist": [[round(now - ts, 1), v] for ts, v in s["hist"]
                                 if now - ts <= WIN]}
                cr = CMD_RESULTS.get(name)
                if cr and now - cr["ts"] <= RESULT_TTL:
                    host["cmd_result"] = {k: cr[k] for k in ("action", "ok", "error", "detail")
                                          if k in cr}
                hosts[name] = host
            # local dev is unauthenticated, so the UI treats the viewer as an admin
            self._send(200, json.dumps({"t": time.strftime("%H:%M:%S"), "me": None,
                                        "admin": True, "hosts": hosts}).encode(),
                       "application/json")
        else:
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")

PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<title>Incept5 NOC</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#080b10;--card:#0d131c;--line:#1b2634;--tile:#0a1017;--tline:#17212e;
 --fg:#c9d4e0;--dim:#8a97a6;--faint:#5a6b7d;--good:#3fb950;--warn:#d29922;--bad:#f0603e;
 --sp-idle:#3b82f6;--sp-ok:#3fb950;--sp-warn:#e3b341;--sp-hot:#f0883e;--sp-crit:#f0603e}
:root[data-theme=light]{--bg:#eef1f5;--card:#fff;--line:#dce3ec;--tile:#f6f8fb;--tline:#e3e9f0;
 --fg:#1b2430;--dim:#5b6b7d;--faint:#8a99ab;--good:#1a7f37;--warn:#9a6700;--bad:#cf372a;
 --sp-idle:#0969da;--sp-ok:#1a7f37;--sp-warn:#bf8700;--sp-hot:#bc4c00;--sp-crit:#cf372a}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;padding:14px}
header{display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap}
h1{font-size:16px;letter-spacing:2px;font-weight:700}
.sp{margin-left:auto}
button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:7px;
 padding:6px 11px;font:inherit;cursor:pointer}button:hover{border-color:var(--faint)}
.pulse{color:var(--faint);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card.stale{opacity:.55}
.card.tw{border-color:var(--warn);box-shadow:0 0 14px -4px var(--warn)}
.card.tc{border-color:var(--bad);box-shadow:0 0 14px -4px var(--bad);animation:tpulse 1.6s ease-in-out infinite}
@keyframes tpulse{50%{box-shadow:0 0 22px -2px var(--bad)}}
.chead{display:flex;align-items:center;gap:9px;margin-bottom:11px}
.dot{width:9px;height:9px;border-radius:50%}
.hn{font-size:15px;font-weight:700}
.badge{font-size:10px;padding:2px 7px;border-radius:20px;border:1px solid var(--line);color:var(--dim)}
.badge.logo{border:none;padding:0;background:none;display:inline-flex;align-items:center}
.badge.logo svg{height:15px;width:auto;display:block}
.logo.apple svg{fill:var(--fg)}.logo.nvidia svg{fill:#76b900}.logo.rpi svg{fill:#c51a4a}
.lat{margin-left:auto;font-size:11px;color:var(--faint)}
.temp{font-size:13px;font-weight:700;font-variant-numeric:tabular-nums}
.row{display:grid;grid-template-columns:46px 1fr 108px;align-items:center;gap:10px;margin:5px 0}
.lbl{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.bar{height:15px;background:var(--tile);border-radius:4px;overflow:hidden}
.fill{height:100%;border-radius:4px;transition:width .4s}
.val{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.sparkwrap{margin:9px 0 5px}
.spark{height:46px;width:100%;display:block;border-radius:7px;background:var(--tile);border:1px solid var(--tline)}
.spark .gl{stroke:var(--faint);opacity:.28;stroke-width:1;vector-effect:non-scaling-stroke}
.ticks{position:relative;height:12px;margin-top:3px}
.ticks span{position:absolute;top:0;font-size:9px;color:var(--faint);letter-spacing:.3px;white-space:nowrap}
.cont{font-size:11px;color:var(--dim);margin:5px 0 7px;min-height:15px}
.sysline{font-size:10.5px;color:var(--faint);margin:-3px 0 8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.models{font-size:11px;color:var(--dim);margin:5px 0 7px}
.models .m{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.models b{color:var(--fg);font-weight:600}.models .eng{color:var(--faint)}
.models .m.wrap{white-space:normal}
.models .m.sub{padding-left:10px;margin-bottom:2px}
.cont b{color:var(--good);font-weight:600}
table{width:100%;border-collapse:collapse;font-size:11px}
th{text-align:left;color:var(--faint);font-weight:500;padding:2px 6px;border-bottom:1px solid var(--line)}
td{padding:2px 6px;border-bottom:1px solid var(--tline);font-variant-numeric:tabular-nums}
td.r{text-align:right}.g{color:var(--good)}.a{color:var(--warn)}.b{color:var(--bad)}
.err{color:var(--bad);font-size:12px}
.menuwrap{position:relative}
.menu{position:absolute;top:34px;left:0;z-index:20;background:var(--card);border:1px solid var(--line);
 border-radius:9px;padding:7px;min-width:220px;box-shadow:0 10px 28px rgba(0,0,0,.4)}
.menu[hidden]{display:none}
.mtitle{font-size:10px;color:var(--faint);text-transform:uppercase;letter-spacing:1px;padding:3px 7px 6px}
.mrow{display:flex;align-items:center;gap:9px;padding:5px 7px;border-radius:6px;cursor:pointer;user-select:none}
.mrow:hover{background:var(--tile)}.mrow input{accent-color:var(--good);cursor:pointer}
.mrow .mdot{width:8px;height:8px;border-radius:50%;margin-left:auto}
.mactions{display:flex;gap:6px;margin-top:5px;border-top:1px solid var(--tline);padding-top:7px}
.mactions button{flex:1;padding:5px}
.tray{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:13px}
.tray:empty{display:none}
.chip{display:flex;align-items:center;gap:7px;background:var(--card);border:1px solid var(--line);
 border-radius:20px;padding:5px 12px;cursor:pointer;font-size:12px}
.chip:hover{border-color:var(--faint)}.chip .dot{width:8px;height:8px}
.chip .cn{font-weight:600}.chip .cm{color:var(--dim);font-size:11px}
.mini-btn{background:none;border:none;color:var(--faint);cursor:pointer;padding:0 3px;font-size:16px;line-height:1;margin-left:8px}
.mini-btn:hover{color:var(--fg)}
.pw-btn{margin-left:6px;font-size:14px}
.pw-btn.hw{font-size:11px;margin-left:4px}
.pw-btn.reboot:hover{color:var(--warn)}
.pw-btn.shutdown:hover{color:var(--bad)}
.cmdres{font-size:11px;margin:-2px 0 8px}
.cmdres.ok{color:var(--good)}.cmdres.err{color:var(--bad)}
.modal{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;padding:18px}
.modal[hidden]{display:none}
.modalbox{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;width:100%;max-width:380px;box-shadow:0 18px 50px rgba(0,0,0,.5)}
.modalbox .mhd{font-size:15px;font-weight:700;margin-bottom:6px}
.modalbox p{color:var(--dim);font-size:12.5px;margin-bottom:14px}
.modalbox input{width:100%;background:var(--tile);border:1px solid var(--line);border-radius:8px;color:var(--fg);font:inherit;padding:10px 11px}
.modalbox input:focus{outline:none;border-color:var(--faint)}
.mbtns{display:flex;gap:9px;margin-top:14px}
.mbtns button{flex:1;padding:9px;border-radius:8px}
.mcancel{background:var(--tile);color:var(--fg)}
.mgo{background:var(--warn);color:#120d00;border-color:var(--warn);font-weight:700}
.mgo.danger{background:var(--bad);color:#190603}
.mgo:disabled{opacity:.6;cursor:default}
.mmsg{margin-top:11px;font-size:12px;min-height:16px}
.mmsg.ok{color:var(--good)}.mmsg.err{color:var(--bad)}
.signout{color:var(--faint);text-decoration:none;font-size:12px;border:1px solid var(--line);border-radius:7px;padding:6px 10px}
.signout:hover{border-color:var(--faint);color:var(--fg)}
.addbtn{color:var(--good);text-decoration:none;font-size:12px;border:1px solid var(--line);border-radius:7px;padding:6px 10px}
.addbtn:hover{border-color:var(--good)}
.owner{font-size:10.5px;color:var(--faint);margin:-3px 0 8px}
.owner .sh{color:var(--good);font-weight:600}
@media (max-width:560px){
 body{padding:9px}
 .grid{grid-template-columns:1fr;gap:11px}
 header{gap:8px}
 h1{font-size:14px;letter-spacing:1px}
 .card{padding:12px}
 .row{grid-template-columns:38px 1fr 86px;gap:8px}
 .menu{min-width:190px}
 table{font-size:11px}th,td{padding:3px 4px}
}
</style></head><body>
<header><h1>◢ Incept5 NOC</h1>
<div class=menuwrap><button id=burger>☰ machines</button><div class=menu id=menu hidden></div></div>
<a class=addbtn href="/add">＋ add machine</a>
<button id=theme>◐ theme</button>
<a class=signout href="/logout">sign out</a>
<span class=sp></span><span class=pulse id=pulse>connecting…</span></header>
<div class=tray id=tray></div>
<div class=grid id=grid></div>
<div class=modal id=pwmodal hidden>
 <div class=modalbox>
  <div class=mhd id=pwtitle>Restart machine</div>
  <p id=pwdesc></p>
  <input id=pwinput type=password autocomplete=off autocapitalize=off autocorrect=off spellcheck=false
   placeholder="root / sudo password for this box">
  <input id=pwconfirm type=text autocomplete=off autocapitalize=off autocorrect=off spellcheck=false
   placeholder="type the machine name to confirm" style="margin-top:9px;display:none">
  <div class=mbtns>
   <button class=mcancel id=pwcancel>Cancel</button>
   <button class=mgo id=pwgo>Restart</button>
  </div>
  <div class=mmsg id=pwmsg></div>
 </div>
</div>
<script>
const showHist = true;
const esc=s=>String(s==null?'':s).replace(/[<>&"']/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c]));
const setTheme=t=>{document.documentElement.setAttribute('data-theme',t);localStorage.setItem('noc-theme',t);};
setTheme(localStorage.getItem('noc-theme')||'dark');
theme.onclick=()=>{setTheme(document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark');render(last);};
// --- machine visibility (burger menu + minimise tray) ---
let hiddenSet = new Set(JSON.parse(localStorage.getItem('noc-hidden')||'[]'));
const saveHidden=()=>localStorage.setItem('noc-hidden',JSON.stringify([...hiddenSet]));
function hide(n){hiddenSet.add(n);saveHidden();render(last);}
function show(n){hiddenSet.delete(n);saveHidden();render(last);}
function toggle(n,vis){vis?hiddenSet.delete(n):hiddenSet.add(n);saveHidden();render(last);}
function allVis(vis){const names=Object.keys(last.hosts);if(vis)hiddenSet.clear();else names.forEach(n=>hiddenSet.add(n));saveHidden();render(last);}
function dotOf(d){return d.age>12?'var(--bad)':(d.age>7?'var(--warn)':'var(--good)');}
function buildMenu(){
 const names=Object.keys(last.hosts).sort();
 menu.innerHTML='<div class=mtitle>show machines</div>'+
  (names.length?names.map(n=>{const d=last.hosts[n];
    return `<label class=mrow><input type=checkbox ${hiddenSet.has(n)?'':'checked'} onchange="toggle('${n}',this.checked)">
     <span>${n}</span><span class=mdot style="background:${dotOf(d)}"></span></label>`;}).join('')
   :'<div class=mrow style="color:var(--faint)">no machines yet</div>')+
  `<div class=mactions><button onclick="allVis(true)">show all</button><button onclick="allVis(false)">hide all</button></div>`;
}
function chip(name,d){
 const m=d.gpu_util!=null?('gpu '+n0(d.gpu_util)+'%'):('cpu '+n0(d.cpu)+'%');
 return `<span class=chip onclick="show('${name}')" title="click to expand">
   <span class=dot style="background:${dotOf(d)}"></span><span class=cn>${name}</span><span class=cm>${m}</span></span>`;
}
burger.onclick=e=>{e.stopPropagation();menu.hidden=!menu.hidden;if(!menu.hidden)buildMenu();};
document.addEventListener('click',e=>{if(!menu.hidden&&!menu.contains(e.target)&&e.target!==burger)menu.hidden=true;});
const cls=(v,g,a)=>v==null?'':v<g?'g':v<a?'a':'b';
const fc=(v,g,a)=>v<g?'var(--good)':v<a?'var(--warn)':'var(--bad)';
const n0=v=>v==null?'—':Math.round(v);
// uptime → two coarsest non-zero units ("23d 4h", "6h 13m", "48s"); the second is dropped
// when it rounds to zero rather than rendering a dead "23d 0h".
const UPU=[[31557600,'y'],[2629800,'mo'],[86400,'d'],[3600,'h'],[60,'m'],[1,'s']];
function fmtUp(s){
 s=Math.max(0,Math.floor(s));
 for(let i=0;i<UPU.length;i++){
  const big=Math.floor(s/UPU[i][0]);
  if(big<1)continue;
  const nxt=UPU[i+1], sm=nxt?Math.floor((s-big*UPU[i][0])/nxt[0]):0;
  return big+UPU[i][1]+(sm>0?' '+sm+nxt[1]:'');
 }
 return '0s';
}
function smooth(p){ // Catmull-Rom → cubic bézier for a soft curve through all points
 let d=`M${p[0][0].toFixed(1)} ${p[0][1].toFixed(1)}`;
 for(let i=0;i<p.length-1;i++){
  const a=p[i-1]||p[i], b=p[i], c=p[i+1], e=p[i+2]||c;
  const c1x=b[0]+(c[0]-a[0])/6, c1y=b[1]+(c[1]-a[1])/6;
  const c2x=c[0]-(e[0]-b[0])/6, c2y=c[1]-(e[1]-b[1])/6;
  d+=` C${c1x.toFixed(1)} ${c1y.toFixed(1)} ${c2x.toFixed(1)} ${c2y.toFixed(1)} ${c[0].toFixed(1)} ${c[1].toFixed(1)}`;
 }
 return d;
}
const WIN=300;                                   // seconds shown end-to-end
const cvar=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim()||'#888';
function sparkline(pairs,name){
 if(!pairs||pairs.length<2)return '';
 const w=400,ht=46,pad=3;
 const yv=v=>ht-pad-(Math.min(100,Math.max(0,v))/100)*(ht-2*pad);
 const xa=a=>+(w*(1-Math.min(WIN,a)/WIN)).toFixed(1);
 const pts=pairs.filter(p=>p[0]<=WIN).map(p=>[xa(p[0]),yv(p[1])]);
 if(pts.length<2)return '';
 const line=smooth(pts), x0=pts[0][0], x1=pts[pts.length-1][0];
 const area=line+` L${x1} ${ht} L${x0} ${ht} Z`;
 const gid='sg-'+(name||'x').replace(/[^a-z0-9]/gi,'');
 // gradient runs vertically: colour tracks the value at each moment (height), not the latest read.
 // resolve theme vars to literal colours — SVG stop-color attributes don't accept var().
 // spectrum: idle(blue) → comfortable(green) → yellow → orange → critical(red)
 const cIdle=cvar('--sp-idle'),cOk=cvar('--sp-ok'),cWarn=cvar('--sp-warn'),
       cHot=cvar('--sp-hot'),cCrit=cvar('--sp-crit');
 let grid='';
 for(let m=1;m<=4;m++){const x=(w*(1-m*60/WIN)).toFixed(1);grid+=`<line class=gl x1=${x} y1=0 x2=${x} y2=${ht}/>`;}
 const marks=[[0,'5m'],[20,'4m'],[40,'3m'],[60,'2m'],[80,'1m'],[100,'now']];
 const ticks=marks.map(([l,t],i)=>{
  const st=i==0?'left:1px':(i==marks.length-1?'left:100%;transform:translateX(-100%)':`left:${l}%;transform:translateX(-50%)`);
  return `<span style="${st}">${t}</span>`;}).join('');
 return `<div class=sparkwrap><svg class=spark viewBox="0 0 ${w} ${ht}" preserveAspectRatio=none>
  <defs><linearGradient id="${gid}" gradientUnits=userSpaceOnUse x1=0 y1=${yv(100)} x2=0 y2=${yv(0)}>
   <stop offset=0 stop-color="${cCrit}"/><stop offset=.20 stop-color="${cHot}"/>
   <stop offset=.42 stop-color="${cWarn}"/><stop offset=.62 stop-color="${cOk}"/>
   <stop offset=1 stop-color="${cIdle}"/></linearGradient></defs>
  <path d="${area}" fill="url(#${gid})" opacity=.20/>
  ${grid}
  <path d="${line}" fill=none stroke="url(#${gid})" stroke-width=1.9 stroke-linejoin=round stroke-linecap=round vector-effect=non-scaling-stroke/></svg>
  <div class=ticks>${ticks}</div></div>`;
}
const LOGOS={apple:'<path d="M12.152 6.896c-.948 0-2.415-1.078-3.96-1.04-2.04.027-3.91 1.183-4.961 3.014-2.117 3.675-.546 9.103 1.519 12.09 1.013 1.454 2.208 3.09 3.792 3.039 1.52-.065 2.09-.987 3.935-.987 1.831 0 2.35.987 3.96.948 1.637-.026 2.676-1.48 3.676-2.948 1.156-1.688 1.636-3.325 1.662-3.415-.039-.013-3.182-1.221-3.22-4.857-.026-3.04 2.48-4.494 2.597-4.559-1.429-2.09-3.623-2.324-4.39-2.376-2-.156-3.675 1.09-4.61 1.09zM15.53 3.83c.843-1.012 1.4-2.427 1.245-3.83-1.207.052-2.662.805-3.532 1.818-.78.896-1.454 2.338-1.273 3.714 1.338.104 2.715-.688 3.559-1.701"/>',nvidia:'<path d="M8.948 8.798v-1.43a6.7 6.7 0 0 1 .424-.018c3.922-.124 6.493 3.374 6.493 3.374s-2.774 3.851-5.75 3.851c-.398 0-.787-.062-1.158-.185v-4.346c1.528.185 1.837.857 2.747 2.385l2.04-1.714s-1.492-1.952-4-1.952a6.016 6.016 0 0 0-.796.035m0-4.735v2.138l.424-.027c5.45-.185 9.01 4.47 9.01 4.47s-4.08 4.964-8.33 4.964c-.37 0-.733-.035-1.095-.097v1.325c.3.035.61.062.91.062 3.957 0 6.82-2.023 9.593-4.408.459.371 2.34 1.263 2.73 1.652-2.633 2.208-8.772 3.984-12.253 3.984-.335 0-.653-.018-.971-.053v1.864H24V4.063zm0 10.326v1.131c-3.657-.654-4.673-4.46-4.673-4.46s1.758-1.944 4.673-2.262v1.237H8.94c-1.528-.186-2.73 1.245-2.73 1.245s.68 2.412 2.739 3.11M2.456 10.9s2.164-3.197 6.5-3.533V6.201C4.153 6.59 0 10.653 0 10.653s2.35 6.802 8.948 7.42v-1.237c-4.84-.6-6.492-5.936-6.492-5.936z"/>',rpi:'<path d="m19.8955 10.8961-.1726-.3028c.0068-2.1746-1.0022-3.061-2.1788-3.7348.356-.0938.7237-.1711.8245-.6182.6118-.1566.7397-.4398.8011-.7398.16-.1066.6955-.4061.6394-.9211.2998-.2069.4669-.4725.3819-.8487.3222-.3515.407-.6419.2702-.9096.3868-.4805.2152-.7295.05-.9817.2897-.5254.0341-1.0887-.7758-.9944-.3221-.4733-1.0244-.3659-1.133-.3637-.1215-.1519-.2819-.2821-.7755-.219-.3197-.2851-.6771-.2364-1.0458-.0964-.4378-.3403-.7275-.0675-1.0584.0356-.53-.1706-.6513.0631-.9117.1583-.5781-.1203-.7538.1416-1.0309.4182l-.3224-.0063c-.8719.5061-1.305 1.5366-1.4585 2.0664-.1536-.5299-.5858-1.5604-1.4575-2.0664l-.3223.0063C9.942.5014 9.7663.2394 9.1883.3597 8.9279.2646 8.807.0309 8.2766.2015c-.2172-.0677-.417-.2084-.6522-.2012l.0004.0002C7.5017.0041 7.369.049 7.2185.166c-.3688-.1401-.7262-.1887-1.0459.0964-.4936-.0631-.654.0671-.7756.219C5.2887.4791 4.5862.3717 4.264.845c-.8096-.0943-1.0655.4691-.7756.9944-.1653.2521-.3366.5013.05.9819-.1367.2677-.0519.5581.2703.9096-.085.3763.0822.6418.3819.8487-.0561.515.4795.8144.6394.9211.0614.3001.1894.5832.8011.7398.1008.4472.4685.5244.8245.6183-1.1766.6737-2.1856 1.56-2.1788 3.7348l-.1724.3028c-1.3491.8082-2.5629 3.4056-.6648 5.5167.124.6609.3319 1.1355.5171 1.6609.2769 2.117 2.0841 3.1082 2.5608 3.2255.6984.524 1.4423 1.0212 2.449 1.3696.949.964 1.977 1.3314 3.0107 1.3308.0152 0 .0306.0002.0457 0 1.0337.0006 2.0618-.3668 3.0107-1.3308 1.0067-.3483 1.7506-.8456 2.4491-1.3696.4766-.1173 2.2838-1.1085 2.5607-3.2255.1851-.5253.3931-1 .517-1.6609 1.8981-2.1113.6843-4.7089-.6649-5.517zm-1.0386-.3715c-.0704.8759-4.6354-3.0504-3.8472-3.1808 2.1391-.3558 3.9191.896 3.8472 3.1808zm-2.0155 4.3649c-1.1481.7409-2.8025.2626-3.6953-1.0681-.8928-1.3306-.6858-3.0101.4623-3.7509 1.1481-.7409 2.8025-.2627 3.6953 1.068.8927 1.3307.6858 3.0101-.4623 3.751zM13.6591 1.3721c.0396.1967.0843.321.1354.3577.2537-.272.4611-.5506.7878-.8123.0011.1537-.0776.3205.1169.4425.1752-.2356.4119-.4459.7263-.6244-.1514.2611-.026.3404.0554.4486.24-.2059.4681-.4144.9109-.5759-.121.1474-.2902.2914-.1108.4607.2473-.1544.496-.3086 1.0833-.4183-.1323.1475-.4059.295-.2401.4426.3104-.1186.6539-.2047 1.034-.2546-.182.1496-.3337.2963-.1846.4122.3323-.1022.7899-.2398 1.2372-.1212l-.2832.2849c-.0314.0382.6623.0297 1.1202.0364-.167.2321-.3375.4562-.437.8548.0454.0459.2723.0204.4862 0-.2194.4618-.6004.5783-.6893.776.134.1015.32.075.5232.006-.158.3254-.4892.5484-.7509.8123.0662.047.1818.075.4555.0425-.2418.257-.5339.492-.8802.7032.0614.0708.2722.0681.4678.0727-.3136.3069-.7173.466-1.0955.6668.1885.1288.3234.0988.4678.097-.2676.2198-.7225.3342-1.1448.4668.0803.1249.1607.1589.3324.194-.447.2473-1.0873.1343-1.2679.2607.0435.1243.1665.2053.3139.2728-.7197.0418-2.6879-.0262-3.0652-1.5156.7367-.8094 2.0813-1.7593 4.394-2.934-1.7994.6022-3.4229 1.405-4.7817 2.5096-1.5978-.7436-.4965-2.6197.283-3.3645zm-1.6126 5.3718c1.1329-.0123 2.5356.8325 2.53 1.6286-.005.7027-.9851 1.2715-2.5213 1.2607-1.5043-.0177-2.5172-.7148-2.5137-1.3957.003-.5603 1.2282-1.5263 2.505-1.4936zm-5.7646-.6006c.1717-.0351.252-.0692.3323-.194-.4223-.1327-.8772-.247-1.1448-.4668.1444.0018.2792.0318.4678-.097-.3783-.2008-.782-.3599-1.0956-.6668.1955-.0048.4064-.002.4677-.0728-.3462-.2113-.6383-.4463-.8801-.7033.2738.0325.3893.0045.4555-.0425-.2617-.264-.593-.487-.7509-.8123.2032.069.3892.0954.5232-.006-.089-.1977-.47-.3142-.6894-.776.214.0204.4409.0459.4863 0-.0994-.3985-.2698-.6226-.4369-.8547.4579-.0067 1.1516.0018 1.1202-.0364l-.2831-.2849c.4472-.1186.9049.019 1.2371.1213.1492-.1159-.0026-.2626-.1847-.4123.3801.05.7236.1361 1.034.2547.1659-.1476-.1076-.2951-.24-.4426.5872.1097.8361.2639 1.0833.4183.1794-.1694.0103-.3133-.1108-.4607.4428.1615.6709.37.911.5759.0814-.1082.2068-.1875.0554-.4486.3143.1785.5511.3888.7263.6244.1945-.122.1159-.2888.1169-.4426.3267.2618.534.5404.7879.8124.0511-.0366.0959-.161.1354-.3577.7794.7448 1.8807 2.6208.2831 3.3646-1.3589-1.1039-2.9817-1.9064-4.78-2.5086 2.3115 1.174 3.6556 2.1239 4.392 2.9328-.3773 1.4895-2.3455 1.5575-3.0651 1.5157.1473-.0676.2703-.1485.3139-.2728-.1806-.1264-.8209-.0134-1.2679-.2607zm2.8175 1.1334c.7881.1304-3.7769 4.0567-3.8472 3.1809-.0719-2.2846 1.7079-3.5367 3.8472-3.1809zm-4.847 8.7567c-1.1094-.8789-1.4668-3.4529.5901-4.6097 1.2394-.3273.4184 5.051-.5901 4.6097zm4.2656 4.5989c-.6257.3719-2.1452.2187-3.2252-1.3095-.7283-1.2823-.6345-2.5872-.123-2.9705.7648-.4589 1.9464.1609 2.8559 1.2003.7923.9405 1.1536 2.5927.4923 3.0797zm-1.2415-5.6086c-1.1481-.7409-1.3551-2.4203-.4623-3.7511.8928-1.3307 2.5472-1.8089 3.6952-1.068 1.1481.7409 1.3551 2.4203.4623 3.7509-.8926 1.3308-2.5471 1.809-3.6952 1.0682zm4.7948 8.2279c-1.3763.0584-2.7258-1.1105-2.7081-1.5157-.0206-.594 1.6758-1.0578 2.782-1.0306 1.1131-.0479 2.6068.3531 2.6097.8851.0184.5166-1.3547 1.6838-2.6836 1.6612zm2.7584-5.8578c.0081 1.3899-1.226 2.5225-2.7562 2.5299-1.5302.0073-2.7773-1.1135-2.7854-2.5033v-.0265c-.008-1.3899 1.2259-2.5226 2.7562-2.5299 1.5302-.0073 2.7773 1.1134 2.7853 2.5033a.7794.7794 0 0 1 .0001.0265zm3.855 2.0029c-1.186 1.6208-2.7916 1.684-3.3896 1.2325-.6255-.5811-.148-2.3854.7094-3.3747v-.0003c.9812-1.0912 2.0302-1.8037 2.7609-1.2469.4919.4828.7805 2.3008-.0807 3.3894zm1.0724-3.4301c-1.0086.4413-1.8298-4.9372-.5901-4.61 2.0568 1.1569 1.6994 3.731.5901 4.61zm-.0256-8.3279h.2985v-.5304h.2986c.1502 0 .2053.0624.2262.2052.0152.1088.0113.2395.0477.3253h.2984c-.0533-.0763-.0515-.2358-.0571-.3213-.0097-.1373-.0513-.2796-.1977-.3176v-.0037c.1502-.061.2149-.1807.2149-.341 0-.2048-.1539-.3738-.3974-.3738h-.732v1.3573zm.2985-1.1255h.3269c.1333 0 .2054.0573.2054.188 0 .1369-.0721.1942-.2054.1942H20.03v-.3822zm-1.0337.4633c0 .7009.5682 1.2694 1.2695 1.2694s1.2695-.5684 1.2695-1.2694c0-.7013-.5683-1.2697-1.2695-1.2697-.7013 0-1.2695.5684-1.2695 1.2697zm2.3275 0c0 .5845-.4737 1.058-1.058 1.058s-1.058-.4735-1.058-1.058c0-.5849.4737-1.058 1.058-1.058s1.058.4731 1.058 1.058z"/>'};
const LOGO_TITLE={apple:'Apple Silicon',nvidia:'NVIDIA',rpi:'Raspberry Pi'};
function vendorBadge(d){
 const k=d.gpu==='Apple'?'apple':(d.gpu==='NVIDIA'?'nvidia':(d.board==='rpi'?'rpi':null));
 if(k)return `<span class="badge logo ${k}" title="${LOGO_TITLE[k]}"><svg viewBox="0 0 24 24">${LOGOS[k]}</svg></span>`;
 return `<span class=badge>${d.gpu||d.os||'?'}</span>`;
}
function card(name,d){
 const stale=d.age>12;
 const dot=stale?'var(--bad)':(d.age>7?'var(--warn)':'var(--good)');
 const memPct=d.mem_total?100*(d.mem_total-d.mem_avail)/d.mem_total:0;
 const usedG=((d.mem_total-d.mem_avail)/1024).toFixed(1),totG=(d.mem_total/1024).toFixed(1);
 const kind=d.gpu==='Apple'?'apple':(d.gpu==='NVIDIA'?'nvidia':'plain');
 // temperature → coloured figure in the header (GPU die, else CPU die/SoC); fan RPM beside it;
 // thermal pressure above Nominal (Apple's throttling signal) → warning marker
 const temp=d.gpu_temp!=null?d.gpu_temp:d.cpu_temp;
 const tchip=temp==null?'':`<span class=temp style="color:${fc(temp,60,78)}" title="temperature">${n0(temp)}°</span>`;
 const fan=d.fan_rpm?`<span title="fan speed" style="color:var(--faint);font-size:11px"> ${d.fan_rpm}rpm</span>`:'';
 const up=d.uptime!=null?`<span title="uptime" style="color:var(--faint);font-size:11px">↑${fmtUp(d.uptime)}</span>`:'';
 // throttling (any platform): ⚠ in the header + coloured/pulsing card border when not Nominal
 const throtLvl=d.thermal&&d.thermal!=='Nominal'?d.thermal:null;
 const throt=throtLvl
   ?`<span style="color:${throtLvl==='Fair'?'var(--warn)':'var(--bad)'}" title="${d.thermal_detail||'thermal pressure: '+throtLvl} — throttling">⚠ ${throtLvl.toLowerCase()}</span>`:'';
 const tcls=throtLvl?(throtLvl==='Fair'?' tw':' tc'):'';
 // power → meter row; GB10 exposes no power.limit (N/A), so scale to nominal TDP: 140W NVIDIA, 80W Apple
 const watts=d.gpu_power;
 const wmax=d.gpu_power_max||(d.gpu==='Apple'?80:140);
 const fmtW=w=>(w<10?w.toFixed(1):Math.round(w))+' W';
 const clk=d.gpu_clk?` · ${n0(d.gpu_clk)}MHz`:'';
 const procs=(d.procs||[]).slice(0,4).map(p=>`<tr><td class="r ${cls(p.cpu,60,120)}">${p.cpu.toFixed(0)}</td>
   <td class=r>${p.mem.toFixed(1)}</td><td>${p.cmd}</td></tr>`).join('');
 const bar=(l,pct,v,g,a)=>`<div class=row><span class=lbl>${l}</span>
   <div class=bar><div class=fill style="width:${Math.min(100,pct||0)}%;background:${fc(pct||0,g,a)}"></div></div>
   <span class="val ${cls(pct,g,a)}">${v}</span></div>`;
 // power controls only where they'd actually work: an admin, or the machine's owner.
 const canPower=last.admin||(d.owner&&last.me&&d.owner===last.me);
 const powerBtns=canPower
   ?`<button class="mini-btn pw-btn reboot" title="software restart (OS reboot)" onclick="askPower('${name}','reboot','os')">⟳</button>`
   +`<button class="mini-btn pw-btn shutdown" title="software shutdown (OS)" onclick="askPower('${name}','shutdown','os')">⏻</button>`
   +(d.hw_power
     ?`<button class="mini-btn pw-btn hw reboot" title="hardware restart (power-cycle the smart plug)" onclick="askPower('${name}','reboot','hardware')">⚡⟳</button>`
     +`<button class="mini-btn pw-btn hw shutdown" title="hardware shutdown (cut the smart plug)" onclick="askPower('${name}','shutdown','hardware')">⚡⏻</button>`
     :'')
   :'';
 const cr=d.cmd_result;
 const CRL={reboot:'restart',shutdown:'shutdown',hw_restart:'hardware restart',hw_shutdown:'hardware shutdown'};
 const crHtml=cr?`<div class="cmdres ${cr.ok?'ok':'err'}">${CRL[cr.action]||esc(cr.action)} ${cr.ok?'issued ✓':'failed'}${cr.error?': '+esc(cr.error):(cr.detail?': '+esc(cr.detail):'')}</div>`:'';
 return `<div class="card${stale?' stale':''}${tcls}"><div class=chead>
   <span class=dot style="background:${dot}"></span><span class=hn>${name}</span>
   ${vendorBadge(d)}${tchip}${fan}${up}${throt}
   <span class=lat>${stale?'stale '+Math.round(d.age)+'s':d.ncpu+' cores'}</span>
   ${powerBtns}<button class=mini-btn title="minimise to tray" onclick="hide('${name}')">–</button></div>
  ${d.owner?`<div class=owner>${d.shared?'<span class=sh>shared</span> · ':''}${d.owner}</div>`:''}
  ${sysHtml(d)}
  ${crHtml}
  ${showHist?sparkline(d.hist,name):''}
  ${kind==='plain'?'':bar('GPU',d.gpu_util,n0(d.gpu_util)+'%'+clk,60,85)}
  ${bar('CPU',d.cpu,(d.cpu!=null?Math.round(d.cpu):'—')+'%',60,85)}
  ${bar('RAM',memPct,usedG+'/'+totG+'G',70,88)}
  ${watts!=null?bar('PWR',100*watts/wmax,fmtW(watts),70,90):''}
  ${reqBar(d,bar)}
  <div class=row><span class=lbl>Load</span><span style="grid-column:span 2;color:var(--dim)">${(d.load||[]).join('  ')}  (1·5·15m)</span></div>
  <div class=cont>containers: ${d.containers?('<b>'+d.containers.replace(/,/g,'</b> <b>')+'</b>'):'—'}</div>
  ${modelsHtml(d)}
  <table><tr><th>CPU%</th><th>MEM%</th><th>PROCESS</th></tr>${procs}</table></div>`;
}
const AUDIO=/whisper|TTS|diariz/i;
// Requests in flight against the scheduler's cap (--max-num-seqs), summed over this box's
// engines — "2 of 3 slots busy" is real headroom where GPU % reads ~100 for one request.
function reqBar(d,bar){
 let run=0,max=0,wait=0;
 for(const s of d.models||[]){const st=s.stats||{};
  if(s.gateway||!st.max_seqs)continue;run+=st.running||0;max+=st.max_seqs;wait+=st.waiting||0;}
 if(!max)return '';
 return bar('REQ',100*run/max,`${n0(run)}/${max}${wait?' +'+n0(wait)+'q':''}`,70,90);
}
// a source build reports a setuptools-scm version (0.1.dev21830+g8244d1c44.d20261015) — show
// "dev 8244d1c"; a release keeps its number, minus any +local suffix
function shortVer(v){
 if(!v)return '';const g=/\.dev\d+\+g([0-9a-f]{7})/.exec(v);
 return g?'dev '+g[1]:String(v).split('+')[0];
}
// Two lines per server: the model(s) with context, then engine · load beneath (port on hover).
// A router (llama.cpp router mode, LM Studio) can load any of dozens of on-disk models: a count,
// with the names on hover, rather than a line each.
function onDemand(s){
 const n=s.available_n||(s.available||[]).length;if(!n)return '';
 const names=(s.available||[]).join('\n')+(n>(s.available||[]).length?'\n…':'');
 return ` · <span title="${esc(names)}" style="cursor:help;text-decoration:underline dotted">+${n} on demand</span>`;
}
function modelsHtml(d){
 // Only what THIS box serves: a cluster gateway lists the whole cluster's menu on every box,
 // and an idle Ollama/LM Studio has nothing loaded — neither is served from here.
 const ss=(d.models||[]).filter(s=>!s.gateway&&s.models.length);
 if(!ss.length)return '';
 const rows=[],audio=[];
 for(const s of ss){
  if(AUDIO.test(s.engine||'')&&!s.models.some(m=>m.state==='loading')){
   for(const m of s.models)audio.push(`<b title="${esc(s.engine)} :${s.port}">${esc(m.id)}</b>`);
   continue;
  }
  const where=s.worker?'shard '+s.rank+' → '+s.head:':'+s.port;
  const st=s.stats||{};
  const load=st.running!=null?` · ${n0(st.running)}${st.max_seqs?'/'+st.max_seqs+' req':' run'}${st.waiting?' +'+n0(st.waiting)+' queued':''}`+
    (st.kv!=null?` · kv ${n0(st.kv)}%`:'')+(st.gen_tps?` · ${n0(st.gen_tps)} tok/s`:''):'';
  for(const m of s.models)
   rows.push(`<span class=m title="${esc(m.root||m.id)}"><b>${esc(m.id)}</b>`+
    `${m.ctx?' '+Math.round(m.ctx/1024)+'k':''}`+
    `${m.state==='loading'?' <span style="color:var(--warn)">loading…</span>':m.state==='sleeping'?' <span class=eng>sleeping</span>':''}</span>`);
  const fit=st.kv_conc!=null&&s.models[0]&&s.models[0].ctx?` · KV cache holds ${st.kv_conc} requests at the full ${Math.round(s.models[0].ctx/1024)}k`:'';
  rows.push(`<span class="m eng sub" title="${esc((s.engine||'')+' '+(s.version||'')+' '+where+fit)}">`+
    `${esc(s.engine)}${s.version?' '+esc(shortVer(s.version)):''}${s.worker?' · '+esc(where):''}${load}${onDemand(s)}</span>`);
 }
 // the speech servers (whisper, TTS, diarize) carry no load stats — one wrapped line, engine:port on hover
 if(audio.length)rows.push(`<span class="m wrap"><span class=eng>audio</span> ${audio.join(' <span class=eng>·</span> ')}</span>`);
 return `<div class=models>models:${rows.join('')}</div>`;
}
function sysHtml(d){
 const y=d.sys;if(!y)return '';
 const parts=[y.os_name,y.dgx_os,y.kernel,y.cpu_model,y.driver?'driver '+y.driver:null,y.cuda?'CUDA '+y.cuda:null].filter(Boolean);
 const net=[d.ip?'LAN '+d.ip:null,y.ts_ip?'TS '+y.ts_ip:null,y.ts_name||null].filter(Boolean).join(' · ');
 return `<div class=sysline title="${esc(parts.join(' · '))}">${esc(parts.join(' · '))}</div>`+
   (net?`<div class=sysline title="${esc(net)}">${esc(net)}</div>`:'');
}
let last={hosts:{}};
function render(j){
 last=j;const all=Object.keys(j.hosts).sort();
 const vis=all.filter(n=>!hiddenSet.has(n)), hid=all.filter(n=>hiddenSet.has(n));
 tray.innerHTML=hid.map(n=>chip(n,j.hosts[n])).join('');
 grid.innerHTML=all.length
   ?(vis.length?vis.map(n=>card(n,j.hosts[n])).join('')
     :'<p class=err>all machines minimised — click a chip above, or use ☰ machines</p>')
   :'<p class=err>no agents reporting yet — start noc_agent.py on your machines</p>';
 if(!menu.hidden)buildMenu();
}
// --- power control modal (software OS + hardware plug, restart / shutdown) ---
// A password is demanded for every action and lives only in the input for the duration of the
// request — never cached, stored, or put in localStorage. OS tier: the box's own root/sudo
// password. Hardware tier: the relay host's root password, plus a typed name confirmation.
const nrm=s=>String(s||'').toLowerCase().replace(/[^a-z0-9]/g,'');
let pwCtx=null;
function askPower(name,action,tier){
 tier=tier||'os';
 pwCtx={name,action,tier};
 const hw=tier==='hardware';
 const verb=action==='reboot'?'restart':'shut down';
 pwtitle.textContent=(hw?'Hardware ':'')+(action==='reboot'?'Restart ':'Shut down ')+name;
 pwdesc.innerHTML=hw
   ?('This will '+(action==='reboot'?'<b>power-cycle</b>':'<b>cut mains power to</b>')+' <b>'+esc(name)+
     '</b> at its smart plug. Enter the <b>root password of the relay host</b> that runs it, and '+
     'type the machine name to confirm.')
   :('This will <b>'+verb+'</b> <b>'+esc(name)+'</b>. Enter the box’s root / sudo password to '+
     'confirm — it is required for every action and is not saved.');
 pwinput.placeholder=hw?'root password (sudo on the relay host)':'root / sudo password for this box';
 pwconfirm.style.display=hw?'':'none';pwconfirm.value='';
 pwgo.textContent=(hw?'Power ':'')+(action==='reboot'?'restart':'shut down');
 pwgo.className='mgo'+(action==='shutdown'?' danger':'');
 pwinput.value='';pwmsg.textContent='';pwmsg.className='mmsg';pwgo.disabled=false;
 pwmodal.hidden=false;setTimeout(()=>pwinput.focus(),30);
}
function closePw(){pwmodal.hidden=true;pwCtx=null;pwinput.value='';pwconfirm.value='';}
pwcancel.onclick=closePw;
pwmodal.onclick=e=>{if(e.target===pwmodal)closePw();};
const pwKey=e=>{if(e.key==='Enter'){e.preventDefault();pwgo.click();}else if(e.key==='Escape')closePw();};
pwinput.addEventListener('keydown',pwKey);
pwconfirm.addEventListener('keydown',pwKey);
pwgo.onclick=async()=>{
 if(!pwCtx)return;
 const ctx=pwCtx, pw=pwinput.value;
 if(!pw){pwmsg.className='mmsg err';pwmsg.textContent='Password required.';return;}
 if(ctx.tier==='hardware'&&nrm(pwconfirm.value)!==nrm(ctx.name)){
  pwmsg.className='mmsg err';pwmsg.textContent='Type the machine name ('+ctx.name+') to confirm.';return;}
 pwgo.disabled=true;pwmsg.className='mmsg';pwmsg.textContent='Sending…';
 try{
  const r=await fetch('/api/power',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({name:ctx.name,action:ctx.action,tier:ctx.tier,password:pw,confirm:pwconfirm.value})});
  const j=await r.json().catch(()=>({}));
  if(r.ok&&j.ok){
   pwinput.value='';pwconfirm.value='';
   pwmsg.className='mmsg ok';
   pwmsg.textContent=(ctx.tier==='hardware'?'Hardware ':'')+(ctx.action==='reboot'?'restart':'shutdown')+
     ' sent'+(j.relay?' via '+j.relay:'')+' — '+ctx.name+' will act within a few seconds.';
   setTimeout(closePw,2200);
  }else{
   const e=j.error||'';
   pwmsg.className='mmsg err';
   pwmsg.textContent=e==='offline'?'That machine is not reporting right now — can’t reach it.':
     e==='no_plug'?'No smart plug is mapped to this machine.':
     e==='no_relay'?'No other LAN host is online to switch its plug right now.':
     e==='bad_confirm'?'The machine name you typed doesn’t match.':
     e==='forbidden'?'You can only control machines you own.':
     e==='unauthorized'?'Session expired — reload and sign in again.':
     e==='password required'?'Password required.':
     'Could not queue the command'+(e?' ('+esc(e)+')':'')+'.';
   pwgo.disabled=false;
  }
 }catch(_){pwmsg.className='mmsg err';pwmsg.textContent='Network error — try again.';pwgo.disabled=false;}
};

async function tick(){
 try{const r=await fetch('/metrics');
  if(r.status===401){location='/login';return;}
  const j=await r.json();render(j);
  pulse.textContent='updated '+j.t+' ● '+Object.keys(j.hosts).length+' hosts';}
 catch(e){pulse.textContent='poll error';}
 setTimeout(tick,2500);
}
tick();
</script></body></html>"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--token", default="")
    a = ap.parse_args()
    TOKEN = a.token
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), H)
    print(f"NOC collector on :{a.port}  (token={'set' if TOKEN else 'none'})", flush=True)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
