import Foundation
import CryptoKit

/// Wi-Fi security. Mirrors pc/secure.py exactly.
///
/// Handshake (raw bytes, before encryption):
///   PC    -> phone : "MLK1" | pairingID(16) | nonceC(32)
///   phone -> PC    : nonceS(32) | HMAC-SHA256(K, "ML-server" | nonceC | nonceS)
///   PC    -> phone : HMAC-SHA256(K, "ML-client" | nonceC | nonceS)
/// Both sides prove they hold the pairing key K without ever sending it.
///
/// Session keys: HKDF-SHA256(K, salt = nonceC | nonceS, info = "ML s2c" / "ML c2s").
/// Records: [4-byte BE length][ChaCha20-Poly1305 ciphertext | 16-byte tag]
/// Nonce = 4 zero bytes | 8-byte BE counter (separate counter per direction),
/// so records can't be reordered, replayed, or modified.
enum Handshake {
    static let magic = Data("MLK1".utf8)
    static let clientHelloSize = 4 + 16 + 32
    static let serverReplySize = 32 + 32
    static let clientProofSize = 32

    static func serverProof(_ key: SymmetricKey, _ nc: Data, _ ns: Data) -> Data {
        Data(HMAC<SHA256>.authenticationCode(for: Data("ML-server".utf8) + nc + ns, using: key))
    }

    static func verifyClientProof(_ proof: Data, _ key: SymmetricKey, _ nc: Data, _ ns: Data) -> Bool {
        HMAC<SHA256>.isValidAuthenticationCode(proof, authenticating: Data("ML-client".utf8) + nc + ns, using: key)
    }
}

enum SecureChannelError: Error { case badRecord }

final class SecureChannel {
    private let sendKey: SymmetricKey
    private let recvKey: SymmetricKey
    private var sendCounter: UInt64 = 0
    private var recvCounter: UInt64 = 0

    /// Phone side: sends with s2c, receives with c2s.
    init(master: SymmetricKey, nonceC: Data, nonceS: Data) {
        let salt = nonceC + nonceS
        sendKey = HKDF<SHA256>.deriveKey(inputKeyMaterial: master, salt: salt,
                                         info: Data("ML s2c".utf8), outputByteCount: 32)
        recvKey = HKDF<SHA256>.deriveKey(inputKeyMaterial: master, salt: salt,
                                         info: Data("ML c2s".utf8), outputByteCount: 32)
    }

    private static func nonce(_ counter: UInt64) -> ChaChaPoly.Nonce {
        var n = Data(count: 4)
        var be = counter.bigEndian
        withUnsafeBytes(of: &be) { n.append(contentsOf: $0) }
        return try! ChaChaPoly.Nonce(data: n)
    }

    /// Encrypts one protocol message into a length-prefixed record.
    func seal(_ plain: Data) throws -> Data {
        let box = try ChaChaPoly.seal(plain, using: sendKey, nonce: SecureChannel.nonce(sendCounter))
        sendCounter += 1
        let body = box.ciphertext + box.tag
        var len = UInt32(body.count).bigEndian
        var out = Data(capacity: 4 + body.count)
        withUnsafeBytes(of: &len) { out.append(contentsOf: $0) }
        out.append(body)
        return out
    }

    /// Decrypts one record body (without its length prefix).
    func open(_ body: Data) throws -> Data {
        guard body.count >= 16 else { throw SecureChannelError.badRecord }
        let b = Data(body)   // reindex from 0
        let box = try ChaChaPoly.SealedBox(nonce: SecureChannel.nonce(recvCounter),
                                           ciphertext: b.prefix(b.count - 16),
                                           tag: b.suffix(16))
        let plain = try ChaChaPoly.open(box, using: recvKey)
        recvCounter += 1
        return plain
    }
}
