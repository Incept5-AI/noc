#!/usr/bin/env python3
"""NOC agent — reports this machine's stats to a NOC collector. Stdlib only.

Run:  python3 noc_agent.py --server http://spark:8787 --name mybox
Test: python3 noc_agent.py --once            # print one sample as JSON and exit

Works on Linux (NVIDIA GPU via nvidia-smi) and macOS (Apple Silicon GPU via ioreg).
Anyone can add a machine by running this one file pointed at the collector.
"""
import argparse, base64, ctypes, ctypes.util, glob, json, os, platform, re, socket, subprocess
import sys, threading, time, urllib.parse, urllib.request

SYS = platform.system()
_prev = {}

def sh(cmd, timeout=6):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""

def _cpu_linux():
    with open("/proc/stat") as f:
        v = list(map(int, f.readline().split()[1:9]))
    total, idle = sum(v), v[3] + v[4]
    p = _prev.get("cpu"); _prev["cpu"] = (total, idle)
    if not p:
        return 0.0
    dt, di = total - p[0], idle - p[1]
    return round(100 * (dt - di) / dt, 1) if dt > 0 else 0.0

_PROCESSOR_CPU_LOAD_INFO, _CPU_STATE_MAX, _CPU_STATE_IDLE = 2, 4, 2
_libc = None   # None = untried, False = unavailable, else the configured handle

def _mach_libc():
    """libSystem with host_processor_info() bound, or False where that isn't possible."""
    global _libc
    if _libc is None:
        try:
            lib = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            lib.mach_host_self.restype = ctypes.c_uint
            lib.host_processor_info.restype = ctypes.c_int
            lib.host_processor_info.argtypes = [
                ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
                ctypes.POINTER(ctypes.POINTER(ctypes.c_uint)), ctypes.POINTER(ctypes.c_uint)]
            lib.vm_deallocate.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t]
            _libc = lib
        except Exception:
            _libc = False
    return _libc

def _mac_cpu_ticks():
    """Per-core (user, system, idle, nice) tick counts since boot, or None.

    Replaces `top -l 2 -n 0`, which blocks ~1.8s because it takes two samples a second
    apart purely to produce a delta — 71% of the agent's whole sample cost. This call
    is ~0.04ms; the agent already loops, so it keeps the previous counts in _prev and
    deltas across the real poll interval instead of an arbitrary 1s window."""
    lib = _mach_libc()
    if not lib:
        return None
    count, info, info_count = ctypes.c_uint(0), ctypes.POINTER(ctypes.c_uint)(), ctypes.c_uint(0)
    if lib.host_processor_info(lib.mach_host_self(), _PROCESSOR_CPU_LOAD_INFO,
                               ctypes.byref(count), ctypes.byref(info),
                               ctypes.byref(info_count)) != 0:
        return None
    try:
        return [tuple(info[i * _CPU_STATE_MAX + s] for s in range(_CPU_STATE_MAX))
                for i in range(count.value)]
    finally:
        lib.vm_deallocate(lib.mach_host_self(), ctypes.cast(info, ctypes.c_void_p),
                          info_count.value * ctypes.sizeof(ctypes.c_uint))

def _mac_cpu():
    """(busy %, per-core busy %) from Mach tick deltas, or (None, []) if unavailable.

    Ticks are cumulative since boot, so the first call has nothing to subtract from and
    takes a short inline sample — that only costs the 0.15s once, and keeps `--once`
    meaningful instead of reporting 0%."""
    now = _mac_cpu_ticks()
    if now is None:
        return None, []
    was = _prev.get("mac_cpu")
    if not was or len(was) != len(now):
        time.sleep(0.15)
        was, now = now, _mac_cpu_ticks()
        if now is None or len(was) != len(now):
            return None, []
    _prev["mac_cpu"] = now
    cores, busy_all, total_all = [], 0, 0
    for before, after in zip(was, now):
        d = [a - b for a, b in zip(after, before)]
        total = sum(d)
        # A parked core can report a tick delta a hair below zero; clamp so it prints
        # 0.0 rather than -0.0.
        busy = max(0, total - d[_CPU_STATE_IDLE])
        total = max(total, busy)
        cores.append(round(100 * busy / total, 1) if total else 0.0)
        busy_all += busy
        total_all += total
    return (round(100 * busy_all / total_all, 1) if total_all else 0.0), cores

def _mac_cpu_top():
    """Fallback CPU busy % via `top` — costs ~1.8s, used only if Mach counters fail."""
    for ln in sh(["top", "-l", "2", "-n", "0"], timeout=8).splitlines():
        if ln.startswith("CPU usage"):
            try:
                return round(100 - float(ln.split(",")[-1].strip().split("%")[0]), 1)
            except Exception:
                pass
    return 0.0

_NV_BENIGN = {"Idle", "Applications Clocks Setting", "Sync Boost", "Display Clock Setting"}

def _nv_pressure():
    """NVIDIA throttle state from 'Clocks Event Reasons' (nvidia-smi -q -d PERFORMANCE).
    Returns (level, detail) mapped onto the same Nominal/Fair/Serious/Critical scale the
    Macs use, so the dashboard handles every host identically."""
    sec = re.search(r"Clocks Event Reasons\n(.*?)\n\s*\n",
                    sh(["nvidia-smi", "-q", "-d", "PERFORMANCE"], timeout=6), re.S)
    if not sec:
        return None, None
    active = [r for r in re.findall(r"^\s{8,}([\w ]+?)\s*:\s+Active\s*$", sec.group(1), re.M)
              if r not in _NV_BENIGN and r != "HW Slowdown"]
    if not active:
        return "Nominal", None
    lvl = ("Critical" if any("HW" in r for r in active)
           else "Serious" if any("Thermal" in r for r in active)
           else "Fair")
    return lvl, ", ".join(active)

_RPI_BITS = [(3, "soft temp limit", "Serious"), (2, "throttled", "Serious"),
             (1, "freq capped", "Fair"), (0, "under-voltage", "Fair")]

def _rpi_pressure():
    """Raspberry Pi throttle state from vcgencmd get_throttled (current-state bits 0-3;
    bits 16+ are historical and ignored). Same level scale as the other platforms."""
    m = re.search(r"throttled=0x([0-9a-fA-F]+)", sh(["vcgencmd", "get_throttled"], timeout=3))
    if not m:
        return None, None
    v = int(m.group(1), 16)
    active = [(name, lvl) for bit, name, lvl in _RPI_BITS if v & (1 << bit)]
    if not active:
        return "Nominal", None
    lvl = "Serious" if any(l == "Serious" for _, l in active) else "Fair"
    return lvl, ", ".join(n for n, _ in active)

def gather_linux():
    d = {"os": "Linux", "cpu": _cpu_linux(), "ncpu": os.cpu_count()}
    try:
        d["load"] = [float(x) for x in open("/proc/loadavg").read().split()[:3]]
    except Exception:
        d["load"] = [0, 0, 0]
    try:
        m = {}
        for ln in open("/proc/meminfo"):
            k, val = ln.split(":"); m[k] = int(val.split()[0])
        tot = m["MemTotal"] // 1024; av = m.get("MemAvailable", m["MemFree"]) // 1024
        d.update(mem_total=tot, mem_avail=av, mem_used=tot - av)
    except Exception:
        d.update(mem_total=0, mem_avail=0, mem_used=0)
    g = sh(["nvidia-smi", "--query-gpu=utilization.gpu,power.draw,temperature.gpu,clocks.sm,power.limit",
            "--format=csv,noheader,nounits"]).strip().splitlines()
    if g:
        p = [x.strip() for x in g[0].split(",")]
        def num(i):
            try: return float(p[i])
            except Exception: return None
        d.update(gpu="NVIDIA", gpu_util=num(0), gpu_power=num(1), gpu_temp=num(2), gpu_clk=num(3),
                 gpu_power_max=num(4))
    if "gpu" not in d:
        # non-NVIDIA Linux (e.g. Raspberry Pi) — no discrete GPU; report CPU temperature instead
        d["gpu"] = None
        try:
            d["cpu_temp"] = round(int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000.0, 1)
        except Exception:
            d["cpu_temp"] = None
        try:
            model = open("/proc/device-tree/model").read().replace("\x00", "").strip()
            if "raspberry pi" in model.lower():
                d["board"] = "rpi"
        except Exception:
            pass
    lvl = det = None
    if d.get("gpu") == "NVIDIA":
        lvl, det = _nv_pressure()
    elif d.get("board") == "rpi":
        lvl, det = _rpi_pressure()
    if lvl: d["thermal"] = lvl
    if det: d["thermal_detail"] = det
    procs = []
    for ln in sh(["ps", "-eo", "pcpu,pmem,comm", "--sort=-pcpu", "--no-headers"]).splitlines()[:4]:
        f = ln.split(None, 2)
        if len(f) == 3:
            procs.append({"cpu": float(f[0]), "mem": float(f[1]), "cmd": os.path.basename(f[2])})
    d["procs"] = procs
    d["containers"] = ",".join(sh(["docker", "ps", "--format", "{{.Names}}"]).split())
    return d

_pm_samplers = None   # cached sampler list that works on this macOS version

def _mac_thermal():
    """Package power (W) via powermetrics, die temp (°C) where the OS exposes it.
    Needs passwordless sudo for powermetrics only, e.g.:
      echo "$USER ALL=(ALL) NOPASSWD: /usr/bin/powermetrics" | sudo tee /etc/sudoers.d/powermetrics
    macOS ≤15 has an `smc` sampler (die temps); macOS 26 dropped it (power only).
    Returns (temp_c, watts); either may be None when unavailable."""
    global _pm_samplers
    out = ""
    for samplers in ([_pm_samplers] if _pm_samplers else
                     ["smc,cpu_power,gpu_power,ane_power", "cpu_power,gpu_power,ane_power"]):
        out = sh(["sudo", "-n", "powermetrics", "--samplers", samplers, "-n", "1", "-i", "1000"],
                 timeout=10)
        if "Power" in out:
            _pm_samplers = samplers
            break
    else:
        return None, None
    temps = [float(t) for t in re.findall(r"(?:CPU|GPU) die temperature: ([\d.]+) C", out)]
    temp = max(temps) if temps else None
    watts = None
    m = re.search(r"Combined Power.*?: ([\d.]+) (mW|W)\b", out)
    parts = ([(m.group(1), m.group(2))] if m else
             re.findall(r"(?:CPU|GPU|ANE) Power: ([\d.]+) (mW|W)\b", out))
    if parts:
        watts = round(sum(float(v) / (1000 if u == "mW" else 1) for v, u in parts), 1)
    return temp, watts

def _mac_system_power():
    """Total system input power (W) from PMU/battery telemetry — laptops only.
    Same source as mactop's 'system power'; works even where SoC energy counters don't."""
    m = re.search(r'"SystemPowerIn"=(\d+)', sh(["ioreg", "-r", "-n", "AppleSmartBattery"], timeout=4))
    # Desktops on macOS 27 publish an AppleSmartBattery node too, with SystemPowerIn=0 —
    # taking that as a reading made a Mac Studio report 0 W and skip every fallback.
    return round(int(m.group(1)) / 1000, 1) if m and int(m.group(1)) > 0 else None

_PWR_DOMAINS = ("AMCC", "DCS", "ISP", "AVE", "MSR", "AFR", "DISP", "DISPEXT")

def _mac_power_ioreport():
    """SoC power (W) summed from IOReport Energy Model channels via the `pwr` helper.
    Channel levels duplicate (core < cluster < 'CPU Energy'; 'GPU' == 'GPU Energy' in
    different units), so pick aggregates, max() across aliases, skip the rest."""
    exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pwr")
    if not os.path.exists(exe):
        return None
    ch = {}
    for ln in sh([exe], timeout=5).splitlines():
        try:
            name, v = ln.rsplit(None, 1)
            ch[name] = float(v)
        except (ValueError, IndexError):
            pass
    if not ch:
        return None
    pick = lambda *names: max((ch[n] for n in names if n in ch), default=0.0)
    cpu = ch.get("CPU Energy", 0.0)
    total = (cpu + pick("GPU", "GPU Energy")
             + pick("ANE", "ANE0", "ANE Energy") + pick("DRAM", "DRAM0", "DRAM Energy")
             + sum(ch.get(n, 0.0) for n in _PWR_DOMAINS)
             + sum(v for n, v in ch.items() if n.startswith(("PCIe", "apciec"))))
    # Without a CPU rail this is not a package total. On M5 only "GPU Energy" is
    # populated, and under GPU load it clears the 0.5W floor on its own — reporting
    # that as SoC power would understate the machine by an order of magnitude. On a
    # laptop _mac_system_power() wins first and hides it; on an M5 desktop it would not.
    if cpu <= 0:
        return None
    return round(total / 1000, 1) if total >= 500 else None   # <0.5W means counters are dead

def _mac_fan():
    """Max fan RPM via the optional `fan` helper binary next to this script
    (fan.c in the repo; AppleSMC user client, no root). None if absent."""
    exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fan")
    if not os.path.exists(exe):
        return None
    rpms = []
    _smc["power"] = None
    for ln in sh([exe], timeout=4).splitlines():
        try:
            if ln.startswith("power "):    # SMC total system W (PSTR/PDTR/PD0R), see fan.c
                _smc["power"] = round(float(ln.rsplit(None, 1)[-1]), 1)
            elif ln.startswith("fan"):
                rpms.append(float(ln.rsplit(None, 1)[-1]))
        except (ValueError, IndexError):
            pass
    return round(max(rpms)) if rpms else None

_smc = {"power": None}

_pm_thermal = True   # latched off after first failure (no sudoers rule → stop trying)

# powermetrics reports a different vocabulary from NSProcessInfo; fold it onto the
# Nominal/Fair/Serious/Critical scale the dashboard and the other platforms use.
_PM_PRESSURE = {"Nominal": "Nominal", "Moderate": "Fair", "Heavy": "Serious",
                "Trapping": "Critical", "Sleeping": "Critical"}

def _mac_pressure():
    """Thermal pressure (Nominal/Fair/Serious/Critical) — Apple's throttling signal.

    Prefers the optional `pressure` helper next to this script (pressure.m in the repo;
    NSProcessInfo.thermalState, public API, no root). Falls back to powermetrics, which
    needs the NOPASSWD sudoers rule and costs ~0.5s per sample."""
    exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pressure")
    if os.path.exists(exe):
        lvl = sh([exe], timeout=4).strip()
        if lvl in ("Nominal", "Fair", "Serious", "Critical"):
            return lvl
    global _pm_thermal
    if not _pm_thermal:
        return None
    out = sh(["sudo", "-n", "powermetrics", "--samplers", "thermal", "-n", "1", "-i", "500"],
             timeout=6)
    m = re.search(r"Current pressure level: (\w+)", out)
    if not m:
        _pm_thermal = False
        return None
    return _PM_PRESSURE.get(m.group(1), m.group(1))

def _mac_temp_hid():
    """Die temp (°C) via the optional `therm` helper binary next to this script
    (therm.c in the repo; IOHIDEventSystem sensors, no root needed). None if absent."""
    exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "therm")
    if not os.path.exists(exe):
        return None
    rows = []
    for ln in sh([exe], timeout=4).splitlines():
        try:
            name, v = ln.rsplit(None, 1)
            v = float(v)
            if 5 < v < 130:
                rows.append((name, v))
        except (ValueError, IndexError):
            pass
    # prefer SoC die sensors (PMU tdieN); tcal/battery/NAND are misleading as a headline temp
    vals = [v for n, v in rows if "tdie" in n] or [v for _, v in rows]
    return round(max(vals), 1) if vals else None

def gather_darwin():
    d = {"os": "macOS", "ncpu": os.cpu_count()}
    cpu, cores = _mac_cpu()
    if cpu is None:                        # Mach counters unavailable — pay for `top`
        cpu, cores = _mac_cpu_top(), []
    d["cpu"] = cpu
    if cores:
        d["cpu_cores"] = cores
    try:
        la = sh(["sysctl", "-n", "vm.loadavg"]).strip().strip("{} ").split()
        d["load"] = [float(la[0]), float(la[1]), float(la[2])]
    except Exception:
        d["load"] = [0, 0, 0]
    try:
        tot = int(sh(["sysctl", "-n", "hw.memsize"])) // (1024 * 1024)
        ps = int(sh(["sysctl", "-n", "hw.pagesize"]) or 16384)
        pg = {}
        for ln in sh(["vm_stat"]).splitlines():
            if ":" in ln:
                k, val = ln.split(":", 1)
                try:
                    pg[k.strip()] = int(val.strip().rstrip(".") or 0)
                except ValueError:
                    pass  # header line ("...: (page size of 16384 bytes)") etc.
        used = (pg.get("Pages active", 0) + pg.get("Pages wired down", 0)
                + pg.get("Pages occupied by compressor", 0)) * ps // (1024 * 1024)
        d.update(mem_total=tot, mem_used=used, mem_avail=tot - used)
    except Exception:
        d.update(mem_total=0, mem_used=0, mem_avail=0)
    io = sh(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"], timeout=6)
    mu = re.search(r'"Device Utilization %"=(\d+)', io)
    mm = re.search(r'"In use system memory"=(\d+)', io)
    d["gpu"] = "Apple"
    d["gpu_util"] = float(mu.group(1)) if mu else None
    d["gpu_mem_used"] = int(mm.group(1)) // (1024 * 1024) if mm else None
    t = _mac_temp_hid()
    f = _mac_fan()                             # also reads the SMC total-power key
    w = _mac_system_power()                    # laptops: total system W (matches mactop)
    if w is None: w = _mac_power_ioreport()    # desktops: SoC domain sum (pwr helper)
    if w is None and _smc["power"] is not None:  # M5 desktops: SMC DC-in (PD0R on the Studio)
        w = _smc["power"]
        # whole-system watts, so the 80 W Apple nominal would peg the meter: scale to the
        # box (Mac Studio's rated 480 W max on an Ultra-class part, else a desktop 200 W)
        d["gpu_power_max"] = 480 if (os.cpu_count() or 0) >= 28 else 200
    if t is None or w is None:                 # last resort: powermetrics (needs sudoers)
        t2, w2 = _mac_thermal()
        if t is None: t = t2
        if w is None: w = w2
    if t is not None: d["cpu_temp"] = t
    if w is not None: d["gpu_power"] = w   # system (laptop) or SoC (desktop) watts → PWR meter
    if f is not None: d["fan_rpm"] = f
    p = _mac_pressure()
    if p is not None: d["thermal"] = p     # throttling signal: anything above "Nominal"
    procs = []
    for ln in sh(["ps", "-A", "-o", "%cpu,%mem,ucomm", "-r"]).splitlines()[1:5]:
        f = ln.split(None, 2)
        if len(f) == 3:
            procs.append({"cpu": float(f[0]), "mem": float(f[1]), "cmd": f[2].strip()})
    d["procs"] = procs
    d["containers"] = ",".join(sh(["docker", "ps", "--format", "{{.Names}}"]).split())
    return d

# ---------------- network probe (optional; --netprobe) ----------------
# Diagnoses the path *between* hosts rather than inside one, which is what a mesh fault
# looks like: every client radio and every cable reads perfect while the node's own
# uplink drops a third of the traffic. Only a host on the LAN can see this, so it rides
# in on the agent report rather than being polled from the collector.

_PING_LOSS = re.compile(r"([\d.]+)% packet loss")
_PING_RTT = re.compile(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)")

def _local_ip():
    """Primary LAN IPv4 of this host, via a connectionless UDP socket — picks whichever
    interface the kernel would route out of without putting a packet on the wire."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()

_MAC_RE = r"([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})"

def _norm_mac(s):
    """Lower-case, zero-padded MAC. macOS drops leading zeros in some outputs
    ('0:5:cd:…'), which would never string-match a router's 'CC:28:AA:…' form."""
    return ":".join(p.rjust(2, "0").lower() for p in s.split(":"))

def _wifi_link():
    """The AP this host is associated to: {dev, bssid, ssid, signal, freq}.

    Lets the dashboard place a machine under the mesh node actually serving it. Wired
    hosts return None, and macOS only reveals a BSSID when Location Services permits it,
    so every caller must treat this as best-effort rather than guaranteed."""
    if SYS == "Linux":
        for path in sorted(glob.glob("/sys/class/net/*/wireless")):
            dev = path.rsplit("/", 2)[-2]
            out = sh(["iw", "dev", dev, "link"], timeout=4)
            m = re.search(r"Connected to " + _MAC_RE, out)
            if not m:
                continue
            d = {"dev": dev, "bssid": _norm_mac(m.group(1))}
            for key, pat in (("ssid", r"SSID:\s*(.+)"), ("signal", r"signal:\s*(-?\d+)"),
                             ("freq", r"freq:\s*([\d.]+)")):
                mm = re.search(pat, out)
                if mm:
                    d[key] = mm.group(1).strip()
            for k in ("signal", "freq"):
                if k in d:
                    d[k] = float(d[k])
            return d
    elif SYS == "Darwin":
        for dev in ("en0", "en1", "en2"):
            out = sh(["ipconfig", "getsummary", dev], timeout=4)
            m = re.search(r"\bBSSID\s*:\s*" + _MAC_RE, out)
            if not m:
                continue
            d = {"dev": dev, "bssid": _norm_mac(m.group(1))}
            mm = re.search(r"\bSSID\s*:\s*(.+)", out)
            if mm:
                d["ssid"] = mm.group(1).strip()
            return d
    return None

def _ping(host, count=20):
    """Loss %% and min/avg/max/jitter ms for one target.

    `-W` is **milliseconds on macOS but seconds on Linux** — the same flag means
    different things per platform, and passing the Linux form to macOS gives every
    target a 1ms deadline (100%% loss on a healthy link). `-i 0.2` is the floor both
    allow unprivileged."""
    wait = ["-W", "1000"] if SYS == "Darwin" else ["-W", "1"]
    out = sh(["ping", "-c", str(count), "-i", "0.2", *wait, host], timeout=count * 0.25 + 8)
    m = _PING_LOSS.search(out)
    # No "packet loss" line at all means ping never ran (unknown host, no route) — that
    # is 100% unreachable, not 0%.
    r = {"host": host, "loss": float(m.group(1)) if m else 100.0,
         "min": None, "avg": None, "max": None, "jitter": None}
    m = _PING_RTT.search(out)
    if m:
        r.update(min=round(float(m.group(1)), 1), avg=round(float(m.group(2)), 1),
                 max=round(float(m.group(3)), 1), jitter=round(float(m.group(4)), 1))
    return r

_asus_tok = {}    # base URL -> session token, reused across probe passes

def _asus_login(base, user, pw):
    """Sign in and return a session token. The token arrives as a **Set-Cookie header**,
    not in the response body (the body is only an HTML meta-refresh)."""
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    req = urllib.request.Request(
        base + "/login.cgi",
        data=urllib.parse.urlencode({"login_authorization": auth}).encode(),
        headers={"Referer": base + "/Main_Login.asp"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        cookies = resp.headers.get_all("Set-Cookie") or []
    return next((m.group(1) for c in cookies
                 if (m := re.search(r"asus_token=([A-Za-z0-9]+)", c))), None)

def _asus_fetch(base, user, pw, path):
    """GET `path` on the router, reusing a cached token and re-logging in once if it has
    gone stale.

    The cache is not just an optimisation: ASUS invalidates the previous session on every
    new login, so logging in each pass would boot a human out of the router's web UI (and
    out of any other tool) every 30 seconds. An expired token is not an HTTP error — the
    router answers 200 with a redirect to Main_Login.asp, so detect that in the body."""
    for attempt in (0, 1):
        tok = _asus_tok.get(base)
        if not tok:
            tok = _asus_tok[base] = _asus_login(base, user, pw)
            if not tok:
                return ""
        req = urllib.request.Request(
            base + path, headers={"Cookie": "asus_token=" + tok, "Referer": base + "/index.asp"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", "replace")
        except Exception:
            _asus_tok.pop(base, None)
            if attempt:
                raise
            continue
        if "Main_Login.asp" in body:
            _asus_tok.pop(base, None)
            continue
        return body
    return ""

_TEMP_KEYS = (("curr_cpuTemp", "cpu"), ("curr_coreTmp_2", "w2g"),
              ("curr_coreTmp_5", "w5g"), ("curr_coreTmp_6", "w6g"))

def _asus_temp(base, user, pw):
    """Router temperatures (°C) from the Sysinfo ajax page.

    Only the AiMesh **root** answers: satellites accept the login but redirect their whole
    web UI to message.htm, so there is no per-satellite temperature to be had this way.
    Radio rails are assigned from `fanctrl_info[]` on models that have a fan and are left
    as bare JS references otherwise — hence matching a number, not just anything."""
    body = _asus_fetch(base, user, pw, "/ajax_coretmp.asp")
    out = {}
    for key, name in _TEMP_KEYS:
        m = re.search(key + r'\s*=\s*"?([\d.]+)"?\s*;', body)
        if m:
            try:
                out[name] = round(float(m.group(1)), 1)
            except ValueError:
                pass
    return out or None

def _asus_mesh(base, user, pw):
    """AiMesh node list, backhaul RSSI and router temperature from an ASUS router.

    STRICTLY READ-ONLY: only `appGet.cgi?hook=…` and Sysinfo ajax GETs, which cannot
    change a setting, alter the operating mode, or reboot anything. Never add
    apply.cgi/applyapp.cgi calls here."""
    base = base.rstrip("/")

    def hook(name):
        return json.loads(_asus_fetch(
            base, user, pw, "/appGet.cgi?hook=" + urllib.parse.quote(name)) or "{}")

    def rssi(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    nodes = []
    for n in hook("get_cfg_clientlist()").get("get_cfg_clientlist", []):
        r5, r6 = rssi(n.get("rssi5g")), rssi(n.get("rssi6g"))
        lvl = int(n.get("level") or 0)
        # The main router usually has no alias set, so ASUS returns its MAC as the name.
        alias = n.get("alias") or ""
        if not alias or alias.replace(":", "").upper() == (n.get("mac") or "").replace(":", "").upper():
            alias = "Router" if lvl == 0 else (n.get("mac") or "node")
        nodes.append({
            "name": alias, "ip": n.get("ip"), "mac": n.get("mac"),
            "model": n.get("ui_model_name") or n.get("model_name"),
            "online": n.get("online") == "1", "level": lvl, "fw": n.get("fwver"),
            # A root node has no uplink to measure; an RE reporting an RSSI is on radio
            # backhaul, one reporting none has been given a cable.
            "link": "root" if lvl == 0 else ("wireless" if (r5 or r6) else "wired"),
            "rssi5": r5, "rssi6": r6,
            # Every BSSID this node serves clients on, so a host's reported association
            # can be matched back to it. Both the fronthaul (_fh) and base radio MACs are
            # included because which one a client sees varies by band and firmware.
            "fh": sorted({_norm_mac(b) for b in (
                n.get("ap2g_fh"), n.get("ap5g_fh"), n.get("ap6g_fh"),
                n.get("ap2g"), n.get("ap5g"), n.get("ap6g")) if b}),
        })
    out = {"nodes": nodes}
    # Which node every online client hangs off, keyed by IP: {ip: [node_mac, band, rssi]}.
    # This is what places the Macs (their own BSSID reads <redacted>) and anything else
    # that cannot report its association. band = ASUS isWL: 0 wired, 1 2.4G, 2 5G, 4 6G.
    try:
        cl = hook("get_clientlist()").get("get_clientlist", {})
        out["clients"] = {c["ip"]: [_norm_mac(c.get("amesh_papMac") or "") or None,
                                    int(c.get("isWL") or 0), int(c.get("rssi") or 0)]
                          for k, c in cl.items()
                          if isinstance(c, dict) and k.count(":") == 5 and c.get("ip")
                          and c.get("isOnline") == "1" and not c.get("amesh_isRe")}
    except Exception:
        pass
    try:
        out["ports"] = hook("get_wan_lan_status()").get("get_wan_lan_status", {}).get("portSpeed")
    except Exception:
        pass
    try:
        temp = _asus_temp(base, user, pw)
        if temp:
            out["temp"] = temp
            # Only the root reports, so hang it on that node rather than implying the
            # satellites are the same temperature.
            for n in nodes:
                if n["level"] == 0:
                    n["temp"] = temp.get("cpu")
    except Exception:
        pass
    return out

class NetProbe(threading.Thread):
    """Samples LAN/mesh health on its own slow cadence.

    Kept off the stats loop deliberately: a 20-packet probe takes ~4s, so probing five
    targets inline would stall every 3s sample. gather() just attaches the latest cached
    result, which may be up to `interval` seconds old — hence the `age` the UI shows."""

    def __init__(self, targets, interval, count, asus):
        super().__init__(daemon=True)
        self.targets, self.interval, self.count, self.asus = targets, interval, count, asus
        self.state = None

    def sample(self):
        probes = []
        for label, host in self.targets:
            p = _ping(host, self.count)
            p["name"] = label
            probes.append(p)
        st = {"probes": probes, "ts": time.time(), "count": self.count}
        if self.asus:
            try:
                mesh = _asus_mesh(*self.asus)
                if mesh:
                    st["mesh"] = mesh
            except Exception as e:
                st["mesh_error"] = str(e)[:120]
        return st

    def run(self):
        while True:
            t0 = time.time()
            try:
                self.state = self.sample()
            except Exception as e:
                self.state = {"error": str(e)[:160], "ts": time.time()}
            time.sleep(max(5.0, self.interval - (time.time() - t0)))

_wifi_cache = {"ts": 0.0, "val": None}

def _wifi_link_cached(ttl=30.0):
    """_wifi_link() behind a TTL — association almost never changes, and the stats loop
    runs every 3s; re-shelling out that often would be pure waste."""
    now = time.time()
    if now - _wifi_cache["ts"] > ttl:
        try:
            _wifi_cache["val"] = _wifi_link()
        except Exception:
            _wifi_cache["val"] = None
        _wifi_cache["ts"] = now
    return _wifi_cache["val"]

_probe = None   # set by main() when --netprobe is given

def parse_targets(spec):
    """'router=192.168.50.1,office=192.168.50.117' -> [(label, host), ...].
    A bare host is allowed and labels itself."""
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        label, _, host = item.partition("=")
        out.append((label, host) if host else (label, label))
    return out


# ---------------- system info + served models (slow cadence) ----------------
# What the box *is* (OS, kernel, chip, driver) and what it is *serving* (models behind any
# local OpenAI-compatible server). Neither changes on a 3s scale, and model discovery probes
# every listening port, so both run on their own thread like NetProbe and gather() attaches
# the cached result.

AGENT_VERSION = "2026.10.25"
AVAIL_MAX = 30   # pulled-but-idle model names sent per server

def _kv_file(path):
    """KEY="value" lines (os-release, dgx-release) -> dict."""
    out = {}
    try:
        for ln in open(path):
            k, sep, v = ln.strip().partition("=")
            if sep:
                out[k] = v.strip().strip('"')
    except Exception:
        pass
    return out

def _sysinfo():
    d = {"kernel": platform.release(), "arch": platform.machine(),
         "hostname": socket.gethostname(), "python": platform.python_version(),
         "agent": AGENT_VERSION}
    if SYS == "Linux":
        osr = _kv_file("/etc/os-release")
        d["os_name"] = osr.get("PRETTY_NAME") or "Linux"
        dgx = _kv_file("/etc/dgx-release")
        if dgx.get("DGX_SWBUILD_VERSION"):
            d["dgx_os"] = "DGX OS " + dgx["DGX_SWBUILD_VERSION"]
        try:
            model = open("/proc/device-tree/model").read().replace("\x00", "").strip()
            if model:
                d["model"] = model
        except Exception:
            pass
        # lscpu lists one "Model name" per cluster on big.LITTLE ARM (GB10: X925 + A725)
        names = re.findall(r"^Model name:\s*(.+)$", sh(["lscpu"], timeout=4), re.M)
        if names:
            d["cpu_model"] = " + ".join(dict.fromkeys(n.strip() for n in names))
        g = sh(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"]).strip()
        if g:
            p = [x.strip() for x in g.splitlines()[0].split(",")]
            d["gpu_name"] = p[0]
            if len(p) > 1:
                d["driver"] = p[1]
            m = re.search(r"CUDA Version:\s*([\d.]+)", sh(["nvidia-smi"]))
            if m:
                d["cuda"] = m.group(1)
    elif SYS == "Darwin":
        ver = sh(["sw_vers", "-productVersion"]).strip()
        build = sh(["sw_vers", "-buildVersion"]).strip()
        d["os_name"] = "macOS " + ver + (f" ({build})" if build else "")
        d["kernel"] = "Darwin " + platform.release()
        d["cpu_model"] = sh(["sysctl", "-n", "machdep.cpu.brand_string"]).strip() or None
        d["model"] = sh(["sysctl", "-n", "hw.model"]).strip() or None
        p, e = (sh(["sysctl", "-n", f"hw.perflevel{i}.physicalcpu"]).strip() for i in (0, 1))
        if p.isdigit():
            d["cores_pe"] = f"{p}P" + (f" + {e}E" if e.isdigit() else "")
        m = re.search(r'"gpu-core-count"\s*=\s*(\d+)',
                      sh(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"], timeout=6))
        if m:
            d["gpu_cores"] = int(m.group(1))
    dv = sh(["docker", "version", "--format", "{{.Server.Version}}"], timeout=4).strip()
    if dv and " " not in dv:
        d["docker"] = dv
    d.update(_tailscale())
    d["ifaces"] = _ifaces()
    return {k: v for k, v in d.items() if v}

def _thunderbolt():
    """Negotiated Thunderbolt/USB4 link to another Mac, from system_profiler:
    {link: Gb/s actually negotiated, max: best "Up to" any port on this Mac offers
    (120 = TB5, 40 = TB4/USB4), peer: the other Mac's model id, port: receptacle}.
    A link well under both ends' max is the cable — e.g. 20 Gb/s on a USB4-20G cable."""
    try:
        buses = json.loads(sh(["system_profiler", "-json", "SPThunderboltDataType"], timeout=10)
                           or "{}").get("SPThunderboltDataType", [])
    except ValueError:
        return None
    gbps = lambda s: int(m.group(1)) if (m := re.search(r"(\d+)\s*Gb/s", s or "")) else None
    out, best = {}, 0
    for b in buses:
        port = b.get("receptacle_1_tag") or {}
        sp = port.get("current_speed_key", "")
        if sp.startswith("Up to"):
            best = max(best, gbps(sp) or 0)
        elif port.get("receptacle_status_key") == "receptacle_connected":
            peer = next((x.get("device_name_key") for x in b.get("_items") or []
                         if any(s.get("_name") == "service_ip" for s in x.get("services_title") or [])), None)
            if peer and not out:
                out = {"link": gbps(sp), "peer": peer, "port": port.get("receptacle_id_key")}
    if best:
        out["max"] = best
    return out or None

_IF_SKIP = ("lo", "docker", "br-", "veth", "tailscale", "virbr", "utun", "awdl", "llw", "anpi", "ap1")

def _ifaces():
    """Every up interface with an IPv4 address: [{dev, kind, ip, prefix, speed, mac}].
    kind = wifi | ethernet | qsfp (ConnectX, the Spark ring) | thunderbolt (macOS bridge0).
    Link-local and tailnet addresses are left out; speed is Mbit/s where the OS says."""
    out = []
    if SYS == "Linux":
        for line in sh(["ip", "-4", "-o", "addr"]).splitlines():
            m = re.match(r"\d+:\s+(\S+)\s+inet\s+([\d.]+)/(\d+)", line)
            if not m or m.group(1).startswith(_IF_SKIP):
                continue
            dev, ip, pre = m.group(1), m.group(2), int(m.group(3))
            base = "/sys/class/net/" + dev
            drv = os.path.basename(os.path.realpath(base + "/device/driver")) if os.path.exists(base + "/device/driver") else ""
            kind = "wifi" if os.path.isdir(base + "/wireless") else "qsfp" if drv.startswith("mlx5") else "ethernet"
            try:
                speed = int(open(base + "/speed").read())
            except (OSError, ValueError):
                speed = None
            try:
                mac = open(base + "/address").read().strip()
            except OSError:
                mac = None
            out.append({"dev": dev, "kind": kind, "ip": ip, "prefix": pre,
                        "speed": speed if speed and speed > 0 else None, "mac": mac})
    elif SYS == "Darwin":
        ports = dict(re.findall(r"Hardware Port: (.+)\nDevice: (\S+)",
                                sh(["networksetup", "-listallhardwareports"])))
        ports = {v: k for k, v in ports.items()}
        for blk in re.split(r"\n(?=\S)", sh(["ifconfig"])):
            dev = blk.split(":", 1)[0]
            m = re.search(r"inet ([\d.]+) netmask 0x([0-9a-f]{8})", blk)
            if not m or dev.startswith(_IF_SKIP) or "status: inactive" in blk:
                continue
            name = ports.get(dev, "")
            kind = ("thunderbolt" if "Thunderbolt" in name else "wifi" if name == "Wi-Fi"
                    else "ethernet")
            sp = re.search(r"media: .*?\((\d+)(G?)base", blk)
            mac = re.search(r"ether ([0-9a-f:]+)", blk)
            out.append({"dev": dev, "kind": kind, "ip": m.group(1),
                        "prefix": bin(int(m.group(2), 16)).count("1"),
                        "speed": int(sp.group(1)) * (1000 if sp.group(2) else 1) if sp else None,
                        "mac": _norm_mac(mac.group(1)) if mac else None})
    if any(i["kind"] == "thunderbolt" for i in out):
        tb = _thunderbolt()
        for i in out:
            if i["kind"] == "thunderbolt" and tb:
                i["tb"] = tb
                if tb.get("link"):
                    i["speed"] = tb["link"] * 1000
    return [i for i in out if not i["ip"].startswith(("169.254.", "127."))
            and not (i["ip"].startswith("100.") and 64 <= int(i["ip"].split(".")[1]) <= 127)]

_TS_CLI = ["tailscale", "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale",
           "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]

def _tailscale():
    """Tailnet IPv4 + MagicDNS name. Asks the CLI first (launchd's PATH lacks /usr/local/bin,
    hence the explicit paths); falls back to any interface address in 100.64.0.0/10."""
    for cli in _TS_CLI:
        try:
            st = json.loads(sh([cli, "status", "--json", "--peers=false"], timeout=4) or "{}")
        except ValueError:
            continue
        me = st.get("Self") or {}
        ip = next((a for a in me.get("TailscaleIPs") or [] if "." in a), None)
        if ip:
            return {"ts_ip": ip, "ts_name": (me.get("DNSName") or "").rstrip(".") or None}
    out = sh(["ip", "-4", "-o", "addr"]) if SYS == "Linux" else sh(["ifconfig"])
    for a in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out):
        o = list(map(int, a.split(".")))
        if o[0] == 100 and 64 <= o[1] <= 127:
            return {"ts_ip": a}
    return {}

# Ports that are never an inference server, so not worth a probe.
_SKIP_PORTS = {22, 53, 111, 139, 445, 548, 631, 3389, 5000, 5353, 5900, 7000, 88}

def _listen_ports():
    """{port: connect-host} for every listening TCP socket on this box.

    Linux reads /proc/net/tcp{,6} (stdlib, no root, no `ss`); macOS has no procfs, so
    `lsof`. A socket bound only to one address (a gateway listening only on its Tailscale
    IP, not loopback) must be connected to on that address, so keep it."""
    out = {}
    if SYS == "Linux":
        for path, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
            try:
                rows = open(path).read().splitlines()[1:]
            except Exception:
                continue
            for r in rows:
                f = r.split()
                if len(f) < 4 or f[3] != "0A":          # 0A = LISTEN
                    continue
                addr, port = f[1].rsplit(":", 1)
                port = int(port, 16)
                if v6:
                    if addr != "0" * 32:                # only [::] — a specific v6 bind
                        continue                        # isn't reachable via 127.0.0.1
                    host = "127.0.0.1"
                elif addr in ("00000000", "0100007F"):
                    host = "127.0.0.1"
                else:
                    host = socket.inet_ntoa(bytes.fromhex(addr)[::-1])
                out.setdefault(port, host)
    elif SYS == "Darwin":
        for ln in sh(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fn"], timeout=6).splitlines():
            m = re.match(r"n(.+):(\d+)$", ln)
            if not m:
                continue
            addr, port = m.group(1), int(m.group(2))
            host = "127.0.0.1" if addr in ("*", "127.0.0.1", "[::1]", "[::]", "localhost") else addr
            if host.startswith("["):
                continue
            out.setdefault(port, host)
    return {p: h for p, h in out.items() if p not in _SKIP_PORTS and h != "127.0.0.54"}

def _http_json(url, timeout=1.0):
    """(json or None, Server header) — never raises."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            server = r.headers.get("Server") or ""
            body = r.read(400_000)
        return json.loads(body), server
    except Exception:
        return None, ""

def _http_text(url, timeout=1.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read(2_000_000).decode("utf-8", "replace")
    except Exception:
        return ""

_OWNER_ENGINE = {"vllm": "vLLM", "llamacpp": "llama.cpp", "splash": "Splash",
                 "organization_owner": "LM Studio", "library": "Ollama", "sglang": "SGLang",
                 "mlx-lm": "mlx-lm", "mlx_lm": "mlx-lm", "mlx-vlm": "mlx-vlm",
                 "lmstudio": "LM Studio", "tgi": "TGI", "spark-cluster": "cluster gateway"}
_GATEWAYS = {"cluster gateway"}   # routers in front of other boxes' engines, not engines

# Prometheus names → our keys. SGLang only exports these when started with --enable-metrics.
_ENGINE_METRICS = {
    "vllm": {"num_requests_running": "running", "num_requests_waiting": "waiting",
             "kv_cache_usage_perc": "kv", "gpu_cache_usage_perc": "kv",
             "generation_tokens_total": "gen_total", "prompt_tokens_total": "prompt_total"},
    "sglang": {"num_running_reqs": "running", "num_queue_reqs": "waiting", "token_usage": "kv",
               "generation_tokens_total": "gen_total", "prompt_tokens_total": "prompt_total"},
}

def _vllm_stats(base, prefix="vllm"):
    """Live load from vLLM's (or SGLang's) Prometheus /metrics: running/waiting requests,
    KV-cache use and cumulative token counters (the caller turns those into a rate between
    passes). `kv_conc` is vLLM's own figure for how many FULL-length requests fit in the KV
    cache — the memory-side ceiling, beside the scheduler's --max-num-seqs."""
    st, names = {}, _ENGINE_METRICS[prefix]
    for ln in _http_text(base + "/metrics", timeout=1.5).splitlines():
        if ln.startswith("#"):
            continue
        if ln.startswith("vllm:cache_config_info"):
            c = re.search(r'kv_cache_max_concurrency="([\d.]+)"', ln)
            if c:
                st["kv_conc"] = round(float(c.group(1)), 1)
            continue
        m = re.match(prefix + r":(\w+)(?:\{[^}]*\})?\s+([-\d.e+]+)$", ln)
        if not m:
            continue
        key = names.get(m.group(1))
        if key:
            st[key] = st.get(key, 0.0) + float(m.group(2))   # sum across engines / DP ranks
    if "kv" in st:
        st["kv"] = round(100 * min(1.0, st["kv"]), 1)
    return st

def _server_argv(port):
    """Command line of the vLLM/SGLang process serving `port`, or None. Containers' processes
    are visible in the host's /proc, so this covers docker and native launches alike; a server
    started without --port is vLLM's / SGLang's default (8000 / 30000)."""
    if SYS == "Linux":
        cmds = []
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    cmds.append(open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace").split("\0"))
                except OSError:
                    pass
    else:
        cmds = [ln.split() for ln in sh(["ps", "-axww", "-o", "args="]).splitlines()]
    for argv in cmds:
        argv = [x for a in argv for x in (a.split("=", 1) if a.startswith("--") and "=" in a else [a])]
        flat = " ".join(argv)
        if not re.search(r"\bvllm\b|sglang", flat) or "--max-num-seqs" not in flat and "--max-running-requests" not in flat:
            continue
        p = _arg(argv, "--port")
        if p == str(port) or (p is None and port == (30000 if "sglang" in flat else 8000)):
            return argv
    return None

def _probe_port(port, host):
    """One server's description, or None if this port isn't an OpenAI-compatible server."""
    base = f"http://{host}:{port}"
    j, server = _http_json(base + "/v1/models", timeout=0.8)
    if not isinstance(j, dict) or not isinstance(j.get("data"), (list, type(None))):
        return None
    data = j.get("data") or []
    owners = {str(m.get("owned_by", "")).lower() for m in data if isinstance(m, dict)}
    engine = next((_OWNER_ENGINE[o] for o in owners if o in _OWNER_ENGINE), None)
    ver = None
    if engine in (None, "Ollama"):
        v, _ = _http_json(base + "/api/version", timeout=0.8)
        if isinstance(v, dict) and v.get("version"):
            engine, ver = "Ollama", v["version"]
    if engine is None:
        engine = (server.split()[0].split("/")[0] if server and "uvicorn" not in server.lower()
                  else "OpenAI-compatible")
    srv = {"engine": engine, "port": port, "models": []}
    if engine in _GATEWAYS:
        srv["gateway"] = True
    if engine == "vLLM":
        v, _ = _http_json(base + "/version", timeout=0.8)
        if isinstance(v, dict):
            ver = v.get("version")
        srv["stats"] = _vllm_stats(base)
    elif engine == "SGLang":
        srv["stats"] = _vllm_stats(base, "sglang")
    if engine in ("vLLM", "SGLang"):
        # The scheduler's cap on parallel requests — "2 of 3 slots busy" says more about
        # headroom than GPU % does, which reads ~100% with one request decoding.
        argv = _server_argv(port)
        ms = _arg(argv, "--max-num-seqs", "--max-running-requests") if argv else None
        if ms and ms.isdigit():
            srv.setdefault("stats", {})["max_seqs"] = int(ms)
    if ver:
        srv["version"] = ver
    if engine == "Ollama":
        # /v1/models lists everything pulled; /api/ps is what is actually resident.
        ps, _ = _http_json(base + "/api/ps", timeout=0.8)
        loaded = {m.get("name"): m for m in (ps or {}).get("models") or [] if isinstance(m, dict)}
        for name, m in loaded.items():
            srv["models"].append({"id": name, "state": "loaded",
                                  "ctx": m.get("context_length"),
                                  "vram_mb": round((m.get("size_vram") or 0) / 2**20) or None})
        avail = [m.get("id") for m in data if isinstance(m, dict) and m.get("id") not in loaded]
        srv["available"] = sorted(a for a in avail if a)
        return srv
    if engine == "LM Studio":
        v0, _ = _http_json(base + "/api/v0/models", timeout=0.8)
        states = {m.get("id"): m for m in (v0 or {}).get("data") or [] if isinstance(m, dict)}
    else:
        states = {}
    for m in data:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        e = {"id": m["id"]}
        root = m.get("root") or m.get("repo")
        if root and root != m["id"]:
            e["root"] = root
        if isinstance(m.get("nodes"), list):
            e["nodes"] = m["nodes"]                 # gateway: which boxes back this model
        ctx = m.get("max_model_len") or m.get("context_length")
        if ctx:
            e["ctx"] = ctx
        lm = states.get(m["id"])
        if lm:
            e["state"] = lm.get("state")
            e["ctx"] = lm.get("loaded_context_length") or lm.get("max_context_length") or e.get("ctx")
        # llama-server in router mode lists every cached GGUF and marks each with
        # status.value = loaded|loading|sleeping|unloaded|downloaded|downloading
        st = m.get("status")
        st = st.get("value") if isinstance(st, dict) else st
        if isinstance(st, str) and st:
            e["state"] = st
        if m.get("vision") or "vision" in (m.get("input_modalities") or []):
            e["vision"] = True
        srv["models"].append(e)
    # LM Studio and a llama.cpp router can each list dozens of on-disk models: keep only what
    # is resident or loading, and report the rest as `available` (a count on the card).
    idle = lambda x: x.get("state") not in (None, "loaded", "loading", "sleeping")
    if any(idle(x) for x in srv["models"]):
        srv["available"] = sorted(x["id"] for x in srv["models"] if idle(x))
        srv["models"] = [x for x in srv["models"] if not idle(x)]
    return srv

def _served_models():
    """Every OpenAI-compatible server on this box, found by probing its listening ports.

    Discovery beats a fixed port list: one box's vLLM is on 8001 inside a container, another's
    on 8000, a Mac's Splash on 8100 and Ollama/LM Studio on their defaults — and the
    next experiment will pick yet another. Probes are concurrent so a port that accepts and
    never answers costs one timeout, not one per port."""
    ports = _listen_ports()
    res = {}
    def work(p, h):
        try:
            r = _probe_port(p, h)
            if r:
                res[p] = r
        except Exception:
            pass
    ts = [threading.Thread(target=work, args=(p, h), daemon=True) for p, h in ports.items()]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    for svc in _local_services():
        p = svc["port"]
        if p in res:   # already answered /v1/models: just say which container it is
            res[p].setdefault("container", svc.get("container"))
        elif p not in ports or svc["engine"] not in ("vLLM", "SGLang"):
            # a listening audio server (no /v1/models), or a model server whose container
            # is up but whose port is not open yet — i.e. still loading weights
            if p not in ports and not svc.get("worker"):
                svc["state"] = "loading"
                for m in svc["models"]:
                    m["state"] = "loading"
            res[p] = svc
    return [res[p] for p in sorted(res)]

# Audio servers in the Spark cluster speak only /health + their own POST routes, so the
# /v1/models probe cannot see them; the gateway that fronts them lists the WHOLE cluster's
# menu on every box. What a box really runs is its containers and native servers, whose
# command lines name the model — that is what this reads.
_UVICORN_APPS = {"kokoro_server": ("Kokoro TTS", "kokoro"), "chatterbox_server": ("Chatterbox TTS", "chatterbox"),
                 "diarize_server": ("NeMo diarize", "nemotron-3-diarization")}

def _arg(argv, *flags):
    for f in flags:
        if f in argv and argv.index(f) + 1 < len(argv):
            return argv[argv.index(f) + 1]
    return None

def _local_services():
    out = []
    if SYS != "Linux":
        return out
    # native whisper.cpp servers (own processes, so ss can name them)
    for line in sh(["ss", "-ltnpH"]).splitlines():
        m = re.search(r":(\d+)\s.*\(\(\"whisper-server\",pid=(\d+)", line)
        if not m:
            continue
        try:
            argv = open(f"/proc/{m.group(2)}/cmdline").read().split("\0")
        except OSError:
            continue
        model = os.path.basename(_arg(argv, "-m", "--model") or "whisper")
        model = "whisper-" + re.sub(r"^ggml-|\.bin$", "", model)
        out.append({"engine": "whisper.cpp", "port": int(m.group(1)), "models": [{"id": model}]})
    ids = sh(["docker", "ps", "-q"], timeout=4).split()
    if not ids:
        return out
    try:
        cs = json.loads(sh(["docker", "inspect"] + ids, timeout=6) or "[]")
    except ValueError:
        return out
    for c in cs:
        argv = (c.get("Config") or {}).get("Cmd") or []
        flat = " ".join(argv)
        name = (c.get("Name") or "").lstrip("/")
        port = _arg(" ".join(argv).split(), "--port")
        if not port or not port.isdigit():
            # A tensor-parallel WORKER shard opens no API port: it joins a head over the
            # fabric (e.g. the second half of a TP-2 run: `… 1 2 10.99.23.2 29620`). Show it as a shard
            # of that model, or the box looks idle while its GPU is full.
            hf = next((re.search(r"models--([^/]+?)--([^/]+)", a) for a in argv if "models--" in a), None)
            ip = next((i for i, a in enumerate(argv) if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", a or "")), None)
            if hf and ip is not None and ip >= 2 and argv[ip - 2].isdigit() and argv[ip - 1].isdigit():
                out.append({"engine": "TensorFold" if "tf-" in flat or "tensorfold" in flat else "worker",
                            "port": -len(out) - 1, "container": name, "worker": True,
                            "head": argv[ip], "rank": f"{argv[ip - 2]}/{argv[ip - 1]}",
                            "models": [{"id": hf.group(2), "root": hf.group(1) + "/" + hf.group(2),
                                        "state": "worker"}]})
            continue
        svc = {"port": int(port), "container": name, "image": (c.get("Config") or {}).get("Image")}
        app = next((k for k in _UVICORN_APPS if k in flat), None)
        if app:
            svc["engine"], mid = _UVICORN_APPS[app]
            svc["models"] = [{"id": mid}]
        else:
            served = _arg(argv, "--served-model-name", "--name")
            path = next((a for a in argv if a and not a.startswith("-") and ("/" in a)), None)
            if not served and not path:
                continue
            svc["engine"] = ("SGLang" if "sglang" in flat else "TensorFold" if "tensorfold" in flat
                             else "vLLM")
            ctx = _arg(argv, "--max-model-len", "--context", "--context-length")
            svc["models"] = [{"id": served or os.path.basename(path.rstrip("/")),
                              "root": path, "ctx": int(ctx) if ctx and ctx.isdigit() else None}]
        out.append(svc)
    return out

class InfoProbe(threading.Thread):
    """System info every `sys_interval`, served models every `interval` (see above)."""

    def __init__(self, interval=30.0, sys_interval=600.0):
        super().__init__(daemon=True)
        self.interval, self.sys_interval = interval, sys_interval
        self.sysinfo, self.models, self._sys_ts, self._tok = None, None, 0.0, {}

    def sample(self):
        now = time.time()
        if now - self._sys_ts > self.sys_interval:
            try:
                self.sysinfo = _sysinfo()
            except Exception:
                pass
            self._sys_ts = now
        servers = _served_models()
        for s in servers:
            st = s.get("stats")
            if not st or "gen_total" not in st:
                continue
            # tokens/s between passes; a counter reset (restart) just skips one pass
            prev = self._tok.get(s["port"])
            if prev and now > prev[0] and st["gen_total"] >= prev[1]:
                dt = now - prev[0]
                st["gen_tps"] = round((st["gen_total"] - prev[1]) / dt, 1)
                st["prompt_tps"] = round((st.get("prompt_total", 0) - prev[2]) / dt, 1)
            self._tok[s["port"]] = (now, st["gen_total"], st.get("prompt_total", 0))
            for k in ("gen_total", "prompt_total"):
                st.pop(k, None)
        self.models = servers

    def run(self):
        while True:
            t0 = time.time()
            try:
                self.sample()
            except Exception:
                pass
            time.sleep(max(5.0, self.interval - (time.time() - t0)))

_info = None   # InfoProbe, set by main()


def _uptime():
    """Seconds since boot. /proc/uptime on Linux; on macOS derive it from kern.boottime
    (`{ sec = 1755000000, usec = 123 } …`) since there is no uptime counter to read."""
    try:
        if SYS == "Linux":
            return float(open("/proc/uptime").read().split()[0])
        if SYS == "Darwin":
            m = re.search(r"sec\s*=\s*(\d+)", sh(["sysctl", "-n", "kern.boottime"]))
            if m:
                return max(0.0, time.time() - int(m.group(1)))
    except Exception:
        pass
    return None


# ---------------- power control (shutdown / restart) ----------------
# The collector cannot reach this host (push-only architecture), so a power command rides
# back on the response to our own /report POST. Two kinds:
#   - "os":   reboot/shutdown THIS box. The operator types the box's root/sudo password.
#   - "plug": cut or power-cycle ANOTHER box via its Meross smart plug, using plug.py. Only
#             agents started with --plug-dir are relays; the collector routes a plug command
#             to a live relay that is NOT the target (a box can't power-cycle itself). Running
#             it is deliberately root-only, so the relay's root password is required too.
# Every password is used transiently to authenticate `sudo` and is never stored or logged.

_plug_dir = ""            # set by main() when --plug-dir is given → this agent is a plug relay
_last_cmd_result = None   # {"action","ok","error","target"?,"ts"} for the next report (<90s)
_handled_ids = set()      # command ids already dispatched, so a re-delivered cmd runs once

def _sudo_ok(pw):
    """True if `pw` is this host's sudo password. -k ignores any cached credential so the
    supplied password is actually tested; -p '' silences the prompt; stdin, never the cmdline."""
    try:
        return subprocess.run(["sudo", "-S", "-k", "-p", "", "true"], input=pw + "\n",
                              capture_output=True, text=True, timeout=12).returncode == 0
    except Exception:
        return False

def _run_power_cmd(cmd):
    """Reboot/shutdown THIS box. `shutdown -r now` / `shutdown -h now` are the same on Linux
    and macOS. Returns a result dict (no password in it)."""
    action = cmd.get("action")
    pw = cmd.get("pw") or ""
    res = {"id": cmd.get("id"), "action": action, "ts": time.time(), "ok": False}
    if action not in ("reboot", "shutdown"):
        res["error"] = "unknown action"; return res
    if not pw:
        res["error"] = "no password supplied"; return res
    if not _sudo_ok(pw):
        res["error"] = "wrong password or no sudo access"; return res
    flag = "-r" if action == "reboot" else "-h"
    try:
        p = subprocess.run(["sudo", "-S", "-p", "", "shutdown", flag, "now"],
                           input=pw + "\n", capture_output=True, text=True, timeout=15)
        res["ok"] = p.returncode == 0
        if p.returncode != 0:
            res["error"] = (p.stderr or p.stdout or "shutdown failed").strip()[:120]
    except Exception as e:
        res["error"] = str(e)[:120]
    # On success the box is going down and this process dies with it; the result mainly
    # matters on failure, where the box stays up and reports it on the next cycle.
    return res

def _plug_names():
    """Plug names this relay can switch (keys of plugs.json next to plug.py), or None."""
    if not _plug_dir:
        return None
    try:
        reg = json.load(open(os.path.join(_plug_dir, "plugs.json")))
        names = list((reg.get("plugs") or {}).keys())
        if names and os.path.exists(os.path.join(_plug_dir, "plug.py")):
            return names
    except Exception:
        pass
    return None

def _run_plug_cmd(cmd):
    """Switch another box's plug via `plug.py off` (shutdown) / `plug.py cycle` (restart).
    Root-only: the relay's root password is validated, then plug.py is run under sudo. The
    result names the TARGET box so the collector shows it on the right card."""
    op, plug = cmd.get("op"), cmd.get("plug")
    pw = cmd.get("pw") or ""
    res = {"id": cmd.get("id"), "action": "hw_shutdown" if op == "off" else "hw_restart",
           "target": cmd.get("target"), "ts": time.time(), "ok": False}
    if op not in ("off", "cycle") or not plug:
        res["error"] = "bad plug command"; return res
    if not _plug_dir:
        res["error"] = "not a plug relay"; return res
    plug_py = os.path.join(_plug_dir, "plug.py")
    if not os.path.exists(plug_py):
        res["error"] = "plug.py missing on relay"; return res
    if not pw:
        res["error"] = "no password supplied"; return res
    if not _sudo_ok(pw):
        res["error"] = "wrong password or no sudo access on relay"; return res
    try:
        p = subprocess.run(["sudo", "-S", "-p", "", sys.executable, plug_py, op, "-n", plug],
                           input=pw + "\n", capture_output=True, text=True,
                           cwd=_plug_dir, timeout=120)
        res["ok"] = p.returncode == 0
        tail = (p.stdout or "").strip().splitlines()
        if p.returncode != 0:
            res["error"] = (p.stderr or p.stdout or "plug command failed").strip()[:140]
        elif tail:
            res["detail"] = tail[-1][:140]
    except Exception as e:
        res["error"] = str(e)[:140]
    return res

def _dispatch_cmd(cmd):
    """Run one queued command on a worker thread (plug `cycle` can take ~75s; never block
    the report loop), recording its result. Idempotent on the command id."""
    cid = cmd.get("id")
    if cid is not None:
        if cid in _handled_ids:
            return
        _handled_ids.add(cid)
    def work():
        global _last_cmd_result
        _last_cmd_result = _run_plug_cmd(cmd) if cmd.get("kind") == "plug" else _run_power_cmd(cmd)
    threading.Thread(target=work, daemon=True).start()

def _handle_response(raw):
    """Act on the collector's reply to /report: a list of queued commands under "cmds"
    (or a single "cmd"). Anything else (the plain "ok") is ignored."""
    if not raw:
        return
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return
    if not isinstance(obj, dict):
        return
    cmds = obj.get("cmds")
    if not cmds and isinstance(obj.get("cmd"), dict):
        cmds = [obj["cmd"]]
    for c in cmds or []:
        if isinstance(c, dict):
            _dispatch_cmd(c)


def gather():
    if SYS == "Linux":
        d = gather_linux()
    elif SYS == "Darwin":
        d = gather_darwin()
    else:
        d = {"os": SYS, "cpu": 0, "ncpu": os.cpu_count(), "procs": []}
    up = _uptime()
    if up is not None:
        d["uptime"] = round(up)
    ip = _local_ip()
    if ip:
        d["ip"] = ip
    w = _wifi_link_cached()
    if w:
        d["wifi"] = w
    if _probe is not None and _probe.state:
        d["net"] = _probe.state
    if _info is not None:
        if _info.sysinfo:
            d["sys"] = _info.sysinfo
        if _info.models is not None:
            # drop an idle Ollama/LM Studio with nothing pulled — it serves nothing
            # and cap the pulled-but-not-loaded list (LM Studio can hold 140+): it rides on
            # every 3s report and every 2.5s dashboard poll, so send the count plus a sample.
            d["models"] = [{**s, "available": s["available"][:AVAIL_MAX],
                            "available_n": len(s["available"])} if s.get("available") else s
                           for s in _info.models if s["models"] or s.get("available")]
    plugs = _plug_names()
    if plugs:
        d["plug_relay"] = {"plugs": plugs}   # advertise which plugs this relay can switch
    return d

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://localhost:8787")
    ap.add_argument("--name", default=socket.gethostname().split(".")[0])
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--token", default="")
    ap.add_argument("--once", action="store_true", help="print one sample and exit")
    ap.add_argument("--netprobe", default="",
                    help="comma-separated label=host targets to ping, e.g. "
                         "'router=192.168.50.1,office=192.168.50.117'")
    ap.add_argument("--netprobe-interval", type=float, default=30.0)
    ap.add_argument("--netprobe-count", type=int, default=20,
                    help="pings per target per pass (more = finer loss resolution)")
    ap.add_argument("--asus", default="",
                    help="ASUS router base URL for read-only AiMesh backhaul stats; "
                         "credentials come from ASUS_USER / ASUS_PASS in the environment")
    ap.add_argument("--plug-dir", default="",
                    help="path to the power-plugs directory (plug.py + plugs.json + key) to act "
                         "as a hardware plug relay — lets this host cut/cycle other boxes' plugs")
    ap.add_argument("--models-interval", type=float, default=30.0,
                    help="seconds between served-model discovery passes")
    a = ap.parse_args()

    global _probe, _plug_dir, _info
    _info = InfoProbe(a.models_interval)
    if a.once:
        _info.sample()                          # --once must show sys + models too
    else:
        _info.start()
    if a.plug_dir:
        _plug_dir = os.path.abspath(os.path.expanduser(a.plug_dir))
    if a.netprobe:
        # Creds stay in the environment so they never reach a process list or a unit file.
        user, pw = os.getenv("ASUS_USER"), os.getenv("ASUS_PASS")
        asus = (a.asus, user, pw) if (a.asus and user and pw) else None
        _probe = NetProbe(parse_targets(a.netprobe), a.netprobe_interval,
                          a.netprobe_count, asus)
        if a.once:
            _probe.state = _probe.sample()      # --once must not return an empty probe
        else:
            _probe.start()
    if a.once:
        print(json.dumps({"name": a.name, **gather()}, indent=2)); return
    url = a.server.rstrip("/") + "/report" + (f"?token={a.token}" if a.token else "")
    print(f"NOC agent '{a.name}' → {url}  every {a.interval}s", flush=True)
    while True:
        t0 = time.time()
        try:
            payload = {"name": a.name, **gather()}
            if _last_cmd_result and time.time() - _last_cmd_result.get("ts", 0) < 90:
                payload["cmd_result"] = {k: _last_cmd_result[k]
                                         for k in ("action", "ok", "error", "target", "detail", "id")
                                         if k in _last_cmd_result}
            body = json.dumps(payload).encode()
            resp = urllib.request.urlopen(urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}), timeout=8).read()
            _handle_response(resp)   # may carry a queued shutdown/restart command
        except Exception as e:
            print("report failed:", str(e)[:100], flush=True)
        time.sleep(max(0.5, a.interval - (time.time() - t0)))

if __name__ == "__main__":
    main()
