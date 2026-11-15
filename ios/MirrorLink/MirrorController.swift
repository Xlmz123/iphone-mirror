import Foundation
import ScreenCaptureKit
import SwiftUI

@MainActor
final class MirrorController: NSObject, ObservableObject {
    @Published var status = "Starting…"
    @Published var isCapturing = false
    @Published var pcConnected = false
    @Published var fps = 0
    @Published var lastError: String?

    private let engine = CaptureEngine()

    var pickerAvailable: Bool { SCContentSharingPicker.shared.isAvailable }

    override init() {
        super.init()
        status = "Waiting for PC on USB…"
        engine.onServerStatus = { [weak self] msg in
            Task { @MainActor in self?.status = msg }
        }
        engine.onClientChanged = { [weak self] connected in
            Task { @MainActor in
                self?.pcConnected = connected
                if !connected { self?.fps = 0 }
            }
        }
        engine.onFPS = { [weak self] fps in
            Task { @MainActor in self?.fps = fps }
        }
        engine.onStopped = { [weak self] error in
            Task { @MainActor in
                self?.isCapturing = false
                self?.fps = 0
                if let error { self?.lastError = "Capture stopped: \(error.localizedDescription)" }
                self?.deactivatePicker()
            }
        }
    }

    // MARK: Actions

    func startMirroring() {
        lastError = nil
        let picker = SCContentSharingPicker.shared
        picker.add(self)
        picker.isActive = true
        picker.present()     // system sheet: user picks "Entire Screen"
    }

    func stopMirroring() {
        engine.stop()
        isCapturing = false
        fps = 0
        deactivatePicker()
    }

    private func deactivatePicker() {
        let picker = SCContentSharingPicker.shared
        picker.isActive = false
        picker.remove(self)
    }

    fileprivate func pickerReturned(filter: SCContentFilter, existingStream: SCStream?) {
        if existingStream != nil || engine.isCapturing {
            engine.update(filter: filter)
            return
        }
        engine.start(filter: filter) { [weak self] error in
            Task { @MainActor in
                guard let self else { return }
                if let error {
                    self.lastError = "Couldn't start capture: \(error.localizedDescription)"
                    self.isCapturing = false
                    self.deactivatePicker()
                } else {
                    self.isCapturing = true
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
