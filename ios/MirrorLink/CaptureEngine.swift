import Foundation
import ScreenCaptureKit
import CoreMedia
import UIKit

/// Screen capture (ScreenCaptureKit, iOS 27+) -> H.264 encoder -> USB server.
final class CaptureEngine: NSObject, SCStreamOutput, SCStreamDelegate {
    let server = StreamServer()
    private let encoder = H264Encoder()
    private var stream: SCStream?
    private let captureQueue = DispatchQueue(label: "mirrorlink.capture", qos: .userInteractive)

    // State shared between queues
    private let lock = NSLock()
    private var clientConnected = false
    private var lastBuffer: CVPixelBuffer?       // re-sent when a viewer joins on a static screen
    private var frameCount = 0
    private var lastStatsTime = CACurrentMediaTime()

    var onStopped: ((Error?) -> Void)?
    var onFPS: ((Int) -> Void)?
    var onClientChanged: ((Bool) -> Void)?
    var onServerStatus: ((String) -> Void)?

    override init() {
        super.init()
        encoder.onEncoded = { [weak self] data, key, ts in
            self?.server.sendVideo(data, keyframe: key, timestampUs: ts)
        }
        server.onKeyframeRequested = { [weak self] in self?.keyframeRequested() }
        server.onClientConnected = { [weak self] in
            self?.setClientConnected(true)
            self?.onClientChanged?(true)
        }
        server.onClientDisconnected = { [weak self] in
            self?.setClientConnected(false)
            self?.onClientChanged?(false)
        }
        server.onStatus = { [weak self] msg in self?.onServerStatus?(msg) }
        server.start()
    }

    var isCapturing: Bool { stream != nil }

    private func setClientConnected(_ connected: Bool) {
        lock.lock(); clientConnected = connected; lock.unlock()
    }

    // MARK: Start / stop

    func start(filter: SCContentFilter, completion: @escaping (Error?) -> Void) {
        let config = SCStreamConfiguration()
        // Capture at the phone's native pixel resolution (portrait).
        let native = UIScreen.main.nativeBounds.size
        config.width = Int(native.width) & ~1       // encoders want even sizes
        config.height = Int(native.height) & ~1
        config.capturesAudio = false

        server.setHello(["v": 1,
                         "w": config.width,
                         "h": config.height,
                         "name": UIDevice.current.name])

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

    func update(filter: SCContentFilter) {
        stream?.updateContentFilter(filter) { _ in }
    }

    func stop() {
        stream?.stopCapture { _ in }
        stream = nil
        captureQueue.async {
            self.lock.lock(); self.lastBuffer = nil; self.lock.unlock()
            self.encoder.invalidate()
        }
    }

    // MARK: SCStreamDelegate

    func stream(_ stream: SCStream, didStopWithError error: Error) {
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
        encoder.encode(pixelBuffer, pts: hostTime())
        countFrame()
    }

    // MARK: Helpers

    /// A viewer joined or lost sync. ScreenCaptureKit only delivers frames when
    /// the screen changes, so re-encode the last frame as a keyframe right away;
    /// otherwise the PC would show black until something on screen moves.
    private func keyframeRequested() {
        encoder.requestKeyframe()
        captureQueue.async {
            self.lock.lock()
            let buf = self.lastBuffer
            let sending = self.clientConnected
            self.lock.unlock()
            if let buf, sending {
                self.encoder.encode(buf, pts: self.hostTime())
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
            onFPS?(fps)
        }
    }
}
