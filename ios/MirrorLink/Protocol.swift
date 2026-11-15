import Foundation

/// Wire protocol shared with the Windows viewer (pc/mirror.py).
///
/// Every message, in both directions, is:
///   [4 bytes] payload length (big-endian UInt32)
///   [1 byte ] message type
///   [8 bytes] timestamp in microseconds (big-endian UInt64, phone clock)
///   [N bytes] payload
enum MsgType: UInt8 {
    // phone -> PC
    case hello      = 0x01   // JSON: {"v":1,"w":W,"h":H,"name":"..."}
    case videoKey   = 0x02   // H.264 Annex-B access unit (keyframe, includes SPS/PPS)
    case videoDelta = 0x03   // H.264 Annex-B access unit
    // PC -> phone
    case requestKeyframe = 0x10
    case ping            = 0x11  // payload echoed back as pong
    case pong            = 0x12
}

enum Wire {
    static let port: UInt16 = 47800
    static let headerSize = 13

    static func frame(_ type: MsgType, timestampUs: UInt64, payload: Data) -> Data {
        var d = Data(capacity: headerSize + payload.count)
        var len = UInt32(payload.count).bigEndian
        var ts = timestampUs.bigEndian
        withUnsafeBytes(of: &len) { d.append(contentsOf: $0) }
        d.append(type.rawValue)
        withUnsafeBytes(of: &ts) { d.append(contentsOf: $0) }
        d.append(payload)
        return d
    }

    static func nowUs() -> UInt64 {
        UInt64(DispatchTime.now().uptimeNanoseconds / 1_000)
    }
}
