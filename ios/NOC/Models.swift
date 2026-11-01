import Foundation

// Mirrors of the collector's JSON (`/metrics`, `/api/admin/net`). Every field is optional and
// decoded leniently: agents in the fleet run different versions (an agent is updated by hand, host
// by host, so old ones linger), and one unexpected type must never blank the whole fleet.

extension KeyedDecodingContainer {
    func opt<T: Decodable>(_ key: Key) -> T? { (try? decodeIfPresent(T.self, forKey: key)) ?? nil }
}

struct AnyKey: CodingKey {
    var stringValue: String
    var intValue: Int? { nil }
    init(_ s: String) { stringValue = s }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { nil }
}

struct MetricsResponse: Decodable, Sendable {
    var t: String?
    var me: String?
    var admin: Bool
    var hosts: [String: Machine]

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        t = c.opt(AnyKey("t"))
        me = c.opt(AnyKey("me"))
        admin = c.opt(AnyKey("admin")) ?? false
        // decode host by host so one bad payload drops one card, not the fleet
        var out: [String: Machine] = [:]
        if let hc = try? c.nestedContainer(keyedBy: AnyKey.self, forKey: AnyKey("hosts")) {
            for k in hc.allKeys {
                if let h = try? hc.decode(Machine.self, forKey: k) {
                    var h = h; h.name = k.stringValue; out[k.stringValue] = h
                }
            }
        }
        hosts = out
    }
}

struct Proc: Decodable, Sendable, Hashable {
    var cpu: Double = 0, mem: Double = 0, cmd: String = ""
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        cpu = c.opt(AnyKey("cpu")) ?? 0; mem = c.opt(AnyKey("mem")) ?? 0
        cmd = c.opt(AnyKey("cmd")) ?? ""
    }
}

struct WifiLink: Decodable, Sendable {
    var dev: String?, bssid: String?, ssid: String?, signal: Double?, freq: Double?
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        dev = c.opt(AnyKey("dev")); bssid = c.opt(AnyKey("bssid")); ssid = c.opt(AnyKey("ssid"))
        signal = c.opt(AnyKey("signal")); freq = c.opt(AnyKey("freq"))
    }
}

struct CmdResult: Decodable, Sendable {
    var action: String?, ok: Bool, error: String?, detail: String?
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        action = c.opt(AnyKey("action")); ok = c.opt(AnyKey("ok")) ?? false
        error = c.opt(AnyKey("error")); detail = c.opt(AnyKey("detail"))
    }
    var label: String {
        let names = ["reboot": "Restart", "shutdown": "Shutdown",
                     "hw_restart": "Hardware restart", "hw_shutdown": "Hardware shutdown"]
        let base = (names[action ?? ""] ?? action ?? "Command") + (ok ? " issued ✓" : " failed")
        if let e = error { return base + ": " + e }
        if let d = detail { return base + ": " + d }
        return base
    }
}

/// Static facts about the box (agent InfoProbe, refreshed every 10 min).
struct SysInfo: Decodable, Sendable {
    var fields: [String: String] = [:]
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        for k in c.allKeys {
            if let s: String = c.opt(k) { fields[k.stringValue] = s }
            else if let i: Int = c.opt(k) { fields[k.stringValue] = String(i) }
        }
    }
    subscript(_ k: String) -> String? { fields[k] }
}

struct ServedModel: Decodable, Sendable, Hashable {
    var id: String
    var root: String?
    var ctx: Int?
    var state: String?
    var vision: Bool
    var vramMB: Int?
    var nodes: [String]
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        id = c.opt(AnyKey("id")) ?? "?"
        root = c.opt(AnyKey("root"))
        ctx = c.opt(AnyKey("ctx"))
        state = c.opt(AnyKey("state"))
        vision = c.opt(AnyKey("vision")) ?? false
        vramMB = c.opt(AnyKey("vram_mb"))
        nodes = c.opt(AnyKey("nodes")) ?? []
    }
}

struct EngineStats: Decodable, Sendable, Hashable {
    var running: Double?, waiting: Double?, kv: Double?, genTps: Double?, promptTps: Double?
    /// scheduler cap on parallel requests (--max-num-seqs); full-length requests the KV cache holds
    var maxSeqs: Int?, kvConc: Double?
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        running = c.opt(AnyKey("running")); waiting = c.opt(AnyKey("waiting"))
        kv = c.opt(AnyKey("kv")); genTps = c.opt(AnyKey("gen_tps"))
        promptTps = c.opt(AnyKey("prompt_tps"))
        maxSeqs = c.opt(AnyKey("max_seqs")); kvConc = c.opt(AnyKey("kv_conc"))
    }
}

/// One inference server on a box (vLLM, Ollama, LM Studio, Splash, a gateway…).
struct ModelServer: Decodable, Sendable, Hashable {
    var engine: String
    var version: String?
    var port: Int
    var gateway: Bool
    var models: [ServedModel]
    var available: [String]
    var availableN: Int
    var stats: EngineStats?
    var container: String?
    var worker: Bool, head: String?, rank: String?
    /// ":8001", or for a tensor-parallel worker shard (no API port) "shard 1/2 → 10.99.23.2".
    var where_: String { worker ? "shard \(rank ?? "?") → \(head ?? "?")" : ":" + String(port) }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        container = c.opt(AnyKey("container"))
        worker = c.opt(AnyKey("worker")) ?? false; head = c.opt(AnyKey("head")); rank = c.opt(AnyKey("rank"))
        engine = c.opt(AnyKey("engine")) ?? "server"
        version = c.opt(AnyKey("version"))
        port = c.opt(AnyKey("port")) ?? 0
        gateway = c.opt(AnyKey("gateway")) ?? false
        models = c.opt(AnyKey("models")) ?? []
        available = c.opt(AnyKey("available")) ?? []
        availableN = c.opt(AnyKey("available_n")) ?? available.count
        stats = c.opt(AnyKey("stats"))
    }
    var title: String { engine + (shortVersion.map { " " + $0 } ?? "") }
    /// A source build reports a setuptools-scm version (0.1.dev21830+g8244d1c44.d20261015):
    /// show "dev 8244d1c". A release keeps its number, minus any +local suffix.
    var shortVersion: String? {
        guard let v = version else { return nil }
        if v.contains(".dev"), let g = v.range(of: "+g") {
            return "dev " + v[g.upperBound...].prefix(7)
        }
        return String(v.split(separator: "+").first ?? Substring(v))
    }
}

enum HostKind { case apple, nvidia, rpi, plain }

struct Machine: Decodable, Sendable, Identifiable {
    var name: String = ""
    var id: String { name }
    var os: String?
    var gpu: String?
    var board: String?
    var cpu: Double?
    var cpuCores: [Double]
    var ncpu: Int?
    var load: [Double]
    var memTotal: Double?, memUsed: Double?, memAvail: Double?
    var gpuUtil: Double?, gpuPower: Double?, gpuTemp: Double?, gpuClk: Double?
    var gpuPowerMax: Double?, gpuMemUsed: Double?
    var cpuTemp: Double?, fanRPM: Double?
    var thermal: String?, thermalDetail: String?
    var uptime: Double?
    var ip: String?
    var procs: [Proc]
    var containers: String?
    var wifi: WifiLink?
    var sys: SysInfo?
    var models: [ModelServer]
    var age: Double
    var owner: String?
    var shared: Bool
    var hwPower: Bool
    var hist: [[Double]]
    var cmdResult: CmdResult?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        func k(_ s: String) -> AnyKey { AnyKey(s) }
        os = c.opt(k("os")); gpu = c.opt(k("gpu")); board = c.opt(k("board"))
        cpu = c.opt(k("cpu")); cpuCores = c.opt(k("cpu_cores")) ?? []; ncpu = c.opt(k("ncpu"))
        load = c.opt(k("load")) ?? []
        memTotal = c.opt(k("mem_total")); memUsed = c.opt(k("mem_used")); memAvail = c.opt(k("mem_avail"))
        gpuUtil = c.opt(k("gpu_util")); gpuPower = c.opt(k("gpu_power")); gpuTemp = c.opt(k("gpu_temp"))
        gpuClk = c.opt(k("gpu_clk")); gpuPowerMax = c.opt(k("gpu_power_max"))
        gpuMemUsed = c.opt(k("gpu_mem_used"))
        cpuTemp = c.opt(k("cpu_temp")); fanRPM = c.opt(k("fan_rpm"))
        thermal = c.opt(k("thermal")); thermalDetail = c.opt(k("thermal_detail"))
        uptime = c.opt(k("uptime")); ip = c.opt(k("ip"))
        procs = c.opt(k("procs")) ?? []
        containers = c.opt(k("containers"))
        wifi = c.opt(k("wifi")); sys = c.opt(k("sys"))
        models = c.opt(k("models")) ?? []
        age = c.opt(k("age")) ?? 0
        owner = c.opt(k("owner")); shared = c.opt(k("shared")) ?? false
        hwPower = c.opt(k("hw_power")) ?? false
        hist = c.opt(k("hist")) ?? []
        cmdResult = c.opt(k("cmd_result"))
    }

    var kind: HostKind {
        gpu == "Apple" ? .apple : gpu == "NVIDIA" ? .nvidia : board == "rpi" ? .rpi : .plain
    }
    var stale: Bool { age > 12 }
    var lagging: Bool { age > 7 }
    /// Headline temperature: GPU die, else CPU die / SoC — same rule as the web card.
    var temp: Double? { gpuTemp ?? cpuTemp }
    var memPct: Double? {
        guard let t = memTotal, t > 0, let a = memAvail else { return nil }
        return 100 * (t - a) / t
    }
    var memUsedGB: Double { ((memTotal ?? 0) - (memAvail ?? 0)) / 1024 }
    var memTotalGB: Double { (memTotal ?? 0) / 1024 }
    /// GB10 exposes no power.limit, so scale to a nominal TDP: 140 W NVIDIA, 80 W Apple.
    var powerMax: Double { gpuPowerMax ?? (gpu == "Apple" ? 80 : 140) }
    var throttle: String? { (thermal != nil && thermal != "Nominal") ? thermal : nil }
    /// The value the sparkline tracks: GPU util, or CPU on GPU-less hosts.
    var headline: Double? { gpuUtil ?? cpu }
    var containerList: [String] {
        (containers ?? "").split(separator: ",").map(String.init).filter { !$0.isEmpty }
    }
    /// Engines actually serving something (gateways excluded — they front other boxes).
    /// Servers that serve something from this box — not the cluster gateway's menu, not an
    /// idle Ollama/LM Studio with nothing loaded.
    var servingServers: [ModelServer] { models.filter { !$0.gateway && !$0.models.isEmpty } }
    /// Requests in flight against the engines' --max-num-seqs, summed over this box. "2 of 3
    /// slots busy" is real headroom, where GPU % reads ~100 with a single request decoding.
    var requests: (running: Double, max: Int, waiting: Double)? {
        let ss = models.filter { !$0.gateway && $0.stats?.maxSeqs != nil }
        guard !ss.isEmpty else { return nil }
        return (ss.reduce(0) { $0 + ($1.stats?.running ?? 0) }, ss.reduce(0) { $0 + ($1.stats?.maxSeqs ?? 0) },
                ss.reduce(0) { $0 + ($1.stats?.waiting ?? 0) })
    }
    var loadedModels: [(ModelServer, ServedModel)] {
        models.filter { !$0.gateway }.flatMap { s in s.models.map { (s, $0) } }
    }
}

// MARK: - admin network payload

struct NetPayload: Decodable, Sendable {
    var t: String?
    var machines: [NetMachine]
    var probes: [NetProbe]
    var mesh: Mesh?
    var meshFrom: String?
    var topo: Topo?
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        t = c.opt(AnyKey("t")); machines = c.opt(AnyKey("machines")) ?? []
        probes = c.opt(AnyKey("probes")) ?? []; mesh = c.opt(AnyKey("mesh"))
        meshFrom = c.opt(AnyKey("mesh_from")); topo = c.opt(AnyKey("topo"))
    }
}

/// The collector's placement (`_topology()` in noc_web.py) — computed once server-side so the
/// web page and the app draw the same picture.
struct Topo: Decodable, Sendable {
    struct Place: Decodable, Sendable {
        var node: String?, via: String?, band: String?, rssi: Int?
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyKey.self)
            node = c.opt(AnyKey("node")); via = c.opt(AnyKey("via"))
            band = c.opt(AnyKey("band")); rssi = c.opt(AnyKey("rssi"))
        }
    }
    struct WanHost: Decodable, Sendable, Identifiable {
        var name: String, ip: String?, kind: String?, speed: Int?
        var id: String { name }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyKey.self)
            name = c.opt(AnyKey("name")) ?? "?"; ip = c.opt(AnyKey("ip"))
            kind = c.opt(AnyKey("kind")); speed = c.opt(AnyKey("speed"))
        }
    }
    struct Link: Decodable, Sendable, Identifiable {
        var net: String, members: [String], kind: String, speed: Int?, note: String?
        var id: String { net }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyKey.self)
            net = c.opt(AnyKey("net")) ?? "?"; members = c.opt(AnyKey("members")) ?? []
            kind = c.opt(AnyKey("kind")) ?? "ethernet"; speed = c.opt(AnyKey("speed"))
            note = c.opt(AnyKey("note"))
        }
        var label: String {
            let sp = speed.map { $0 >= 1000 ? "\($0 / 1000)G " : "\($0)M " } ?? ""
            return sp + (kind == "qsfp" ? "QSFP" : kind == "thunderbolt" ? "Thunderbolt" : "Ethernet")
        }
    }
    var placed: [String: Place], wan: [WanHost], wanLAN: String?, unplaced: [String], remote: [[String]]
    var links: [Link], others: [String: Int]
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        placed = c.opt(AnyKey("placed")) ?? [:]; wan = c.opt(AnyKey("wan")) ?? []; wanLAN = c.opt(AnyKey("wan_lan"))
        unplaced = c.opt(AnyKey("unplaced")) ?? []; remote = c.opt(AnyKey("remote")) ?? []
        links = c.opt(AnyKey("links")) ?? []; others = c.opt(AnyKey("others")) ?? [:]
    }
    func kids(of nodeIP: String?) -> [(name: String, place: Place)] {
        placed.filter { $0.value.node == nodeIP }.map { ($0.key, $0.value) }.sorted { $0.name < $1.name }
    }
}

struct NetMachine: Decodable, Sendable, Identifiable {
    var name: String, ip: String?, tsIP: String?, os: String?, board: String?, gpu: String?, age: Double
    var wifi: WifiLink?
    var id: String { name }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        name = c.opt(AnyKey("name")) ?? "?"; ip = c.opt(AnyKey("ip")); tsIP = c.opt(AnyKey("ts_ip")); os = c.opt(AnyKey("os"))
        board = c.opt(AnyKey("board")); gpu = c.opt(AnyKey("gpu")); age = c.opt(AnyKey("age")) ?? 0
        wifi = c.opt(AnyKey("wifi"))
    }
}

struct ProbeTarget: Decodable, Sendable, Identifiable {
    var name: String, host: String, loss: Double, avg: Double?, max: Double?, jitter: Double?
    var id: String { name }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        name = c.opt(AnyKey("name")) ?? "?"; host = c.opt(AnyKey("host")) ?? ""
        loss = c.opt(AnyKey("loss")) ?? 100; avg = c.opt(AnyKey("avg")); max = c.opt(AnyKey("max"))
        jitter = c.opt(AnyKey("jitter"))
    }
}

struct ProbeWindow: Decodable, Sendable {
    var worst: Double, mean: Double, n: Int, bad: Int
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        worst = c.opt(AnyKey("worst")) ?? 0; mean = c.opt(AnyKey("mean")) ?? 0
        n = c.opt(AnyKey("n")) ?? 0; bad = c.opt(AnyKey("bad")) ?? 0
    }
}

struct NetProbe: Decodable, Sendable, Identifiable {
    var from: String, count: Int?, age: Double, targets: [ProbeTarget]
    var window: [String: ProbeWindow], windowMin: Int
    var id: String { from }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        from = c.opt(AnyKey("from")) ?? "?"; count = c.opt(AnyKey("count"))
        age = c.opt(AnyKey("age")) ?? 0; targets = c.opt(AnyKey("targets")) ?? []
        window = c.opt(AnyKey("window")) ?? [:]; windowMin = c.opt(AnyKey("window_min")) ?? 30
    }
}

struct MeshNode: Decodable, Sendable, Identifiable {
    var name: String, ip: String?, mac: String?, model: String?, online: Bool, level: Int
    var link: String, rssi5: Int?, rssi6: Int?, fh: [String], temp: Double?, fw: String?
    var id: String { (mac ?? "") + name }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        name = c.opt(AnyKey("name")) ?? "node"; ip = c.opt(AnyKey("ip")); mac = c.opt(AnyKey("mac"))
        model = c.opt(AnyKey("model")); online = c.opt(AnyKey("online")) ?? false
        level = c.opt(AnyKey("level")) ?? 0; link = c.opt(AnyKey("link")) ?? "?"
        rssi5 = c.opt(AnyKey("rssi5")); rssi6 = c.opt(AnyKey("rssi6"))
        fh = c.opt(AnyKey("fh")) ?? []; temp = c.opt(AnyKey("temp")); fw = c.opt(AnyKey("fw"))
    }
    var bestRSSI: Int? {
        let v = [rssi5, rssi6].compactMap { $0 }
        return v.max()
    }
}

struct Mesh: Decodable, Sendable {
    var nodes: [MeshNode], ports: [String: String]
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        nodes = c.opt(AnyKey("nodes")) ?? []; ports = c.opt(AnyKey("ports")) ?? [:]
    }
}
