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

    var body: some View {
        VStack(spacing: 28) {
            Spacer()

            Image(systemName: c.isCapturing ? "iphone.radiowaves.left.and.right" : "iphone")
                .font(.system(size: 64, weight: .light))
                .foregroundStyle(c.isCapturing ? .green : .secondary)

            Text("MirrorLink")
                .font(.largeTitle.bold())

            VStack(spacing: 10) {
                row("PC", c.pcConnected ? "Connected over USB" : "Not connected",
                    ok: c.pcConnected)
                row("Capture", c.isCapturing ? "Sharing screen" : "Off",
                    ok: c.isCapturing)
                if c.isCapturing && c.pcConnected {
                    row("Frames", "\(c.fps) fps", ok: c.fps > 0)
                }
            }
            .padding()
            .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))

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
                 : "Tap Start, choose \u{201C}Entire Screen\u{201D}, then open the viewer on your PC.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            Text(c.status).font(.caption2).foregroundStyle(.tertiary)
            Spacer()
        }
        .padding(28)
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
