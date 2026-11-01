import Foundation
import Security

/// Session token in the Keychain. The collector's auth is a `noc_session` cookie; the app
/// lifts it out of the `/api/auth/verify` response once and sends it explicitly afterwards,
/// rather than trusting a cookie jar it doesn't control.
enum Keychain {
    private static let service = "com.incept5.noc"

    static func get(_ account: String) -> String? {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrService as String: service,
                                kSecAttrAccount as String: account,
                                kSecReturnData as String: true,
                                kSecMatchLimit as String: kSecMatchLimitOne]
        var out: AnyObject?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess,
              let d = out as? Data else { return nil }
        return String(data: d, encoding: .utf8)
    }

    static func set(_ account: String, _ value: String?) {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrService as String: service,
                                kSecAttrAccount as String: account]
        SecItemDelete(q as CFDictionary)
        guard let v = value else { return }
        var add = q
        add[kSecValueData as String] = Data(v.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(add as CFDictionary, nil)
    }
}

enum APIError: LocalizedError {
    case unauthorized, notFound, http(Int, String?), transport(String)
    var errorDescription: String? {
        switch self {
        case .unauthorized: return "Signed out"
        case .notFound: return "Not found"
        case .http(let c, let e): return e ?? "HTTP \(c)"
        case .transport(let s): return s
        }
    }
}

struct API: Sendable {
    static let defaultBase = "https://noc.example.com"   // editable in the app (Settings / sign-in)
    static let cookieName = "noc_session"

    var base: URL
    var token: String?

    private static let session: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.httpShouldSetCookies = false          // we attach the session cookie ourselves
        cfg.httpCookieAcceptPolicy = .never
        cfg.timeoutIntervalForRequest = 10
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: cfg)
    }()

    private func request(_ path: String, method: String = "GET") -> URLRequest {
        var r = URLRequest(url: base.appendingPathComponent(path))
        r.httpMethod = method
        if let t = token { r.setValue("\(Self.cookieName)=\(t)", forHTTPHeaderField: "Cookie") }
        return r
    }

    private func send(_ r: URLRequest) async throws -> (Data, HTTPURLResponse) {
        do {
            let (d, resp) = try await Self.session.data(for: r)
            guard let h = resp as? HTTPURLResponse else { throw APIError.transport("no response") }
            return (d, h)
        } catch let e as APIError {
            throw e
        } catch {
            throw APIError.transport(error.localizedDescription)
        }
    }

    private func check(_ d: Data, _ h: HTTPURLResponse) throws {
        switch h.statusCode {
        case 200..<300: return
        case 401: throw APIError.unauthorized
        case 404: throw APIError.notFound
        default:
            let e = (try? JSONSerialization.jsonObject(with: d) as? [String: Any])?["error"] as? String
            throw APIError.http(h.statusCode, e)
        }
    }

    private static func form(_ fields: [String: String]) -> Data {
        var cs = CharacterSet.alphanumerics; cs.insert(charactersIn: "-._~")
        return fields.map { k, v in
            "\(k)=\(v.addingPercentEncoding(withAllowedCharacters: cs) ?? "")"
        }.joined(separator: "&").data(using: .utf8)!
    }

    // MARK: auth

    /// Always "succeeds" — the server never discloses whether an address is allowed.
    func requestCode(email: String) async throws {
        var r = request("api/auth/request", method: "POST")
        r.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        r.httpBody = Self.form(["email": email])
        let (d, h) = try await send(r)
        try check(d, h)
    }

    /// Redeem the 6-digit code and return the session token from Set-Cookie.
    func verify(email: String, code: String) async throws -> String {
        var r = request("api/auth/verify", method: "POST")
        r.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        r.httpBody = Self.form(["email": email, "code": code])
        let (d, h) = try await send(r)
        if h.statusCode == 400 { throw APIError.http(400, "Invalid or expired code") }
        try check(d, h)
        let headers = h.allHeaderFields.reduce(into: [String: String]()) { acc, kv in
            if let k = kv.key as? String, let v = kv.value as? String { acc[k] = v }
        }
        let cookies = HTTPCookie.cookies(withResponseHeaderFields: headers, for: base)
        guard let tok = cookies.first(where: { $0.name == Self.cookieName })?.value else {
            throw APIError.transport("Server did not return a session")
        }
        return tok
    }

    func logout() async {
        _ = try? await send(request("logout"))
    }

    // MARK: data

    func metrics() async throws -> MetricsResponse {
        let (d, h) = try await send(request("metrics"))
        try check(d, h)
        return try JSONDecoder().decode(MetricsResponse.self, from: d)
    }

    func network() async throws -> NetPayload {
        let (d, h) = try await send(request("api/admin/net"))
        try check(d, h)
        return try JSONDecoder().decode(NetPayload.self, from: d)
    }

    /// Queue a power command. Returns the relay name for a hardware action.
    func power(name: String, action: String, tier: String, password: String,
               confirm: String) async throws -> String? {
        var r = request("api/power", method: "POST")
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = try JSONSerialization.data(withJSONObject: [
            "name": name, "action": action, "tier": tier, "password": password, "confirm": confirm])
        let (d, h) = try await send(r)
        try check(d, h)
        return (try? JSONSerialization.jsonObject(with: d) as? [String: Any])?["relay"] as? String
    }
}
