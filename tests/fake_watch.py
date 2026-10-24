"""A simulated watch: implements the peer side of the protocol for tests.

The implementation is written from the same specification but independently of
the client code paths (it parses frames, decrypts, and drives the upload), so a
successful round-trip exercises framing, TLV, crypto, HiChain and the upload
state machine end to end.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random

from hwmusic import constants as C
from hwmusic import protocol as P
from hwmusic.crypto import (
    aes_cbc_decrypt,
    aes_cbc_encrypt,
    aes_gcm_open,
    aes_gcm_seal,
    digest_lite,
    digest_secret,
    hkdf_sha256,
    hmac_sha256,
    sha256,
)
from hwmusic.packet import Packet, PacketParser, serialize, serialize_sliced
from hwmusic.tlv import TLV, TLVError, be16, be32


class MockTransport:
    def __init__(self, watch: FakeWatch) -> None:
        self.watch = watch
        self.max_write_size = 244
        self.use_response = True
        self.connected = False

    async def connect(self, address: str | None = None) -> None:
        self.connected = True

    async def start_notify(self, callback) -> None:
        self.watch.notify = callback

    async def stop_notify(self) -> None:
        pass

    async def disconnect(self) -> None:
        self.connected = False

    async def write(self, data: bytes) -> None:
        self.watch.feed(data)


class FakeWatch:
    def __init__(
        self,
        *,
        pin: bytes = b"123456",
        auth_version: int = 3,
        support_type: int = 4,
        auth_algo: int = 0,
        encrypt_method: int = 1,
        unit_size: int = 200,
        chunk_size: int = 600,
        fail_after: int | None = None,
        no_encrypt_chunks: bool = False,
    ) -> None:
        self.notify = None
        self.parser = PacketParser()
        self.pin = pin
        self.auth_version = auth_version
        self.support_type = support_type
        self.auth_algo = auth_algo
        self.encrypt_method = encrypt_method
        self.unit_size = unit_size
        self.chunk_size = chunk_size
        self.fail_after = fail_after
        self.no_encrypt_chunks = no_encrypt_chunks

        self.session_key: bytes | None = None
        self.secret_key: bytes | None = None
        self.server_nonce = b""
        self.first_key: bytes | None = None
        self.lite_client_nonce = b""
        self.encryption_counter = 0
        self.auth_token = bytes(random.randrange(256) for _ in range(32))
        self.auth_id_peer = b"E" * 32
        self.rand_peer = bytes(random.randrange(256) for _ in range(16))
        self.psk: bytes | None = None
        self.iso_key: bytes | None = None
        self.client_id: bytes = b""

        self.upload_name = ""
        self.upload_size = 0
        self.upload_type = 0
        self.upload_digest = b""
        self.file_id = 3
        self.received = bytearray()
        self.requested = 0
        self.upload_done = False
        self.song_info: tuple[str, str] | None = None
        self.info_requested = False
        self.encrypt_chunks = True
        self.pin_requests = 0
        self.songs: list[tuple[int, str, str, str]] = [
            (0, "晨跑", "测试歌手", "run.mp3"),
            (1, "夜曲", "测试歌手", "night.flac"),
        ]

    # -- io --------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        for packet in self.parser.feed(data):
            self._handle(packet)

    def send(self, service: int, command: int, tlv: TLV, *, encrypted: bool | None = None) -> None:
        assert self.notify is not None
        use_encryption = self.session_key is not None if encrypted is None else encrypted
        body = tlv.serialize()
        if use_encryption:
            body = self._encrypt(body)
        frames = serialize_sliced(service, command, body, 244) if len(body) + 8 > 244 else [serialize(service, command, body)]
        for frame in frames:
            self.notify(frame)

    def _encrypt(self, body: bytes) -> bytes:
        assert self.session_key is not None
        iv = bytes(random.randrange(256) for _ in range(16))
        cipher = (
            aes_gcm_seal(body, self.session_key, iv)
            if self.uses_gcm
            else aes_cbc_encrypt(body, self.session_key, iv)
        )
        return TLV().put(0x7C, b"\x01").put(0x7D, iv).put(0x7E, cipher).serialize()

    def _decrypt(self, body: bytes) -> bytes:
        try:
            tlv = TLV.parse(body)
        except TLVError:
            return body
        if tlv.get(0x7C) != b"\x01":
            return body
        assert self.session_key is not None
        iv = tlv.get(0x7D) or b""
        cipher = tlv.get(0x7E) or b""
        return (
            aes_gcm_open(cipher, self.session_key, iv)
            if self.uses_gcm
            else aes_cbc_decrypt(cipher, self.session_key, iv)
        )

    @property
    def uses_gcm(self) -> bool:
        return self.encrypt_method == 1 or self.support_type == 4

    # -- dispatch --------------------------------------------------------
    def _handle(self, packet: Packet) -> None:
        plain = self._decrypt(packet.body) if self.session_key is not None else packet.body
        try:
            tlv = TLV.parse(plain)
        except TLVError:
            tlv = None
        key = (packet.service, packet.command)

        if key == (0x01, 0x01):
            self._link_params()
        elif key == (0x01, 0x33):
            self._security_negotiation(tlv)
        elif key == (0x01, 0x2C):
            self._pincode()
        elif key == (0x01, 0x28):
            self._hichain(tlv)
        elif key == (0x01, 0x13):
            self._lite_auth(tlv)
        elif key == (0x01, 0x0F):
            self._lite_bond_params(tlv)
        elif key == (0x01, 0x0E):
            self._lite_bond(tlv)
        elif key == (0x01, 0x08):
            self.send(0x01, 0x08, TLV().put(0x01, b"\x5a"))
        elif key == (0x01, 0x07):
            self._product_info()
        elif key == (0x01, 0x02):
            self._supported_services()
        elif key == (0x25, 0x04):
            self.send(
                0x25,
                0x04,
                TLV()
                .put(0x01, be16(len(self.songs)))
                .put(0x02, be32(2048))
                .put(0x03, bytes([0x81, 0x0C]))
                .put(0x04, be16(200))
                .put(0x05, be16(len(self.songs))),
            )
        elif key == (0x25, 0x05):
            self._music_list(tlv)
        elif key == (0x28, 0x02):
            self._upload_info(tlv)
        elif key == (0x28, 0x03):
            self._upload_hash(tlv)
        elif key == (0x28, 0x04):
            asyncio.get_running_loop().call_soon(self._request_chunk, 0)
        elif key == (0x28, 0x06):
            self._chunk(plain)
        elif key == (0x28, 0x07):
            self._upload_finished()
        elif key == (0x25, 0x09):
            assert tlv is not None
            self.song_info = (tlv.text(0x03) or "", tlv.text(0x04) or "")

    # -- pairing ---------------------------------------------------------
    def _link_params(self) -> None:
        nonce = bytes(random.randrange(256) for _ in range(16))
        self.server_nonce = nonce
        tlv = (
            TLV()
            .put(0x01, be16(2))
            .put(0x02, be16(244))
            .put(0x03, be16(20))
            .put(0x05, bytes([0x00, self.auth_version]) + nonce)
            .put(0x07, bytes([self.support_type]))
            .put(0x08, bytes([self.auth_algo]))
            .put(0x0C, bytes([self.encrypt_method]))
        )
        self.send(0x01, 0x01, tlv, encrypted=False)

    def _security_negotiation(self, tlv: TLV | None) -> None:
        assert tlv is not None
        self.client_id = tlv.get(0x05) or b""
        udid = b"AB" * 32
        if self.support_type == 4:
            self.send(0x01, 0x33, TLV().put(0x01, b"\x01").put(0x05, udid), encrypted=False)
        else:
            # HiChain Lite: device support types 1-3 answer with auth type 0x01
            self.send(0x01, 0x33, TLV().put(0x02, b"\x01").put(0x05, udid), encrypted=False)

    def _pincode(self) -> None:
        self.pin_requests += 1
        key = digest_secret(self.auth_version)
        iv = bytes(random.randrange(256) for _ in range(16))
        cipher = (
            aes_gcm_seal(self.pin, key, iv) if self.encrypt_method == 1 else aes_cbc_encrypt(self.pin, key, iv)
        )
        self.send(0x01, 0x2C, TLV().put(0x01, cipher).put(0x02, iv), encrypted=False)

    def _hichain(self, tlv: TLV | None) -> None:
        assert tlv is not None
        body = json.loads(tlv.get(0x01) or b"{}")
        payload = body["payload"]
        message = body["message"] & 0x0F
        operation = tlv.get(0x02)[0]

        if message == 1:
            key = sha256(self.pin.hex().upper().encode()) if operation == 1 else self.auth_token
            self.psk = hmac_sha256(key, bytes.fromhex(payload["seed"]))
            rand_self = bytes.fromhex(payload["isoSalt"])
            self.rand_self_current = rand_self
            peer_id = payload["peerAuthId"].encode()
            token = hmac_sha256(self.psk, self.rand_peer + rand_self + peer_id + self.auth_id_peer)
            self.iso_key = hkdf_sha256(self.psk, rand_self + self.rand_peer, "hichain_iso_session_key", 32)
            reply = {
                "payload": {
                    "isoSalt": self.rand_peer.hex().upper(),
                    "peerAuthId": self.auth_id_peer.decode(),
                    "token": token.hex().upper(),
                }
            }
            self._hichain_reply(reply)
        elif message == 2:
            assert self.psk is not None
            self._hichain_reply({"payload": {"returnCodeMac": hmac_sha256(self.psk, b"\x00\x00\x00\x00").hex().upper()}})
        elif message == 3 and operation == 1:
            assert self.iso_key is not None
            nonce = bytes.fromhex(payload["nonce"])
            challenge = aes_gcm_open(
                bytes.fromhex(payload["encData"]), self.iso_key, nonce, b"hichain_iso_exchange"
            )
            reply_nonce = bytes(random.randrange(256) for _ in range(12))
            enc = aes_gcm_seal(self.auth_token, self.iso_key, reply_nonce, challenge)
            self._hichain_reply(
                {"payload": {"encAuthToken": enc.hex().upper(), "nonce": reply_nonce.hex().upper()}}
            )
        elif (message == 4 and operation == 1) or (message == 3 and operation == 2):
            assert self.iso_key is not None
            result = aes_gcm_open(
                bytes.fromhex(payload["encResult"]),
                self.iso_key,
                bytes.fromhex(payload["nonce"]),
                b"hichain_iso_result",
            )
            assert result == b"\x00\x00\x00\x00"
            if operation == 2:
                rand_self = self.rand_self_current
                self.session_key = hkdf_sha256(
                    self.iso_key, rand_self + self.rand_peer, "hichain_return_key", 32
                )
            self.send(0x01, 0x28, TLV().put(0x04, b"\x01"), encrypted=False)
        else:  # pragma: no cover
            raise AssertionError(f"unexpected HiChain message {message}/{operation}")

    def _hichain_reply(self, payload: dict) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        self.send(0x01, 0x28, TLV().put(0x04, b"\x00").put(0x01, raw), encrypted=False)

    # -- HiChain Lite (device support types 1-3) -------------------------
    def _lite_auth(self, tlv: TLV | None) -> None:
        assert tlv is not None
        challenge = tlv.get(0x01) or b""
        nonce = tlv.get(0x02) or b""
        key = self.secret_key if self.auth_version == 0x02 else self.pin
        self.lite_client_nonce = nonce[2:]
        double_nonce = self.server_nonce + self.lite_client_nonce
        expected = digest_lite(
            b"\x01\x00", key, double_nonce, auth_version=self.auth_version, auth_algo=self.auth_algo
        )[:32]
        if challenge != expected:
            self.send(0x01, 0x13, TLV().put(0x7F, be32(1)), encrypted=False)
            return
        self.first_key = digest_lite(
            b"\x01\x00", key, double_nonce, auth_version=self.auth_version, auth_algo=self.auth_algo
        )[32:48]
        answer = digest_lite(
            b"\x01\x10", key, double_nonce, auth_version=self.auth_version, auth_algo=self.auth_algo
        )[:32]
        self.send(0x01, 0x13, TLV().put(0x01, answer), encrypted=False)

    def _lite_bond_params(self, tlv: TLV | None) -> None:
        assert tlv is not None
        self.lite_serial = tlv.get(0x03) or b""
        self.lite_mac = tlv.get(0x07) or b""
        self.encryption_counter = 0x10203040
        self.send(
            0x01,
            0x0F,
            TLV().put(0x01, b"\x00").put(0x09, be32(self.encryption_counter)),
            encrypted=False,
        )

    def _lite_bond(self, tlv: TLV | None) -> None:
        assert tlv is not None
        wrapped = tlv.get(0x06) or b""
        iv = tlv.get(0x07) or b""
        assert self.first_key is not None
        secret = (
            aes_gcm_open(wrapped, self.first_key, iv)
            if self.encrypt_method == 1
            else aes_cbc_decrypt(wrapped, self.first_key, iv)
        )
        self.secret_key = secret
        self.session_key = secret
        self.send(0x01, 0x0E, TLV(), encrypted=False)

    # -- info ------------------------------------------------------------
    def _product_info(self) -> None:
        self.send(
            0x01,
            0x07,
            TLV()
            .put(0x0A, b"HUAWEI WATCH GT 4")
            .put(0x11, b"GT 4")
            .put(0x07, b"1.0.0.100")
            .put(0x09, b"SN123456"),
        )

    def _supported_services(self) -> None:
        flags = bytes(1 if svc in (0x25, 0x28, 0x01) else 0 for svc in P.SUPPORTED_SERVICES)
        self.send(0x01, 0x02, TLV().put(0x02, flags))

    def _music_list(self, tlv: TLV | None) -> None:
        assert tlv is not None
        start = tlv.integer(0x01) or 0
        end = tlv.integer(0x04) or 0xFFFF
        inner = TLV()
        for index, title, artist, name in self.songs:
            if start <= index <= end:
                inner.put(
                    0x83,
                    TLV()
                    .put(0x04, bytes([index & 0xFF]))
                    .put(0x05, title.encode())
                    .put(0x06, artist.encode())
                    .put(0x07, name.encode())
                    .serialize(),
                )
        self.send(
            0x25,
            0x05,
            TLV().put(0x01, be16(start)).put(0x04, be16(end)).put(0x82, inner.serialize()),
        )

    # -- upload ----------------------------------------------------------
    def _upload_info(self, tlv: TLV | None) -> None:
        assert tlv is not None
        self.upload_name = tlv.text(0x01) or ""
        self.upload_size = tlv.integer(0x02) or 0
        self.upload_type = (tlv.get(0x03) or b"\x00")[0]
        self.send(0x28, 0x02, TLV().put(0x7F, be32(C.RESULT_SUCCESS)))
        asyncio.get_running_loop().call_soon(
            self.send, 0x28, 0x03, TLV().put(0x01, bytes([self.file_id]))
        )

    def _upload_hash(self, tlv: TLV | None) -> None:
        assert tlv is not None
        self.upload_digest = tlv.get(0x03) or b""
        self.send(
            0x28,
            0x04,
            TLV()
            .put(0x01, bytes([self.file_id]))
            .put(0x02, b"1.0")
            .put(0x05, be16(self.unit_size))
            .put(0x09, b"\x01" if self.no_encrypt_chunks else b"\x00"),
        )

    def _request_chunk(self, offset: int) -> None:
        self.requested = offset
        self.send(0x28, 0x05, TLV().put(0x02, be32(offset)).put(0x03, be32(self.chunk_size)))

    def _chunk(self, plain: bytes) -> None:
        if len(plain) < 6:
            return
        file_id, _index = plain[0], plain[1]
        offset = int.from_bytes(plain[2:6], "big")
        data = plain[6:]
        assert file_id == self.file_id
        assert offset == len(self.received), f"offset {offset} != received {len(self.received)}"
        self.received += data
        if len(self.received) >= self.upload_size:
            self.send(0x28, 0x07, TLV().put(0x01, bytes([self.file_id])))
        elif len(self.received) - self.requested >= self.chunk_size:
            asyncio.get_running_loop().call_soon(self._request_chunk, len(self.received))

    def _upload_finished(self) -> None:
        self.upload_done = True
        assert hashlib.sha256(self.received).digest() == self.upload_digest
        self.info_requested = True
        asyncio.get_running_loop().call_soon(
            self.send,
            0x25,
            0x09,
            TLV().put(0x01, be16(0)).put(0x02, self.upload_name.encode()),
        )
