import SwiftUI

@main
struct MirrorLinkApp: App {
    @StateObject private var controller = MirrorController()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(controller)
        }
    }
}

struct ContentView: View {
    @EnvironmentObject var c: MirrorController
    @Environment(\.scenePhase) private var scenePhase
    @State private var confirmReset = false

    private var pcText: String {
        switch c.link {
        case .usb: return "Connected over USB"
        case .wifi: return "Connected over Wi-Fi"
        case nil: return "Not connected"
        }
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 24) {
                Image(systemName: c.isCapturing ? "iphone.radiowaves.left.and.right" : "iphone")
                    .font(.system(size: 60, weight: .light))
                    .foregroundStyle(c.isCapturing ? .green : .secondary)
                    .padding(.top, 30)

                Text("MirrorLink").font(.largeTitle.bold())

                VStack(spacing: 10) {
                    row("PC", pcText, ok: c.link != nil)
                    row("Capture", c.isCapturing ? "Sharing screen" : "Off", ok: c.isCapturing)
                    if c.isCapturing && c.link != nil {
                        row("Frames", "\(c.fps) fps · \(c.skipped) skipped · \(c.keyframes) key", ok: c.fps > 0)
                        if c.jpegMode {
                            row("Mode", "JPEG (background)", ok: true)
                        }
                    }
                }
                .padding()
                .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))

                if c.justPaired {
                    Label("Paired with your PC — Wi-Fi will work from now on",
                          systemImage: "checkmark.seal.fill")
                        .font(.footnote).foregroundStyle(.green)
                }

                if let err = c.lastError {
                    Text(err).font(.footnote).foregroundStyle(.red).multilineTextAlignment(.center)
                }

                Button {
                    c.isCapturing ? c.stopMirroring() : c.startMirroring()
                } label: {
                    Text(c.isCapturing ? "Stop Mirroring" : "Start Mirroring")
                        .font(.headline)
                        .frame(maxWidth: .infinity)
                        .padding()
                }
                .buttonStyle(.borderedProminent)
                .tint(c.isCapturing ? .red : .blue)
                .disabled(!c.isCapturing && !c.pickerAvailable)

                Text(c.isCapturing
                     ? "You can leave this app — mirroring keeps running in the background."
                     : "Tap Start, choose the entire screen, then open the viewer on your PC.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)

                // Wi-Fi section
                VStack(alignment: .leading, spacing: 10) {
                    Toggle("Allow Wi-Fi mirroring", isOn: $c.wifiEnabled)
                    if c.wifiEnabled {
                        Text(c.wifiStatus.isEmpty ? "Starting…" : c.wifiStatus)
                            .font(.caption).foregroundStyle(.secondary)
                        if let ip = c.wifiAddress {
                            Text("This iPhone: \(ip)").font(.caption.monospaced()).foregroundStyle(.secondary)
                        }
                        Text("Pair once by connecting with the USB cable. After that, the PC finds this iPhone on the same Wi-Fi (or this iPhone's hotspot). Everything over Wi-Fi is encrypted.")
                            .font(.caption2).foregroundStyle(.tertiary)
                    }
                    Button("Reset pairing…", role: .destructive) { confirmReset = true }
                        .font(.footnote)
                }
                .padding()
                .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))

                // Event log (troubleshooting)
                if !c.events.isEmpty {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Recent events").font(.caption.bold()).foregroundStyle(.secondary)
                        ForEach(Array(c.events.reversed().enumerated()), id: \.offset) { _, e in
                            Text(e).font(.caption2.monospaced()).foregroundStyle(.secondary)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding()
                    .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))
                }

                Text(c.status).font(.caption2).foregroundStyle(.tertiary)
            }
            .padding(24)
        }
        .onAppear { c.refreshAddress() }
        .onChange(of: scenePhase) { _, phase in c.scenePhaseChanged(phase) }
        .confirmationDialog("Reset pairing?", isPresented: $confirmReset, titleVisibility: .visible) {
            Button("Reset", role: .destructive) { c.resetPairing() }
        } message: {
            Text("Every PC will need to connect over USB again before it can use Wi-Fi.")
        }
    }

    private func row(_ label: String, _ value: String, ok: Bool) -> some View {
        HStack {
            Circle().fill(ok ? Color.green : Color.gray.opacity(0.4)).frame(width: 9, height: 9)
            Text(label).foregroundStyle(.secondary)
            Spacer()
            Text(value)
        }
        .font(.subheadline)
    }
}
