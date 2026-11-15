import Foundation
import Network
import CryptoKit

enum LinkType: String { case usb, wifi }

/// Two listeners, one viewer at a time:
///  - USB  (port 47800): bound to loopback; the PC reaches it through the cable
///    via Apple's usbmux service. Plain messages; also hands out the pairing key.
///  - Wi-Fi (port 47801): advertised with Bonjour; every connection must prove it
///    holds the pairing key, and all traffic is encrypted (SecureChannel).
///
/// Lag control: the PC acknowledges each video frame as it arrives. If too many
/// frames are unacknowledged (the link is falling behind), new frames are dropped
/// until the backlog clears, then a fresh keyframe resyncs the picture. This keeps
/// what you see current instead of letting a delay build up.
final class StreamServer {
    var onClientConnected: ((LinkType) -> Void)?
    var onClientDisconnected: (() -> Void)?
    var onKeyframeRequested: (() -> Void)?
    var onStatus: ((String) -> Void)?
    var onWifiStatus: ((String) -> Void)?
    var onPaired: (() -> Void)?
    var onConfig: (([String: Any]) -> Void)?

    private let queue = DispatchQueue(label: "mirrorlink.net", qos: .userInteractive)
    private var usbListener: NWListener?
    private var wifiListener: NWListener?
    private var wifiEnabled: Bool
    private var active: Session?
    private var pending: [ObjectIdentifier: Session] = [:]   // connected, not yet active (keeps them alive)
    private var helloBase: [String: Any] = [:]

    // Lag control state (queue-confined)
    private var outstanding: [UInt64] = []
    private var dropping = false
    private var keyAskedWhileDropping = false
    private var skipped = 0
    private var maxOutstandingUSB = 4
    private var maxOutstandingWiFi = 6

    final class Session {
        enum State { case awaitClientHello, awaitClientProof, ready }
        let conn: NWConnection
        let link: LinkType
        var state: State
        var raw = Data()        // bytes off the socket
        var plain = Data()      // decrypted protocol bytes
        var channel: SecureChannel?
        var nonceC = Data()
        var nonceS = Data()
        var closed = false

        init(_ conn: NWConnection, _ link: LinkType) {
            self.conn = conn
            self.link = link
            self.state = link == .usb ? .ready : .awaitClientHello
        }
    }

    init(wifiEnabled: Bool) {
        self.wifiEnabled = wifiEnabled
    }

    func start() {
        queue.async {
            self.startUSB()
            if self.wifiEnabled { self.startWiFi() } else { self.onWifiStatus?("Wi-Fi off") }
        }
    }

    // MARK: Listeners

    private func tcpParams() -> NWParameters {
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        let p = NWParameters(tls: nil, tcp: tcp)
        p.allowLocalEndpointReuse = true
        return p
    }

    private func startUSB() {
        // Listen normally, but only accept connections that come from the phone itself
        // (loopback). That's how usbmux delivers traffic from the USB cable. Anything from
        // the network is rejected in accept(). (Restricting the listener to the loopback
        // interface type directly isn't reliable on iOS.)
        let params = tcpParams()
        params.prohibitedInterfaceTypes = [.cellular]
        let l: NWListener
        do {
            l = try NWListener(using: params, on: NWEndpoint.Port(rawValue: Wire.usbPort)!)
        } catch {
            onStatus?("USB: could not open port \(Wire.usbPort): \(error)")
            queue.asyncAfter(deadline: .now() + 2) { self.startUSB() }
            return
        }
        l.newConnectionHandler = { [weak self] c in
            guard let self else { return }
            if StreamServer.isLoopback(c.endpoint) {
                self.accept(c, .usb)
            } else {
                self.onStatus?("USB: ignored a connection from \(c.endpoint)")
                c.cancel()   // not from the cable
            }
        }
        l.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            switch state {
            case .ready: self.onStatus?("USB ready (port \(Wire.usbPort))")
            case .waiting(let err): self.onStatus?("USB waiting: \(err)")
            case .failed(let err):
                self.onStatus?("USB failed: \(err) — retrying")
                l.cancel()
                self.queue.asyncAfter(deadline: .now() + 1) { self.startUSB() }
            default: break
            }
        }
        l.start(queue: queue)
        usbListener = l
    }

    private func startWiFi() {
        wifiListener?.cancel()
        let params = tcpParams()
        params.prohibitedInterfaceTypes = [.cellular, .loopback]
        guard let l = try? NWListener(using: params, on: NWEndpoint.Port(rawValue: Wire.wifiPort)!) else {
            onWifiStatus?("Could not open Wi-Fi port"); return
        }
        l.service = NWListener.Service(name: nil, type: Wire.bonjourType, domain: nil,
                                       txtRecord: NWTXTRecord(["id": Pairing.shared.idHex]))
        l.newConnectionHandler = { [weak self] c in self?.accept(c, .wifi) }
        l.stateUpdateHandler = { [weak self, weak l] state in
            guard let self, let l, self.wifiListener === l else { return }
            switch state {
            case .ready:
                self.onWifiStatus?("Wi-Fi ready")
            case .waiting(let err):
                self.onWifiStatus?(StreamServer.describe(err))
            case .failed(let err):
                self.onWifiStatus?(StreamServer.describe(err))
                l.cancel()
                self.queue.asyncAfter(deadline: .now() + 2) {
                    if self.wifiEnabled && self.wifiListener === l { self.startWiFi() }
                }
            default: break
            }
        }
        l.start(queue: queue)
        wifiListener = l
    }

    static func isLoopback(_ endpoint: NWEndpoint) -> Bool {
        guard case .hostPort(let host, _) = endpoint else { return false }
        switch host {
        case .ipv4(let a): return a.isLoopback
        case .ipv6(let a): return a.isLoopback || (a.asIPv4?.isLoopback ?? false)
        case .name(let n, _): return n == "localhost"
        @unknown default: return false
        }
    }

    private static func describe(_ err: NWError) -> String {
        if case .dns(let code) = err, code == -65570 {   // kDNSServiceErr_PolicyDenied
            return "Allow Local Network: Settings → Privacy & Security → Local Network → MirrorLink"
        }
        return "Wi-Fi unavailable (\(err))"
    }

    func setWifiEnabled(_ on: Bool) {
        queue.async {
            self.wifiEnabled = on
            if on {
                self.startWiFi()
            } else {
                self.wifiListener?.cancel()
                self.wifiListener = nil
                self.dropWiFiSessions()
                self.onWifiStatus?("Wi-Fi off")
            }
        }
    }

    /// New key + ID. Every previously paired PC must pair over USB again.
    func resetPairing() {
        queue.async {
            Pairing.shared.regenerate()
            self.dropWiFiSessions()
            if self.wifiEnabled { self.startWiFi() }   // re-advertise with the new ID
        }
    }

    private func dropWiFiSessions() {
        for s in pending.values where s.link == .wifi { s.conn.cancel() }
        if let a = active, a.link == .wifi { a.conn.cancel() }
    }

    // MARK: Connections

    private func accept(_ conn: NWConnection, _ link: LinkType) {
        let s = Session(conn, link)
        pending[ObjectIdentifier(s)] = s
        if link == .wifi {
            // Unauthenticated connections get 5 seconds to prove themselves.
            queue.asyncAfter(deadline: .now() + 5) { [weak self, weak s] in
                guard let self, let s, s.state != .ready else { return }
                self.close(s)
            }
        }
        conn.stateUpdateHandler = { [weak self, weak s] state in
            guard let self, let s else { return }
            switch state {
            case .ready:
                if s.link == .usb { self.activate(s) }
                self.receive(s)
            case .failed, .cancelled:
                self.closed(s)
            default: break
            }
        }
        conn.start(queue: queue)
    }

    private func activate(_ s: Session) {
        pending.removeValue(forKey: ObjectIdentifier(s))
        let old = active
        active = s                      // set first so the old one's teardown is ignored
        old?.conn.cancel()
        outstanding.removeAll()
        dropping = true                 // nothing useful until a keyframe
        keyAskedWhileDropping = true

        var hello = helloBase
        hello["v"] = 2
        hello["id"] = Pairing.shared.idHex
        hello["link"] = s.link.rawValue
        if let data = try? JSONSerialization.data(withJSONObject: hello) {
            transmit(s, Wire.frame(.hello, timestampUs: Wire.nowUs(), payload: data))
        }
        onClientConnected?(s.link)
        onKeyframeRequested?()
    }

    private func close(_ s: Session) {
        s.conn.cancel()
    }

    private func closed(_ s: Session) {
        guard !s.closed else { return }
        s.closed = true
        pending.removeValue(forKey: ObjectIdentifier(s))
        if active === s {
            active = nil
            outstanding.removeAll()
            onClientDisconnected?()
        }
    }

    // MARK: Sending

    /// How many unacknowledged frames are allowed before skipping starts.
    func setLimits(usb: Int, wifi: Int) {
        queue.async {
            self.maxOutstandingUSB = max(1, usb)
            self.maxOutstandingWiFi = max(1, wifi)
        }
    }

    /// Frames skipped for lag control since the last call. (Never call from `queue`.)
    func takeSkippedCount() -> Int {
        queue.sync { let n = skipped; skipped = 0; return n }
    }

    func setHello(_ json: [String: Any]) {
        queue.async {
            self.helloBase = json
            guard let s = self.active else { return }
            var hello = json
            hello["v"] = 2
            hello["id"] = Pairing.shared.idHex
            hello["link"] = s.link.rawValue
            if let data = try? JSONSerialization.data(withJSONObject: hello) {
                self.transmit(s, Wire.frame(.hello, timestampUs: Wire.nowUs(), payload: data))
            }
        }
    }

    private func transmit(_ s: Session, _ msg: Data) {
        guard !s.closed else { return }
        if s.link == .wifi {
            guard let ch = s.channel, let record = try? ch.seal(msg) else { return }
            s.conn.send(content: record, completion: .contentProcessed { _ in })
        } else {
            s.conn.send(content: msg, completion: .contentProcessed { _ in })
        }
    }

    func sendVideo(_ annexB: Data, keyframe: Bool, timestampUs: UInt64) {
        queue.async {
            guard let s = self.active else { return }
            let limit = s.link == .usb ? self.maxOutstandingUSB : self.maxOutstandingWiFi

            if keyframe {
                self.dropping = false
            } else if self.dropping {
                self.skipped += 1
                self.maybeRequestKeyframe(limit: limit)
                return
            } else if self.outstanding.count >= limit {
                // Link is behind: start skipping frames.
                self.skipped += 1
                self.dropping = true
                self.keyAskedWhileDropping = false
                return
            }

            self.outstanding.append(timestampUs)
            self.transmit(s, Wire.frame(keyframe ? .videoKey : .videoDelta,
                                        timestampUs: timestampUs, payload: annexB))
        }
    }

    /// JPEG frames are self-contained: if the link is behind, just skip this one.
    func sendJPEG(_ jpeg: Data, timestampUs: UInt64) {
        queue.async {
            guard let s = self.active else { return }
            let limit = s.link == .usb ? self.maxOutstandingUSB : self.maxOutstandingWiFi
            if self.outstanding.count >= limit { self.skipped += 1; return }
            self.outstanding.append(timestampUs)
            self.transmit(s, Wire.frame(.jpegFrame, timestampUs: timestampUs, payload: jpeg))
        }
    }

    /// Once the backlog has mostly cleared, ask for a keyframe to resync.
    private func maybeRequestKeyframe(limit: Int) {
        guard dropping, !keyAskedWhileDropping, outstanding.count <= limit / 2 else { return }
        keyAskedWhileDropping = true
        onKeyframeRequested?()
    }

    // MARK: Receiving

    private func receive(_ s: Session) {
        s.conn.receive(minimumIncompleteLength: 1, maximumLength: 256 * 1024) { [weak self] data, _, isComplete, error in
            guard let self, !s.closed else { return }
            if let data, !data.isEmpty {
                s.raw.append(data)
                if !self.process(s) { self.close(s); return }
            }
            if isComplete || error != nil { self.close(s); return }
            self.receive(s)
        }
    }

    /// Returns false if the connection should be dropped.
    private func process(_ s: Session) -> Bool {
        if s.link == .usb {
            s.plain.append(s.raw)
            s.raw.removeAll(keepingCapacity: true)
            parseMessages(s)
            return true
        }

        // Wi-Fi: handshake first.
        if s.state == .awaitClientHello {
            guard s.raw.count >= Handshake.clientHelloSize else { return true }
            let hello = Data(s.raw.prefix(Handshake.clientHelloSize))
            s.raw = Data(s.raw.dropFirst(Handshake.clientHelloSize))
            guard hello.prefix(4) == Handshake.magic,
                  Data(hello[4..<20]) == Pairing.shared.id else { return false }
            s.nonceC = Data(hello[20..<52])
            s.nonceS = Pairing.random(32)
            let reply = s.nonceS + Handshake.serverProof(Pairing.shared.key, s.nonceC, s.nonceS)
            s.conn.send(content: reply, completion: .contentProcessed { _ in })
            s.state = .awaitClientProof
        }
        if s.state == .awaitClientProof {
            guard s.raw.count >= Handshake.clientProofSize else { return true }
            let proof = Data(s.raw.prefix(Handshake.clientProofSize))
            s.raw = Data(s.raw.dropFirst(Handshake.clientProofSize))
            guard Handshake.verifyClientProof(proof, Pairing.shared.key, s.nonceC, s.nonceS) else { return false }
            s.channel = SecureChannel(master: Pairing.shared.key, nonceC: s.nonceC, nonceS: s.nonceS)
            s.state = .ready
            activate(s)
        }

        // Encrypted records.
        while s.raw.count >= 4 {
            let b = [UInt8](s.raw.prefix(4))
            let len = Int(UInt32(b[0]) << 24 | UInt32(b[1]) << 16 | UInt32(b[2]) << 8 | UInt32(b[3]))
            guard len <= 1 << 20 else { return false }
            guard s.raw.count >= 4 + len else { break }
            let body = Data(s.raw.dropFirst(4).prefix(len))
            s.raw = Data(s.raw.dropFirst(4 + len))
            guard let ch = s.channel, let plain = try? ch.open(body) else { return false }
            s.plain.append(plain)
        }
        parseMessages(s)
        return true
    }

    private func parseMessages(_ s: Session) {
        while s.plain.count >= Wire.headerSize {
            let b = [UInt8](s.plain.prefix(Wire.headerSize))
            let len = Int(UInt32(b[0]) << 24 | UInt32(b[1]) << 16 | UInt32(b[2]) << 8 | UInt32(b[3]))
            guard s.plain.count >= Wire.headerSize + len else { return }
            let type = b[4]
            let payload = Data(s.plain.dropFirst(Wire.headerSize).prefix(len))
            s.plain = Data(s.plain.dropFirst(Wire.headerSize + len))
            handle(type, payload, from: s)
        }
    }

    private func handle(_ type: UInt8, _ payload: Data, from s: Session) {
        switch MsgType(rawValue: type) {
        case .ping:
            transmit(s, Wire.frame(.pong, timestampUs: Wire.nowUs(), payload: payload))
        case .requestKeyframe:
            if s === active { onKeyframeRequested?() }
        case .ack:
            guard s === active, payload.count >= 8 else { return }
            let ts = Wire.readUInt64BE(payload)
            outstanding.removeAll { $0 <= ts }
            maybeRequestKeyframe(limit: s.link == .usb ? maxOutstandingUSB : maxOutstandingWiFi)
        case .config:
            guard s === active,
                  let obj = try? JSONSerialization.jsonObject(with: payload) as? [String: Any] else { return }
            onConfig?(obj)
        case .pairRequest:
            // Only over the cable: physical access + "Trust This Computer" = allowed to pair.
            guard s.link == .usb else { return }
            let info: [String: Any] = [
                "id": Pairing.shared.idHex,
                "key": Pairing.shared.keyData.base64EncodedString(),
                "name": (helloBase["name"] as? String) ?? "iPhone",
            ]
            if let data = try? JSONSerialization.data(withJSONObject: info) {
                transmit(s, Wire.frame(.pairInfo, timestampUs: Wire.nowUs(), payload: data))
                onPaired?()
            }
        default:
            break   // step 2: input events go here
        }
    }
}
