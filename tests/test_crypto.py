from __future__ import annotations

import pytest

from hwmusic.crypto import (
    DIGEST_SECRET_V1,
    CryptoError,
    aes_cbc_decrypt,
    aes_cbc_encrypt,
    aes_gcm_open,
    aes_gcm_seal,
    digest_lite,
    digest_secret,
    hkdf_sha256,
    hmac_sha256,
    initialization_vector,
    ordered_json,
    sha256,
)


def test_sha256() -> None:
    assert sha256(b"abc").hex() == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_hmac_sha256_rfc4231_case1() -> None:
    key = b"\x0b" * 20
    assert (
        hmac_sha256(key, b"Hi There").hex()
        == "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
    )


def test_hkdf_sha256_rfc5869_case1() -> None:
    ikm = b"\x0b" * 22
    salt = bytes.fromhex("000102030405060708090a0b0c")
    info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    okm = hkdf_sha256(ikm, salt, info, 42)
    assert okm.hex() == (
        "3cb25f25faacd57a90434f64d0362f2a"
        "2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
        "34007208d5b887185865"
    )


def test_aes_gcm_empty_plaintext_vector() -> None:
    key = bytes(16)
    nonce = bytes(12)
    sealed = aes_gcm_seal(b"", key, nonce)
    assert sealed.hex() == "58e2fccefa7e3061367f1d57a4e7455a"
    assert aes_gcm_open(sealed, key, nonce) == b""


def test_aes_gcm_roundtrip_with_aad() -> None:
    key = bytes(range(32))
    nonce = bytes(range(12))
    plain = b"hichain_iso_exchange payload"
    sealed = aes_gcm_seal(plain, key, nonce, b"hichain_iso_exchange")
    assert len(sealed) == len(plain) + 16
    assert aes_gcm_open(sealed, key, nonce, b"hichain_iso_exchange") == plain
    with pytest.raises(CryptoError):
        aes_gcm_open(sealed, key, nonce, b"other aad")


def test_aes_cbc_roundtrip_and_padding() -> None:
    key = bytes(range(16))
    iv = bytes(range(16))
    for size in (0, 1, 15, 16, 17, 100):
        plain = bytes(size)
        cipher = aes_cbc_encrypt(plain, key, iv)
        assert len(cipher) % 16 == 0
        assert aes_cbc_decrypt(cipher, key, iv) == plain


def test_cbc_wrong_key_fails() -> None:
    cipher = aes_cbc_encrypt(b"secret", bytes(16), bytes(16))
    with pytest.raises(CryptoError):
        aes_cbc_decrypt(cipher, b"\x01" * 16, bytes(16))


def test_digest_secrets_are_stable() -> None:
    assert digest_secret(1) == digest_secret(4)
    assert digest_secret(1) != digest_secret(2) != digest_secret(3)
    assert digest_secret(1).hex().startswith("70fb6c24")


def test_ordered_json_keeps_order_and_is_compact() -> None:
    data = {"b": 1, "a": {"y": "1.0.0", "x": 0}}
    assert ordered_json(data) == b'{"b":1,"a":{"y":"1.0.0","x":0}}'


def test_digest_lite_matches_gadgetbridge_formula() -> None:
    import hashlib
    import hmac as _hmac

    key = bytes(range(16))
    nonce = bytes(range(16, 32))
    message = b"\x01\x00"

    def hexup(data: bytes) -> bytes:
        return data.hex().upper().encode("ascii")

    secret = bytearray(DIGEST_SECRET_V1)
    hash_key = hashlib.sha256(hexup(key)).digest()
    for i in range(16):
        secret[i] ^= hash_key[i]
    msg = bytes(secret) + message
    step = hashlib.pbkdf2_hmac("sha256", hexup(msg), hexup(nonce), 1000, dklen=32)
    expected = _hmac.new(step, nonce, hashlib.sha256).digest() + step

    assert digest_lite(message, key, nonce, auth_version=1, auth_algo=0x01) == expected
    assert len(expected) == 64


def test_digest_lite_uses_hmac_when_auth_algo_is_zero() -> None:
    key = b"pin-code"
    nonce = b"\x11" * 32
    result = digest_lite(b"\x01\x10", key, nonce, auth_version=1, auth_algo=0)
    assert len(result) == 64
    assert result[:32] == hmac_sha256(result[32:], nonce)


def test_initialization_vector_advances_the_counter() -> None:
    iv, counter = initialization_vector(0x10203040)
    assert len(iv) == 16
    assert counter == 0x10203041
    assert iv[12:] == (0x10203041).to_bytes(4, "big")

    iv, counter = initialization_vector(0xFFFFFFFF)
    assert counter == 1
    assert iv[12:] == b"\x00\x00\x00\x01"
