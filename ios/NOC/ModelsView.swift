import SwiftUI

/// Fleet-wide view of what is being served: one row per model, with every box (and engine)
/// serving it. Only what a box serves itself — not gateway menus, not idle pulls.
struct ModelsView: View {
    @Environment(Store.self) private var store

    private struct Placement: Identifiable {
        var host: String, server: ModelServer, model: ServedModel, stale: Bool
        var id: String { host + ":\(server.port):" + model.id }
    }

    var body: some View {
        let live = store.hosts
        let placements = live.flatMap { h in
            h.loadedModels.map { Placement(host: h.name, server: $0.0, model: $0.1, stale: h.stale) }
        }
        let byModel = Dictionary(grouping: placements, by: { $0.model.root ?? $0.model.id })

        List {
            if placements.isEmpty {
                ContentUnavailableView("No models reported", systemImage: "brain",
                                       description: Text("Machines need the updated agent (2026.10.20+) to report what they serve."))
            }
            if !byModel.isEmpty {
                Section("Serving now") {
                    ForEach(byModel.keys.sorted(), id: \.self) { key in
                        let ps = byModel[key]!
                        VStack(alignment: .leading, spacing: 5) {
                            Text(ps[0].model.id).font(.system(.subheadline, design: .monospaced).weight(.semibold))
                            if ps[0].model.root != nil && ps[0].model.root != ps[0].model.id {
                                Text(key).font(.caption2).foregroundStyle(.secondary)
                            }
                            ForEach(ps) { p in
                                NavigationLink(value: p.host) {
                                    HStack(spacing: 6) {
                                        StatusDot(color: p.stale ? Pal.bad : Pal.good)
                                        Text(p.host).font(.system(.footnote, design: .monospaced))
                                        Text(p.server.title).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                        Spacer()
                                        if p.model.state == "loading" { Text("loading…").font(.caption2).foregroundStyle(Pal.warn) }
                                        if let c = fmtCtx(p.model.ctx) { Text(c).font(.caption2).foregroundStyle(.secondary) }
                                        if let st = p.server.stats {
                                            Text("\(n0(st.running))" + (st.maxSeqs.map { "/\($0) req" } ?? " run") + (st.genTps.map { $0 > 0 ? " · \(n0($0)) t/s" : "" } ?? ""))
                                                .font(.caption2.weight(.semibold))
                                                .foregroundStyle((st.running ?? 0) > 0 ? Pal.good : .secondary)
                                        }
                                    }
                                }
                            }
                        }
                        .padding(.vertical, 2)
                    }
                }
            }
        }
        .navigationTitle("Models")
        .navigationDestination(for: String.self) { HostDetailView(name: $0) }
        .refreshable { await store.refresh() }
    }
}
