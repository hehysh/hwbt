"""Cryptographic primitives for the Huawei protocol.

All constructions mirror Gadgetbridge ``HuaweiCrypto``/``CryptoUtils``:

* pairing PIN blob: AES-CBC (PKCS#7) or AES-GCM, keyed with a fixed digest secret
* HiChain: HMAC-SHA256, HKDF-SHA256, AES-GCM with 12-byte nonce and AAD
* transport: AES-GCM (ciphertext || tag, 16-byte tag) or AES-CBC (PKCS#7)
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import secrets
from typing import Any

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class CryptoError(Exception):
    pass


# Fixed digest secrets used to decrypt the PIN blob handed out before pairing.
DIGEST_SECRET_V1 = bytes(
    [0x70, 0xFB, 0x6C, 0x24, 0x03, 0x5F, 0xDB, 0x55, 0x2F, 0x38, 0x89, 0x8A, 0xEE, 0xDE, 0x3F, 0x69]
)
DIGEST_SECRET_V2 = bytes(
    [0x93, 0xAC, 0xDE, 0xF7, 0x6A, 0xCB, 0x09, 0x85, 0x7D, 0xBF, 0xE5, 0x26, 0x1A, 0xAB, 0xCD, 0x78]
)
DIGEST_SECRET_V3 = bytes(
    [0x9C, 0x27, 0x63, 0xA9, 0xCC, 0xE1, 0x34, 0x76, 0x6D, 0xE3, 0xFF, 0x61, 0x18, 0x20, 0x05, 0x53]
)


def digest_secret(auth_version: int) -> bytes:
    if auth_version in (1, 4):
        return DIGEST_SECRET_V1
    if auth_version == 2:
        return DIGEST_SECRET_V2
    return DIGEST_SECRET_V3


def random_bytes(size: int) -> bytes:
    return secrets.token_bytes(size)


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hmac_sha256(key: bytes, message: bytes) -> bytes:
    return _hmac.new(key, message, hashlib.sha256).digest()


def hkdf_sha256(input_key: bytes, salt: bytes, info: str | bytes, length: int = 32) -> bytes:
    info_bytes = info.encode() if isinstance(info, str) else info
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info_bytes).derive(input_key)


def aes_gcm_seal(plain: bytes, key: bytes, nonce: bytes, aad: bytes = b"") -> bytes:
    try:
        return AESGCM(key).encrypt(nonce, plain, aad or None)
    except Exception as exc:
        raise CryptoError(f"AES-GCM encrypt failed: {exc}") from exc


def aes_gcm_open(data: bytes, key: bytes, nonce: bytes, aad: bytes = b"") -> bytes:
    if len(data) < 16:
        raise CryptoError("AES-GCM payload too short")
    try:
        return AESGCM(key).decrypt(nonce, data, aad or None)
    except Exception as exc:
        raise CryptoError(f"AES-GCM decrypt failed: {exc}") from exc


def aes_cbc_encrypt(plain: bytes, key: bytes, iv: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def aes_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise CryptoError("AES-CBC padding error (wrong key/IV?)") from exc


# -- HiChain Lite ------------------------------------------------------------
def hex_upper(data: bytes) -> bytes:
    """``GB.hexdump``: uppercase hex, no separators, as ASCII bytes."""
    return data.hex().upper().encode("ascii")


def pbkdf2_sha256(password: bytes, salt: bytes, iterations: int, length: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password, salt, iterations, dklen=length)


def digest_lite(
    message: bytes, key: bytes, nonce: bytes, *, auth_version: int, auth_algo: int
) -> bytes:
    """Gadgetbridge ``HuaweiCrypto.computeDigestHiChainLite``.

    Returns 64 bytes: ``HMAC(step, nonce) || step``. The first 32 bytes are the
    challenge (or its expected answer), bytes 32..48 become the bonding key.
    """
    hash_key = sha256(hex_upper(key))
    secret = bytearray(digest_secret(auth_version))
    for index in range(len(secret)):
        secret[index] ^= hash_key[index]
    msg = bytes(secret) + message
    if auth_algo == 0x01:
        # Java's PBEKeySpec length is in bits: 0x100 bits == 32 bytes.
        step = pbkdf2_sha256(hex_upper(msg), hex_upper(nonce), 1000, 32)
    else:
        step = hmac_sha256(msg, nonce)
    return hmac_sha256(step, nonce) + step


def initialization_vector(counter: int) -> tuple[bytes, int]:
    """IV for CBC transport encryption: 12 random bytes plus a BE32 counter."""
    counter = 1 if counter == 0xFFFFFFFF else counter + 1
    return random_bytes(12) + counter.to_bytes(4, "big"), counter


def ordered_json(value: OrderedDictish) -> bytes:
    """Serialise with insertion order preserved and compact separators.

    Android's ``org.json`` writes minified JSON in insertion order; the watch
    expects the same field names (order is not semantically important, but keep
    it identical to the reference implementations to stay predictable).
    """
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


OrderedDictish = Any
