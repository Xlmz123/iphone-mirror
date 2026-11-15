import Foundation
import AVFoundation

/// Keeps the app running while mirroring by playing silence.
///
/// iOS 27 is supposed to keep a screen-capturing app alive via the "screen-capture"
/// background mode, but it can still suspend the app once you leave it. An active
/// audio session prevents that. It mixes with other audio, so your music keeps playing.
final class KeepAlive {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var attached = false
    private(set) var running = false
    var onLog: ((String) -> Void)?

    init() {
        NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification, object: nil, queue: .main
        ) { [weak self] note in
            // e.g. a phone call ended: restart the silent audio.
            guard let self, self.running,
                  let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  AVAudioSession.InterruptionType(rawValue: raw) == .ended else { return }
            self.running = false
            self.start()
        }
    }

    func start() {
        guard !running else { return }
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, options: [.mixWithOthers])
            try session.setActive(true)

            let format = AVAudioFormat(standardFormatWithSampleRate: 44_100, channels: 1)!
            if !attached {
                engine.attach(player)
                engine.connect(player, to: engine.mainMixerNode, format: format)
                attached = true
            }
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 44_100)!
            buffer.frameLength = 44_100
            if let ch = buffer.floatChannelData {
                ch[0].update(repeating: 0, count: Int(buffer.frameLength))   // silence
            }
            try engine.start()
            player.scheduleBuffer(buffer, at: nil, options: .loops)
            player.play()
            running = true
            onLog?("Background keep-alive on")
        } catch {
            onLog?("Keep-alive failed: \(error.localizedDescription)")
        }
    }

    func stop() {
        guard running else { return }
        player.stop()
        engine.stop()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        running = false
    }
}
