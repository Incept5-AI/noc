import SwiftUI

// Colours and thresholds are the web dashboard's (noc_server.py PAGE), so a value reads the
// same colour on the phone as on the wall screen.

enum Pal {
    static let good = Color(red: 0.247, green: 0.725, blue: 0.314)   // #3fb950
    static let warn = Color(red: 0.824, green: 0.600, blue: 0.133)   // #d29922
    static let bad = Color(red: 0.941, green: 0.376, blue: 0.243)    // #f0603e
    static let idle = Color(red: 0.231, green: 0.510, blue: 0.965)   // #3b82f6
    static let yellow = Color(red: 0.890, green: 0.702, blue: 0.255) // #e3b341
    static let hot = Color(red: 0.941, green: 0.533, blue: 0.243)    // #f0883e
    static let faint = Color.secondary
}

/// green below `g`, amber below `a`, red above — the web's `fc()`.
func tierColor(_ v: Double?, _ g: Double, _ a: Double) -> Color {
    guard let v else { return .secondary }
    return v < g ? Pal.good : v < a ? Pal.warn : Pal.bad
}

func statusColor(_ h: Machine) -> Color { h.stale ? Pal.bad : h.lagging ? Pal.warn : Pal.good }

func throttleColor(_ lvl: String) -> Color { lvl == "Fair" ? Pal.warn : Pal.bad }

func n0(_ v: Double?) -> String { v.map { String(Int($0.rounded())) } ?? "—" }

func fmtWatts(_ w: Double) -> String { w < 10 ? String(format: "%.1f W", w) : "\(Int(w.rounded())) W" }

/// Two coarsest non-zero units, like the web's fmtUp(): "23d 4h", "6h 13m", "48s".
func fmtUptime(_ s: Double) -> String {
    let units: [(Double, String)] = [(31_557_600, "y"), (2_629_800, "mo"), (86_400, "d"),
                                     (3_600, "h"), (60, "m"), (1, "s")]
    let s = max(0, s.rounded(.down))
    for (i, (u, l)) in units.enumerated() {
        let big = (s / u).rounded(.down)
        if big < 1 { continue }
        if i + 1 < units.count {
            let (u2, l2) = units[i + 1]
            let sm = ((s - big * u) / u2).rounded(.down)
            return "\(Int(big))\(l)" + (sm > 0 ? " \(Int(sm))\(l2)" : "")
        }
        return "\(Int(big))\(l)"
    }
    return "0s"
}

func fmtCtx(_ c: Int?) -> String? {
    guard let c, c > 0 else { return nil }
    return c >= 1024 ? "\(c / 1024)k ctx" : "\(c) ctx"
}

// MARK: - sparkline

/// 300 s window, value 0–100. The stroke is a vertical gradient so colour tracks the value at
/// each moment (blue idle → green → yellow → orange → red), exactly like the web sparkline.
struct Sparkline: View {
    var hist: [[Double]]
    var window: Double = 300
    var showTicks = false

    private var points: [(Double, Double)] {
        hist.compactMap { p in p.count >= 2 && p[0] <= window ? (p[0], p[1]) : nil }
    }

    private var gradient: LinearGradient {
        LinearGradient(stops: [.init(color: Pal.bad, location: 0), .init(color: Pal.hot, location: 0.2),
                               .init(color: Pal.yellow, location: 0.42), .init(color: Pal.good, location: 0.62),
                               .init(color: Pal.idle, location: 1)],
                       startPoint: .top, endPoint: .bottom)
    }

    private func path(in r: CGRect, closed: Bool) -> Path {
        let pad: CGFloat = 2
        let pts = points.map { (a, v) -> CGPoint in
            CGPoint(x: r.width * (1 - CGFloat(min(window, a) / window)),
                    y: r.height - pad - CGFloat(min(100, max(0, v)) / 100) * (r.height - 2 * pad))
        }.sorted { $0.x < $1.x }
        var p = Path()
        guard pts.count >= 2 else { return p }
        p.move(to: pts[0])
        // Catmull-Rom → cubic Bézier, the web's smooth()
        for i in 0..<(pts.count - 1) {
            let a = i > 0 ? pts[i - 1] : pts[i], b = pts[i], c = pts[i + 1]
            let e = i + 2 < pts.count ? pts[i + 2] : c
            p.addCurve(to: c,
                       control1: CGPoint(x: b.x + (c.x - a.x) / 6, y: b.y + (c.y - a.y) / 6),
                       control2: CGPoint(x: c.x - (e.x - b.x) / 6, y: c.y - (e.y - b.y) / 6))
        }
        if closed {
            p.addLine(to: CGPoint(x: pts.last!.x, y: r.height))
            p.addLine(to: CGPoint(x: pts[0].x, y: r.height))
            p.closeSubpath()
        }
        return p
    }

    var body: some View {
        VStack(spacing: 2) {
            GeometryReader { geo in
                let r = CGRect(origin: .zero, size: geo.size)
                ZStack {
                    ForEach(1..<5) { m in
                        Path { p in
                            let x = r.width * (1 - CGFloat(m) * 60 / window)
                            p.move(to: CGPoint(x: x, y: 0)); p.addLine(to: CGPoint(x: x, y: r.height))
                        }.stroke(Color.secondary.opacity(0.18), lineWidth: 0.5)
                    }
                    path(in: r, closed: true).fill(gradient.opacity(0.2))
                    path(in: r, closed: false)
                        .stroke(gradient, style: StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round))
                }
            }
            if showTicks {
                HStack {
                    ForEach(["5m", "4m", "3m", "2m", "1m", "now"], id: \.self) { t in
                        Text(t)
                        if t != "now" { Spacer() }
                    }
                }
                .font(.system(size: 9, design: .monospaced))
                .foregroundStyle(.tertiary)
            }
        }
    }
}

// MARK: - meters

struct Meter: View {
    var label: String
    var pct: Double?
    var value: String
    var g: Double, a: Double

    var body: some View {
        HStack(spacing: 8) {
            Text(label)
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 34, alignment: .leading)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.secondary.opacity(0.15))
                    Capsule().fill(tierColor(pct ?? 0, g, a))
                        .frame(width: geo.size.width * CGFloat(min(100, max(0, pct ?? 0)) / 100))
                }
            }
            .frame(height: 7)
            Text(value)
                .font(.system(size: 12, design: .monospaced))
                .foregroundStyle(pct == nil ? Color.secondary : tierColor(pct, g, a))
                .frame(minWidth: 92, alignment: .trailing)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
    }
}

/// A miniature bar for the summary rows.
struct MiniBar: View {
    var label: String
    var pct: Double?
    var g: Double, a: Double
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 3) {
                Text(label).foregroundStyle(.secondary)
                Text(pct.map { "\(Int($0.rounded()))%" } ?? "—").foregroundStyle(tierColor(pct, g, a))
            }
            .font(.system(size: 10, design: .monospaced))
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.secondary.opacity(0.15))
                    Capsule().fill(tierColor(pct ?? 0, g, a))
                        .frame(width: geo.size.width * CGFloat(min(100, max(0, pct ?? 0)) / 100))
                }
            }
            .frame(height: 4)
        }
    }
}

// MARK: - badges

struct VendorBadge: View {
    var kind: HostKind
    var body: some View {
        switch kind {
        case .apple:
            Image(systemName: "apple.logo").foregroundStyle(.primary)
        case .nvidia:
            Text("NV").font(.system(size: 9, weight: .heavy))
                .padding(.horizontal, 3).padding(.vertical, 1)
                .background(RoundedRectangle(cornerRadius: 3).fill(Color(red: 0.46, green: 0.73, blue: 0)))
                .foregroundStyle(.black)
        case .rpi:
            Image(systemName: "cpu").foregroundStyle(Color(red: 0.77, green: 0.10, blue: 0.29))
        case .plain:
            Image(systemName: "server.rack").foregroundStyle(.secondary)
        }
    }
}

struct StatusDot: View {
    var color: Color
    var body: some View { Circle().fill(color).frame(width: 8, height: 8) }
}

struct Pill: View {
    var text: String
    var color: Color
    var body: some View {
        Text(text)
            .font(.system(size: 11, weight: .bold, design: .monospaced))
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(RoundedRectangle(cornerRadius: 5).fill(color.opacity(0.16)))
            .foregroundStyle(color)
    }
}

/// Key/value row for the detail lists.
struct KV: View {
    var k: String
    var v: String?
    var mono = true
    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(k).foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(v ?? "—")
                .font(mono ? .system(.subheadline, design: .monospaced) : .subheadline)
                .multilineTextAlignment(.trailing)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
        .font(.subheadline)
    }
}
