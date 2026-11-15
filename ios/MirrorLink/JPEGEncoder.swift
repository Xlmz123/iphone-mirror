import Foundation
import CoreImage
import CoreVideo
import ImageIO

/// CPU-only JPEG encoder, used as a fallback while the app is in the background.
/// iOS can refuse the hardware video encoder (VideoToolbox) for background apps;
/// CoreImage's software renderer still works there.
///
/// To get closer to 60 fps it compresses several frames at once on separate CPU
/// cores. Each frame is first copied out of ScreenCaptureKit's buffer so capture
/// isn't held up while it's being compressed.
final class JPEGEncoder {
    /// (jpeg, timestampMicroseconds) — may arrive slightly out of order; the
    /// server drops anything older than what it already sent.
    var onEncoded: ((Data, UInt64) -> Void)?

    private let workers: Int
    private let contexts: [CIContext]
    private let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
    private let queue = DispatchQueue(label: "mirrorlink.jpeg", qos: .userInteractive, attributes: .concurrent)
    private let lock = NSLock()
    private var busy: [Bool]
    private var pool: CVPixelBufferPool?
    private var poolKey = ""

    init(workers: Int = 3) {
        self.workers = workers
        contexts = (0..<workers).map { _ in
            CIContext(options: [.useSoftwareRenderer: true, .cacheIntermediates: false])
        }
        busy = Array(repeating: false, count: workers)
    }

    /// Starts compressing a frame. Returns false (frame skipped) if every worker is busy.
    @discardableResult
    func submit(_ pixelBuffer: CVPixelBuffer, quality: CGFloat, scale: CGFloat, timestampUs: UInt64) -> Bool {
        lock.lock()
        guard let slot = busy.firstIndex(of: false) else { lock.unlock(); return false }
        busy[slot] = true
        lock.unlock()

        guard let copy = copyBuffer(pixelBuffer) else {
            lock.lock(); busy[slot] = false; lock.unlock()
            return false
        }
        queue.async {
            defer { self.lock.lock(); self.busy[slot] = false; self.lock.unlock() }
            var image = CIImage(cvPixelBuffer: copy)
            if scale < 0.999 {
                image = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
            }
            if let data = self.contexts[slot].jpegRepresentation(
                of: image, colorSpace: self.colorSpace,
                options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: quality]) {
                self.onEncoded?(data, timestampUs)
            }
        }
        return true
    }

    /// Copies ScreenCaptureKit's buffer into one of ours (any pixel format, any planes).
    private func copyBuffer(_ src: CVPixelBuffer) -> CVPixelBuffer? {
        let w = CVPixelBufferGetWidth(src), h = CVPixelBufferGetHeight(src)
        let fmt = CVPixelBufferGetPixelFormatType(src)
        let key = "\(w)x\(h)x\(fmt)"
        if pool == nil || key != poolKey {
            let attrs: [String: Any] = [
                kCVPixelBufferPixelFormatTypeKey as String: fmt,
                kCVPixelBufferWidthKey as String: w,
                kCVPixelBufferHeightKey as String: h,
                kCVPixelBufferIOSurfacePropertiesKey as String: [:] as [String: Any],
            ]
            var p: CVPixelBufferPool?
            CVPixelBufferPoolCreate(nil, [kCVPixelBufferPoolMinimumBufferCountKey as String: workers + 1] as CFDictionary,
                                    attrs as CFDictionary, &p)
            pool = p
            poolKey = key
        }
        guard let pool else { return nil }
        var dst: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(nil, pool, &dst) == kCVReturnSuccess, let dst else { return nil }

        CVPixelBufferLockBaseAddress(src, .readOnly)
        CVPixelBufferLockBaseAddress(dst, [])
        defer {
            CVPixelBufferUnlockBaseAddress(dst, [])
            CVPixelBufferUnlockBaseAddress(src, .readOnly)
        }
        if CVPixelBufferIsPlanar(src) {
            for plane in 0..<CVPixelBufferGetPlaneCount(src) {
                guard let s = CVPixelBufferGetBaseAddressOfPlane(src, plane),
                      let d = CVPixelBufferGetBaseAddressOfPlane(dst, plane) else { return nil }
                copyRows(s, CVPixelBufferGetBytesPerRowOfPlane(src, plane),
                         d, CVPixelBufferGetBytesPerRowOfPlane(dst, plane),
                         CVPixelBufferGetHeightOfPlane(src, plane))
            }
        } else {
            guard let s = CVPixelBufferGetBaseAddress(src), let d = CVPixelBufferGetBaseAddress(dst) else { return nil }
            copyRows(s, CVPixelBufferGetBytesPerRow(src), d, CVPixelBufferGetBytesPerRow(dst), h)
        }
        return dst
    }

    private func copyRows(_ s: UnsafeMutableRawPointer, _ sStride: Int,
                          _ d: UnsafeMutableRawPointer, _ dStride: Int, _ rows: Int) {
        if sStride == dStride {
            memcpy(d, s, sStride * rows)
        } else {
            let n = min(sStride, dStride)
            for r in 0..<rows { memcpy(d + r * dStride, s + r * sStride, n) }
        }
    }
}
