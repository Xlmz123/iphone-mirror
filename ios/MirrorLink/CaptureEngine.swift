import Foundation
import ScreenCaptureKit
import CoreMedia
import UIKit

/// Screen capture (ScreenCaptureKit, iOS 27+) -> H.264 encoder -> USB server.
final class CaptureEngine: NSObject, SCStreamOutput, SCStreamDelegate {
    let server: StreamServer
    private let encoder = H264Encoder()
    private var stream: SCStream?
    private let captureQueue = DispatchQueue(label: "mirrorlink.capture", qos: .userInteractive)

    // State shared between queues
    private let lock = NSLock()
    private var clientConnected = false
    private var lastBuffer: CVPixelBuffer?       // re-sent when a viewer joins on a static screen
    private var frameCount = 0
    private var lastStatsTime = CACurrentMediaTime()
    private var lastEncodeTime: CFTimeInterval = 0   // capture-queue only
    // Background fallback (capture-queue only)
    private let jpeg = JPEGEncoder()
    private var jpegMode = false
    private var hwFailStreak = 0
    private let jpegMaxFPS: Double = 20
    var onLog: ((String) -> Void)?
    var onMode: ((Bool) -> Void)?      // true = JPEG fallback active

    /// Speed/quality settings. Defaults here; the PC can change them live by
    /// sending pc/settings.json (see apply(config:)).
    struct Tuning {
        /// Stream height cap. The PC window is ~1000 px tall, so capturing the full
        /// ~2600 px native screen is ~4x the work for no visible gain.
        var maxHeight: CGFloat = 1400
        var maxFPS: Double = 60
        var bitrateUSB = 1.0
        var bitrateWiFi = 0.6
        var queuedUSB = 4
        var queuedWiFi = 6
        var keyframeInterval = 3.0
    }
    private var tuning = Tuning()                    // guarded by lock
    private var currentLink: LinkType?               // guarded by lock
    private var lastFilter: SCContentFilter?         // main thread
    private var lastPixelSize: CGSize = .zero        // main thread

    var onStopped: ((Error?) -> Void)?
    var onStats: ((Int, Int, Int) -> Void)?        // (fps sent, frames skipped, keyframes) per second
    var onClientChanged: ((LinkType?) -> Void)?    // nil = disconnected
    var onServerStatus: ((String) -> Void)?
    var onWifiStatus: ((String) -> Void)?
    var onPaired: (() -> Void)?

    private let deviceName: String

    /// Call from the main thread (UIKit values are passed in, not read here).
    init(wifiEnabled: Bool, deviceName: String) {
        server = StreamServer(wifiEnabled: wifiEnabled)
        self.deviceName = deviceName
        super.init()
        server.setHello(["name": deviceName])
        encoder.onEncoded = { [weak self] data, key, ts in
            self?.server.sendVideo(data, keyframe: key, timestampUs: ts)
        }
        server.onKeyframeRequested = { [weak self] in self?.keyframeRequested() }
        server.onConfig = { [weak self] cfg in self?.apply(config: cfg) }
        server.onClientConnected = { [weak self] link in
            guard let self else { return }
            self.lock.lock()
            self.currentLink = link
            let t = self.tuning
            self.lock.unlock()
            self.encoder.setBitrateFactor(link == .wifi ? t.bitrateWiFi : t.bitrateUSB)
            self.setClientConnected(true)
            self.onClientChanged?(link)
        }
        server.onClientDisconnected = { [weak self] in
            self?.setClientConnected(false)
            self?.onClientChanged?(nil)
        }
        server.onStatus = { [weak self] msg in self?.onServerStatus?(msg) }
        server.onWifiStatus = { [weak self] msg in self?.onWifiStatus?(msg) }
        server.onPaired = { [weak self] in self?.onPaired?() }
        // server.start() is called by the owner after it has hooked up callbacks.
    }

    var isCapturing: Bool { stream != nil }

    private func setClientConnected(_ connected: Bool) {
        lock.lock(); clientConnected = connected; lock.unlock()
    }

    // MARK: Start / stop

    /// `pixelSize`: the screen's native resolution in pixels (portrait).
    func start(filter: SCContentFilter, pixelSize: CGSize, completion: @escaping (Error?) -> Void) {
        lastFilter = filter
        lastPixelSize = pixelSize
        lock.lock(); let maxHeight = tuning.maxHeight; lock.unlock()
        let config = SCStreamConfiguration()
        let scale = min(1.0, maxHeight / max(pixelSize.height, 1))
        config.width = Int(pixelSize.width * scale) & ~1       // encoders want even sizes
        config.height = Int(pixelSize.height * scale) & ~1
        config.capturesAudio = false

        server.setHello(["w": config.width,
                         "h": config.height,
                         "name": deviceName])

        let s = SCStream(filter: filter, configuration: config, delegate: self)
        do {
            try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: captureQueue)
        } catch {
            completion(error)
            return
        }
        stream = s
        s.startCapture { [weak self] error in
            if error != nil { self?.stream = nil }
            completion(error)
        }
    }

    /// iOS can't change what a running stream captures, so stop it and start a new one.
    func restart(filter: SCContentFilter, pixelSize: CGSize, completion: @escaping (Error?) -> Void) {
        guard let old = stream else {
            start(filter: filter, pixelSize: pixelSize, completion: completion)
            return
        }
        stream = nil
        old.stopCapture { [weak self] _ in
            self?.start(filter: filter, pixelSize: pixelSize, completion: completion)
        }
    }

    func stop() {
        stream?.stopCapture { _ in }
        stream = nil
        captureQueue.async {
            self.lock.lock(); self.lastBuffer = nil; self.lock.unlock()
            self.encoder.invalidate()
        }
    }

    // MARK: Live tuning from the PC

    /// Applies settings sent by the PC (pc/settings.json). Unknown keys are ignored;
    /// values are clamped to sane ranges. Resolution changes restart the capture.
    func apply(config c: [String: Any]) {
        func num(_ k: String) -> Double? { (c[k] as? NSNumber)?.doubleValue }
        func clamp(_ v: Double, _ lo: Double, _ hi: Double) -> Double { min(hi, max(lo, v)) }

        lock.lock()
        let old = tuning
        if let v = num("max_height") { tuning.maxHeight = CGFloat(clamp(v, 480, 3000)) }
        if let v = num("max_fps") { tuning.maxFPS = clamp(v, 5, 120) }
        if let v = num("bitrate_usb") { tuning.bitrateUSB = clamp(v, 0.1, 3) }
        if let v = num("bitrate_wifi") { tuning.bitrateWiFi = clamp(v, 0.1, 3) }
        if let v = num("max_queued_frames_usb") { tuning.queuedUSB = Int(clamp(v, 1, 30)) }
        if let v = num("max_queued_frames_wifi") { tuning.queuedWiFi = Int(clamp(v, 1, 30)) }
        if let v = num("keyframe_interval_s") { tuning.keyframeInterval = clamp(v, 1, 10) }
        let t = tuning
        let link = currentLink
        lock.unlock()

        encoder.setBitrateFactor(link == .wifi ? t.bitrateWiFi : t.bitrateUSB)
        encoder.setKeyframeInterval(t.keyframeInterval)
        server.setLimits(usb: t.queuedUSB, wifi: t.queuedWiFi)

        if t.maxHeight != old.maxHeight {
            DispatchQueue.main.async {
                guard self.isCapturing, let f = self.lastFilter else { return }
                self.restart(filter: f, pixelSize: self.lastPixelSize) { [weak self] error in
                    if let error { self?.onStopped?(error) }
                }
            }
        }
    }

    // MARK: SCStreamDelegate

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        guard stream === self.stream else { return }   // an old stream we replaced
        self.stream = nil
        onStopped?(error)
    }

    // MARK: SCStreamOutput

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen,
              CMSampleBufferIsValid(sampleBuffer),
              let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        lock.lock()
        lastBuffer = pixelBuffer
        let sending = clientConnected
        lock.unlock()

        guard sending else { return }       // nobody watching: don't burn battery encoding

        // Cap at 60 fps. 120 Hz iPhones can deliver twice that, which doubles the
        // work for no benefit on a 60 Hz PC monitor.
        lock.lock(); let maxFPS = tuning.maxFPS; lock.unlock()
        let now = CACurrentMediaTime()
        let fpsCap = jpegMode ? min(maxFPS, jpegMaxFPS) : maxFPS
        guard now - lastEncodeTime >= 1.0 / (fpsCap + 2) else { return }
        lastEncodeTime = now

        if jpegMode {
            sendJPEG(pixelBuffer)
        } else {
            let ok = encoder.encode(pixelBuffer, pts: hostTime())
            let errors = encoder.takeErrorCount()
            if !ok || errors > 0 {
                // Hardware encoder rejected the frame (iOS does this when the app
                // leaves the foreground). Rebuild it; if that keeps failing, fall back.
                hwFailStreak += 1
                encoder.invalidate()
                if hwFailStreak >= 3 {
                    jpegMode = true
                    onMode?(true)
                    onLog?("Video encoder blocked by iOS in background → JPEG fallback")
                    sendJPEG(pixelBuffer)
                }
            } else {
                hwFailStreak = 0
            }
        }
        countFrame()
    }

    // MARK: Helpers

    /// A viewer joined or lost sync. ScreenCaptureKit only delivers frames when
    /// the screen changes, so re-encode the last frame as a keyframe right away;
    /// otherwise the PC would show black until something on screen moves.
    private func keyframeRequested() {
        encoder.requestKeyframe()
        captureQueue.async {
            // If frames are flowing, the next one will carry the keyframe; only
            // re-send the last frame when the screen is static.
            guard CACurrentMediaTime() - self.lastEncodeTime > 0.1 else { return }
            self.lock.lock()
            let buf = self.lastBuffer
            let sending = self.clientConnected
            self.lock.unlock()
            if let buf, sending {
                self.lastEncodeTime = CACurrentMediaTime()
                if self.jpegMode {
                    self.sendJPEG(buf)
                } else {
                    self.encoder.encode(buf, pts: self.hostTime(), forceKey: true)
                }
            }
        }
    }

    private func sendJPEG(_ pixelBuffer: CVPixelBuffer) {
        guard let data = jpeg.encode(pixelBuffer) else { return }
        server.sendJPEG(data, timestampUs: Wire.nowUs())
    }

    /// Call when the app returns to the foreground: hardware video works again,
    /// and iOS has invalidated the old encoder session anyway.
    func appBecameActive() {
        captureQueue.async {
            self.encoder.invalidate()
            self.hwFailStreak = 0
            if self.jpegMode {
                self.jpegMode = false
                self.onMode?(false)
                self.onLog?("Back to hardware video")
            }
        }
    }

    private func hostTime() -> CMTime {
        CMClockGetTime(CMClockGetHostTimeClock())
    }

    private func countFrame() {
        frameCount += 1
        let now = CACurrentMediaTime()
        if now - lastStatsTime >= 1 {
            let fps = Int(Double(frameCount) / (now - lastStatsTime) + 0.5)
            frameCount = 0
            lastStatsTime = now
            onStats?(fps, server.takeSkippedCount(), encoder.takeKeyframeCount())
        }
    }
}
