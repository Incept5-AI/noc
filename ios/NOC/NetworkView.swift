import SwiftUI

// Port of the /admin page's judgement (noc_web.py ADMIN_PAGE): severity comes from what is
// MEASURED, not from RSSI alone. Red = a path losing ≥10% now or a node ≥80°C; amber = clean
// now but lossy inside the window, or a sub −70 dBm backhaul carrying traffic cleanly.

private func lossColor(_ v: Double?) -> Color { v == nil ? .secondary : v! < 1 ? Pal.good : v! < 10 ? Pal.warn : Pal.bad }
private func msColor(_ v: Double?) -> Color { v == nil ? .secondary : v! < 10 ? Pal.good : v! < 80 ? Pal.warn : Pal.bad }
private func rssiColor(_ v: Int?) -> Color { v == nil ? .secondary : v! >= -60 ? Pal.good : v! >= -70 ? Pal.warn : Pal.bad }
private func tempColor(_ v: Double?) -> Color { v == nil ? .secondary : v! < 70 ? Pal.good : v! < 80 ? Pal.warn : Pal.bad }

struct NetVerdict {
    var bad: [String] = [], warn: [String] = []

    init(_ d: NetPayload) {
        for p in d.probes {
            for t in p.targets {
                if t.loss >= 10 {
                    bad.append("\(p.from) → \(t.name) (\(t.host)) losing \(String(format: "%.1f", t.loss))% right now" +
                               (t.avg.map { ", \(Int($0)) ms avg" } ?? "") + ".")
                } else if let w = p.window[t.name], w.worst >= 10 {
                    warn.append("\(p.from) → \(t.name) is clean now but lost up to \(Int(w.worst))% in the last \(p.windowMin) min (\(w.bad) of \(w.n) samples bad) — intermittent.")
                }
            }
        }
        let lossy = !bad.isEmpty
        for n in d.mesh?.nodes ?? [] {
            if let t = n.temp, t >= 80 { bad.append("\(n.name) is running hot at \(String(format: "%.1f", t))°C.") }
            guard n.link == "wireless", let best = n.bestRSSI, best < -70 else { continue }
            let w = "\(n.name) (\(n.ip ?? "?")) is on a wireless backhaul at \(best) dBm"
            if lossy { bad.append(w + " — too weak to carry its clients; everything behind it inherits the loss.") }
            else { warn.append(w + " — very little margin, though it is carrying traffic cleanly now.") }
        }
    }
}

struct NetworkView: View {
    @Environment(Store.self) private var store

    var body: some View {
        ScrollViewReader { proxy in
        List {
            if let d = store.net {
                verdict(d)
                Section {
                    TopologyView(d: d)
                } header: {
                    Text("Topology")
                } footer: {
                    Text("Dashed = wireless backhaul. Coloured figures are cumulative from \(d.probes.first?.from ?? "the prober"), not per hop. Machines are placed from the router's client table; * = by latency.")
                }
                if let links = d.topo?.links, !links.isEmpty {
                    Section {
                        DirectLinksView(links: links).id("links")
                    } header: {
                        Text("Direct links")
                    } footer: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Cabled point-to-point subnets that bypass both LANs.")
                            ForEach(links.filter { $0.note != nil }) { l in
                                Text("**\(l.members.joined(separator: " ↔ "))**: \(l.note!)")
                            }
                        }
                    }
                }
                if let t = d.topo, !(t.remote.isEmpty && t.unplaced.isEmpty) {
                    Section("Elsewhere") {
                        ForEach(t.remote, id: \.self) { g in
                            Text(g.joined(separator: " + ")).font(.system(.footnote, design: .monospaced))
                        }
                        if !t.unplaced.isEmpty {
                            Text("At home, not placed: " + t.unplaced.joined(separator: ", ")).font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                }
                meshSection(d)
                ForEach(d.probes) { p in probeSection(p) }
                machinesSection(d)
            } else if let e = store.netError {
                ContentUnavailableView("Network data unavailable", systemImage: "wifi.exclamationmark", description: Text(e))
            } else {
                ProgressView()
            }
        }
        #if DEBUG
        .task {   // screenshot automation: NOC_DEBUG_SECTION=links
            if let s = ProcessInfo.processInfo.environment["NOC_DEBUG_SECTION"] {
                try? await Task.sleep(for: .seconds(3)); proxy.scrollTo(s, anchor: .top)
            }
        }
        #endif
        }
        .navigationTitle("Home network")
        .refreshable { await store.refreshNet() }
        .task {
            // the admin page's 5 s cadence, only while this tab is on screen
            while !Task.isCancelled {
                await store.refreshNet()
                try? await Task.sleep(for: .seconds(5))
            }
        }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Text(store.net?.t ?? "").font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder private func verdict(_ d: NetPayload) -> some View {
        let v = NetVerdict(d)
        Section {
            if d.probes.isEmpty && d.mesh == nil {
                Label("No probe data — no agent with --netprobe / --asus is reporting, so path health is unknown.",
                      systemImage: "questionmark.circle").foregroundStyle(.secondary).font(.subheadline)
            } else if v.bad.isEmpty && v.warn.isEmpty {
                Label("Network healthy — every path clean now and across the window.", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(Pal.good).font(.subheadline)
            }
            ForEach(v.bad, id: \.self) { Label($0, systemImage: "xmark.octagon.fill").foregroundStyle(Pal.bad).font(.footnote) }
            ForEach(v.warn, id: \.self) { Label($0, systemImage: "exclamationmark.triangle.fill").foregroundStyle(Pal.warn).font(.footnote) }
        }
    }

    @ViewBuilder private func meshSection(_ d: NetPayload) -> some View {
        Section {
            if let m = d.mesh {
                ForEach(m.nodes) { n in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(n.name).font(.subheadline.weight(.semibold))
                            Text(n.model ?? "").font(.caption2).foregroundStyle(.secondary)
                            Spacer()
                            if !n.online { Pill(text: "offline", color: Pal.bad) }
                            else { Pill(text: n.link == "root" ? "root" : n.link == "wired" ? "ethernet" : "wireless",
                                        color: n.link == "wireless" ? rssiColor(n.bestRSSI) : Pal.good) }
                        }
                        HStack(spacing: 8) {
                            Text(n.ip ?? "—").font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary)
                            if let r = n.rssi5 { Pill(text: "5G \(r)", color: rssiColor(r)) }
                            if let r = n.rssi6 { Pill(text: "6G \(r)", color: rssiColor(r)) }
                            if let t = n.temp { Pill(text: String(format: "%.0f°C", t), color: tempColor(t)) }
                        }
                    }
                }
                if !m.ports.isEmpty {
                    Text("Ports: " + m.ports.sorted { $0.key < $1.key }.map { "\($0.key) \($0.value == "X" ? "—" : $0.value)" }.joined(separator: "  "))
                        .font(.caption2).foregroundStyle(.secondary)
                }
            } else {
                Text("No router data yet.").font(.footnote).foregroundStyle(.secondary)
            }
        } header: {
            Text("AiMesh backhaul")
        } footer: {
            if let f = d.meshFrom { Text("via agent on \(f)") }
        }
    }

    private func probeSection(_ p: NetProbe) -> some View {
        Section {
            ForEach(p.targets) { t in
                let w = p.window[t.name]
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text(t.name).font(.subheadline)
                        Text(t.host).font(.system(.caption2, design: .monospaced)).foregroundStyle(.secondary)
                        Spacer()
                        Pill(text: String(format: "%.1f%%", t.loss), color: lossColor(t.loss))
                        if let a = t.avg { Pill(text: "\(Int(a)) ms", color: msColor(a)) }
                    }
                    HStack(spacing: 10) {
                        if let m = t.max { Text("max \(Int(m)) ms") }
                        if let j = t.jitter { Text("jitter \(String(format: "%.1f", j))") }
                        if let w { Text("worst \(p.windowMin)m \(Int(w.worst))%").foregroundStyle(lossColor(w.worst)) }
                        if let w { Text("mean \(String(format: "%.1f", w.mean))% n=\(w.n)") }
                    }
                    .font(.system(.caption2, design: .monospaced)).foregroundStyle(.secondary)
                }
            }
        } header: {
            Text("Path quality from \(p.from)")
        } footer: {
            Text("\(p.count ?? 0) pings/target, \(Int(p.age))s ago. Loss is cumulative along the path from \(p.from).")
        }
    }

    private func machinesSection(_ d: NetPayload) -> some View {
        Section("Machines") {
            ForEach(d.machines) { m in
                HStack {
                    StatusDot(color: m.age < 20 ? Pal.good : Pal.bad)
                    Text(m.name).font(.system(.footnote, design: .monospaced))
                    Spacer()
                    VStack(alignment: .trailing, spacing: 1) {
                        Text(m.ip ?? "—")
                        if let t = m.tsIP { Text(t).foregroundStyle(.tertiary) }
                    }
                    .font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary)
                    if let s = m.wifi?.ssid { Text(s).font(.caption2).foregroundStyle(.secondary).lineLimit(1) }
                }
            }
        }
    }
}
