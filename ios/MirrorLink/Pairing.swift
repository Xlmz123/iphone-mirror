import Foundation
import Security
import CryptoKit

/// The phone's pairing identity: a random ID and a 256-bit secret key.
/// A PC learns both by connecting once over USB; after that it can connect over
/// Wi-Fi. "Reset pairing" makes a new key, so every previously paired PC is locked out.
final class Pairing {
    static let shared = Pairing()

    private let lock = NSLock()
    private var _key = Data()
    private var _id = Data()
    private let service = "com.mirrorlink.pairing"

    private init() {
        if let k = load("key"), k.count == 32, let i = load("id"), i.count == 16 {
            _key = k; _id = i
        } else {
            regenerate()
        }
    }

    var key: SymmetricKey { lock.lock(); defer { lock.unlock() }; return SymmetricKey(data: _key) }
    var keyData: Data { lock.lock(); defer { lock.unlock() }; return _key }
    var id: Data { lock.lock(); defer { lock.unlock() }; return _id }
    var idHex: String { id.map { String(format: "%02x", $0) }.joined() }

    func regenerate() {
        let k = Pairing.random(32), i = Pairing.random(16)
        lock.lock(); _key = k; _id = i; lock.unlock()
        save("key", k)
        save("id", i)
    }

    static func random(_ n: Int) -> Data {
        var d = Data(count: n)
        let ok = d.withUnsafeMutableBytes { SecRandomCopyBytes(kSecRandomDefault, n, $0.baseAddress!) }
        precondition(ok == errSecSuccess, "no secure randomness")
        return d
    }

    // MARK: Keychain (falls back to UserDefaults if the keychain is unavailable)

    private func query(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    private func load(_ account: String) -> Data? {
        var q = query(account)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        if SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data {
            return d
        }
        return UserDefaults.standard.data(forKey: "pairing.\(account)")
    }

    private func save(_ account: String, _ data: Data) {
        SecItemDelete(query(account) as CFDictionary)
        var q = query(account)
        q[kSecValueData as String] = data
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        if SecItemAdd(q as CFDictionary, nil) == errSecSuccess {
            UserDefaults.standard.removeObject(forKey: "pairing.\(account)")
        } else {
            UserDefaults.standard.set(data, forKey: "pairing.\(account)")
        }
    }
}
