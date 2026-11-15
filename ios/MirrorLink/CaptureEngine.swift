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
    private var lastHWProbe: CFTimeInterval = 0     // background: retry the hardware encoder
    private var hwProbing = false
    private var lastOrientation = ""               // capture-queue only
    let input = InputBridge()
    var onLog: ((String) -> Void)?
    private var nextFrameDue: CFTimeInterval = 0     // capture-queue only (frame pacing)
    var onMode: ((Bool) -> Void)?      // true = JPEG fallback active
    var onControlReady: ((Bool) -> Void)?

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
        /// Background (JPEG) mode. Encoding is on the CPU, so the real rate may be
        /// lower than this on older iPhones; frames are simply skipped.
        var jpegMaxFPS = 60.0
        var jpegQuality = 0.6
        var jpegMaxHeight: CGFloat = 1400
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
        jpeg.onEncoded = { [weak self] data, ts in
            self?.server.sendJPEG(data, timestampUs: ts)
        }
        server.onKeyframeRequested = { [weak self] in self?.keyframeRequested() }
        server.onConfig = { [weak self] cfg in self?.apply(config: cfg) }
        server.onInput = { [weak self] m in self?.input.handle(m) }
        input.onStatus = { [weak self] st in
            self?.server.sendInputStatus(st)
            self?.onControlReady?(st["ready"] as? Bool ?? false)
        }
        input.onLog = { [weak self] msg in self?.onLog?(msg) }
        server.onClientConnected = { [weak self] link in
            guard let self else { return }
            self.lock.lock()
            self.currentLink = link
            let t = self.tuning
            self.lock.unlock()
            self.encoder.setBitrateFactor(link == .wifi ? t.bitrateWiFi : t.bitrateUSB)
            self.setClientConnected(true)
            self.input.start()
            self.onClientChanged?(link)
        }
        server.onClientDisconnected = { [weak self] in
            self?.input.stop()
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

    /// The screen in points (portrait); sent to the PC for Bluetooth control. Set on the main thread before start.
    var screenPoints: CGSize = .zero

    /// `pixelSize`: the screen's native resolution in pixels (portrait).
    func start(filter: SCContentFilter, pixelSize: CGSize, completion: @escaping (Error?) -> Void) {
        lastFilter = filter
        lastPixelSize = pixelSize
        captureQueue.async { self.orientationDiagLogged = false }
        lock.lock(); let maxHeight = tuning.maxHeight; lock.unlock()
        let config = SCStreamConfiguration()
        let scale = min(1.0, maxHeight / max(pixelSize.height, 1))
        config.width = Int(pixelSize.width * scale) & ~1       // encoders want even sizes
        config.height = Int(pixelSize.height * scale) & ~1
        config.capturesAudio = false
        // Deliver every screen refresh (up to 120 Hz); the pacing above picks the frames.
        config.minimumFrameInterval = CMTime(value: 1, timescale: 120)
        config.queueDepth = 4

        var hello: [String: Any] = ["w": config.width, "h": config.height, "name": deviceName]
        if screenPoints.width > 0 {          // for Bluetooth control: the screen in points
            hello["pt_w"] = Double(screenPoints.width)
            hello["pt_h"] = Double(screenPoints.height)
        }
        server.setHello(hello)

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
        if let v = num("jpeg_max_fps") { tuning.jpegMaxFPS = clamp(v, 5, 60) }
        if let v = num("jpeg_quality") { tuning.jpegQuality = clamp(v, 0.2, 0.95) }
        if let v = num("jpeg_max_height") { tuning.jpegMaxHeight = CGFloat(clamp(v, 480, 3000)) }
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

        reportOrientation(sampleBuffer, pixelBuffer: pixelBuffer)

        guard sending else { return }       // nobody watching: don't burn battery encoding

        // Cap at 60 fps. 120 Hz iPhones can deliver twice that, which doubles the
        // work for no benefit on a 60 Hz PC monitor.
        lock.lock(); let maxFPS = tuning.maxFPS; let jpegFPS = tuning.jpegMaxFPS; lock.unlock()
        let now = CACurrentMediaTime()
        let fpsCap = jpegMode ? min(maxFPS, jpegFPS) : maxFPS
        // Keep frames on a schedule rather than "at least 1/60 s since the last one":
        // on a 60 Hz screen frames arrive ~16.7 ms apart with a little jitter, and a
        // strict gap check threw away every frame that came a hair early (~45 fps).
        let interval = 1.0 / fpsCap
        guard now >= nextFrameDue - interval * 0.3 else { return }
        nextFrameDue = max(nextFrameDue + interval, now + interval * 0.5)
        lastEncodeTime = now

        if jpegMode {
            // Every 3 s, see whether iOS lets us use the hardware encoder again
            // (it can, depending on iOS version); if so, go back to smooth H.264.
            if now - lastHWProbe > 3 {
                lastHWProbe = now
                encoder.invalidate()
                _ = encoder.takeErrorCount()
                _ = encoder.takeSuccessCount()
                hwProbing = encoder.encode(pixelBuffer, pts: hostTime(), forceKey: true)
            } else if hwProbing {
                if encoder.takeSuccessCount() > 0 {
                    hwProbing = false
                    hwFailStreak = 0
                    jpegMode = false
                    onMode?(false)
                    onLog?("Hardware video works in the background → back to H.264")
                    encoder.encode(pixelBuffer, pts: hostTime())
                    countFrame()
                    return
                }
                if encoder.takeErrorCount() > 0 || now - lastHWProbe > 1 { hwProbing = false }
            }
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

    /// Which way the screen content is turned (portrait, landscape left/right...). The
    /// pixels themselves always arrive portrait. iOS can put this in several places
    /// depending on the version, so look in all of them; the first time, write what
    /// was there to the event log so it can be checked on a real phone.
    private var orientationDiagLogged = false     // capture-queue only

    private func reportOrientation(_ sampleBuffer: CMSampleBuffer, pixelBuffer: CVPixelBuffer) {
        var found: Any?
        var source = ""
        let arr = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false) as? [[String: Any]]
        let info = arr?.first ?? [:]
        if let v = info[SCStreamFrameInfo.videoOrientation.rawValue] {
            found = v; source = "frame info"
        } else if let kv = info.first(where: { $0.key.lowercased().contains("orient") }) {
            found = kv.value; source = "frame info (\(kv.key))"
        }
        if found == nil {
            for key in ["RPVideoSampleOrientationKey", SCStreamFrameInfo.videoOrientation.rawValue] {
                if let v = CMGetAttachment(sampleBuffer, key: key as CFString, attachmentModeOut: nil) {
                    found = v; source = "sample (\(key))"; break
                }
            }
        }
        let pbAttach = (CVBufferCopyAttachments(pixelBuffer, .shouldPropagate) as? [String: Any]) ?? [:]
        if found == nil, let kv = pbAttach.first(where: { $0.key.lowercased().contains("orient") }) {
            found = kv.value; source = "pixel buffer (\(kv.key))"
        }

        // One-time report of what this iOS version attaches (only for frames with content).
        if !orientationDiagLogged, info.count > 1 {
            orientationDiagLogged = true
            let short: (Any) -> String = { v in String(String(describing: v).prefix(40)) }
            let infoKeys = info.keys.sorted().map { k -> String in
                let name = k.replacingOccurrences(of: "SCStreamFrameInfo", with: "")
                return name.lowercased().contains("rect") || name.lowercased().contains("orient")
                    ? "\(name)=\(short(info[k]!))" : name
            }
            onLog?("Frame info: " + infoKeys.joined(separator: ", "))
            onLog?("Pixel attachments: " + pbAttach.keys.sorted().joined(separator: ", "))
            onLog?(found == nil ? "Orientation: not provided by iOS" : "Orientation from \(source)")
        }

        guard let value = found else { return }
        let raw: Any = (value as? NSNumber) ?? String(describing: value)
        let key = String(describing: raw)
        guard key != lastOrientation else { return }
        lastOrientation = key
        server.sendOrientation(["o": raw])
        input.invalidateWindowSize()
        onLog?("Orientation changed (raw \(key))")
    }

    private func sendJPEG(_ pixelBuffer: CVPixelBuffer) {
        lock.lock(); let q = tuning.jpegQuality; let maxH = tuning.jpegMaxHeight; lock.unlock()
        let h = CGFloat(CVPixelBufferGetHeight(pixelBuffer))
        let scale = min(1, maxH / max(h, 1))
        // Returns false if all workers are busy: this frame is skipped, the next one goes.
        jpeg.submit(pixelBuffer, quality: CGFloat(q), scale: scale, timestampUs: Wire.nowUs())
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
