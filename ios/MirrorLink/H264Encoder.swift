import Foundation
import VideoToolbox
import CoreMedia
import QuartzCore

/// Hardware H.264 encoder tuned for latency, not file size:
/// - low-latency rate control (Apple's mode for video calls)
/// - no B-frames / frame reordering (decoder can show each frame immediately)
/// - real-time priority
/// Output is Annex-B (start-code) access units, the format FFmpeg on the PC
/// decodes directly. Keyframes carry SPS/PPS so the PC can join at any keyframe.
final class H264Encoder {
    /// (annexB, isKeyframe, timestampMicroseconds)
    var onEncoded: ((Data, Bool, UInt64) -> Void)?

    private var session: VTCompressionSession?
    private(set) var width: Int32 = 0
    private(set) var height: Int32 = 0
    private var forceKeyframe = true
    private var bitrateFactor = 1.0          // lower on Wi-Fi
    private var bitrateDirty = false
    private var keyframeInterval = 3.0
    private var keyIntervalDirty = false
    private var lastForcedKey: CFTimeInterval = 0
    private var keyframeCount = 0
    private let lock = NSLock()

    private static let startCode: [UInt8] = [0, 0, 0, 1]

    func requestKeyframe() {
        lock.lock(); forceKeyframe = true; lock.unlock()
    }

    /// Keyframes produced since the last call.
    func takeKeyframeCount() -> Int {
        lock.lock(); defer { lock.unlock() }
        let n = keyframeCount; keyframeCount = 0; return n
    }

    /// 1.0 for USB; less for Wi-Fi, where a smaller stream means fewer stalls.
    func setBitrateFactor(_ f: Double) {
        lock.lock()
        if f != bitrateFactor { bitrateFactor = f; bitrateDirty = true }
        lock.unlock()
    }

    func setKeyframeInterval(_ seconds: Double) {
        lock.lock()
        if seconds != keyframeInterval { keyframeInterval = seconds; keyIntervalDirty = true }
        lock.unlock()
    }

    private func targetBitrate(_ w: Int32, _ h: Int32) -> Int {
        lock.lock(); let f = bitrateFactor; lock.unlock()
        // ~0.08 bits per pixel per frame at 60 fps, scaled by link.
        let base = Double(w) * Double(h) * 60 * 0.08 * f
        return max(3_000_000, min(30_000_000, Int(base)))
    }

    func invalidate() {
        if let s = session {
            VTCompressionSessionCompleteFrames(s, untilPresentationTimeStamp: .invalid)
            VTCompressionSessionInvalidate(s)
        }
        session = nil
        width = 0; height = 0
    }

    private func makeSession(width: Int32, height: Int32) -> Bool {
        invalidate()

        func create(lowLatency: Bool) -> VTCompressionSession? {
            var s: VTCompressionSession?
            let spec: CFDictionary? = lowLatency
                ? [kVTVideoEncoderSpecification_EnableLowLatencyRateControl: true] as CFDictionary
                : nil
            let status = VTCompressionSessionCreate(
                allocator: nil, width: width, height: height,
                codecType: kCMVideoCodecType_H264,
                encoderSpecification: spec,
                imageBufferAttributes: nil,
                compressedDataAllocator: nil,
                outputCallback: nil, refcon: nil,
                compressionSessionOut: &s)
            return status == noErr ? s : nil
        }

        guard let s = create(lowLatency: true) ?? create(lowLatency: false) else { return false }

        let bitrate = targetBitrate(width, height)

        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_RealTime, value: kCFBooleanTrue)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_AllowFrameReordering, value: kCFBooleanFalse)
        if VTSessionSetProperty(s, key: kVTCompressionPropertyKey_ProfileLevel,
                                value: kVTProfileLevel_H264_ConstrainedHigh_AutoLevel) != noErr {
            VTSessionSetProperty(s, key: kVTCompressionPropertyKey_ProfileLevel,
                                 value: kVTProfileLevel_H264_High_AutoLevel)
        }
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_AverageBitRate, value: bitrate as CFNumber)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_ExpectedFrameRate, value: 60 as CFNumber)
        // Periodic keyframe so a glitch heals within a few seconds even if
        // the PC never asks for one.
        lock.lock(); let kInt = keyframeInterval; keyIntervalDirty = false; lock.unlock()
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration, value: kInt as CFNumber)
        VTCompressionSessionPrepareToEncodeFrames(s)

        session = s
        self.width = width
        self.height = height
        requestKeyframe()
        return true
    }

    func encode(_ pixelBuffer: CVPixelBuffer, pts: CMTime) {
        let w = Int32(CVPixelBufferGetWidth(pixelBuffer))
        let h = Int32(CVPixelBufferGetHeight(pixelBuffer))
        if session == nil || w != width || h != height {
            guard makeSession(width: w, height: h) else { return }
        }
        guard let session else { return }

        lock.lock()
        // Keyframes are big; never force more than ~3 per second, or a struggling
        // link gets buried in them. A pending request waits for the next slot.
        let now = CACurrentMediaTime()
        let key = forceKeyframe && now - lastForcedKey >= 0.3
        if key { forceKeyframe = false; lastForcedKey = now }
        let dirty = bitrateDirty
        bitrateDirty = false
        let kDirty = keyIntervalDirty
        keyIntervalDirty = false
        let kInt = keyframeInterval
        lock.unlock()

        if kDirty {
            VTSessionSetProperty(session, key: kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration,
                                 value: kInt as CFNumber)
        }

        if dirty {
            VTSessionSetProperty(session, key: kVTCompressionPropertyKey_AverageBitRate,
                                 value: targetBitrate(width, height) as CFNumber)
        }

        let props: CFDictionary? = key
            ? [kVTEncodeFrameOptionKey_ForceKeyFrame: true] as CFDictionary
            : nil
        let ts = Wire.nowUs()

        VTCompressionSessionEncodeFrame(
            session, imageBuffer: pixelBuffer, presentationTimeStamp: pts,
            duration: .invalid, frameProperties: props, infoFlagsOut: nil
        ) { [weak self] status, _, sampleBuffer in
            guard status == noErr, let sampleBuffer, let self else { return }
            self.handleOutput(sampleBuffer, timestampUs: ts)
        }
    }

    private func handleOutput(_ sbuf: CMSampleBuffer, timestampUs: UInt64) {
        guard let block = CMSampleBufferGetDataBuffer(sbuf),
              let fmt = CMSampleBufferGetFormatDescription(sbuf) else { return }

        var isKey = true
        if let attachments = CMSampleBufferGetSampleAttachmentsArray(sbuf, createIfNecessary: false) as? [[CFString: Any]],
           let first = attachments.first,
           let notSync = first[kCMSampleAttachmentKey_NotSync] as? Bool {
            isKey = !notSync
        }

        var out = Data()
        var nalLengthSize: Int32 = 4

        if isKey {
            // Prepend SPS + PPS so the decoder can (re)start here.
            var count = 0
            CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                fmt, parameterSetIndex: 0, parameterSetPointerOut: nil, parameterSetSizeOut: nil,
                parameterSetCountOut: &count, nalUnitHeaderLengthOut: &nalLengthSize)
            for i in 0..<count {
                var ptr: UnsafePointer<UInt8>?
                var size = 0
                if CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                    fmt, parameterSetIndex: i, parameterSetPointerOut: &ptr, parameterSetSizeOut: &size,
                    parameterSetCountOut: nil, nalUnitHeaderLengthOut: nil) == noErr, let ptr {
                    out.append(contentsOf: H264Encoder.startCode)
                    out.append(ptr, count: size)
                }
            }
        } else {
            CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                fmt, parameterSetIndex: 0, parameterSetPointerOut: nil, parameterSetSizeOut: nil,
                parameterSetCountOut: nil, nalUnitHeaderLengthOut: &nalLengthSize)
        }

        // Copy the encoded bytes out (block buffers may be non-contiguous).
        let total = CMBlockBufferGetDataLength(block)
        var avcc = Data(count: total)
        let copyStatus = avcc.withUnsafeMutableBytes { raw -> OSStatus in
            CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: total, destination: raw.baseAddress!)
        }
        guard copyStatus == noErr else { return }

        // AVCC (length-prefixed NAL units) -> Annex-B (start codes).
        let n = Int(nalLengthSize)
        avcc.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            let bytes = raw.bindMemory(to: UInt8.self)
            var offset = 0
            while offset + n <= total {
                var nalLen = 0
                for k in 0..<n { nalLen = (nalLen << 8) | Int(bytes[offset + k]) }
                offset += n
                guard nalLen > 0, offset + nalLen <= total else { break }
                out.append(contentsOf: H264Encoder.startCode)
                out.append(UnsafeBufferPointer(rebasing: bytes[offset ..< offset + nalLen]))
                offset += nalLen
            }
        }

        if isKey { lock.lock(); keyframeCount += 1; lock.unlock() }
        onEncoded?(out, isKey, timestampUs)
    }
}
