import Foundation
import CoreImage
import CoreVideo
import ImageIO

/// CPU-only JPEG encoder, used as a fallback while the app is in the background.
/// iOS invalidates the hardware video encoder (VideoToolbox) when an app leaves the
/// foreground; CoreImage's software renderer still works there.
final class JPEGEncoder {
    private let context = CIContext(options: [.useSoftwareRenderer: true, .cacheIntermediates: false])
    private let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!

    func encode(_ pixelBuffer: CVPixelBuffer, quality: CGFloat = 0.6) -> Data? {
        let image = CIImage(cvPixelBuffer: pixelBuffer)
        return context.jpegRepresentation(
            of: image, colorSpace: colorSpace,
            options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: quality])
    }
}
