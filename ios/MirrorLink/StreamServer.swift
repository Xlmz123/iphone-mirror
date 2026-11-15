import Foundation
import Network

/// TCP server bound to the phone's loopback interface. The Windows PC reaches it
/// through the USB cable via Apple's usbmux service (the same channel iTunes uses),
/// so nothing is exposed on Wi-Fi.
final class StreamServer {
    var onClientConnected: (() -> Void)?
    var onClientDisconnected: (() -> Void)?
    var onKeyframeRequested: (() -> Void)?
    var onStatus: ((String) -> Void)?

    private let queue = DispatchQueue(label: "mirrorlink.net")
    private var listener: NWListener?
    private var client: NWConnection?
    private var inflight = 0                 // sends not yet handed to the kernel
    private var droppingUntilKeyframe = false
    private var rxBuffer = Data()
    private var helloPayload: Data?

    /// Max frames allowed to queue before we start dropping (keeps latency low
    /// instead of letting a backlog build up).
    private let maxInflight = 3

    func start() {
        queue.async { self.startListener() }
    }

    private func startListener() {
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        let params = NWParameters(tls: nil, tcp: tcp)
        params.requiredInterfaceType = .loopback
        params.allowLocalEndpointReuse = true

        do {
            let l = try NWListener(using: params, on: NWEndpoint.Port(rawValue: Wire.port)!)
            l.newConnectionHandler = { [weak self] conn in self?.accept(conn) }
            l.stateUpdateHandler = { [weak self] state in
                switch state {
                case .ready: self?.onStatus?("Waiting for PC on USB…")
                case .failed(let err):
                    self?.onStatus?("Server error: \(err). Retrying…")
                    self?.listener?.cancel()
                    self?.queue.asyncAfter(deadline: .now() + 1) { self?.startListener() }
                default: break
                }
            }
            l.start(queue: queue)
            listener = l
        } catch {
            onStatus?("Could not start server: \(error)")
        }
    }

    private func accept(_ conn: NWConnection) {
        // Only one viewer at a time: newest wins.
        client?.cancel()
        client = conn
        inflight = 0
        rxBuffer.removeAll()
        droppingUntilKeyframe = true          // first thing the PC needs is a keyframe

        conn.stateUpdateHandler = { [weak self, weak conn] state in
            guard let self, let conn else { return }
            switch state {
            case .ready:
                if let hello = self.helloPayload {
                    conn.send(content: Wire.frame(.hello, timestampUs: Wire.nowUs(), payload: hello),
                              completion: .contentProcessed { _ in })
                }
                self.onClientConnected?()
                self.onKeyframeRequested?()
                self.receive(on: conn)
            case .failed, .cancelled:
                if self.client === conn {
                    self.client = nil
                    self.onClientDisconnected?()
                }
            default: break
            }
        }
        conn.start(queue: queue)
    }

    // MARK: Sending

    /// Set the hello message (stream size etc). Sent to every new viewer.
    func setHello(_ json: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: json) else { return }
        queue.async {
            self.helloPayload = data
            if let c = self.client {
                c.send(content: Wire.frame(.hello, timestampUs: Wire.nowUs(), payload: data),
                       completion: .contentProcessed { _ in })
            }
        }
    }

    func sendVideo(_ annexB: Data, keyframe: Bool, timestampUs: UInt64) {
        queue.async {
            guard let c = self.client else { return }

            if keyframe {
                self.droppingUntilKeyframe = false
            } else if self.droppingUntilKeyframe {
                return
            }

            // Backpressure: if the link can't keep up, drop instead of queueing.
            // Dropping a delta frame corrupts following frames, so we skip until
            // the next keyframe and ask the encoder for one right away.
            if self.inflight >= self.maxInflight && !keyframe {
                self.droppingUntilKeyframe = true
                self.onKeyframeRequested?()
                return
            }

            self.inflight += 1
            let msg = Wire.frame(keyframe ? .videoKey : .videoDelta, timestampUs: timestampUs, payload: annexB)
            c.send(content: msg, completion: .contentProcessed { [weak self] _ in
                self?.inflight -= 1     // runs on self.queue
            })
        }
    }

    // MARK: Receiving (PC -> phone)

    private func receive(on conn: NWConnection) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { [weak self] data, _, isComplete, error in
            guard let self else { return }
            if let data, !data.isEmpty {
                self.rxBuffer.append(data)
                self.parseMessages(conn)
            }
            if isComplete || error != nil {
                conn.cancel()
                return
            }
            self.receive(on: conn)
        }
    }

    private func parseMessages(_ conn: NWConnection) {
        while rxBuffer.count >= Wire.headerSize {
            let b = [UInt8](rxBuffer.prefix(Wire.headerSize))
            let len = Int(UInt32(b[0]) << 24 | UInt32(b[1]) << 16 | UInt32(b[2]) << 8 | UInt32(b[3]))
            guard rxBuffer.count >= Wire.headerSize + len else { return }
            let type = b[4]
            let payload = rxBuffer.subdata(in: rxBuffer.startIndex + Wire.headerSize ..< rxBuffer.startIndex + Wire.headerSize + len)
            rxBuffer.removeFirst(Wire.headerSize + len)

            switch MsgType(rawValue: type) {
            case .requestKeyframe:
                onKeyframeRequested?()
            case .ping:
                conn.send(content: Wire.frame(.pong, timestampUs: Wire.nowUs(), payload: payload),
                          completion: .contentProcessed { _ in })
            default:
                break   // future: input events go here (step 2)
            }
        }
    }
}
