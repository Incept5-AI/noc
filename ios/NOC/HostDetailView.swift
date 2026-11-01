import SwiftUI

struct HostDetailView: View {
    @Environment(Store.self) private var store
    var name: String
    @State private var power: PowerRequest?

    var body: some View {
        Group {
            if let h = store.host(name) {
                content(h)
            } else {
                ContentUnavailableView("\(name) is not reporting", systemImage: "antenna.radiowaves.left.and.right.slash")
            }
        }
        .navigationTitle(name)
        .navigationBarTitleDisplayMode(.inline)
        .sheet(item: $power) { PowerSheet(req: $0) }
    }

    @ViewBuilder
    private func content(_ h: Machine) -> some View {
        ScrollViewReader { proxy in
            List {
                headerSection(h)
                metersSection(h)
                if !h.servingServers.isEmpty { modelsSection(h).id("models") }
                if !h.cpuCores.isEmpty { coresSection(h).id("cores") }
                processSection(h).id("procs")
                systemSection(h).id("system")
                networkSection(h).id("network")
                if store.canPower(h) { powerSection(h).id("power") }
            }
            .listStyle(.insetGrouped)
            .refreshable { await store.refresh() }
            #if DEBUG
            .task {   // screenshot automation: NOC_DEBUG_SECTION=system
                if let s = ProcessInfo.processInfo.environment["NOC_DEBUG_SECTION"] {
                    try? await Task.sleep(for: .seconds(1)); proxy.scrollTo(s, anchor: .top)
                }
            }
            #endif
        }
    }

    // MARK: sections

    private func headerSection(_ h: Machine) -> some View {
        Section {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 8) {
                    StatusDot(color: statusColor(h))
                    VendorBadge(kind: h.kind)
                    Text(h.stale ? "stale — last report \(Int(h.age))s ago" : "live · \(String(format: "%.0f", h.age))s ago")
                        .font(.caption).foregroundStyle(h.stale ? Pal.bad : .secondary)
                    Spacer()
                    if let up = h.uptime {
                        Text("↑ \(fmtUptime(up))").font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary)
                    }
                }
                HStack(spacing: 18) {
                    if let t = h.temp {
                        BigStat(label: h.gpuTemp != nil ? "GPU temp" : "CPU temp", value: "\(n0(t))°C",
                                color: tierColor(t, 60, 78))
                    }
                    if let f = h.fanRPM { BigStat(label: "Fan", value: "\(Int(f)) rpm", color: .primary) }
                    if let w = h.gpuPower {
                        BigStat(label: "Power", value: fmtWatts(w), color: tierColor(100 * w / h.powerMax, 70, 90))
                    }
                    if let n = h.ncpu { BigStat(label: "Cores", value: "\(n)", color: .primary) }
                }
                if let lvl = h.throttle {
                    Label("Throttling: \(lvl)" + (h.thermalDetail.map { " — \($0)" } ?? ""),
                          systemImage: "exclamationmark.triangle.fill")
                        .font(.subheadline.weight(.semibold)).foregroundStyle(throttleColor(lvl))
                } else if h.thermal != nil {
                    Label("Thermal pressure nominal", systemImage: "thermometer.medium")
                        .font(.caption).foregroundStyle(.secondary)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(h.gpuUtil != nil ? "GPU utilisation · last 5 min" : "CPU utilisation · last 5 min")
                        .font(.caption2).foregroundStyle(.secondary)
                    Sparkline(hist: h.hist, showTicks: true).frame(height: 70)
                }
                if let cr = h.cmdResult {
                    Text(cr.label).font(.caption).foregroundStyle(cr.ok ? Pal.good : Pal.bad)
                }
                if let o = h.owner {
                    Text((h.shared ? "shared · " : "private · ") + o).font(.caption2).foregroundStyle(.secondary)
                }
            }
            .padding(.vertical, 4)
        }
    }

    private func metersSection(_ h: Machine) -> some View {
        Section("Utilisation") {
            if h.kind != .plain && h.kind != .rpi {
                Meter(label: "GPU", pct: h.gpuUtil,
                      value: n0(h.gpuUtil) + "%" + (h.gpuClk.map { " · \(n0($0))MHz" } ?? ""), g: 60, a: 85)
            }
            Meter(label: "CPU", pct: h.cpu, value: n0(h.cpu) + "%", g: 60, a: 85)
            Meter(label: "RAM", pct: h.memPct,
                  value: String(format: "%.1f/%.1fG", h.memUsedGB, h.memTotalGB), g: 70, a: 88)
            if let w = h.gpuPower {
                Meter(label: "PWR", pct: 100 * w / h.powerMax, value: fmtWatts(w) + " / \(Int(h.powerMax))", g: 70, a: 90)
            }
            if let r = h.requests {
                Meter(label: "REQ", pct: 100 * r.running / Double(r.max),
                      value: "\(n0(r.running))/\(r.max)" + (r.waiting > 0 ? " +\(n0(r.waiting)) queued" : ""), g: 70, a: 90)
            }
            if let gm = h.gpuMemUsed, gm > 0 {
                KV(k: "GPU memory in use", v: String(format: "%.1f GB", gm / 1024))
            }
            if h.load.count >= 3 {
                KV(k: "Load (1·5·15m)", v: h.load.map { String(format: "%.2f", $0) }.joined(separator: "  "))
            }
            if h.kind == .nvidia {
                Text("GB10 memory is unified — GPU allocations show up as system RAM.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    private func modelsSection(_ h: Machine) -> some View {
        Section("Models served") {
            ForEach(h.servingServers, id: \.port) { s in ServerBlock(s: s) }
        }
    }

    private func coresSection(_ h: Machine) -> some View {
        Section("Per-core CPU") {
            let cols = Array(repeating: GridItem(.flexible(), spacing: 4), count: 6)
            LazyVGrid(columns: cols, spacing: 6) {
                ForEach(Array(h.cpuCores.enumerated()), id: \.offset) { i, v in
                    VStack(spacing: 2) {
                        GeometryReader { geo in
                            ZStack(alignment: .bottom) {
                                RoundedRectangle(cornerRadius: 3).fill(Color.secondary.opacity(0.15))
                                RoundedRectangle(cornerRadius: 3).fill(tierColor(v, 60, 85))
                                    .frame(height: geo.size.height * CGFloat(min(100, v) / 100))
                            }
                        }
                        .frame(height: 30)
                        Text("\(i)").font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.vertical, 4)
        }
    }

    private func processSection(_ h: Machine) -> some View {
        Section {
            ForEach(h.procs, id: \.self) { p in
                HStack {
                    Text(String(format: "%5.0f", p.cpu)).foregroundStyle(tierColor(p.cpu, 60, 120))
                        .frame(width: 48, alignment: .trailing)
                    Text(String(format: "%5.1f", p.mem)).foregroundStyle(.secondary)
                        .frame(width: 48, alignment: .trailing)
                    Text(p.cmd).lineLimit(1)
                }
                .font(.system(.footnote, design: .monospaced))
            }
            if !h.containerList.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Containers").font(.caption).foregroundStyle(.secondary)
                    FlowText(items: h.containerList)
                }
            } else {
                KV(k: "Containers", v: "—")
            }
        } header: {
            HStack {
                Text("Top processes")
                Spacer()
                Text("CPU%  MEM%").font(.system(.caption2, design: .monospaced))
            }
        }
    }

    private func systemSection(_ h: Machine) -> some View {
        Section("System") {
            let y = h.sys
            KV(k: "OS", v: y?["os_name"] ?? h.os)
            if let d = y?["dgx_os"] { KV(k: "DGX OS", v: d) }
            KV(k: "Kernel", v: y?["kernel"])
            if let m = y?["model"] { KV(k: "Model", v: m) }
            KV(k: "CPU", v: y?["cpu_model"])
            if let pe = y?["cores_pe"] { KV(k: "Cores", v: pe + (h.ncpu.map { " (\($0))" } ?? "")) }
            KV(k: "Arch", v: y?["arch"])
            if let g = y?["gpu_name"] { KV(k: "GPU", v: g) }
            if let gc = y?["gpu_cores"] { KV(k: "GPU cores", v: gc) }
            if let d = y?["driver"] { KV(k: "NVIDIA driver", v: d) }
            if let c = y?["cuda"] { KV(k: "CUDA", v: c) }
            if let d = y?["docker"] { KV(k: "Docker", v: d) }
            let vllm = h.models.filter { $0.engine == "vLLM" }.compactMap(\.shortVersion)
            if !vllm.isEmpty { KV(k: "vLLM", v: Array(Set(vllm)).sorted().joined(separator: ", ")) }
            let oll = h.models.filter { $0.engine == "Ollama" }.compactMap(\.version)
            if !oll.isEmpty { KV(k: "Ollama", v: oll.joined(separator: ", ")) }
            KV(k: "RAM", v: String(format: "%.0f GB", h.memTotalGB))
            if let up = h.uptime {
                KV(k: "Booted", v: Date().addingTimeInterval(-up).formatted(date: .abbreviated, time: .shortened), mono: false)
            }
            KV(k: "Hostname", v: y?["hostname"])
            KV(k: "Python", v: y?["python"])
            KV(k: "Agent", v: y?["agent"] ?? "old (no system info)")
        }
    }

    private func networkSection(_ h: Machine) -> some View {
        Section("Network") {
            KV(k: "LAN IP", v: h.ip)
            if let t = h.sys?["ts_ip"] { KV(k: "Tailscale IP", v: t) }
            if let t = h.sys?["ts_name"] { KV(k: "Tailscale name", v: t) }
            if let w = h.wifi {
                KV(k: "Wi-Fi", v: [w.ssid, w.dev].compactMap { $0 }.joined(separator: " · "))
                if let b = w.bssid { KV(k: "BSSID", v: b) }
                if let s = w.signal { KV(k: "Signal", v: "\(Int(s)) dBm") }
                if let f = w.freq { KV(k: "Frequency", v: "\(Int(f)) MHz") }
            } else {
                KV(k: "Wi-Fi", v: "wired / not reported")
            }
        }
    }

    private func powerSection(_ h: Machine) -> some View {
        Section {
            Button { power = PowerRequest(name: h.name, action: "reboot", tier: "os") } label: {
                Label("Restart (OS)", systemImage: "arrow.clockwise")
            }
            Button(role: .destructive) { power = PowerRequest(name: h.name, action: "shutdown", tier: "os") } label: {
                Label("Shut down (OS)", systemImage: "power")
            }
            if h.hwPower {
                Button { power = PowerRequest(name: h.name, action: "reboot", tier: "hardware") } label: {
                    Label("Hardware restart (power-cycle plug)", systemImage: "bolt.horizontal.circle")
                }
                Button(role: .destructive) { power = PowerRequest(name: h.name, action: "shutdown", tier: "hardware") } label: {
                    Label("Hardware shutdown (cut plug)", systemImage: "bolt.slash")
                }
            }
        } header: {
            Text("Power")
        } footer: {
            Text("Every action asks for a password, which is sent once and never stored.")
        }
    }
}

struct BigStat: View {
    var label: String, value: String, color: Color
    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.system(.title3, design: .monospaced).weight(.semibold)).foregroundStyle(color)
                .lineLimit(1).minimumScaleFactor(0.6)
        }
    }
}

/// Wrapping list of monospaced chips (containers, pulled models).
struct FlowText: View {
    var items: [String]
    var body: some View {
        Text(items.map { "▪︎ " + $0 }.joined(separator: "   "))
            .font(.system(.footnote, design: .monospaced))
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// One inference server and its models.
struct ServerBlock: View {
    var s: ModelServer

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Image(systemName: "server.rack").foregroundStyle(Pal.good)
                Text(s.title).font(.subheadline.weight(.semibold))
                if let c = s.container { Text(c).font(.system(.caption2, design: .monospaced)).foregroundStyle(.secondary).lineLimit(1) }
                Spacer()
                Text(s.where_).font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary)
            }
            if let st = s.stats {
                HStack(spacing: 14) {
                    MiniStat(k: "running", v: n0(st.running) + (st.maxSeqs.map { "/\($0)" } ?? ""), hot: (st.running ?? 0) > 0)
                    MiniStat(k: "waiting", v: n0(st.waiting), hot: (st.waiting ?? 0) > 0)
                    MiniStat(k: "KV cache", v: st.kv.map { "\(n0($0))%" } ?? "—", hot: (st.kv ?? 0) > 80)
                    MiniStat(k: "gen tok/s", v: n0(st.genTps), hot: false)
                    if let p = st.promptTps, p > 0 { MiniStat(k: "prefill/s", v: n0(p), hot: false) }
                }
            }
            if let c = s.stats?.kvConc, let ctx = fmtCtx(s.models.first?.ctx) {
                Text("KV cache holds \(String(format: "%.1f", c)) requests at the full \(ctx)")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            ForEach(s.models, id: \.self) { m in
                VStack(alignment: .leading, spacing: 1) {
                    HStack(spacing: 5) {
                        Text(m.id).font(.system(.footnote, design: .monospaced).weight(.semibold)).lineLimit(2)
                        if m.vision { Image(systemName: "eye").font(.caption2).foregroundStyle(.secondary) }
                        if let st = m.state, st != "loaded" {
                            Text(st == "loading" ? "loading…" : st).font(.caption2)
                                .foregroundStyle(st == "loading" ? Pal.warn : .secondary)
                        }
                    }
                    let meta = [m.root, fmtCtx(m.ctx), m.vramMB.map { String(format: "%.1f GB VRAM", Double($0) / 1024) },
                                m.nodes.isEmpty ? nil : "→ " + m.nodes.joined(separator: ", ")].compactMap { $0 }
                    if !meta.isEmpty {
                        Text(meta.joined(separator: " · ")).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                    }
                }
            }
        }
        .padding(.vertical, 3)
    }
}

struct MiniStat: View {
    var k: String, v: String, hot: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(v).font(.system(.subheadline, design: .monospaced).weight(.semibold))
                .foregroundStyle(hot ? Pal.good : .primary)
            Text(k).font(.system(size: 9)).foregroundStyle(.secondary)
        }
    }
}
