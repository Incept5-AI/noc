#!/usr/bin/env python3
"""Run the production collector (noc_web) locally against a synthetic fleet — no agents,
no email, no cloud. Used for the screenshots in the README and for UI work.

    pip install -r deploy/requirements.txt
    python3 demo/demo.py [--port 8788]

It prints a one-time sign-in link for the browser and a session token for the iPhone
simulator (see README → iPhone app). Everything lives in a throwaway SQLite file and in
memory; nothing is sent anywhere.
"""
import argparse
import json
import math
import os
import random
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "demo-token"
ADMIN = "admin@example.com"

# The env must be in place before noc_auth / noc_web are imported (they read it at import).
os.environ.setdefault("NOC_DB", os.path.join(tempfile.mkdtemp(prefix="noc-demo-"), "noc.db"))
os.environ.update(NOC_TOKEN=TOKEN, COOKIE_SECURE="0", ADMIN_EMAILS=ADMIN,
                  ALLOWED_EMAIL_DOMAINS="example.com",
                  NOC_AUDIT_LOG=os.path.join(os.path.dirname(os.environ["NOC_DB"]), "audit.log"))
sys.path.insert(0, ROOT)

# ---------------------------------------------------------------- the pretend fleet
ROUTER, LIVING, OFFICE = "aa:00:00:00:01:00", "aa:00:00:00:02:00", "aa:00:00:00:03:00"
MESH = {
    "nodes": [
        {"name": "Router", "ip": "192.168.50.1", "mac": ROUTER.upper(), "model": "ZenWiFi BT10",
         "online": True, "level": 0, "link": "root", "rssi5": None, "rssi6": None, "temp": 62.5,
         "fh": [ROUTER[:-2] + "1b"]},
        {"name": "Living Room", "ip": "192.168.50.2", "mac": LIVING.upper(), "model": "ZenWiFi BT10",
         "online": True, "level": 1, "link": "wireless", "rssi5": -58, "rssi6": -66,
         "fh": [LIVING[:-2] + "1b"]},
        {"name": "Office", "ip": "192.168.50.3", "mac": OFFICE.upper(), "model": "ZenWiFi BT10",
         "online": True, "level": 1, "link": "wireless", "rssi5": -64, "rssi6": -72,
         "fh": [OFFICE[:-2] + "1b"]},
    ],
    "clients": {  # ip -> [node mac, band (0 wired, 2 5G, 4 6G), rssi]
        "192.168.50.11": [OFFICE, 4, -31], "192.168.50.12": [OFFICE, 4, -34],
        "192.168.50.13": [OFFICE, 4, -29], "192.168.50.14": [OFFICE, 4, -38],
        "192.168.50.21": [LIVING, 0, 0], "192.168.50.22": [LIVING, 4, -44],
        "192.168.50.23": [ROUTER, 4, -41], "192.168.50.31": [ROUTER, 2, -55],
        "192.168.50.90": [LIVING, 2, -61], "192.168.50.91": [ROUTER, 2, -48],
    },
}

GB10 = {"os_name": "Ubuntu 24.04.3 LTS", "dgx_os": "DGX OS 7.2.3", "kernel": "6.11.0-1016-nvidia",
        "arch": "aarch64", "model": "NVIDIA DGX Spark", "cpu_model": "Cortex-X925 + Cortex-A725",
        "gpu_name": "NVIDIA GB10", "driver": "580.95.05", "cuda": "13.0", "docker": "28.5.1",
        "python": "3.12.3"}

def vllm(model, ctx, max_seqs, port=8000):
    return {"engine": "vLLM", "port": port, "version": "0.11.1", "container": "vllm",
            "models": [{"id": model, "ctx": ctx, "state": "loaded"}],
            "stats": {"max_seqs": max_seqs, "kv_conc": round(max_seqs * 0.6, 1)}}

HOSTS = {
    "gpu-node-1": dict(kind="nvidia", ip="192.168.50.11", base=78, ncpu=20, mem=124000,
                       q=("10.99.12.1", None), serve=[vllm("Qwen/Qwen3-32B-FP8", 131072, 16)]),
    "gpu-node-2": dict(kind="nvidia", ip="192.168.50.12", base=64, ncpu=20, mem=124000,
                       q=("10.99.12.2", "10.99.23.2"),
                       serve=[vllm("openai/gpt-oss-120b", 131072, 8)]),
    "gpu-node-3": dict(kind="nvidia", ip="192.168.50.13", base=22, ncpu=20, mem=124000,
                       q=("10.99.23.3", "10.99.34.3"),
                       serve=[{"engine": "Whisper", "port": 8178,
                               "models": [{"id": "whisper-large-v3-turbo", "state": "loaded"}]},
                              {"engine": "Kokoro", "port": 8880,
                               "models": [{"id": "kokoro-82m", "state": "loaded"}]}]),
    "gpu-node-4": dict(kind="nvidia", ip="192.168.50.14", base=5, ncpu=20, mem=124000,
                       q=("10.99.34.4", None), serve=[]),
    "mac-studio": dict(kind="apple", ip="192.168.50.21", base=55, ncpu=32, mem=262144,
                       chip="Apple M3 Ultra", pe="24P + 8E", gcores=80, model="Mac15,14", tb="10.0.10.1", wan="192.168.1.40",
                       serve=[{"engine": "LM Studio", "port": 1234,
                               "models": [{"id": "qwen3-coder-30b-a3b-instruct-mlx", "state": "loaded",
                                           "ctx": 65536}],
                               "available": ["gemma-3-27b-it", "mistral-small-3.2-24b"],
                               "available_n": 2}]),
    "mac-mini": dict(kind="apple", ip="192.168.50.22", base=18, ncpu=14, mem=65536,
                     chip="Apple M4 Pro", pe="10P + 4E", gcores=20, model="Mac16,11", tb="10.0.10.2",
                     serve=[{"engine": "Ollama", "port": 11434, "version": "0.12.6",
                             "models": [{"id": "llama3.2:3b", "state": "loaded", "ctx": 8192}]}],
                     prober=True),
    "macbook-pro": dict(kind="apple", ip="192.168.50.23", base=9, ncpu=14, mem=49152, laptop=True,
                        chip="Apple M4 Max", pe="10P + 4E", gcores=32, model="Mac16,6", serve=[]),
    "pi-edge": dict(kind="plain", ip="192.168.50.31", base=12, ncpu=4, mem=8064, serve=[]),
}

PROCS = {
    "nvidia": ["python3", "vllm", "dockerd", "nvidia-persistenced"],
    "apple": ["WindowServer", "python3.12", "lms", "Safari"],
    "plain": ["python3", "node-exporter", "sshd", "systemd-journald"],
}


class Walk:
    """A smooth, bounded random walk around a base level — plausible-looking telemetry."""
    def __init__(self, base, spread=18):
        self.base, self.v, self.spread, self.t = base, base, spread, random.random() * 100

    def step(self):
        self.t += 0.21
        target = self.base + self.spread * math.sin(self.t) * math.sin(self.t / 3.7)
        self.v += (target - self.v) * 0.35 + random.uniform(-4, 4)
        self.v = max(0.0, min(100.0, self.v))
        return round(self.v, 1)


def sample(name, h, w, started):
    u = w.step()
    kind = h["kind"]
    d = {"name": name, "ncpu": h["ncpu"], "ip": h["ip"],
         "uptime": int(time.time() - started + h["ncpu"] * 86400 * 1.7),
         "load": [round(u / 100 * h["ncpu"] * f, 2) for f in (0.6, 0.55, 0.5)],
         "thermal": "Nominal", "containers": "",
         "procs": [{"cpu": round(max(0.3, u * k), 1), "mem": round(3 * k, 1), "cmd": c}
                   for k, c in zip((1.6, 0.4, 0.15, 0.05), PROCS[kind])]}
    used = int(h["mem"] * (0.35 + u / 220))
    d.update(mem_total=h["mem"], mem_used=used, mem_avail=h["mem"] - used)
    ifaces = [{"dev": "wlP9s9" if kind == "nvidia" else "en1", "kind": "wifi", "ip": h["ip"],
               "prefix": 24, "speed": None, "mac": None}]
    sysd = {"hostname": name, "agent": "2026.10.25"}
    if kind == "nvidia":
        d.update(os="Linux", gpu="NVIDIA", gpu_util=u, gpu_power=round(12 + u * 0.95, 1),
                 gpu_temp=round(38 + u * 0.33, 0), gpu_clk=round(800 + u * 14), gpu_power_max=None,
                 cpu=round(u * 0.25 + 3, 1), containers="vllm" if h["serve"] else "")
        sysd.update(GB10)
        for ip, dev in zip(h["q"], ("enP2p1s0f0np0", "enP2p1s0f1np1")):
            if ip:
                ifaces.append({"dev": dev, "kind": "qsfp", "ip": ip, "prefix": 24, "speed": 200000})
    elif kind == "apple":
        pw = round(8 + u * (2.2 if h["ncpu"] > 20 else 1.1), 1)
        d.update(os="macOS", gpu="Apple", gpu_util=u, gpu_power=pw,
                 gpu_power_max=480 if h["ncpu"] > 20 else (None if h.get("laptop") else 200),
                 cpu=round(u * 0.3 + 4, 1), cpu_temp=round(42 + u * 0.4, 1),
                 fan_rpm=0 if u < 40 else int(900 + u * 22), gpu_mem_used=int(used * 0.4),
                 cpu_cores=[round(max(0, min(100, u * random.uniform(0.1, 1.2))), 1)
                            for _ in range(h["ncpu"])])
        sysd.update(os_name="macOS 26.1 (25B78)", kernel="Darwin 25.1.0", arch="arm64",
                    cpu_model=h["chip"], cores_pe=h["pe"], gpu_cores=h["gcores"], model=h["model"],
                    python="3.12.8")
        if h.get("wan"):  # a wired leg on the upstream LAN (ISP gateway, double NAT)
            ifaces.append({"dev": "en0", "kind": "ethernet", "ip": h["wan"], "prefix": 24,
                           "speed": 10000, "mac": None})
        if h.get("tb"):
            ifaces.append({"dev": "bridge0", "kind": "thunderbolt", "ip": h["tb"], "prefix": 24,
                           "speed": 40000, "tb": {"link": 40, "max": 120 if h["ncpu"] > 20 else 80}})
    else:
        d.update(os="Linux", gpu=None, board="rpi", cpu=u, cpu_temp=round(45 + u * 0.3, 1))
        sysd.update(os_name="Debian GNU/Linux 12 (bookworm)", kernel="6.12.25+rpt-rpi-2712",
                    arch="aarch64", model="Raspberry Pi 5 Model B Rev 1.0",
                    cpu_model="Cortex-A76", python="3.11.2")
    sysd["ifaces"] = ifaces
    d["sys"] = sysd
    models = json.loads(json.dumps(h["serve"]))
    for s in models:
        st = s.get("stats")
        if st is not None:
            run = max(0, min(st["max_seqs"], round(st["max_seqs"] * u / 100)))
            st.update(running=run, waiting=1 if u > 92 else 0, kv=round(u * 0.7, 1),
                      gen_tps=round(run * 21.5, 1), prompt_tps=round(run * 310.0, 1))
    d["models"] = models
    if h.get("prober"):
        jit = lambda a: round(a * random.uniform(0.8, 1.3), 1)
        probes = [("living-room", "192.168.50.2", 0.0, 1.0), ("router", "192.168.50.1", 0.0, 6.0),
                  ("office", "192.168.50.3", 0.0, 7.5), ("wan-gateway", "192.168.1.1", 0.0, 7.0),
                  ("internet", "1.1.1.1", random.choice([0.0, 0.0, 0.0, 5.0]), 14.0)]
        d["net"] = {"count": 20, "ts": time.time() // 30 * 30,
                    "probes": [{"name": n, "host": ip, "loss": loss, "min": round(a * 0.8, 1), "avg": jit(a),
                                "max": round(a * 2.1, 1), "jitter": round(a * 0.15, 1)}
                               for n, ip, loss, a in probes],
                    "mesh": MESH}
    return d


def feed(base, interval):
    time.sleep(1.5)  # let uvicorn start listening
    started = time.time()
    walks = {n: Walk(h["base"]) for n, h in HOSTS.items()}
    while True:
        for name, h in HOSTS.items():
            body = json.dumps(sample(name, h, walks[name], started)).encode()
            req = urllib.request.Request(f"{base}/report?token={TOKEN}", data=body,
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=5).read()
            except Exception as e:  # server still starting
                print("report failed:", e, file=sys.stderr)
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between synthetic reports")
    a = ap.parse_args()

    import uvicorn
    import noc_auth as auth
    import noc_web

    for name, h in HOSTS.items():
        auth.seed_machine(name, ADMIN, "macos" if h["kind"] == "apple" else "linux", shared=True)
    url, _ = auth.issue_login(ADMIN)
    url = url.replace(auth.APP_BASE_URL.rstrip("/"), f"http://127.0.0.1:{a.port}")
    tok_url, _ = auth.issue_login(ADMIN)
    session = auth.redeem_magic_link(tok_url.split("token=", 1)[1])

    threading.Thread(target=feed, args=(f"http://127.0.0.1:{a.port}", a.interval), daemon=True).start()
    print(f"\n  Fleet NOC demo — {len(HOSTS)} synthetic machines\n"
          f"  browser : {url}\n"
          f"  session : {session}   (cookie noc_session; for the iPhone simulator see README)\n"
          f"  DB      : {os.environ['NOC_DB']}\n", flush=True)
    uvicorn.run(noc_web.app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
