import Foundation
import ScreenCaptureKit
import SwiftUI
import UIKit

@MainActor
final class MirrorController: NSObject, ObservableObject {
    @Published var status = "Starting…"
    @Published var isCapturing = false
    @Published var link: LinkType?            // nil = no PC connected
    @Published var fps = 0
    @Published var skipped = 0
    @Published var keyframes = 0
    @Published var lastError: String?
    @Published var wifiStatus = ""
    @Published var wifiAddress: String?
    @Published var justPaired = false
    @Published var wifiEnabled: Bool {
        didSet {
            UserDefaults.standard.set(wifiEnabled, forKey: "wifiEnabled")
            engine.server.setWifiEnabled(wifiEnabled)
        }
    }

    @Published var events: [String] = []
    @Published var jpegMode = false
    @Published var controlReady = false

    private let engine: CaptureEngine
    private let keepAlive = KeepAlive()

    var pickerAvailable: Bool { SCContentSharingPicker.shared.isAvailable }

    override init() {
        let wifi = UserDefaults.standard.object(forKey: "wifiEnabled") as? Bool ?? true
        _wifiEnabled = Published(initialValue: wifi)
        engine = CaptureEngine(wifiEnabled: wifi, deviceName: UIDevice.current.name)
        super.init()

        events = UserDefaults.standard.stringArray(forKey: "eventLog") ?? []
        if UserDefaults.standard.bool(forKey: "captureActive") {
            log("Last session ended while mirroring (app was closed by iOS or crashed)")
            UserDefaults.standard.set(false, forKey: "captureActive")
        }
        log("App started")
        keepAlive.onLog = { [weak self] msg in self?.log(msg) }
        engine.onLog = { [weak self] msg in Task { @MainActor in self?.log(msg) } }
        engine.onMode = { [weak self] on in Task { @MainActor in self?.jpegMode = on } }
        engine.onControlReady = { [weak self] on in Task { @MainActor in self?.controlReady = on } }

        engine.onServerStatus = { [weak self] msg in
            Task { @MainActor in self?.status = msg }
        }
        engine.onWifiStatus = { [weak self] msg in
            Task { @MainActor in
                self?.wifiStatus = msg
                self?.wifiAddress = MirrorController.localAddress()
            }
        }
        engine.onClientChanged = { [weak self] link in
            Task { @MainActor in
                self?.link = link
                if link == nil { self?.fps = 0 }
                self?.log(link == nil ? "PC disconnected" : "PC connected (\(link!.rawValue))")
            }
        }
        engine.onStats = { [weak self] fps, skipped, keys in
            Task { @MainActor in
                self?.fps = fps
                self?.skipped = skipped
                self?.keyframes = keys
            }
        }
        engine.onPaired = { [weak self] in
            Task { @MainActor in
                self?.justPaired = true
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                self?.justPaired = false
            }
        }
        engine.onStopped = { [weak self] error in
            Task { @MainActor in
                self?.isCapturing = false
                self?.fps = 0
                if let error {
                    let e = error as NSError
                    self?.lastError = "Capture stopped: \(error.localizedDescription)"
                    self?.log("Capture stopped by iOS: \(e.domain) \(e.code)")
                } else {
                    self?.log("Capture stopped")
                }
                self?.captureEnded()
                self?.deactivatePicker()
            }
        }
        engine.server.start()
        wifiAddress = MirrorController.localAddress()
    }

    // MARK: Actions

    func startMirroring() {
        lastError = nil
        let picker = SCContentSharingPicker.shared
        picker.add(self)
        picker.isActive = true
        picker.present()     // system sheet: user picks the entire screen
    }

    func stopMirroring() {
        engine.stop()
        isCapturing = false
        fps = 0
        log("Stopped mirroring")
        captureEnded()
        deactivatePicker()
    }

    /// Short event log shown in the app (and kept across launches) for troubleshooting.
    func log(_ msg: String) {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        events.append("\(f.string(from: Date()))  \(msg)")
        if events.count > 8 { events.removeFirst(events.count - 8) }
        UserDefaults.standard.set(events, forKey: "eventLog")
    }

    func scenePhaseChanged(_ phase: ScenePhase) {
        switch phase {
        case .background: log("App went to background")
        case .active:
            log("App in foreground")
            engine.appBecameActive()
        default: break
        }
    }

    private func captureEnded() {
        keepAlive.stop()
        UserDefaults.standard.set(false, forKey: "captureActive")
    }

    func resetPairing() {
        engine.server.resetPairing()
    }

    func refreshAddress() {
        wifiAddress = MirrorController.localAddress()
    }

    private func deactivatePicker() {
        let picker = SCContentSharingPicker.shared
        picker.isActive = false
        picker.remove(self)
    }

    fileprivate func pickerReturned(filter: SCContentFilter, existingStream: SCStream?) {
        let size = UIScreen.main.nativeBounds.size
        let scale = max(UIScreen.main.nativeScale, 1)
        engine.screenPoints = CGSize(width: size.width / scale, height: size.height / scale)
        engine.restart(filter: filter, pixelSize: size) { [weak self] error in
            Task { @MainActor in
                guard let self else { return }
                if let error {
                    self.lastError = "Couldn't start capture: \(error.localizedDescription)"
                    self.log("Capture failed to start: \((error as NSError).code)")
                    self.isCapturing = false
                    self.deactivatePicker()
                } else {
                    self.isCapturing = true
                    self.log("Capture started")
                    self.keepAlive.start()
                    UserDefaults.standard.set(true, forKey: "captureActive")
                }
            }
        }
    }

    fileprivate func pickerCancelled() {
        if !engine.isCapturing { deactivatePicker() }
    }

    fileprivate func pickerFailed(_ error: Error) {
        lastError = "Picker failed: \(error.localizedDescription)"
        deactivatePicker()
    }

    /// The phone's IPv4 address on Wi-Fi (en0) or its own hotspot (bridge100).
    nonisolated static func localAddress() -> String? {
        var ifaddr: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&ifaddr) == 0, let first = ifaddr else { return nil }
        defer { freeifaddrs(ifaddr) }
        var found: [String: String] = [:]
        var p: UnsafeMutablePointer<ifaddrs>? = first
        while let cur = p {
            let ifa = cur.pointee
            if let addr = ifa.ifa_addr, addr.pointee.sa_family == UInt8(AF_INET) {
                let name = String(cString: ifa.ifa_name)
                var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
                if getnameinfo(addr, socklen_t(addr.pointee.sa_len), &host, socklen_t(host.count),
                               nil, 0, NI_NUMERICHOST) == 0 {
                    found[name] = String(cString: host)
                }
            }
            p = ifa.ifa_next
        }
        return found["en0"] ?? found["bridge100"]
    }
}

extension MirrorController: SCContentSharingPickerObserver {
    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker,
                                          didUpdateWith filter: SCContentFilter,
                                          for stream: SCStream?) {
        Task { @MainActor in self.pickerReturned(filter: filter, existingStream: stream) }
    }

    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker, didCancelFor stream: SCStream?) {
        Task { @MainActor in self.pickerCancelled() }
    }

    nonisolated func contentSharingPickerStartDidFailWithError(_ error: Error) {
        Task { @MainActor in self.pickerFailed(error) }
    }
}
