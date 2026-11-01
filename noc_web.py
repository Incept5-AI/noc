#!/usr/bin/env python3
"""Public NOC dashboard with magic-link auth (ALLOWED_EMAIL_DOMAINS only).

Runs behind nginx/TLS (see deploy/).
  - Agents POST metrics to  /report?token=<NOC_TOKEN>   (no cookie)
  - Humans sign in with a magic link, then view  /  and  /metrics  (cookie-gated)

Dev:  uvicorn noc_web:app --port 8788   (set COOKIE_SECURE=0 to test over http)
"""
import datetime
import ipaddress
import os
import re
import secrets
import time
from collections import deque

from fastapi import FastAPI, Form, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse, Response)

import noc_auth as auth
from noc_server import PAGE  # single source of truth for the dashboard HTML

REPORT_TOKEN = os.getenv("NOC_TOKEN", "")
AGENT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "noc_agent.py")
HIST = 320          # samples retained per host
WIN = 305           # seconds of history sent to the client (renders 300s)
STORE: dict[str, dict] = {}   # name -> {"data", "ts", "hist": deque}

# Rolling network-probe history, so the admin page can distinguish "fixed" from "quiet
# right now". A marginal link fails in bursts — a single clean 20-ping sample proves
# nothing, and the whole fault here was intermittent.
NETHIST: dict[str, deque] = {}      # name -> deque[(probe_ts, {target: loss%})]
NETHIST_MAX = 240                   # ~2h at the default 30s probe interval
NETWIN = 1800                       # seconds summarised for the UI (30 min)

# Push-only architecture: the collector can't reach the agents, so a queued power command is
# handed back one-shot on the next /report from the agent that must run it. It holds the
# operator-supplied root password transiently (in memory, never persisted/logged) and is
# dropped after CMD_TTL if the agent doesn't poll — so a stale password never fires late.
#   - an "os" command is queued to the target box itself (reboot/shutdown).
#   - a "plug" command is queued to a live RELAY (an agent run with --plug-dir) that is not the
#     target, to cut/cycle the target's Meross plug (a box can't power-cycle itself).
PENDING_CMDS: dict[str, list] = {}   # agent name -> [command dicts] (FIFO, one-shot)
CMD_RESULTS: dict[str, dict] = {}    # TARGET name -> {"action","ok","error","detail"?,"ts"}
CMD_AUDIT: dict[str, dict] = {}      # command id -> {by, ip, machine, tier, action} for outcome log
CMD_AUDIT_MAX = 500
CMD_TTL = 25                         # seconds a queued command waits for its agent to collect it
RESULT_TTL = 120                     # seconds a command result is shown on the dashboard
POWER_ACTIONS = {"reboot", "shutdown"}

# Every power request and every execution outcome is logged with IP, user and UTC time to the
# journal (stdout) and to an append-only file, so there is a durable record whether or not the
# action succeeded. Never contains a password.
AUDIT_LOG = os.getenv("NOC_AUDIT_LOG",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "power-audit.log"))


def _audit(event: str, **fields) -> None:
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
    print("AUDIT " + line, flush=True)            # journald
    try:
        with open(AUDIT_LOG, "a") as f:            # durable append-only trail
            f.write(line + "\n")
    except Exception:
        pass


def _client_ip(request) -> str:
    """Real client IP behind nginx (it sets X-Real-IP / X-Forwarded-For for this vhost)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


def _norm(s: str) -> str:
    """Fold a name for matching a box to its plug: 'GPU Node 3' and 'gpu-node-3' both -> 'gpunode3'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _plug_relays(now: float) -> dict[str, dict]:
    """Live relays -> {normalized_plug_name: plug_name}. Only agents reporting within the last
    20s and advertising plug_relay count, so a dead relay is never routed to."""
    out = {}
    for name, s in STORE.items():
        if "data" not in s or now - s["ts"] > 20:
            continue
        pr = (s["data"] or {}).get("plug_relay") or {}
        if pr.get("plugs"):
            out[name] = {_norm(p): p for p in pr["plugs"]}
    return out


def _hw_for(name: str, relays: dict) -> tuple[str | None, list]:
    """(plug name, [relay names that can switch it]) for a machine, or (None, [])."""
    key = _norm(name)
    plug, cands = None, []
    for relay, plugs in relays.items():
        if key in plugs:
            plug = plugs[key]
            cands.append(relay)
    return plug, cands

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# ---------------- collector (agents) ----------------
@app.post("/report")
async def report(request: Request):
    token = request.query_params.get("token")
    try:
        d = await request.json()
    except Exception:
        return PlainTextResponse("bad json", status_code=400)
    name = d.pop("name", None) or "unknown"
    # Accept the legacy shared token (any name), or a per-machine token bound to this name.
    if REPORT_TOKEN and token == REPORT_TOKEN:
        pass
    else:
        m = auth.machine_by_token(token)
        if not m or m["name"] != name:
            return PlainTextResponse("forbidden", status_code=403)
    s = STORE.setdefault(name, {"hist": deque(maxlen=HIST)})
    s["pub"] = _client_ip(request)   # public egress IP: which site the box is at (never shipped)
    now = time.time()
    # A power-command result rides in on the report; route it to the TARGET box (a plug result
    # names another box), not into the host payload, so the target's own reports don't wipe it.
    cres = d.pop("cmd_result", None)
    if isinstance(cres, dict):
        tgt = cres.get("target") or name
        CMD_RESULTS[tgt] = {**{k: cres[k] for k in ("action", "ok", "error", "detail")
                               if k in cres}, "ts": now}
        # Log the execution outcome, correlated back to who requested it (and from where).
        info = CMD_AUDIT.pop(cres.get("id"), {}) if cres.get("id") else {}
        _audit("power.outcome", id=cres.get("id"), machine=tgt,
               tier=info.get("tier"), action=cres.get("action"),
               status="SUCCESS" if cres.get("ok") else "FAILED",
               by=info.get("by"), ip=info.get("ip"),
               error=cres.get("error"), detail=cres.get("detail"))
    s["data"] = d
    s["ts"] = now
    gu = d.get("gpu_util")                       # GPU-less hosts track CPU instead
    hv = gu if gu is not None else d.get("cpu")
    s["hist"].append((now, round(hv) if hv is not None else 0))
    net = d.get("net") or {}
    if net.get("probes"):
        # The agent reports every 3s but only re-probes every --netprobe-interval, so the
        # same pass arrives ~10 times; key on the probe's own timestamp to record it once.
        pts = net.get("ts", now)
        h = NETHIST.setdefault(name, deque(maxlen=NETHIST_MAX))
        if not h or h[-1][0] != pts:
            h.append((pts, {p["name"]: p["loss"] for p in net["probes"]
                            if p.get("name") and p.get("loss") is not None}))
    auth.touch_machine(name)
    # Hand back any queued commands for this agent — one-shot (popped on delivery), dropping any
    # that waited past CMD_TTL. Only the keys the agent needs are sent (the password included).
    pend = PENDING_CMDS.pop(name, None)
    if pend:
        fresh = [c for c in pend if now - c["ts"] <= CMD_TTL]
        if fresh:
            keys = ("kind", "action", "op", "plug", "pw", "target", "id")
            return JSONResponse({"cmds": [{k: c[k] for k in keys if k in c} for c in fresh]})
    return PlainTextResponse("ok")


@app.get("/metrics")
def metrics(request: Request):
    email = auth.current_user(request)
    if not email:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    now = time.time()
    meta = auth.machines_meta()
    relays = _plug_relays(now)
    hosts = {}
    for name, s in STORE.items():
        if "data" not in s or not auth.can_see(name, meta, email):
            continue
        m = meta.get(name) or {}
        plug, _ = _hw_for(name, relays)
        host = {**s["data"], "age": round(now - s["ts"], 1),
                "owner": m.get("owner"), "shared": bool(m.get("shared")),
                "hw_power": bool(plug),   # a live relay advertises a plug for this box
                "hist": [[round(now - ts, 1), v] for ts, v in s["hist"]
                         if now - ts <= WIN]}
        cr = CMD_RESULTS.get(name)
        if cr and now - cr["ts"] <= RESULT_TTL:
            host["cmd_result"] = {k: cr[k] for k in ("action", "ok", "error", "detail") if k in cr}
        hosts[name] = host
    # `me`/`admin` let the dashboard show the power controls only where they'd actually work
    # (admin, or the machine's owner) — the server still re-checks on every /api/power.
    return JSONResponse({"t": time.strftime("%H:%M:%S"), "me": email,
                         "admin": auth.is_admin(email), "hosts": hosts})


@app.post("/api/power")
async def power(request: Request):
    """Queue a power command for a machine. Allowed for an admin or the machine's owner only —
    a destructive control, so being able to *see* a shared machine is deliberately not enough.
    A password is always required and is passed to the agent on its next /report, never stored
    here beyond CMD_TTL.

      tier "os"       — reboot/shutdown the box itself; `password` is that box's root/sudo pw.
      tier "hardware" — cut (shutdown) or power-cycle (restart) the box's Meross plug; runs
                        root-only on a live relay, so `password` is the RELAY's root pw, and
                        `confirm` must equal the machine name (guards a mains-power action)."""
    ip = _client_ip(request)
    email = auth.current_user(request)
    if not email:
        _audit("power.request", ip=ip, user="?", result="REJECTED", reason="unauthorized")
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        _audit("power.request", ip=ip, user=email, result="REJECTED", reason="bad_json")
        return JSONResponse({"error": "bad json"}, status_code=400)
    name = (body.get("name") or "").strip()
    action = (body.get("action") or "").strip()
    tier = (body.get("tier") or "os").strip()
    pw = body.get("password") or ""

    def reject(reason, code):
        _audit("power.request", ip=ip, user=email, machine=name, tier=tier, action=action,
               result="REJECTED", reason=reason)
        return JSONResponse({"error": reason}, status_code=code)

    if action not in POWER_ACTIONS:
        return reject("bad action", 400)
    if tier not in ("os", "hardware"):
        return reject("bad tier", 400)
    if not pw:
        return reject("password required", 400)
    meta = auth.machines_meta()
    m = meta.get(name)
    if not auth.can_see(name, meta, email):
        return reject("not found", 404)          # don't disclose it exists
    if not (auth.is_admin(email) or (m and m.get("owner") == email.lower())):
        return reject("forbidden", 403)
    now = time.time()
    cid = secrets.token_hex(8)

    if tier == "os":
        if name not in STORE:
            return reject("offline", 409)        # no agent to pick it up
        PENDING_CMDS.setdefault(name, []).append(
            {"kind": "os", "action": action, "pw": pw, "id": cid, "ts": now, "by": email})
        CMD_AUDIT[cid] = {"by": email, "ip": ip, "machine": name, "tier": tier, "action": action}
        _audit("power.request", ip=ip, user=email, machine=name, tier="os", action=action,
               result="QUEUED", id=cid)
        return JSONResponse({"ok": True})

    # hardware (plug) — typed confirmation, then route to a live relay that isn't the target
    if _norm(body.get("confirm") or "") != _norm(name):
        return reject("bad_confirm", 400)
    relays = _plug_relays(now)
    plug, cands = _hw_for(name, relays)
    if not plug:
        return reject("no_plug", 409)
    op = "off" if action == "shutdown" else "cycle"
    others = [r for r in cands if r != name]
    # A box can't power-cycle itself; for a hard shutdown it may cut its own plug if it's the
    # only relay, but a restart must be driven from another host.
    relay = others[0] if others else (cands[0] if op == "off" else None)
    if not relay:
        return reject("no_relay", 409)
    PENDING_CMDS.setdefault(relay, []).append(
        {"kind": "plug", "op": op, "plug": plug, "pw": pw, "target": name,
         "id": cid, "ts": now, "by": email})
    CMD_AUDIT[cid] = {"by": email, "ip": ip, "machine": name, "tier": tier, "action": action}
    if len(CMD_AUDIT) > CMD_AUDIT_MAX:            # bound the outcome-correlation map
        for k in list(CMD_AUDIT)[:len(CMD_AUDIT) - CMD_AUDIT_MAX]:
            CMD_AUDIT.pop(k, None)
    _audit("power.request", ip=ip, user=email, machine=name, tier="hardware", action=action,
           plug=plug, relay=relay, result="QUEUED", id=cid)
    return JSONResponse({"ok": True, "relay": relay})


# ---------------- dashboard (gated) ----------------
_ADMIN_LINK = '<a class=addbtn href="/admin">◈ network</a>'


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    email = auth.current_user(request)
    if not email:
        return RedirectResponse("/login", status_code=302)
    page = PAGE
    if auth.is_admin(email):
        # Injected per-request rather than baked into PAGE, so non-admins are never
        # served the link at all (the route 404s for them regardless).
        page = page.replace('<button id=theme>', _ADMIN_LINK + '<button id=theme>', 1)
    # PAGE carries all the dashboard JS/CSS inline, so a cached copy silently serves
    # stale UI after a redeploy (metrics still update live via /metrics). Never cache it.
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


# ---------------- auth ----------------
@app.get("/login", response_class=HTMLResponse)
def login_page():
    return HTMLResponse(LOGIN_PAGE)


@app.post("/api/auth/request")
def auth_request(email: str = Form(...)):
    # Always return ok (no account/domain enumeration). Only send for allowed domains.
    email = (email or "").strip().lower()
    if auth.email_domain_allowed(email) and not auth.recently_sent(email):
        try:
            url, code = auth.issue_login(email)
            auth.send_login_email(email, url, code)
        except Exception:
            pass
    return JSONResponse({"ok": True})


@app.post("/api/auth/verify")
def auth_verify(email: str = Form(...), code: str = Form(...)):
    try:
        sess = auth.redeem_code(email, code)
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    resp = JSONResponse({"ok": True})
    auth.set_session_cookie(resp, sess)
    return resp


@app.get("/magic")
def magic(token: str = ""):
    try:
        sess = auth.redeem_magic_link(token)
    except ValueError:
        return RedirectResponse("/login?e=1", status_code=302)
    resp = RedirectResponse("/", status_code=302)
    auth.set_session_cookie(resp, sess)
    return resp


@app.get("/logout")
def logout(request: Request):
    tok = request.cookies.get(auth.SESSION_COOKIE)
    if tok:
        auth.logout(tok)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


# ---------------- self-service machine enrolment ----------------
@app.get("/agent")
def agent_file():
    return FileResponse(AGENT_PATH, media_type="text/x-python", filename="noc_agent.py")


@app.get("/install.sh")
def install_sh():
    return PlainTextResponse(INSTALL_SH.replace("{{SERVER}}", auth.APP_BASE_URL.rstrip("/")),
                             media_type="text/x-shellscript")


@app.get("/add", response_class=HTMLResponse)
def add_page(request: Request):
    email = auth.current_user(request)
    if not email:
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse(ADD_PAGE.replace("{{EMAIL}}", email).replace("{{ERR}}", ""))


@app.post("/add", response_class=HTMLResponse)
def add_machine(request: Request, name: str = Form(...), os_type: str = Form(...),
                shared: str = Form("")):
    email = auth.current_user(request)
    if not email:
        return RedirectResponse("/login", status_code=302)
    token, err = auth.register_machine(email, name, os_type, shared=bool(shared))
    if err:
        msgs = {"bad_name": "Use letters, numbers, dot, dash or underscore (max 40 chars).",
                "bad_os": "Pick an operating system.",
                "name_taken": "That name is already registered — pick another."}
        return HTMLResponse(ADD_PAGE.replace("{{EMAIL}}", email)
                            .replace("{{ERR}}", msgs.get(err, err)))
    return HTMLResponse(_render_instructions(name.strip(), os_type, token))


@app.get("/machines", response_class=HTMLResponse)
def machines_page(request: Request):
    email = auth.current_user(request)
    if not email:
        return RedirectResponse("/login", status_code=302)
    admin = auth.is_admin(email)
    live = {n for n, s in STORE.items() if "data" in s}
    rows = ""
    for m in auth.list_machines():
        # a user only manages the full list if admin; otherwise just their own + shared
        if not (admin or m["shared"] or m["owner_email"] == email):
            continue
        online = m["name"] in live
        can_toggle = admin or m["owner_email"] == email
        if can_toggle:
            nxt = 0 if m["shared"] else 1
            label = "unshare" if m["shared"] else "share"
            ctl = ('<form method=post action="/machines/share" style="margin:0">'
                   '<input type=hidden name=name value="%s"><input type=hidden name=shared value="%d">'
                   '<button class=tinybtn>%s</button></form>' % (m["name"], nxt, label))
        else:
            ctl = "—"
        rows += ("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class=%s>%s</td><td>%s</td></tr>" % (
            m["name"], m["owner_email"], m["os_type"] or "—",
            "shared" if m["shared"] else "private",
            "on" if online else "off", "live" if online else "offline", ctl))
    return HTMLResponse(MACHINES_PAGE.replace("{{ROWS}}",
                        rows or "<tr><td colspan=6 style='color:var(--faint)'>no machines yet</td></tr>"))


@app.post("/machines/share")
def machines_share(request: Request, name: str = Form(...), shared: str = Form("")):
    email = auth.current_user(request)
    if not email:
        return RedirectResponse("/login", status_code=302)
    auth.set_shared(name, shared == "1", email)
    return RedirectResponse("/machines", status_code=302)


# ---------------- admin: home network / AiMesh ----------------
# Admin-only, and *invisible* to everyone else: non-admins get a plain 404 rather than a
# 403, so the page's existence isn't disclosed, and the dashboard link is only injected
# into PAGE for admins.

def _admin_payload() -> dict:
    """Machines + their IPs, plus whatever LAN/mesh probe data the agents have shipped.

    The mesh and the LAN hosts are only reachable from inside the LAN, so this is
    assembled from agent reports — the collector itself (in the cloud) cannot see them."""
    now = time.time()
    machines, probes, mesh, mesh_from = [], [], None, None
    for name, s in sorted(STORE.items()):
        if "data" not in s:
            continue
        d = s["data"]
        machines.append({"name": name, "ip": d.get("ip"), "os": d.get("os"),
                         "ts_ip": (d.get("sys") or {}).get("ts_ip"),
                         "ifaces": (d.get("sys") or {}).get("ifaces") or [],
                         "board": d.get("board"), "gpu": d.get("gpu"),
                         "wifi": d.get("wifi"), "age": round(now - s["ts"], 1)})
        net = d.get("net") or {}
        if net.get("probes"):
            # Summarise the retained window so the UI can say "clean now, but lost 30%
            # inside the last half hour" — the only way to judge an intermittent link.
            seen: dict[str, list] = {}
            for pts, losses in NETHIST.get(name, ()):
                if now - pts <= NETWIN:
                    for k, v in losses.items():
                        seen.setdefault(k, []).append(v)
            window = {k: {"worst": max(v), "mean": round(sum(v) / len(v), 1), "n": len(v),
                          "bad": sum(1 for x in v if x >= 10)}
                      for k, v in seen.items()}
            probes.append({"from": name, "count": net.get("count"),
                           "age": round(now - net.get("ts", now), 1),
                           "targets": net["probes"], "window": window,
                           "window_min": round(NETWIN / 60)})
        # Every agent on the LAN sees the same AiMesh; first one wins.
        if net.get("mesh") and mesh is None:
            mesh, mesh_from = net["mesh"], name
    # "At home" = same public egress IP as the mesh prober. a private subnet alone proves nothing:
    # a remote site can use the same 192.168.1.x range.
    home_pub = STORE.get(mesh_from, {}).get("pub") if mesh_from else None
    for m in machines:
        m["home"] = (STORE[m["name"]].get("pub") == home_pub) if home_pub else None
        if m["home"] is False:   # boxes sharing an egress IP are at the same other site
            m["site"] = STORE[m["name"]].get("pub")
    return {"t": time.strftime("%H:%M:%S"), "machines": machines, "probes": probes,
            "mesh": mesh, "mesh_from": mesh_from, "topo": _topology(machines, probes, mesh)}


# The home LAN (behind the mesh router) and the upstream LAN between the router and the ISP
# gateway (double NAT). Neither counts as a "direct link" between machines.
_MESH_LAN = ipaddress.ip_network(os.getenv("NOC_MESH_LAN", "192.168.50.0/24"))
_WAN_LAN = ipaddress.ip_network(os.getenv("NOC_WAN_LAN", "192.168.1.0/24"))
_NOT_DIRECT = [_MESH_LAN, _WAN_LAN, ipaddress.ip_network("100.64.0.0/10"),
               ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("169.254.0.0/16")]
_BANDS = {0: "wired", 1: "2.4G", 2: "5G", 3: "5G", 4: "6G", 5: "6G"}


def _norm_mac(s):
    return ":".join(p.rjust(2, "0").lower() for p in (s or "").split(":")) if s else ""


def _topology(machines: list, probes: list, mesh: dict | None) -> dict:
    """Where each machine sits, computed once so the web page and the iPhone app draw the same
    thing. Placement, best evidence first: the router's own client table (by any of the
    machine's IPs), then the machine's reported BSSID, then latency (only possible for a host
    that probes). Direct links are private subnets shared by two machines that are neither
    LAN — the Spark QSFP ring, the minis' Thunderbolt bridge."""
    nodes = (mesh or {}).get("nodes") or []
    by_mac = {_norm_mac(n.get("mac")): n for n in nodes}
    root = next((n for n in nodes if n.get("level") == 0), nodes[0] if nodes else None)
    fh = {_norm_mac(b): n for n in nodes for b in n.get("fh") or []}
    clients = (mesh or {}).get("clients") or {}
    probe_by = {p["from"]: p["targets"] for p in probes}
    placed, wan, unplaced, remote, used = {}, [], [], [], set()
    nets: dict = {}
    for m in machines:
        ifs = m.get("ifaces") or []
        ips = [i["ip"] for i in ifs] or ([m["ip"]] if m.get("ip") else [])
        if m.get("ip") and m["ip"] not in ips:
            ips.append(m["ip"])
        where = None
        for ip in ips:
            c = clients.get(ip)
            if not c:
                continue
            node = by_mac.get(c[0] or "") or (root if c[1] == 0 else None)
            if node:
                used.add(ip)
                where = {"node": node.get("ip"), "via": "router", "ip": ip,
                         "band": _BANDS.get(c[1], "wifi"), "rssi": c[2] or None}
                break
        if not where and (m.get("wifi") or {}).get("bssid") in fh:
            node = fh[m["wifi"]["bssid"]]
            where = {"node": node.get("ip"), "via": "bssid", "band": "wifi",
                     "rssi": m["wifi"].get("signal")}
        if not where:
            near = [t for t in probe_by.get(m["name"], []) if t.get("loss") == 0
                    and t.get("avg") is not None and t["avg"] < 2.5
                    and any(n.get("ip") == t.get("host") for n in nodes)]
            if len(near) == 1:
                where = {"node": near[0]["host"], "via": "rtt"}
        if where:
            placed[m["name"]] = where
        on_wan = [] if m.get("home") is False else [i for i in ifs if ipaddress.ip_address(i["ip"]) in _WAN_LAN] or \
            ([{"ip": m["ip"], "kind": None}] if not ifs and m.get("ip")
             and ipaddress.ip_address(m["ip"]) in _WAN_LAN else [])
        if on_wan:
            wan.append({"name": m["name"], "ip": on_wan[0]["ip"], "kind": on_wan[0].get("kind"),
                        "speed": on_wan[0].get("speed")})
        if not where and not on_wan:
            (remote if m.get("home") is False else unplaced).append(m["name"])
        for i in ifs:
            try:
                net = ipaddress.ip_network(f'{i["ip"]}/{i.get("prefix") or 24}', strict=False)
            except ValueError:
                continue
            if any(net.subnet_of(x) for x in _NOT_DIRECT if x.version == net.version):
                continue
            nets.setdefault(str(net), []).append((m["name"], i))
    links = []
    for net, ends in sorted(nets.items()):
        names = sorted({n for n, _ in ends})
        if len(names) < 2:
            continue
        kinds = {i.get("kind") for _, i in ends}
        speeds = [i["speed"] for _, i in ends if i.get("speed")]
        link = {"net": net, "members": names,
                "kind": next((k for k in ("qsfp", "thunderbolt") if k in kinds), "ethernet"),
                "speed": min(speeds) if speeds else None}
        # Thunderbolt: what was negotiated vs what each end's ports can do. A link below
        # both ends' rating is the cable (a USB4-20G cable negotiates 20 on TB4/TB5 ports).
        tbs = {n: i.get("tb") or {} for n, i in ends if i.get("tb")}
        if tbs:
            gen = lambda g: {120: "TB5", 80: "TB5", 40: "TB4/USB4", 20: "USB4 20G"}.get(g, f"{g} Gb/s")
            caps = [t["max"] for t in tbs.values() if t.get("max")]
            got = min((t["link"] for t in tbs.values() if t.get("link")), default=None)
            parts = [f"{n} ports up to {t['max']} Gb/s ({gen(t['max'])})" for n, t in sorted(tbs.items()) if t.get("max")]
            note = (f"negotiated {got} Gb/s" if got else "link speed unknown") + "; " + ", ".join(parts)
            if got and caps and got < min(caps):
                note += f" — running below both ends ({min(caps)} Gb/s possible), so the cable is the limit"
            link["note"] = note
        links.append(link)
    others: dict = {}
    for ip, c in clients.items():
        if ip in used:
            continue
        node = by_mac.get(c[0] or "") or (root if c[1] == 0 else None)
        if node:
            others[node.get("ip")] = others.get(node.get("ip"), 0) + 1
    sites: dict = {}
    for m in machines:
        if m["name"] in remote:
            sites.setdefault(m.pop("site", None), []).append(m["name"])
        m.pop("site", None)
    return {"placed": placed, "wan": wan, "wan_lan": str(_WAN_LAN), "unplaced": unplaced,
            "remote": sorted(sites.values(), key=lambda g: (-len(g), g)),
            "links": links, "others": others}


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    if not auth.is_admin(auth.current_user(request)):
        return PlainTextResponse("Not Found", status_code=404)
    return HTMLResponse(ADMIN_PAGE, headers={"Cache-Control": "no-store"})


@app.get("/api/admin/net")
def admin_net(request: Request):
    if not auth.is_admin(auth.current_user(request)):
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_admin_payload())


def _render_instructions(name: str, os_type: str, token: str) -> str:
    server = auth.APP_BASE_URL.rstrip("/")
    sudo = "sudo " if os_type == "linux" else ""
    cmd = (f"curl -fsSL {server}/install.sh -o /tmp/noc-install.sh \\\n"
           f"  && {sudo}NOC_NAME='{name}' NOC_TOKEN='{token}' bash /tmp/noc-install.sh")
    if os_type == "linux":
        note = ("Linux — installs a <b>systemd</b> service that starts on boot. Auto-detects an "
                "NVIDIA GPU (DGX/Spark) or Raspberry Pi. Needs <b>sudo</b> and <b>python3</b>.")
    else:
        note = ("macOS — installs a <b>LaunchAgent</b> that starts on login. No sudo needed; uses "
                "<b>/usr/bin/python3</b> (Command Line Tools).")
    return (INSTRUCT_PAGE.replace("{{NAME}}", name).replace("{{NOTE}}", note)
            .replace("{{CMD}}", cmd))


LOGIN_PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<title>Incept5 NOC — sign in</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#080b10;--card:#0d131c;--line:#1b2634;--fg:#c9d4e0;--dim:#8a97a6;--faint:#5a6b7d;--good:#3fb950}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
 min-height:100vh;display:flex;flex-direction:column;gap:14px;align-items:center;justify-content:center;padding:20px}
.box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:26px 24px;width:100%;max-width:360px}
h1{font-size:17px;letter-spacing:2px;margin-bottom:5px}
p{color:var(--dim);font-size:12.5px;margin-bottom:18px}
input{width:100%;background:#0a1017;border:1px solid var(--line);border-radius:8px;color:var(--fg);
 font:inherit;padding:11px 12px;margin-bottom:11px}
input:focus{outline:none;border-color:var(--faint)}
button{width:100%;background:var(--good);color:#04140a;border:none;border-radius:8px;font:inherit;
 font-weight:700;padding:11px;cursor:pointer}button:disabled{opacity:.6;cursor:default}
.msg{margin-top:14px;font-size:12.5px;color:var(--good);min-height:18px}
.err{color:#f0603e}
</style></head><body>
<form class=box id=f>
<h1>◢ INCEPT5 NOC</h1>
<p>Enter your email and we'll send you a magic link and a 6-digit code.</p>
<input id=email type=email name=email placeholder="email address" autocomplete=email required autofocus>
<button id=btn type=submit>Send code &amp; link</button>
<div class=msg id=msg></div>
</form>
<form class=box id=cf style="display:none">
<h1>◢ INCEPT5 NOC</h1>
<p>Enter the 6-digit code from your email, or just click the link.</p>
<input id=code name=code inputmode=numeric autocomplete=one-time-code pattern="[0-9 -]*" maxlength=24
 placeholder="000000" style="letter-spacing:.3em;text-align:center;font-size:20px">
<button id=cbtn type=submit>Sign in</button>
<div class=msg id=cmsg></div>
</form>
<script>
const p=new URLSearchParams(location.search);
if(p.get('e'))msg.innerHTML='<span class=err>That link was invalid or expired — request a new one.</span>';
f.onsubmit=async e=>{e.preventDefault();btn.disabled=true;
 const body=new URLSearchParams({email:email.value});
 try{await fetch('/api/auth/request',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});
  cf.style.display='';code.focus();
  msg.className='msg';msg.textContent='Check your inbox — if that address is allowed, a code and link are on the way.';}
 catch(_){msg.className='msg err';msg.textContent='Something went wrong, try again.';btn.disabled=false;}};
// the email shows the code spaced ("1 2 3 4 5 6") — accept any spacing/punctuation on paste
code.addEventListener('input',()=>{const v=code.value.replace(/[^0-9]/g,'').slice(0,6);
 if(v!==code.value)code.value=v;});
cf.onsubmit=async e=>{e.preventDefault();cbtn.disabled=true;cmsg.className='msg';cmsg.textContent='';
 const body=new URLSearchParams({email:email.value,code:code.value.replace(/[^0-9]/g,'').slice(0,6)});
 try{const r=await fetch('/api/auth/verify',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});
  if(r.ok){location.href='/';return;}
  cmsg.className='msg err';cmsg.textContent='Invalid or expired code — check it, or use the link instead.';cbtn.disabled=false;}
 catch(_){cmsg.className='msg err';cmsg.textContent='Something went wrong, try again.';cbtn.disabled=false;}};
</script></body></html>"""


# ---------------- shared HTML + installer ----------------
_CSS = """
:root{--bg:#080b10;--card:#0d131c;--line:#1b2634;--tile:#0a1017;--fg:#c9d4e0;--dim:#8a97a6;
 --faint:#5a6b7d;--good:#3fb950;--bad:#f0603e}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
 min-height:100vh;padding:22px 16px;display:flex;justify-content:center}
.box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;width:100%;
 max-width:520px;height:fit-content}
.box.wide{max-width:760px}
h1{font-size:18px;letter-spacing:1px;margin-bottom:8px}
p{color:var(--dim);font-size:13px;margin-bottom:14px}.small{font-size:12px}
a{color:var(--good)}
label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--faint);margin:14px 0 5px}
input,select{width:100%;background:var(--tile);border:1px solid var(--line);border-radius:8px;color:var(--fg);
 font:inherit;padding:10px 11px}
input:focus,select:focus{outline:none;border-color:var(--faint)}
button{margin-top:16px;background:var(--good);color:#04140a;border:none;border-radius:8px;font:inherit;
 font-weight:700;padding:10px 16px;cursor:pointer}
.err{color:var(--bad);font-size:12.5px;margin-top:12px;min-height:16px}
.back{color:var(--faint);text-decoration:none;font-size:12px;display:inline-block;margin-bottom:14px}
.back:hover{color:var(--fg)}
.mlink{display:inline-block;margin-top:16px;font-size:12.5px;text-decoration:none}
pre{background:#05090e;border:1px solid var(--line);border-radius:8px;padding:14px;overflow-x:auto;
 font-size:12.5px;color:#d7e6d7;white-space:pre-wrap;word-break:break-all;margin-top:6px}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:6px}
th{text-align:left;color:var(--faint);font-weight:500;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:6px 8px;border-bottom:1px solid var(--tile);word-break:break-word}
td.on{color:var(--good)}td.off{color:var(--faint)}
.check{display:flex;align-items:center;gap:8px;text-transform:none;letter-spacing:0;font-size:12.5px;
 color:var(--dim);margin-top:16px;cursor:pointer}
.check input{width:auto;accent-color:var(--good)}
.tinybtn{margin:0;padding:3px 9px;font-size:11px;background:var(--tile);color:var(--fg);
 border:1px solid var(--line);border-radius:6px;cursor:pointer}
.tinybtn:hover{border-color:var(--good);color:var(--good)}
"""


def _page(title, body, css=""):
    return ("<!doctype html><html lang=en><head><meta charset=utf-8><title>" + title +
            "</title><meta name=viewport content=\"width=device-width,initial-scale=1\"><style>" +
            _CSS + css + "</style></head><body>" + body + "</body></html>")


ADD_PAGE = _page("Add a machine — Incept5 NOC", """
<div class=box>
  <a class=back href="/">← dashboard</a>
  <h1>Add your machine</h1>
  <p>Signed in as {{EMAIL}}. Name your machine and pick its OS; you'll get a one-line install command.</p>
  <form method=post action="/add">
    <label>Machine name</label>
    <input name=name placeholder="e.g. gpu-node-1" autocomplete=off autofocus
           pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,39}" required>
    <label>Operating system</label>
    <select name=os_type>
      <option value=linux>Linux — DGX Spark / NVIDIA, Raspberry Pi, or generic</option>
      <option value=macos>macOS — Apple Silicon</option>
    </select>
    <label class=check><input type=checkbox name=shared value=1>
      Shared — visible to everyone (otherwise only you see it)</label>
    <button type=submit>Create &amp; get install command</button>
    <div class=err>{{ERR}}</div>
  </form>
  <a class=mlink href="/machines">view registered machines →</a>
</div>""")


INSTRUCT_PAGE = _page("Install — Incept5 NOC", """
<div class=box>
  <a class=back href="/add">← add another</a>
  <h1>Install on &ldquo;{{NAME}}&rdquo;</h1>
  <p class=note>{{NOTE}}</p>
  <p>Run this <b>on that machine</b> (copy &amp; paste into a terminal):</p>
  <pre id=cmd>{{CMD}}</pre>
  <button onclick="navigator.clipboard.writeText(document.getElementById('cmd').innerText)">Copy command</button>
  <p class=small style="margin-top:16px">Keep the token private — it's how this machine authenticates.
     The agent reports CPU / GPU / RAM every few seconds and restarts on boot; the machine appears on the
     <a href="/">dashboard</a> within seconds of install.</p>
</div>""")


MACHINES_PAGE = _page("Machines — Incept5 NOC", """
<div class="box wide">
  <a class=back href="/">← dashboard</a>
  <h1>Registered machines</h1>
  <p>Who owns what. <b>Shared</b> machines are visible to everyone; <b>private</b> ones only to
     their owner (and admins). Use share / unshare to change it.</p>
  <table><thead><tr><th>name</th><th>owner</th><th>os</th><th>sharing</th><th>status</th><th></th></tr></thead>
  <tbody>{{ROWS}}</tbody></table>
  <a class=mlink href="/add">+ add your machine</a>
</div>""")


_ADMIN_CSS = """
.box.admin{max-width:1000px}
h2{font-size:13px;letter-spacing:1px;text-transform:uppercase;color:var(--faint);
 font-weight:500;margin:26px 0 8px;border-bottom:1px solid var(--line);padding-bottom:6px}
h2:first-of-type{margin-top:18px}
.pill{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11px;font-weight:700}
.pill.g{background:rgba(63,185,80,.16);color:#5fd47a}
.pill.w{background:rgba(219,161,26,.16);color:#e2b341}
.pill.b{background:rgba(240,96,62,.16);color:#ff7d5c}
.pill.n{background:var(--tile);color:var(--faint)}
.bar{height:5px;border-radius:3px;background:var(--tile);overflow:hidden;min-width:60px}
.bar>i{display:block;height:100%}
.bar>i.g{background:#3fb950}.bar>i.w{background:#dba11a}.bar>i.b{background:#f0603e}
.diag{border:1px solid;border-radius:9px;padding:13px 15px;margin-top:16px;font-size:12.5px}
.diag.b{border-color:rgba(240,96,62,.45);background:rgba(240,96,62,.07)}
.diag.w{border-color:rgba(219,161,26,.45);background:rgba(219,161,26,.06);margin-top:10px}
.diag.g{border-color:rgba(63,185,80,.35);background:rgba(63,185,80,.06)}
.diag b{color:var(--fg)}
.diag ul{margin:7px 0 0 17px;color:var(--dim)}.diag li{margin:3px 0}
.clock{float:right;font-size:11px;color:var(--faint);font-weight:400;letter-spacing:0}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.mono{color:var(--dim)}
tr.self td{color:var(--faint)}
.sub{font-size:11px;color:var(--faint)}
"""

ADMIN_PAGE = _page("Home network — Incept5 NOC", r"""
<div class="box admin">
  <a class=back href="/">← dashboard</a>
  <h1>Home network <span class=clock id=clock>connecting…</span></h1>
  <p>Live view of the home LAN — the AiMesh backhaul, path quality between hosts,
     and every machine's address. Probe data is gathered by NOC agents inside the LAN;
     router figures are read-only queries to the ASUS API.</p>
  <div id=diag></div>
  <h2>Topology</h2><div id=topo></div>
  <h2>AiMesh backhaul</h2><div id=mesh></div>
  <h2>Path quality</h2><div id=probes></div>
  <h2>Machines</h2><div id=machines></div>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
const pill=(c,t)=>'<span class="pill '+c+'">'+esc(t)+'</span>';
// Thresholds: RSSI is dBm (less negative is better); a 160MHz link needs far more
// headroom than a 20MHz one, so -70 is already marginal rather than "fine".
const rssiCls=v=>v==null?'n':v>=-60?'g':v>=-70?'w':'b';
const lossCls=v=>v==null?'n':v<1?'g':v<10?'w':'b';
const msCls  =v=>v==null?'n':v<10?'g':v<80?'w':'b';
// Broadcom router SoCs idle around 60-70C; 80C+ is where they start throttling radios.
const tempCls=v=>v==null?'n':v<70?'g':v<80?'w':'b';
const PORT={G:'1 Gb/s',Q:'2.5 Gb/s',M:'100 Mb/s',T:'10 Gb/s',X:'not connected'};
const bar=(pct,cls)=>'<div class=bar><i class="'+cls+'" style="width:'+Math.max(2,Math.min(100,pct))+'%"></i></div>';

// --- topology diagram -------------------------------------------------------
// SVG rather than boxes-and-CSS because the point is the *edges*: which node hangs off
// which, and how good each hop is. Colours are literal hex — a presentation attribute
// like stroke="var(--x)" does not resolve — while text uses currentColor so it follows
// the page.
const C={g:'#3fb950',w:'#dba11a',b:'#f0603e',n:'#5a6b7d'};

function placeHosts(d){
  const nodes=(d.mesh||{}).nodes||[];
  // The collector's placement (router client table first) — shared with the iPhone app.
  if(d.topo){
    const byNode={};
    for(const [name,p] of Object.entries(d.topo.placed||{}))
      (byNode[p.node]=byNode[p.node]||[]).push({name:name,how:p.via,band:p.band,rssi:p.rssi});
    for(const k of Object.values(byNode)) k.sort((a,b)=>a.name.localeCompare(b.name));
    return {byNode:byNode,unplaced:(d.topo.unplaced||[]).slice()};
  }
  const fh={};                       // client-facing BSSID -> node ip
  for(const n of nodes) for(const b of (n.fh||[])) fh[b]=n.ip;
  const probeBy={};
  for(const p of d.probes) probeBy[p.from]=p.targets;
  const byNode={}, unplaced=[];
  for(const m of d.machines){
    let ip=m.wifi&&fh[m.wifi.bssid]?fh[m.wifi.bssid]:null, how='wifi';
    if(!ip){
      // macOS redacts the BSSID, so fall back to latency: a host's own node answers in
      // ~1ms with zero loss while every other node sits behind a backhaul hop.
      const local=(probeBy[m.name]||[]).filter(t=>t.loss===0&&t.avg!=null&&t.avg<2.5
                                                 &&nodes.some(n=>n.ip===t.host));
      if(local.length===1){ip=local[0].host;how='rtt';}
    }
    if(ip){(byNode[ip]=byNode[ip]||[]).push({name:m.name,how:how});}
    else unplaced.push(m.name);
  }
  return {byNode:byNode,unplaced:unplaced};
}

// Measured behaviour of the branch the prober sits on: worst loss now, and worst inside
// the retained window, across everything beyond its own node.
function branchHealth(d){
  const nodes=(d.mesh||{}).nodes||[];
  const {byNode}=placeHosts(d);
  for(const p of d.probes){
    const home=nodes.find(n=>(byNode[n.ip]||[]).some(k=>k.name===p.from));
    if(!home) continue;
    let now=0,win=0;
    for(const t of p.targets){
      if(t.host===home.ip) continue;          // local segment — not across the backhaul
      now=Math.max(now,t.loss);
      const w=(p.window||{})[t.name];
      if(w) win=Math.max(win,w.worst);
    }
    return {home:home.ip,now:now,win:win};
  }
  return null;
}

// One rule for every place an uplink is coloured, so the diagram, the table and the
// banner cannot disagree. Signal alone never justifies red: a -71 dBm link carrying
// traffic at 0% loss is a thin margin to watch, not a failure. An unmeasured branch is
// capped at amber because nothing observed it failing.
function uplinkTier(d,n,bh){
  if(n.level===0||n.link!=='wireless') return 'g';
  const best=Math.max(n.rssi5==null?-999:n.rssi5,n.rssi6==null?-999:n.rssi6);
  const weak=best!==-999&&best<-70;
  if(bh&&bh.home===n.ip){
    if(bh.now>=10) return 'b';
    if(bh.win>=10||weak) return 'w';
    return 'g';
  }
  return weak?'w':'g';
}
const TIER={g:'good',w:'thin margin',b:'weak'};

function renderTopo(d){
  const m=d.mesh;
  if(!m||!m.nodes.length) return '<p class=small>No router data yet.</p>';
  const root=m.nodes.find(n=>n.level===0)||m.nodes[0];
  const sats=m.nodes.filter(n=>n!==root);
  const {byNode,unplaced}=placeHosts(d);
  const W=920, colW=sats.length?W/sats.length:W, midX=W/2;
  const yNet=28, yWan=112, yRoot=200, ySat=312, BH=58;
  const maxKids=Math.max(0,...sats.map(n=>(byNode[n.ip]||[]).length));
  const H=ySat+BH+18+maxKids*26+(unplaced.length?40:20);

  // A probe measures the WHOLE path from the probing host, so its loss cannot be pinned
  // on the final hop — a 30% loss to the WAN gateway may be incurred entirely on the
  // satellite backhaul it crosses first. Differencing consecutive targets was tried and abandoned:
  // loss on this link swings 0-40% within seconds, so subtracting two samples taken
  // seconds apart is mostly noise and can even invert. Instead each hop is annotated with
  // the CUMULATIVE cost of reaching it from the prober, clearly labelled as such — the
  // step between two hops is then visible without claiming a precision we do not have.
  let seg={}, wanT=null;
  for(const p of d.probes){
    const home=m.nodes.find(n=>(byNode[n.ip]||[]).some(k=>k.name===p.from));
    if(!home) continue;
    const at=ip=>p.targets.find(t=>t.host===ip)||null;
    const named=re=>p.targets.find(t=>re.test(t.name))||null;
    wanT=named(/gateway|adapter|modem|wan/i);
    seg={from:p.from, home:home.ip, own:at(home.ip), root:at(root.ip), wan:wanT,
         inet:named(/internet|isp|dns/i)};
    break;
  }
  const cum=t=>!t?'':(t.loss<1?'0':t.loss.toFixed(0))+'% loss'+
                     (t.avg!=null?' · '+t.avg.toFixed(0)+' ms':'');
  const cumCol=t=>!t?C.n:C[lossCls(t.loss)];

  let s='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto;font:12px ui-monospace,Menlo,monospace" role=img>';

  const box=(x,y,w,h,label,sub,reach,reachCol)=>
    '<g><rect x="'+(x-w/2)+'" y="'+y+'" width="'+w+'" height="'+h+'" rx="7" '+
    'style="fill:var(--tile);stroke:var(--line)"/>'+
    '<text x="'+x+'" y="'+(y+21)+'" text-anchor="middle" fill="currentColor" '+
    'font-weight="700">'+esc(label)+'</text>'+
    (sub?'<text x="'+x+'" y="'+(y+37)+'" text-anchor="middle" fill="'+C.n+'" '+
         'font-size="11">'+esc(sub)+'</text>':'')+
    (reach?'<text x="'+x+'" y="'+(y+52)+'" text-anchor="middle" fill="'+reachCol+'" '+
         'font-size="11" font-weight="700">'+esc(reach)+'</text>':'')+'</g>';

  // The label needs an opaque plate, not a text halo — a dashed stroke shows straight
  // through paint-order and makes the RSSI unreadable.
  const edge=(x1,y1,x2,y2,col,label,dash)=>{
    const mx=(x1+x2)/2, my=(y1+y2)/2;
    let e='<path d="M'+x1+' '+y1+' C'+x1+' '+my+' '+x2+' '+my+' '+x2+' '+y2+'" '+
          'fill="none" stroke="'+col+'" stroke-width="2.5"'+
          (dash?' stroke-dasharray="5 4"':'')+'/>';
    if(label){
      const w=label.length*6.7+12;
      e+='<rect x="'+(mx-w/2)+'" y="'+(my-9)+'" width="'+w+'" height="19" rx="4" '+
         'style="fill:var(--card)"/>'+
         '<text x="'+mx+'" y="'+(my+4.5)+'" text-anchor="middle" fill="'+col+'" '+
         'font-size="11" font-weight="700">'+esc(label)+'</text>';
    }
    return e;
  };

  s+=edge(midX,yNet+BH,midX,yWan,cumCol(seg.inet));
  s+=edge(midX,yWan+BH,midX,yRoot,cumCol(seg.wan));
  s+=box(midX,yNet,200,BH,'☁ Internet',seg.inet?seg.inet.host:'',cum(seg.inet),cumCol(seg.inet));
  s+=box(midX,yWan,200,BH,'WAN gateway',wanT?wanT.host:'',cum(seg.wan),cumCol(seg.wan));
  s+=box(midX,yRoot,230,BH,root.name,
         root.ip+(root.temp!=null?'  ·  '+root.temp.toFixed(1)+'°C':''),
         cum(seg.root),cumCol(seg.root));

  const chipCol=(list,x0,y0)=>{let r='';list.forEach((k,i)=>{const y=y0+i*24;
    r+='<rect x="'+x0+'" y="'+y+'" width="200" height="20" rx="5" style="fill:var(--card);stroke:var(--line)"/>'+
       '<text x="'+(x0+100)+'" y="'+(y+14)+'" text-anchor="middle" fill="currentColor" font-size="11">'+esc(k)+'</text>';});
    return r;};
  const wanK=((d.topo||{}).wan||[]).map(h=>h.name+' · '+(h.kind==='ethernet'?'wired'+(h.speed?' '+(h.speed>=1000?h.speed/1000+'G':h.speed+'M'):''):h.kind==='wifi'?'Wi-Fi':'')+' '+h.ip.split('.').slice(-1)[0].replace(/^/,'.'));
  if(wanK.length){
    s+='<text x="'+(midX+120)+'" y="'+(yWan-6)+'" fill="'+C.n+'" font-size="10">upstream LAN '+esc((d.topo||{}).wan_lan||'')+'</text>';
    s+='<line x1="'+(midX+100)+'" y1="'+(yWan+BH/2)+'" x2="'+(midX+118)+'" y2="'+(yWan+BH/2)+'" stroke="'+C.n+'"/>';
    s+=chipCol(wanK,midX+120,yWan);
  }
  const rootK=(byNode[root.ip]||[]).map(k=>k.name+(k.band?' · '+k.band+(k.rssi?' '+k.rssi:''):''));
  if(rootK.length){
    s+='<line x1="'+(midX-115)+'" y1="'+(yRoot+BH/2)+'" x2="'+(midX-122)+'" y2="'+(yRoot+BH/2)+'" stroke="'+C.n+'"/>';
    s+=chipCol(rootK,midX-322,yRoot+BH/2-10-(rootK.length-1)*12);
  }
  const bh=branchHealth(d);
  sats.forEach((n,i)=>{
    const x=colW*(i+0.5);
    const cls=uplinkTier(d,n,bh);
    const rssiLbl=n.link==='wireless'
      ? [n.rssi5!=null?'5G '+n.rssi5:null,n.rssi6!=null?'6G '+n.rssi6:null]
          .filter(Boolean).join(' · ')+' dBm'
      : n.link;
    const home=seg.home===n.ip;
    s+=edge(midX,yRoot+BH,x,ySat,C[cls],rssiLbl,n.link==='wireless');
    // Only the prober's own node has a measured reach; the other satellite's uplink is
    // described by its signal alone, because nothing probes from behind it.
    const oth=((d.topo||{}).others||{})[n.ip];
    s+=box(x,ySat,230,BH,n.name,n.ip+(oth?'  ·  +'+oth+' devices':''),home?cum(seg.own):'',cumCol(seg.own));
    const kids=byNode[n.ip]||[];
    // One spine drawn first, so the chips paint over it instead of it striping through
    // each box in turn.
    if(kids.length)
      s+='<line x1="'+x+'" y1="'+(ySat+BH)+'" x2="'+x+'" y2="'+
         (ySat+BH+29+(kids.length-1)*26)+'" stroke="'+C.n+'"/>';
    let y=ySat+BH+26;
    for(const k of kids){
      s+='<rect x="'+(x-95)+'" y="'+(y-4)+'" width="190" height="21" rx="5" '+
         'style="fill:var(--card);stroke:var(--line)"/>'+
         '<text x="'+x+'" y="'+(y+11)+'" text-anchor="middle" fill="currentColor" '+
         'font-size="11">'+esc(k.name)+(k.how==='rtt'?' *':'')+
         (k.band?' · '+esc(k.band)+(k.rssi?' '+k.rssi:''):'')+'</text>';
      y+=26;
    }
  });
  s+='</svg>';
  let note='<p class=sub>Dashed edge = wireless backhaul; solid = wired. '+
           '<b>*</b> = placed by latency rather than BSSID (macOS redacts it).</p>';
  if(seg.from)
    note+='<p class=sub>Coloured figures are <b>cumulative</b> — the cost of reaching that '+
          'point <i>from '+esc(seg.from)+'</i>, not the cost of the last hop. The hop where '+
          'they jump is where the loss is introduced. Branches nothing probes from show '+
          'signal only.</p>';
  const rem=(d.topo||{}).remote||[];
  if(rem.length) note+='<p class=sub>Elsewhere — grouped by shared internet connection: '+
    rem.map(g=>esc(g.join(' + '))).join('  ·  ')+'</p>';
  if(unplaced.length) note+='<p class=sub>At home but not placed yet: '+esc(unplaced.join(', '))+'</p>';
  return s+note+renderLinks(d);
}

// Direct links that bypass the LAN: each connected group laid out on a circle, so the
// Spark QSFP ring draws as a ring and a cabled pair as a single edge.
function renderLinks(d){
  const L=(d.topo||{}).links||[];
  if(!L.length) return '';
  const adj={};
  for(const l of L) for(const a of l.members) for(const b of l.members) if(a!==b)
    (adj[a]=adj[a]||new Set()).add(b);
  const seen=new Set(), groups=[];
  for(const n of Object.keys(adj).sort()){ if(seen.has(n)) continue;
    const g=[], st=[n]; seen.add(n);
    while(st.length){const x=st.pop(); g.push(x); for(const y of adj[x]) if(!seen.has(y)){seen.add(y);st.push(y);}}
    groups.push(g.sort()); }
  const lbl=l=>(l.speed?(l.speed>=1000?l.speed/1000+'G ':l.speed+'M '):'')+
               (l.kind==='qsfp'?'QSFP':l.kind==='thunderbolt'?'Thunderbolt':'Ethernet');
  const col=l=>l.kind==='qsfp'?'#a371f7':l.kind==='thunderbolt'?'#58a6ff':C.n;
  const W=920, GH=groups.some(g=>g.length>2)?250:70; let s='<h3 style="margin:18px 0 6px">Direct links</h3>'+
    '<svg viewBox="0 0 '+W+' '+GH+'" style="width:100%;height:auto;font:12px ui-monospace,Menlo,monospace" role=img>';
  const gw=W/groups.length;
  groups.forEach((g,gi)=>{
    const cx=gw*(gi+0.5), cy=GH/2, r=g.length<=2?Math.min(gw/2-90,150):Math.min(gw/2-60,170), ry=Math.min(GH/2-30,90);
    const pos={};
    // order a ring by walking neighbours so its cables do not cross
    let ord=[g[0]];
    while(ord.length<g.length){const nx=[...adj[ord[ord.length-1]]].find(y=>!ord.includes(y))||g.find(y=>!ord.includes(y)); ord.push(nx);}
    ord.forEach((n,i)=>{const a=g.length<=2?Math.PI*i:-Math.PI/2-Math.PI/4+2*Math.PI*i/g.length;
      pos[n]=[cx+r*Math.cos(a), cy+(g.length<=2?0:ry*Math.sin(a))];});
    for(const l of L){ if(!g.includes(l.members[0])) continue;
      for(let i=0;i<l.members.length;i++) for(let j=i+1;j<l.members.length;j++){
        const [x1,y1]=pos[l.members[i]], [x2,y2]=pos[l.members[j]], mx=(x1+x2)/2, my=(y1+y2)/2, t=lbl(l), w=t.length*6.7+12;
        s+='<line x1="'+x1+'" y1="'+y1+'" x2="'+x2+'" y2="'+y2+'" stroke="'+col(l)+'" stroke-width="3"/>'+
           '<rect x="'+(mx-w/2)+'" y="'+(my-9)+'" width="'+w+'" height="19" rx="4" style="fill:var(--card)"/>'+
           '<text x="'+mx+'" y="'+(my+4.5)+'" text-anchor="middle" fill="'+col(l)+'" font-size="11" font-weight="700">'+esc(t)+'</text>'+
           '<title>'+esc(l.net+(l.note?' — '+l.note:''))+'</title>';
      }}
    for(const n of g){const [x,y]=pos[n];
      s+='<rect x="'+(x-55)+'" y="'+(y-13)+'" width="110" height="26" rx="6" style="fill:var(--tile);stroke:var(--line)"/>'+
         '<text x="'+x+'" y="'+(y+4)+'" text-anchor="middle" fill="currentColor" font-weight="700">'+esc(n)+'</text>';}
  });
  const notes=L.filter(l=>l.note).map(l=>'<p class=sub><b>'+esc(l.members.join(' ↔ '))+'</b>: '+esc(l.note)+'</p>').join('');
  return s+'</svg><p class=sub>Cabled point-to-point subnets (not on either LAN) shared by two machines; hover a cable for its subnet.</p>'+notes;
}

function renderMesh(d){
  const m=d.mesh, from=d.mesh_from, bh=branchHealth(d);
  if(!m) return '<p class=small>No router data yet — an agent on the LAN needs <code>--asus</code> '+
                'with ASUS_USER / ASUS_PASS set.</p>';
  let r='<table><thead><tr><th>node</th><th>ip</th><th>uplink</th><th class=num>5 GHz</th>'+
        '<th class=num>6 GHz</th><th class=num>temp</th><th>state</th></tr></thead><tbody>';
  for(const n of m.nodes){
    let cls,txt;
    if(n.link==='root'){cls='g';txt='root';}
    else if(n.link==='wired'){cls='g';txt='ethernet';}
    else {cls=uplinkTier(d,n,bh); txt=TIER[cls];}
    r+='<tr><td>'+esc(n.name)+'<div class=sub>'+esc(n.model||'')+'</div></td>'+
       '<td class=mono>'+esc(n.ip)+'</td>'+
       '<td>'+esc(n.link)+'</td>'+
       '<td class=num>'+(n.rssi5==null?'—':pill(rssiCls(n.rssi5),n.rssi5+' dBm'))+'</td>'+
       '<td class=num>'+(n.rssi6==null?'—':pill(rssiCls(n.rssi6),n.rssi6+' dBm'))+'</td>'+
       '<td class=num>'+(n.temp==null?'<span class=sub>n/a</span>'
                                     :pill(tempCls(n.temp),n.temp.toFixed(1)+'°C'))+'</td>'+
       '<td>'+(n.online?pill(cls,txt):pill('b','offline'))+'</td></tr>';
  }
  r+='</tbody></table>';
  if(m.ports){
    r+='<p class=small style="margin-top:10px">Router ports — '+
       Object.entries(m.ports).map(([k,v])=>esc(k)+': '+pill(v==='X'?'n':'g',PORT[v]||v)).join(' &nbsp; ')+
       '</p>';
  }
  if(m.nodes.some(n=>n.temp==null))
    r+='<p class=sub>Temperature is only readable from the AiMesh root — satellites accept '+
       'the login but redirect their whole web UI to message.htm.</p>';
  if(from) r+='<p class=sub>via agent on '+esc(from)+'</p>';
  return r;
}

function renderProbes(ps){
  if(!ps.length) return '<p class=small>No path probes reporting. Start an agent with '+
                        '<code>--netprobe</code> to populate this.</p>';
  let r='';
  for(const p of ps){
    r+='<p class=sub style="margin-top:12px">from <b style="color:var(--fg)">'+esc(p.from)+
       '</b> — '+p.count+' pings/target, '+p.age.toFixed(0)+'s ago</p>'+
       '<table><thead><tr><th>target</th><th>address</th><th class=num>loss now</th>'+
       '<th class=num>avg</th><th class=num>max</th><th class=num>jitter</th>'+
       '<th class=num>worst '+p.window_min+'m</th><th class=num>mean '+p.window_min+'m</th>'+
       '</tr></thead><tbody>';
    for(const t of p.targets){
      const w=(p.window||{})[t.name];
      r+='<tr><td>'+esc(t.name)+'</td><td class=mono>'+esc(t.host)+'</td>'+
         '<td class=num>'+pill(lossCls(t.loss),t.loss.toFixed(1)+'%')+'</td>'+
         '<td class=num>'+(t.avg==null?'—':pill(msCls(t.avg),t.avg+' ms'))+'</td>'+
         '<td class="num mono">'+(t.max==null?'—':t.max+' ms')+'</td>'+
         '<td class="num mono">'+(t.jitter==null?'—':t.jitter+' ms')+'</td>'+
         '<td class=num>'+(w?pill(lossCls(w.worst),w.worst.toFixed(0)+'%'):'—')+'</td>'+
         '<td class="num mono">'+(w?w.mean.toFixed(1)+'%  <span class=sub>n='+w.n+'</span>':'—')+
         '</td></tr>';
    }
    r+='</tbody></table>';
  }
  return r;
}

function renderMachines(ms){
  let r='<table><thead><tr><th>machine</th><th>address</th><th>tailscale</th><th>os</th><th>kind</th>'+
        '<th class=num>last seen</th></tr></thead><tbody>';
  for(const m of ms){
    const live=m.age<20;
    const kind=m.gpu==='NVIDIA'?'NVIDIA':m.gpu==='Apple'?'Apple':m.board==='rpi'?'Raspberry Pi':'—';
    r+='<tr><td>'+esc(m.name)+'</td><td class=mono>'+esc(m.ip||'—')+'</td><td class=mono>'+esc(m.ts_ip||'—')+'</td>'+
       '<td>'+esc(m.os||'—')+'</td><td>'+kind+'</td>'+
       '<td class=num>'+pill(live?'g':'b',live?'live':m.age.toFixed(0)+'s')+'</td></tr>';
  }
  return r+'</tbody></table>';
}

// Turn the numbers into the conclusion, so the page says what is wrong rather than
// leaving it to be re-derived from a table every time.
// Severity must come from what is *measured*, not from signal strength alone. A -71 dBm
// backhaul that is currently carrying traffic cleanly is a thin margin to warn about, not
// a fault to alarm on — reporting it as broken while every path reads 0% loss trains the
// reader to ignore the banner.
function renderDiag(d){
  const bad=[], warn=[];
  for(const p of d.probes) for(const t of p.targets){
    const w=(p.window||{})[t.name];
    if(t.loss>=10)
      bad.push('<b>'+esc(p.from)+' → '+esc(t.name)+'</b> ('+esc(t.host)+') losing <b>'+
               t.loss.toFixed(1)+'%</b> of packets right now'+
               (t.avg!=null?', '+t.avg+' ms average':'')+'.');
    else if(w&&w.worst>=10)
      warn.push('<b>'+esc(p.from)+' → '+esc(t.name)+'</b> is clean now but lost up to <b>'+
                w.worst.toFixed(0)+'%</b> in the last '+p.window_min+' min ('+w.bad+' of '+
                w.n+' samples bad) — <b>intermittent</b>, not yet proven fixed.');
  }
  const lossy=bad.length>0;
  if(d.mesh) for(const n of d.mesh.nodes){
    if(n.temp!=null&&n.temp>=80)
      bad.push('<b>'+esc(n.name)+'</b> is running hot at <b>'+n.temp.toFixed(1)+
               '&deg;C</b> — Broadcom SoCs throttle their radios up here.');
    if(n.link!=='wireless') continue;
    const best=Math.max(n.rssi5==null?-999:n.rssi5,n.rssi6==null?-999:n.rssi6);
    if(best===-999||best>=-70) continue;
    const where='<b>'+esc(n.name)+'</b> ('+esc(n.ip)+') is on a <b>wireless</b> backhaul at '+
                best+' dBm';
    // Only call the weak link a fault when something behind it is actually suffering.
    if(lossy) bad.push(where+' — too weak to carry its clients. Everything behind this '+
                       'node inherits its packet loss.');
    else warn.push(where+' — very little margin, though it is carrying traffic cleanly '+
                   'at the moment. Expect it to degrade again under interference or load.');
  }
  let h='';
  if(bad.length)
    h+='<div class="diag b"><b>'+bad.length+' problem'+(bad.length>1?'s':'')+' detected</b>'+
       '<ul><li>'+bad.join('</li><li>')+'</li></ul></div>';
  if(warn.length)
    h+='<div class="diag w"><b>'+warn.length+' thing'+(warn.length>1?'s':'')+' to watch</b>'+
       '<ul><li>'+warn.join('</li><li>')+'</li></ul></div>';
  if(!h) h='<div class="diag g">Network healthy — every path clean now and across the '+
           'retained window, and no backhaul running short of margin.</div>';
  return h;
}

async function tick(){
  try{
    const r=await fetch('/api/admin/net',{cache:'no-store'});
    if(!r.ok){$('clock').textContent='signed out?';return;}
    const d=await r.json();
    $('clock').textContent=d.t;
    $('diag').innerHTML=renderDiag(d);
    $('topo').innerHTML=renderTopo(d);
    $('mesh').innerHTML=renderMesh(d);
    $('probes').innerHTML=renderProbes(d.probes);
    $('machines').innerHTML=renderMachines(d.machines);
  }catch(e){$('clock').textContent='offline';}
}
tick();setInterval(tick,5000);
</script>""", _ADMIN_CSS)


INSTALL_SH = r"""#!/usr/bin/env bash
set -euo pipefail
SERVER="{{SERVER}}"
: "${NOC_NAME:?Set NOC_NAME (the machine name you registered on the website)}"
: "${NOC_TOKEN:?Set NOC_TOKEN (the enrolment token from the website)}"
OS="$(uname -s)"

if [ "$OS" = "Linux" ]; then
  if [ "$(id -u)" != "0" ]; then
    echo "This installs a systemd service and needs root. Re-run with sudo:" >&2
    echo "  sudo NOC_NAME='$NOC_NAME' NOC_TOKEN='<your token>' bash $0" >&2
    exit 1
  fi
  command -v python3 >/dev/null || { echo "python3 not found — install it first." >&2; exit 1; }
  install -d /opt/noc
  curl -fsSL "$SERVER/agent" -o /opt/noc/noc_agent.py
  RUN_USER="${SUDO_USER:-root}"
  cat > /etc/systemd/system/noc-agent.service <<UNIT
[Unit]
Description=Incept5 NOC agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
ExecStart=/usr/bin/python3 /opt/noc/noc_agent.py --server $SERVER --name $NOC_NAME --token $NOC_TOKEN --interval 3
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now noc-agent
  echo "OK: installed and live now; starts on boot.  Check: systemctl status noc-agent"

elif [ "$OS" = "Darwin" ]; then
  command -v /usr/bin/python3 >/dev/null || { echo "python3 missing — run: xcode-select --install" >&2; exit 1; }
  mkdir -p "$HOME/noc" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  curl -fsSL "$SERVER/agent" -o "$HOME/noc/noc_agent.py"
  PLIST="$HOME/Library/LaunchAgents/com.incept5.noc-agent.plist"
  cat > "$PLIST" <<PLST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.incept5.noc-agent</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string><string>$HOME/noc/noc_agent.py</string>
    <string>--server</string><string>$SERVER</string>
    <string>--name</string><string>$NOC_NAME</string>
    <string>--token</string><string>$NOC_TOKEN</string>
    <string>--interval</string><string>3</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/noc-agent.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/noc-agent.log</string>
</dict></plist>
PLST
  launchctl bootout gui/$(id -u)/com.incept5.noc-agent 2>/dev/null || true
  launchctl bootstrap gui/$(id -u) "$PLIST"
  echo "OK: installed and live now; starts on login.  Log: ~/Library/Logs/noc-agent.log"

else
  echo "Unsupported OS: $OS" >&2; exit 1
fi
"""
