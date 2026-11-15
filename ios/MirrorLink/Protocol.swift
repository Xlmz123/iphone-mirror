import Foundation

/// Wire protocol shared with the Windows viewer (pc/mirror.py).
///
/// Every message, in both directions, is:
///   [4 bytes] payload length (big-endian UInt32)
///   [1 byte ] message type
///   [8 bytes] timestamp in microseconds (big-endian UInt64, phone clock)
///   [N bytes] payload
///
/// Over USB messages are sent as-is (the cable is a private, trusted link).
/// Over Wi-Fi each message is wrapped in an encrypted record (SecureChannel.swift).
enum MsgType: UInt8 {
    // phone -> PC
    case hello      = 0x01   // JSON: {"v":2,"w":W,"h":H,"name":"..","id":"..","link":"usb"|"wifi"}
    case videoKey   = 0x02   // H.264 Annex-B access unit (keyframe, includes SPS/PPS)
    case videoDelta = 0x03   // H.264 Annex-B access unit
    case jpegFrame  = 0x04   // complete JPEG picture (background fallback)
    case orientation = 0x05  // JSON: {"o": raw SCStreamFrameInfo.videoOrientation value}
    case kicked     = 0x22   // another viewer connected and took over; don't auto-reconnect
    case inputStatus = 0x31  // JSON: {"ready": Bool, "detail": String} (control helper state)
    case pairInfo   = 0x21   // JSON: {"id":hex,"key":base64,"name":".."}  (USB only)
    // PC -> phone
    case requestKeyframe = 0x10
    case ping            = 0x11  // payload echoed back as pong
    case pong            = 0x12
    case ack             = 0x13  // payload: 8-byte timestamp of the video frame received
    case config          = 0x14  // JSON tuning settings from pc/settings.json (see CaptureEngine.apply)
    case pairRequest     = 0x20  // USB only
    case input           = 0x30  // JSON input event (see InputBridge)
}

enum Wire {
    static let usbPort: UInt16 = 47800
    static let wifiPort: UInt16 = 47801
    static let bonjourType = "_mirrorlink._tcp"
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

    static func readUInt64BE(_ d: Data) -> UInt64 {
        var v: UInt64 = 0
        for b in d.prefix(8) { v = (v << 8) | UInt64(b) }
        return v
    }
}
