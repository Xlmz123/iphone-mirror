import Foundation
import QuartzCore

/// Passes input from the PC to WebDriverAgent (WDA), which runs on this iPhone and
/// performs touches through Apple's UI-testing system.
///
/// WDA is started with USE_IP=127.0.0.1, so it only listens *inside* the phone. The
/// PC never talks to it directly: input arrives over MirrorLink's own channel (the
/// USB cable, or encrypted Wi-Fi) and this bridge forwards it over loopback.
///
/// Coordinates from the PC are normalized (0...1) in the picture as you see it
/// (upright). WDA's touch API uses the same upright space in points, so we just
/// scale by WDA's window size, which follows the current orientation.
final class InputBridge {
    /// Status for the PC: ["ready": Bool, "detail": String]
    var onStatus: (([String: Any]) -> Void)?
    var onLog: ((String) -> Void)?

    private let base = "http://127.0.0.1:8100/"
    private let queue = DispatchQueue(label: "mirrorlink.input", qos: .userInteractive)  // one gesture at a time
    private let http: URLSession
    private var sessionId: String?
    private var window = CGSize.zero
    private var windowTime: CFTimeInterval = 0
    private var ready = false
    private var lastReported: (Bool, String)?
    private var timer: DispatchSourceTimer?
    private var queued = 0                      // gestures waiting (queue-confined)

    init() {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 15
        cfg.httpMaximumConnectionsPerHost = 2
        cfg.connectionProxyDictionary = [:]     // never route loopback through a proxy
        http = URLSession(configuration: cfg)
    }

    // MARK: Lifecycle

    /// Start checking for WDA (every 3 s) while a PC is connected.
    func start() {
        queue.async {
            guard self.timer == nil else { self.report(force: true); return }
            let t = DispatchSource.makeTimerSource(queue: self.queue)
            t.schedule(deadline: .now(), repeating: 3)
            t.setEventHandler { [weak self] in self?.probe() }
            t.resume()
            self.timer = t
        }
    }

    func stop() {
        queue.async {
            self.timer?.cancel()
            self.timer = nil
            self.lastReported = nil
        }
    }

    /// Orientation changed: the window size (and so the coordinate scale) changes too.
    func invalidateWindowSize() {
        queue.async { self.windowTime = 0 }
    }

    // MARK: Input from the PC

    func handle(_ m: [String: Any]) {
        queue.async {
            // If gestures pile up (WDA slow), drop pointer gestures rather than
            // replaying a long backlog of stale taps later. Keys/text are kept.
            let t = m["t"] as? String ?? ""
            if self.queued > 6 && (t == "tap" || t == "path" || t == "long") { return }
            self.queued += 1
            defer { self.queued -= 1 }
            self.perform(m)
        }
    }

    private func perform(_ m: [String: Any]) {
        guard ready || probe() else { return }
        guard let t = m["t"] as? String else { return }
        func d(_ k: String) -> Double? { (m[k] as? NSNumber)?.doubleValue }

        switch t {
        case "tap":
            guard let x = d("x"), let y = d("y") else { return }
            touch([(x, y, 0)], holdMs: 40)
        case "long":
            guard let x = d("x"), let y = d("y") else { return }
            touch([(x, y, 0)], holdMs: max(350, d("ms") ?? 650))
        case "path":
            // [[x, y, tMs], ...] — a drag/swipe, replayed with its real timing.
            guard let raw = m["pts"] as? [[NSNumber]], raw.count >= 2 else { return }
            let pts = raw.compactMap { p -> (Double, Double, Double)? in
                p.count >= 3 ? (p[0].doubleValue, p[1].doubleValue, p[2].doubleValue) : nil
            }
            touch(pts, holdMs: d("hold") ?? 0)
        case "key":
            key(m["k"] as? String ?? "")
        case "text":
            if let s = m["s"] as? String, !s.isEmpty { type(s) }
        default:
            break
        }
    }

    // MARK: Gestures

    /// One finger: down at the first point, move through the rest, optionally hold, up.
    private func touch(_ pts: [(Double, Double, Double)], holdMs: Double) {
        guard let size = windowSize() else { return }
        func px(_ v: Double, _ max: CGFloat) -> Double {
            (min(1, Swift.max(0, v)) * Double(max)).clamped(0, Double(max) - 1)
        }
        var actions: [[String: Any]] = []
        let first = pts[0]
        actions.append(["type": "pointerMove", "duration": 0,
                        "x": px(first.0, size.width), "y": px(first.1, size.height)])
        actions.append(["type": "pointerDown", "button": 0])
        var lastT = first.2
        for p in pts.dropFirst() {
            let dt = max(0, min(1000, p.2 - lastT))
            lastT = p.2
            actions.append(["type": "pointerMove", "duration": Int(dt),
                            "x": px(p.0, size.width), "y": px(p.1, size.height)])
        }
        if holdMs > 0 { actions.append(["type": "pause", "duration": Int(holdMs)]) }
        actions.append(["type": "pointerUp", "button": 0])

        let body: [String: Any] = ["actions": [[
            "type": "pointer", "id": "finger1",
            "parameters": ["pointerType": "touch"],
            "actions": actions,
        ]]]
        withSession { sid in self.request("POST", "session/\(sid)/actions", body) }
    }

    /// Normalized points helper for system gestures.
    private func swipe(_ x0: Double, _ y0: Double, _ x1: Double, _ y1: Double, ms: Double, hold: Double = 0) {
        touch([(x0, y0, 0), (x1, y1, ms)], holdMs: hold)
    }

    private func key(_ k: String) {
        switch k {
        case "home":
            request("POST", "wda/homescreen", [:])
        case "switcher":
            // Swipe up from the bottom edge and pause mid-screen.
            swipe(0.5, 0.999, 0.5, 0.62, ms: 300, hold: 450)
        case "spotlight":
            request("POST", "wda/homescreen", [:])
            Thread.sleep(forTimeInterval: 0.7)
            swipe(0.5, 0.30, 0.5, 0.62, ms: 250)
        case "control":
            swipe(0.92, 0.002, 0.92, 0.55, ms: 300)
        case "notifications":
            swipe(0.35, 0.002, 0.35, 0.70, ms: 300)
        case "volup":
            withSession { sid in self.request("POST", "session/\(sid)/wda/pressButton", ["name": "volumeup"]) }
        case "voldown":
            withSession { sid in self.request("POST", "session/\(sid)/wda/pressButton", ["name": "volumedown"]) }
        default:
            break
        }
    }

    /// Types into whatever text field has focus. "\u{8}" = delete, "\n" = return.
    private func type(_ s: String) {
        // A moderate typing speed: iOS 27 is known to drop characters when
        // automated typing is fast.
        let chars = s.map { String($0) }
        withSession { sid in
            self.request("POST", "session/\(sid)/wda/keys", ["value": chars, "frequency": 15])
        }
    }

    // MARK: WDA plumbing

    /// Checks whether WDA is up. Returns the new ready state.
    @discardableResult
    private func probe() -> Bool {
        let (code, json) = request("GET", "status", nil, timeout: 2)
        let value = json?["value"] as? [String: Any]
        let ok = code == 200 && (value?["ready"] as? Bool ?? true)
        if ok != ready {
            ready = ok
            onLog?(ok ? "Control helper (WDA) connected" : "Control helper (WDA) not running")
            if !ok { sessionId = nil }
        }
        report()
        return ok
    }

    private func report(force: Bool = false) {
        let detail = ready ? "ready" : "WDA not running"
        if force || lastReported == nil || lastReported! != (ready, detail) {
            lastReported = (ready, detail)
            onStatus?(["ready": ready, "detail": detail])
        }
    }

    private func windowSize() -> CGSize? {
        let now = CACurrentMediaTime()
        if window.width > 0 && now - windowTime < 1.5 { return window }
        let (code, json) = request("GET", "window/size", nil, timeout: 5)
        if code == 200, let v = json?["value"] as? [String: Any],
           let w = (v["width"] as? NSNumber)?.doubleValue, let h = (v["height"] as? NSNumber)?.doubleValue,
           w > 0, h > 0 {
            window = CGSize(width: w, height: h)
            windowTime = now
        }
        return window.width > 0 ? window : nil
    }

    /// Runs `body` with a WDA session, creating one if needed and retrying once if
    /// WDA forgot it (e.g. WDA was restarted).
    private func withSession(_ body: (String) -> (Int, [String: Any]?)) {
        for attempt in 0..<2 {
            guard let sid = ensureSession() else { return }
            let (code, json) = body(sid)
            if code == 404 || isInvalidSession(json) {
                sessionId = nil
                if attempt == 0 { continue }
            }
            if code >= 400 {
                let msg = ((json?["value"] as? [String: Any])?["message"] as? String) ?? "HTTP \(code)"
                onLog?("Control error: \(msg.prefix(80))")
            }
            return
        }
    }

    private func isInvalidSession(_ json: [String: Any]?) -> Bool {
        let err = (json?["value"] as? [String: Any])?["error"] as? String
        return err == "invalid session id"
    }

    private func ensureSession() -> String? {
        if let s = sessionId { return s }
        let caps: [String: Any] = ["capabilities": ["alwaysMatch": [
            "shouldWaitForQuiescence": false,
            "disableAutomaticScreenshots": true,
            "maxTypingFrequency": 15,
        ]]]
        let (code, json) = request("POST", "session", caps, timeout: 20)
        let sid = (json?["sessionId"] as? String)
            ?? ((json?["value"] as? [String: Any])?["sessionId"] as? String)
        guard code == 200, let sid else {
            onLog?("Control: couldn't start a WDA session (HTTP \(code))")
            return nil
        }
        sessionId = sid
        // Don't wait for the UI to settle before each action: that's what makes
        // automation tools feel laggy.
        request("POST", "session/\(sid)/appium/settings", ["settings": [
            "waitForIdleTimeout": 0,
            "animationCoolOffTimeout": 0,
        ]])
        return sid
    }

    /// Synchronous HTTP to WDA (we're on a serial queue; one request at a time).
    @discardableResult
    private func request(_ method: String, _ path: String, _ body: [String: Any]?,
                         timeout: TimeInterval = 15) -> (Int, [String: Any]?) {
        guard let url = URL(string: base + path) else { return (0, nil) }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.timeoutInterval = timeout
        if let body {
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let done = DispatchSemaphore(value: 0)
        var code = 0
        var json: [String: Any]?
        http.dataTask(with: req) { data, resp, _ in
            code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            if let data { json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] }
            done.signal()
        }.resume()
        done.wait()
        return (code, json)
    }
}

private extension Double {
    func clamped(_ lo: Double, _ hi: Double) -> Double { Swift.min(hi, Swift.max(lo, self)) }
}
