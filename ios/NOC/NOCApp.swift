import SwiftUI

@main
struct NOCApp: App {
    @State private var store = Store()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            Group {
                if store.signedIn {
                    MainTabs()
                } else {
                    LoginView()
                }
            }
            .environment(store)
            .onChange(of: phase, initial: true) { _, p in
                if p == .active { store.startPolling() } else { store.stopPolling() }
            }
        }
    }
}

struct MainTabs: View {
    @Environment(Store.self) private var store

    @State private var tab = 0
    @State private var fleetPath: [String] = []
    @State private var modelsPath: [String] = []

    var body: some View {
        TabView(selection: $tab) {
            NavigationStack(path: $fleetPath) { FleetView() }
                .tabItem { Label("Fleet", systemImage: "square.grid.2x2") }.tag(0)
            NavigationStack(path: $modelsPath) { ModelsView() }
                .tabItem { Label("Models", systemImage: "brain") }.tag(1)
            if store.isAdmin {
                NavigationStack { NetworkView() }
                    .tabItem { Label("Network", systemImage: "wifi") }.tag(2)
            }
            NavigationStack { SettingsView() }
                .tabItem { Label("Settings", systemImage: "gearshape") }.tag(3)
        }
        #if DEBUG
        .task {   // screenshot automation: NOC_DEBUG_TAB=1 / NOC_DEBUG_HOST=gpu-node-1
            let env = ProcessInfo.processInfo.environment
            if let t = env["NOC_DEBUG_TAB"].flatMap(Int.init) { tab = t }
            if let h = env["NOC_DEBUG_HOST"] { fleetPath = [h] }
        }
        #endif
    }
}

// MARK: - sign in (email → 6-digit code; the magic link would open Safari, not the app)

struct LoginView: View {
    @Environment(Store.self) private var store
    @State private var email = ""
    @State private var code = ""
    @State private var sent = false
    @State private var busy = false
    @State private var msg: String?
    @State private var showServer = false
    @FocusState private var codeFocused: Bool

    var body: some View {
        @Bindable var store = store
        NavigationStack {
            Form {
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("◢ INCEPT5 NOC")
                            .font(.system(.title2, design: .monospaced).weight(.bold))
                        Text("Enter your email and we'll send you a 6-digit sign-in code.")
                            .font(.subheadline).foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 6)
                }
                Section {
                    TextField("email address", text: $email)
                        .textContentType(.emailAddress)
                        .keyboardType(.emailAddress)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .disabled(sent)
                    if !sent {
                        Button(busy ? "Sending…" : "Send code") { Task { await send() } }
                            .disabled(busy || !email.contains("@"))
                    }
                }
                if sent {
                    Section {
                        TextField("000000", text: $code)
                            .keyboardType(.numberPad)
                            .textContentType(.oneTimeCode)
                            .font(.system(.title2, design: .monospaced))
                            .multilineTextAlignment(.center)
                            .focused($codeFocused)
                            // the email prints the code spaced ("1 2 3 4 5 6") — keep digits only
                            .onChange(of: code) { _, v in
                                let d = String(v.filter(\.isNumber).prefix(6))
                                if d != v { code = d }
                            }
                        Button(busy ? "Signing in…" : "Sign in") { Task { await verify() } }
                            .disabled(busy || code.count != 6)
                        Button("Use a different email", role: .cancel) {
                            sent = false; code = ""; msg = nil
                        }
                    } footer: {
                        Text("Check your inbox — if that address is allowed, a code is on the way.")
                    }
                }
                if let msg {
                    Section { Text(msg).foregroundStyle(Pal.bad).font(.footnote) }
                }
                Section {
                    DisclosureGroup("Server", isExpanded: $showServer) {
                        TextField("https://…", text: $store.baseURL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .keyboardType(.URL)
                            .font(.system(.footnote, design: .monospaced))
                    }
                }
            }
            .onAppear { if email.isEmpty { email = store.email ?? "" } }
        }
    }

    private func send() async {
        busy = true; msg = nil
        defer { busy = false }
        do {
            try await store.api.requestCode(email: email.trimmingCharacters(in: .whitespaces).lowercased())
            sent = true
            codeFocused = true
        } catch {
            msg = "Something went wrong (\(error.localizedDescription)) — try again."
        }
    }

    private func verify() async {
        busy = true; msg = nil
        defer { busy = false }
        let e = email.trimmingCharacters(in: .whitespaces).lowercased()
        do {
            let tok = try await store.api.verify(email: e, code: code)
            store.signIn(token: tok, email: e)
        } catch {
            msg = "Invalid or expired code — check it, or request a new one."
        }
    }
}
