import SwiftUI

// Phone-shaped port of the /admin topology (renderTopo/renderLinks in noc_web.py). Placement
// comes from the collector's `topo`, so the two always agree; only the layout differs —
// a vertical tree here instead of the web's wide diagram.

private func lossTint(_ v: Double?) -> Color { v == nil ? .secondary : v! < 1 ? Pal.good : v! < 10 ? Pal.warn : Pal.bad }
private func rssiTint(_ v: Int?) -> Color { v == nil || v == 0 ? .secondary : v! >= -60 ? Pal.good : v! >= -70 ? Pal.warn : Pal.bad }

/// Cumulative reach from the prober — the same caveat as the web: a probe measures the whole
/// path, so these figures are the cost of getting *to* a point, not of the last hop.
private struct Reach {
    var from: String?, home: String?, own: ProbeTarget?, root: ProbeTarget?, wan: ProbeTarget?, inet: ProbeTarget?
    init(_ d: NetPayload, root rootIP: String?) {
        guard let topo = d.topo else { return }
        for p in d.probes {
            guard let home = topo.placed[p.from]?.node else { continue }
            func named(_ re: String) -> ProbeTarget? { p.targets.first { $0.name.range(of: re, options: [.regularExpression, .caseInsensitive]) != nil } }
            from = p.from; self.home = home
            own = p.targets.first { $0.host == home }
            root = p.targets.first { $0.host == rootIP }
            wan = named("gateway|adapter|modem|wan"); inet = named("internet|isp|dns")
            break
        }
    }
    static func text(_ t: ProbeTarget?) -> String? {
        guard let t else { return nil }
        return (t.loss < 1 ? "0" : String(format: "%.0f", t.loss)) + "% loss" + (t.avg.map { " · \(Int($0)) ms" } ?? "")
    }
}

private struct NodeCard: View {
    var title: String, sub: String?, reach: ProbeTarget?
    var body: some View {
        VStack(spacing: 2) {
            Text(title).font(.system(.subheadline, design: .monospaced).weight(.bold))
            if let sub { Text(sub).font(.system(.caption2, design: .monospaced)).foregroundStyle(.secondary).lineLimit(1).minimumScaleFactor(0.7) }
            if let r = Reach.text(reach) {
                Text(r).font(.system(.caption2, design: .monospaced).weight(.bold)).foregroundStyle(lossTint(reach?.loss))
            }
        }
        .padding(.vertical, 7).padding(.horizontal, 10)
        .frame(maxWidth: 220)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color(.secondarySystemBackground)))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color(.separator)))
    }
}

private struct Chip: View {
    var name: String, detail: String?, tint: Color = .secondary, mark: String? = nil
    var body: some View {
        HStack(spacing: 4) {
            Text(name + (mark ?? "")).font(.system(.caption2, design: .monospaced).weight(.semibold))
            if let detail { Text(detail).font(.system(.caption2, design: .monospaced)).foregroundStyle(tint) }
        }
        .padding(.horizontal, 7).padding(.vertical, 3)
        .background(Capsule().fill(Color(.systemBackground)))
        .overlay(Capsule().stroke(Color(.separator)))
        .lineLimit(1).minimumScaleFactor(0.75)
    }
}

/// Vertical connector with an optional plated label (the plate stops a dashed line showing
/// through the text, the same lesson as the web edge labels).
private struct Wire: View {
    var color: Color, dashed = false, label: String? = nil, height: CGFloat = 26
    var body: some View {
        ZStack {
            Path { p in p.move(to: .init(x: 1, y: 0)); p.addLine(to: .init(x: 1, y: height)) }
                .stroke(color, style: StrokeStyle(lineWidth: 2.5, dash: dashed ? [5, 4] : []))
                .frame(width: 2, height: height)
            if let label {
                Text(label).font(.system(.caption2, design: .monospaced).weight(.bold)).foregroundStyle(color)
                    .padding(.horizontal, 4).background(Color(.systemBackground)).lineLimit(1).minimumScaleFactor(0.7)
            }
        }
        .frame(height: height)
    }
}

struct TopologyView: View {
    var d: NetPayload

    var body: some View {
        if let m = d.mesh, !m.nodes.isEmpty, let topo = d.topo {
            let root = m.nodes.first { $0.level == 0 } ?? m.nodes[0]
            let sats = m.nodes.filter { $0.id != root.id }
            let reach = Reach(d, root: root.ip)
            VStack(spacing: 0) {
                NodeCard(title: "☁ Internet", sub: reach.inet?.host, reach: reach.inet)
                Wire(color: lossTint(reach.inet?.loss))
                NodeCard(title: "WAN gateway", sub: reach.wan?.host, reach: reach.wan)
                if !topo.wan.isEmpty { wanLAN(topo.wan, net: topo.wanLAN) }
                Wire(color: lossTint(reach.wan?.loss))
                NodeCard(title: root.name, sub: (root.ip ?? "") + (root.temp.map { String(format: " · %.0f°C", $0) } ?? "")
                         + (topo.others[root.ip ?? ""].map { " · +\($0)" } ?? ""), reach: reach.root)
                chips(topo.kids(of: root.ip), reach: reach).padding(.top, 6)
                if !sats.isEmpty {
                    Wire(color: .secondary, height: 14)
                    bus(count: sats.count)
                    HStack(alignment: .top, spacing: 6) {
                        ForEach(sats) { n in satellite(n, topo: topo, reach: reach).frame(maxWidth: .infinity) }
                    }
                }
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 6)
        } else {
            Text("No router data yet.").font(.footnote).foregroundStyle(.secondary)
        }
    }

    private func wanLAN(_ hs: [Topo.WanHost], net: String?) -> some View {
        VStack(spacing: 4) {
            Text("upstream LAN " + (net ?? "")).font(.caption2).foregroundStyle(.secondary)
            FlowChips(items: hs.map { h in
                let k = h.kind == "ethernet" ? "wired" + (h.speed.map { $0 >= 1000 ? " \($0 / 1000)G" : " \($0)M" } ?? "")
                      : h.kind == "wifi" ? "Wi-Fi" : nil
                return (h.name, k, Color.secondary, nil)
            })
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 8).stroke(Color(.separator), style: StrokeStyle(lineWidth: 1, dash: [3, 3])))
        .padding(.top, 6)
    }

    private func chips(_ kids: [(name: String, place: Topo.Place)], reach: Reach) -> some View {
        FlowChips(items: kids.map { k in
            let det = k.place.band.map { $0 + (k.place.rssi.map { " \($0)" } ?? "") }
            return (k.name, det, rssiTint(k.place.rssi), k.place.via == "rtt" ? " *" : nil)
        })
    }

    /// Horizontal bus joining the satellite columns, so each drop hangs off one line.
    private func bus(count: Int) -> some View {
        GeometryReader { g in
            let w = g.size.width, step = w / CGFloat(count)
            Path { p in p.move(to: .init(x: step / 2, y: 1)); p.addLine(to: .init(x: w - step / 2, y: 1)) }
                .stroke(Color.secondary, lineWidth: 2)
        }
        .frame(height: 2)
    }

    private func satellite(_ n: MeshNode, topo: Topo, reach: Reach) -> some View {
        // Severity from what is measured (a lossy path through it), signal only as a margin
        // warning — the web's uplinkTier() rule in short.
        let lossNow = d.probes.flatMap(\.targets).filter { $0.host == n.ip }.map(\.loss).max()
        let tint: Color = (lossNow ?? 0) >= 10 ? Pal.bad : (n.bestRSSI ?? 0) < -70 && n.link == "wireless" ? Pal.warn : Pal.good
        let rssi = [n.rssi5.map { "5G \($0)" }, n.rssi6.map { "6G \($0)" }].compactMap { $0 }.joined(separator: " ")
        let kids = topo.kids(of: n.ip)
        return VStack(spacing: 0) {
            Wire(color: n.online ? tint : Pal.bad, dashed: n.link == "wireless",
                 label: n.link == "wireless" ? (rssi.isEmpty ? "wireless" : rssi) : n.link, height: 34)
            NodeCard(title: n.name, sub: (n.ip ?? "") + (topo.others[n.ip ?? ""].map { " · +\($0)" } ?? ""),
                     reach: reach.home == n.ip ? reach.own : nil)
            if !kids.isEmpty {
                VStack(spacing: 4) {
                    ForEach(kids, id: \.name) { k in
                        Chip(name: k.name, detail: k.place.band.map { $0 + (k.place.rssi.map { " \($0)" } ?? "") },
                             tint: rssiTint(k.place.rssi), mark: k.place.via == "rtt" ? " *" : nil)
                    }
                }
                .padding(.top, 6)
            }
        }
    }
}

/// Wrapping row of chips (iOS 16 Layout).
private struct FlowChips: View {
    var items: [(String, String?, Color, String?)]
    var body: some View {
        Flow(spacing: 4) {
            ForEach(items.indices, id: \.self) { i in
                Chip(name: items[i].0, detail: items[i].1, tint: items[i].2, mark: items[i].3)
            }
        }
    }
}

private struct Flow: Layout {
    var spacing: CGFloat
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        rows(proposal.width ?? .infinity, subviews).size
    }
    func placeSubviews(in b: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let r = rows(b.width, subviews)
        for (i, p) in r.points.enumerated() {
            subviews[i].place(at: .init(x: b.minX + p.x + (b.width - r.widths[r.rowOf[i]]) / 2, y: b.minY + p.y), proposal: .unspecified)
        }
    }
    private func rows(_ maxW: CGFloat, _ sv: Subviews) -> (size: CGSize, points: [CGPoint], widths: [CGFloat], rowOf: [Int]) {
        var pts: [CGPoint] = [], widths: [CGFloat] = [], rowOf: [Int] = []
        var x: CGFloat = 0, y: CGFloat = 0, rowH: CGFloat = 0, maxX: CGFloat = 0
        for s in sv {
            let sz = s.sizeThatFits(.unspecified)
            if x > 0 && x + sz.width > maxW { widths.append(x - spacing); y += rowH + spacing; x = 0; rowH = 0 }
            pts.append(.init(x: x, y: y)); rowOf.append(widths.count)
            x += sz.width + spacing; rowH = max(rowH, sz.height); maxX = max(maxX, x - spacing)
        }
        widths.append(max(0, x - spacing))
        return (.init(width: maxX, height: y + rowH), pts, widths, rowOf)
    }
}

// MARK: - direct links (QSFP ring, Thunderbolt bridge)

struct DirectLinksView: View {
    var links: [Topo.Link]

    private var groups: [[String]] {
        var adj: [String: Set<String>] = [:]
        for l in links { for a in l.members { for b in l.members where a != b { adj[a, default: []].insert(b) } } }
        var seen = Set<String>(), out: [[String]] = []
        for n in adj.keys.sorted() where !seen.contains(n) {
            var g: [String] = [], st = [n]; seen.insert(n)
            while let x = st.popLast() { g.append(x); for y in adj[x] ?? [] where !seen.contains(y) { seen.insert(y); st.append(y) } }
            // walk neighbours so a ring's cables do not cross
            var ord = [g.sorted()[0]]
            while ord.count < g.count {
                let nx = (adj[ord.last!] ?? []).sorted().first { !ord.contains($0) } ?? g.first { !ord.contains($0) }!
                ord.append(nx)
            }
            out.append(ord)
        }
        return out.sorted { $0.count > $1.count }
    }

    var body: some View {
        VStack(spacing: 14) {
            ForEach(groups, id: \.self) { g in
                Canvas { ctx, size in draw(g, in: &ctx, size: size) }
                    .frame(height: g.count > 2 ? 190 : 44)
            }
        }
    }

    private func color(_ l: Topo.Link) -> Color {
        l.kind == "qsfp" ? Color(red: 0.64, green: 0.44, blue: 0.97) : l.kind == "thunderbolt" ? Color(red: 0.35, green: 0.65, blue: 1) : .secondary
    }

    private func draw(_ g: [String], in ctx: inout GraphicsContext, size: CGSize) {
        let cx = size.width / 2, cy = size.height / 2
        var pos: [String: CGPoint] = [:]
        if g.count <= 2 {
            let dx = min(size.width / 2 - 50, 120)
            for (i, n) in g.enumerated() { pos[n] = .init(x: cx + (i == 0 ? -dx : dx), y: cy) }
        } else {
            let rx = min(size.width / 2 - 52, 150), ry = size.height / 2 - 22
            for (i, n) in g.enumerated() {
                let a = -Double.pi * 3 / 4 + 2 * .pi * Double(i) / Double(g.count)
                pos[n] = .init(x: cx + rx * CGFloat(cos(a)) * 1.414, y: cy + ry * CGFloat(sin(a)) * 1.414)
            }
            // keep boxes inside the canvas after the corner stretch
            for (k, p) in pos { pos[k] = .init(x: min(max(p.x, 50), size.width - 50), y: min(max(p.y, 13), size.height - 13)) }
        }
        for l in links where g.contains(l.members.first ?? "") {
            for i in 0..<l.members.count { for j in (i + 1)..<l.members.count {
                guard let a = pos[l.members[i]], let b = pos[l.members[j]] else { continue }
                var p = Path(); p.move(to: a); p.addLine(to: b)
                ctx.stroke(p, with: .color(color(l)), lineWidth: 3)
                let mid = CGPoint(x: (a.x + b.x) / 2, y: (a.y + b.y) / 2)
                let t = ctx.resolve(Text(l.label).font(.system(size: 10, weight: .bold, design: .monospaced)).foregroundStyle(color(l)))
                let sz = t.measure(in: size)
                ctx.fill(Path(roundedRect: CGRect(x: mid.x - sz.width / 2 - 4, y: mid.y - sz.height / 2 - 2,
                                                  width: sz.width + 8, height: sz.height + 4), cornerRadius: 4),
                         with: .color(Color(.systemBackground)))
                ctx.draw(t, at: mid)
            } }
        }
        for n in g {
            guard let p = pos[n] else { continue }
            let r = CGRect(x: p.x - 46, y: p.y - 12, width: 92, height: 24)
            ctx.fill(Path(roundedRect: r, cornerRadius: 6), with: .color(Color(.secondarySystemBackground)))
            ctx.stroke(Path(roundedRect: r, cornerRadius: 6), with: .color(Color(.separator)))
            ctx.draw(ctx.resolve(Text(n).font(.system(size: 11, weight: .bold, design: .monospaced))), at: p)
        }
    }
}
