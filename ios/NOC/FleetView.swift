import SwiftUI

/// Summary: fleet totals, then one compact card per machine. Tap a card for full detail.
struct FleetView: View {
    @Environment(Store.self) private var store
    @State private var showHidden = false

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 10) {
                if store.metrics != nil {
                    FleetSummary(hosts: store.hosts)
                    if store.hosts.isEmpty {
                        ContentUnavailableView("No agents reporting", systemImage: "antenna.radiowaves.left.and.right.slash")
                    }
                    ForEach(store.visibleHosts) { h in
                        NavigationLink(value: h.name) { HostRow(h: h) }
                            .buttonStyle(.plain)
                            .contextMenu {
                                Button("Hide from summary", systemImage: "eye.slash") { store.hidden.insert(h.name) }
                            }
                    }
                    let hid = store.hosts.filter { store.hidden.contains($0.name) }
                    if !hid.isEmpty {
                        DisclosureGroup("Hidden (\(hid.count))", isExpanded: $showHidden) {
                            ForEach(hid) { h in
                                HStack {
                                    StatusDot(color: statusColor(h))
                                    Text(h.name).font(.system(.subheadline, design: .monospaced))
                                    Spacer()
                                    Button("Show") { store.hidden.remove(h.name) }.font(.subheadline)
                                }
                                .padding(.vertical, 4)
                            }
                        }
                        .padding(.horizontal, 4)
                        .font(.subheadline)
                    }
                } else if let e = store.error {
                    ContentUnavailableView("Can't reach the NOC", systemImage: "wifi.exclamationmark",
                                           description: Text(e))
                } else {
                    ProgressView().padding(.top, 80)
                }
            }
            .padding(.horizontal, 12)
            .padding(.bottom, 20)
        }
        .background(Color(.systemGroupedBackground))
        .navigationTitle("Fleet")
        .navigationDestination(for: String.self) { HostDetailView(name: $0) }
        .refreshable { await store.refresh() }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) { PulseLabel() }
        }
    }
}

/// "updated 10:31:04" / error, like the web header's pulse.
struct PulseLabel: View {
    @Environment(Store.self) private var store
    var body: some View {
        if store.error != nil {
            Label("offline", systemImage: "exclamationmark.triangle.fill")
                .labelStyle(.titleAndIcon).font(.caption).foregroundStyle(Pal.bad)
        } else if let t = store.metrics?.t {
            Text(t).font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary)
        }
    }
}

struct FleetSummary: View {
    var hosts: [Machine]

    var body: some View {
        let live = hosts.filter { !$0.stale }
        let stale = hosts.count - live.count
        let throttled = live.filter { $0.throttle != nil }.count
        let watts = live.compactMap(\.gpuPower).reduce(0, +)
        let models = live.flatMap(\.loadedModels)
        let running = live.flatMap(\.models).compactMap { $0.stats?.running }.reduce(0, +)
        let tps = live.flatMap(\.models).compactMap { $0.stats?.genTps }.reduce(0, +)
        let gpus = live.compactMap(\.gpuUtil)
        let avgGpu = gpus.isEmpty ? nil : gpus.reduce(0, +) / Double(gpus.count)

        VStack(spacing: 10) {
            HStack(spacing: 10) {
                Tile(title: "Online", value: "\(live.count)/\(hosts.count)",
                     sub: stale > 0 ? "\(stale) stale" : "all reporting",
                     color: stale > 0 ? Pal.warn : Pal.good)
                Tile(title: "Power", value: watts > 0 ? fmtWatts(watts) : "—", sub: "measured hosts",
                     color: .primary)
                Tile(title: "GPU avg", value: avgGpu.map { "\(Int($0.rounded()))%" } ?? "—",
                     sub: "\(gpus.count) GPUs", color: tierColor(avgGpu, 60, 85))
            }
            HStack(spacing: 10) {
                Tile(title: "Models", value: "\(models.count)", sub: "loaded & serving", color: .primary)
                Tile(title: "Requests", value: n0(running), sub: tps > 0 ? "\(n0(tps)) tok/s" : "running now",
                     color: running > 0 ? Pal.good : .primary)
                Tile(title: "Throttling", value: "\(throttled)", sub: throttled > 0 ? "hosts" : "none",
                     color: throttled > 0 ? Pal.bad : Pal.good)
            }
        }
        .padding(.top, 4)
    }
}

struct Tile: View {
    var title: String, value: String, sub: String, color: Color
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title.uppercased()).font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
            Text(value).font(.system(.title3, design: .monospaced).weight(.semibold)).foregroundStyle(color)
                .lineLimit(1).minimumScaleFactor(0.6)
            Text(sub).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color(.secondarySystemGroupedBackground)))
    }
}

struct HostRow: View {
    @Environment(Store.self) private var store
    var h: Machine

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 6) {
                StatusDot(color: statusColor(h))
                Text(h.name).font(.system(.headline, design: .monospaced))
                VendorBadge(kind: h.kind).font(.caption)
                if store.shows(.temp), let t = h.temp {
                    Text("\(n0(t))°").font(.system(.subheadline, design: .monospaced).weight(.semibold))
                        .foregroundStyle(tierColor(t, 60, 78))
                }
                if store.shows(.temp), let lvl = h.throttle {
                    Text("⚠ \(lvl.lowercased())").font(.caption.weight(.semibold)).foregroundStyle(throttleColor(lvl))
                }
                Spacer()
                if h.stale {
                    Text("stale \(Int(h.age))s").font(.caption).foregroundStyle(Pal.bad)
                } else if store.shows(.uptime), let up = h.uptime {
                    Text("↑\(fmtUptime(up))").font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary)
                }
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
            }
            if store.shows(.sparkline) { Sparkline(hist: h.hist).frame(height: 30) }
            HStack(spacing: 10) {
                if store.shows(.gpu), h.kind != .plain && h.kind != .rpi { MiniBar(label: "GPU", pct: h.gpuUtil, g: 60, a: 85) }
                if store.shows(.cpu) { MiniBar(label: "CPU", pct: h.cpu, g: 60, a: 85) }
                if store.shows(.ram) { MiniBar(label: "RAM", pct: h.memPct, g: 70, a: 88) }
                if store.shows(.requests), let r = h.requests {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("REQ").font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
                        Text("\(n0(r.running))/\(r.max)" + (r.waiting > 0 ? " +\(n0(r.waiting))q" : ""))
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(tierColor(100 * r.running / Double(r.max), 70, 90))
                    }
                }
                if store.shows(.power), let w = h.gpuPower {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("PWR").font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
                        Text(fmtWatts(w)).font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(tierColor(100 * w / h.powerMax, 70, 90))
                    }
                }
            }
            let lm = store.shows(.models) ? h.loadedModels : []
            if !lm.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(lm.prefix(3).enumerated()), id: \.offset) { _, pair in
                        HStack(spacing: 4) {
                            Image(systemName: "brain").font(.system(size: 9)).foregroundStyle(Pal.good)
                            Text(pair.1.id).font(.system(size: 11, design: .monospaced)).lineLimit(1)
                            Text(pair.0.engine).font(.system(size: 10)).foregroundStyle(.secondary)
                            if let r = pair.0.stats?.running, r > 0 {
                                Text("\(n0(r))" + (pair.0.stats?.maxSeqs.map { "/\($0)" } ?? " run")).font(.system(size: 10, weight: .semibold)).foregroundStyle(Pal.good)
                            }
                        }
                    }
                    if lm.count > 3 {
                        Text("+\(lm.count - 3) more").font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
            if let cr = h.cmdResult {
                Text(cr.label).font(.caption).foregroundStyle(cr.ok ? Pal.good : Pal.bad)
            }
        }
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 14).fill(Color(.secondarySystemGroupedBackground))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .stroke((store.shows(.temp) ? h.throttle.map(throttleColor) : nil) ?? .clear, lineWidth: 1.5)
        )
        .opacity(h.stale ? 0.6 : 1)
        .contentShape(Rectangle())
    }
}
