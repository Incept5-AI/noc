import SwiftUI

struct SettingsView: View {
    @Environment(Store.self) private var store

    var body: some View {
        @Bindable var store = store
        Form {
            Section {
                ForEach(store.hosts) { h in
                    Toggle(isOn: Binding(
                        get: { !store.hidden.contains(h.name) },
                        set: { on in if on { store.hidden.remove(h.name) } else { store.hidden.insert(h.name) } })) {
                        HStack(spacing: 6) {
                            StatusDot(color: statusColor(h))
                            Text(h.name).font(.system(.body, design: .monospaced))
                            VendorBadge(kind: h.kind).font(.caption)
                        }
                    }
                }
                HStack {
                    Button("Show all") { store.hidden.removeAll() }
                    Spacer()
                    Button("Hide all") { store.hidden = Set(store.hosts.map(\.name)) }
                }
                .buttonStyle(.borderless)
            } header: {
                Text("Machines on the board")
            } footer: {
                Text("Hidden machines stay in the Models tab and can still be opened from the Hidden list on the Fleet screen.")
            }

            Section {
                ForEach(CardStat.allCases) { s in
                    Toggle(s.label, isOn: Binding(
                        get: { store.stats.contains(s) },
                        set: { on in if on { store.stats.insert(s) } else { store.stats.remove(s) } }))
                }
                HStack {
                    Button("All") { store.stats = Set(CardStat.allCases) }
                    Spacer()
                    Button("CPU only") { store.stats = [.cpu] }
                }
                .buttonStyle(.borderless)
            } header: {
                Text("Summary card shows")
            } footer: {
                Text("Applies to the Fleet summary. The machine detail screen always shows everything.")
            }

            Section("Account") {
                KV(k: "Signed in as", v: store.metrics?.me ?? store.email, mono: false)
                KV(k: "Role", v: store.isAdmin ? "admin" : "member", mono: false)
                Button("Sign out", role: .destructive) { store.signOut() }
            }
            Section("Server") {
                TextField("https://…", text: $store.baseURL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .font(.system(.footnote, design: .monospaced))
                if let t = store.lastUpdate {
                    KV(k: "Last update", v: t.formatted(date: .omitted, time: .standard))
                }
                if let e = store.error { Text(e).font(.footnote).foregroundStyle(Pal.bad) }
            }
        }
        .navigationTitle("Settings")
    }
}

// MARK: - power

struct PowerRequest: Identifiable {
    var name: String, action: String, tier: String
    var id: String { name + action + tier }
    var hardware: Bool { tier == "hardware" }
    var verb: String { action == "reboot" ? "restart" : "shut down" }
}

/// Same contract as the web modal: a password every time (never stored), and for the
/// hardware tier the relay's root password plus the typed machine name.
struct PowerSheet: View {
    @Environment(Store.self) private var store
    @Environment(\.dismiss) private var dismiss
    var req: PowerRequest
    @State private var password = ""
    @State private var confirm = ""
    @State private var busy = false
    @State private var msg: String?
    @State private var done = false

    private func norm(_ s: String) -> String { s.lowercased().filter { $0.isLetter || $0.isNumber } }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    if req.hardware {
                        Text("This will \(req.action == "reboot" ? "**power-cycle**" : "**cut mains power to**") **\(req.name)** at its smart plug. Enter the **root password of the relay host** that runs it, and type the machine name to confirm.")
                    } else {
                        Text("This will **\(req.verb)** **\(req.name)**. Enter the box's root / sudo password — it is required for every action and is not saved.")
                    }
                }
                .font(.subheadline)
                Section {
                    SecureField(req.hardware ? "root password (relay host)" : "root / sudo password", text: $password)
                        .textContentType(.password)
                    if req.hardware {
                        TextField("type \(req.name) to confirm", text: $confirm)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                    }
                }
                if let msg {
                    Section { Text(msg).font(.footnote).foregroundStyle(done ? Pal.good : Pal.bad) }
                }
                Section {
                    Button(role: req.action == "shutdown" ? .destructive : nil) {
                        Task { await go() }
                    } label: {
                        Text(busy ? "Sending…" : (req.hardware ? "Power " : "") + req.verb.capitalized)
                            .frame(maxWidth: .infinity)
                    }
                    .disabled(busy || done || password.isEmpty)
                }
            }
            .navigationTitle((req.hardware ? "Hardware " : "") + req.verb.capitalized)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
        .presentationDetents([.medium, .large])
    }

    private func go() async {
        if req.hardware && norm(confirm) != norm(req.name) {
            msg = "Type the machine name (\(req.name)) to confirm."; return
        }
        busy = true; msg = nil
        defer { busy = false }
        do {
            let relay = try await store.api.power(name: req.name, action: req.action, tier: req.tier,
                                                  password: password, confirm: confirm)
            password = ""; confirm = ""
            done = true
            msg = (req.hardware ? "Hardware " : "") + req.verb + " sent" + (relay.map { " via \($0)" } ?? "")
                + " — \(req.name) will act within a few seconds."
            try? await Task.sleep(for: .seconds(2))
            dismiss()
        } catch let APIError.http(_, e) {
            msg = switch e ?? "" {
            case "offline": "That machine is not reporting right now — can't reach it."
            case "no_plug": "No smart plug is mapped to this machine."
            case "no_relay": "No other LAN host is online to switch its plug right now."
            case "bad_confirm": "The machine name you typed doesn't match."
            case "forbidden": "You can only control machines you own."
            default: "Could not queue the command" + (e.map { " (\($0))" } ?? "") + "."
            }
        } catch APIError.unauthorized {
            msg = "Session expired — sign in again."
        } catch {
            msg = "Network error — try again."
        }
    }
}
