import Foundation
import Observation

/// App state + the 2.5 s poll (the web dashboard's cadence). Polling only runs while the app
/// is in the foreground; the collector keeps 5 min of history, so a resumed app is never
/// more than one poll away from a full sparkline.
@MainActor @Observable
final class Store {
    var baseURL: String { didSet { UserDefaults.standard.set(baseURL, forKey: "baseURL") } }
    var token: String? { didSet { Keychain.set("session", token) } }
    var email: String? { didSet { UserDefaults.standard.set(email, forKey: "email") } }

    var metrics: MetricsResponse?
    var lastUpdate: Date?
    var error: String?
    var hidden: Set<String> {
        didSet { UserDefaults.standard.set(Array(hidden), forKey: "hidden") }
    }

    /// Which stats the summary cards show (Settings → Summary card). Detail always shows all.
    var stats: Set<CardStat> {
        didSet { UserDefaults.standard.set(stats.map(\.rawValue), forKey: "cardStats") }
    }
    func shows(_ s: CardStat) -> Bool { stats.contains(s) }

    var net: NetPayload?
    var netError: String?

    private var pollTask: Task<Void, Never>?

    init() {
        let d = UserDefaults.standard
        baseURL = d.string(forKey: "baseURL") ?? API.defaultBase
        email = d.string(forKey: "email")
        hidden = Set(d.stringArray(forKey: "hidden") ?? [])
        token = Keychain.get("session")
        #if DEBUG
        // simulator testing: `SIMCTL_CHILD_NOC_DEBUG_TOKEN=… xcrun simctl launch …`
        let env = ProcessInfo.processInfo.environment
        if let t = env["NOC_DEBUG_TOKEN"] { token = t }
        if let b = env["NOC_DEBUG_BASE"] { baseURL = b }
        #endif
        if let saved = d.stringArray(forKey: "cardStats") {
            stats = Set(saved.compactMap(CardStat.init(rawValue:)))
            // a stat added in an update starts switched on, not silently hidden
            let known = Set((d.stringArray(forKey: "cardStatsKnown") ?? saved.filter { $0 != "requests" })
                .compactMap(CardStat.init(rawValue:)))
            stats.formUnion(Set(CardStat.allCases).subtracting(known))
        } else {
            stats = Set(CardStat.allCases)
        }
        d.set(CardStat.allCases.map(\.rawValue), forKey: "cardStatsKnown")
    }

    var api: API {
        API(base: URL(string: baseURL) ?? URL(string: API.defaultBase)!, token: token)
    }
    var signedIn: Bool { token != nil }
    var isAdmin: Bool { metrics?.admin ?? false }

    var hosts: [Machine] {
        (metrics?.hosts.values.map { $0 } ?? []).sorted { $0.name < $1.name }
    }
    var visibleHosts: [Machine] { hosts.filter { !hidden.contains($0.name) } }
    func host(_ name: String) -> Machine? { metrics?.hosts[name] }

    func canPower(_ h: Machine) -> Bool {
        guard let m = metrics else { return false }
        return m.admin || (h.owner != nil && h.owner == m.me)
    }

    // MARK: polling

    func startPolling() {
        guard pollTask == nil, signedIn else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(2.5))
            }
        }
    }

    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    func refresh() async {
        guard signedIn else { return }
        do {
            metrics = try await api.metrics()
            lastUpdate = Date()
            error = nil
        } catch APIError.unauthorized {
            signOut(remote: false)
        } catch {
            self.error = error.localizedDescription
        }
    }

    func refreshNet() async {
        do {
            net = try await api.network()
            netError = nil
        } catch APIError.unauthorized {
            signOut(remote: false)
        } catch {
            netError = error.localizedDescription
        }
    }

    // MARK: auth

    func signIn(token: String, email: String) {
        self.token = token
        self.email = email
        startPolling()
    }

    func signOut(remote: Bool = true) {
        let api = self.api
        if remote { Task { await api.logout() } }
        stopPolling()
        token = nil
        metrics = nil
        net = nil
    }
}

enum CardStat: String, CaseIterable, Identifiable, Sendable {
    case sparkline, gpu, cpu, ram, power, requests, temp, uptime, models
    var id: String { rawValue }
    var label: String {
        switch self {
        case .sparkline: return "Sparkline (5 min)"
        case .gpu: return "GPU usage"
        case .cpu: return "CPU usage"
        case .ram: return "RAM usage"
        case .power: return "Power draw"
        case .requests: return "Requests in flight (of max)"
        case .temp: return "Temperature & throttling"
        case .uptime: return "Uptime"
        case .models: return "Models served"
        }
    }
}
