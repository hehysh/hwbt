"""Wire messages: builders for requests and parsers for responses."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import constants as C
from .crypto import (
    CryptoError,
    aes_cbc_decrypt,
    aes_gcm_open,
    digest_secret,
    hkdf_sha256,
    hmac_sha256,
    ordered_json,
    random_bytes,
)
from .tlv import TLV, TLVError, be16, be32


# ---------------------------------------------------------------------------
# 0x01/0x01 Link parameters
# ---------------------------------------------------------------------------
def link_params_request() -> TLV:
    return TLV().put(0x01).put(0x02).put(0x03).put(0x04)


@dataclass(slots=True)
class LinkInfo:
    protocol_version: int = 0
    slice_size: int = C.DEFAULT_SLICE_SIZE
    mtu: int = 0x14
    auth_version: int = 0
    device_support_type: int = 0
    auth_algo: int = 0
    bond_state: int = 0
    encrypt_method: int = 0
    server_nonce: bytes = b""

    @classmethod
    def from_tlv(cls, tlv: TLV) -> LinkInfo:
        info = cls()
        if (v := tlv.integer(0x01)) is not None:
            info.protocol_version = v
        if (v := tlv.integer(0x02)) is not None:
            info.slice_size = v
        if (v := tlv.integer(0x03)) is not None:
            info.mtu = v
        raw = tlv.get(0x05)
        if raw and len(raw) >= 18:
            info.auth_version = raw[1]
            info.server_nonce = raw[2:18]
        if (v := tlv.integer(0x07)) is not None:
            info.device_support_type = v
        if (v := tlv.integer(0x08)) is not None:
            info.auth_algo = v
        if (v := tlv.integer(0x09)) is not None:
            info.bond_state = v
        if (v := tlv.integer(0x0C)) is not None:
            info.encrypt_method = v
        return info

    @property
    def uses_gcm(self) -> bool:
        return self.encrypt_method == 1 or self.device_support_type == 4

    def describe(self) -> str:
        return (
            f"协议 v{self.protocol_version}, 分片 {self.slice_size}B, "
            f"手表 MTU {self.mtu}B, "
            f"认证 v{self.auth_version}, 设备类型 {self.device_support_type}, "
            f"{'AES-GCM' if self.uses_gcm else 'AES-CBC'}"
        )


# ---------------------------------------------------------------------------
# 0x01/0x33 Security negotiation
# ---------------------------------------------------------------------------
def security_negotiation_request(
    auth_mode: int, device_id: bytes, phone_model: str, encrypt_method: int
) -> TLV:
    tlv = TLV().put(0x01, bytes([auth_mode]))
    if auth_mode in (0x02, 0x04):
        tlv.put(0x02, b"\x01")
    tlv.put(0x05, device_id).put(0x03, b"\x01").put(0x04, b"\x00")
    if auth_mode == 0x04:
        tlv.put(0x06).put(0x07, phone_model.encode())
    if encrypt_method == 1:
        tlv.put(0x0D, b"\x01")
    return tlv


def parse_auth_type(tlv: TLV) -> int:
    auth_type = -1
    pw = -1
    raw = tlv.get(0x01)
    if raw:
        if raw[0] == 0x01:
            auth_type = C.RESULT_SUCCESS
        if raw[0] == 0x04:
            pw = 4
    raw = tlv.get(0x02)
    if raw:
        auth_type = _signed(raw[0])
        if pw != -1:
            auth_type ^= pw
    raw = tlv.get(0x7F)
    if raw:
        auth_type = _signed(raw[0])
    return auth_type


def is_hichain(auth_type: int) -> bool:
    return auth_type == C.RESULT_SUCCESS or (auth_type ^ 0x01) == 0x04 or (auth_type ^ 0x02) == 0x04


def is_hichain_lite(auth_type: int) -> bool:
    """HiChain Lite: used by device support types 1/2/3 (e.g. the GT 2e)."""
    return auth_type in (0x01, 0x02)


# ---------------------------------------------------------------------------
# 0x01/0x13 Auth challenge (HiChain Lite)
# ---------------------------------------------------------------------------
def auth_request(challenge: bytes, nonce: bytes, auth_algo: int) -> TLV:
    return TLV().put(0x01, challenge).put(0x02, nonce).put(0x03, bytes([auth_algo]))


def auth_nonce(auth_version: int, client_nonce: bytes) -> bytes:
    return auth_version.to_bytes(2, "big") + client_nonce


# ---------------------------------------------------------------------------
# 0x01/0x0F Bond parameters / 0x01/0x0E Bond (HiChain Lite)
# ---------------------------------------------------------------------------
def bond_params_request(serial: bytes, mac: bytes) -> TLV:
    return (
        TLV()
        .put(0x01)
        .put(0x03, serial)
        .put(0x04, b"\x02")
        .put(0x05)
        .put(0x07, mac)
        .put(0x09)
    )


def parse_bond_params(tlv: TLV) -> tuple[int, int]:
    status = tlv.integer(0x01) or 0
    counter = tlv.integer(0x09) or 0
    return status, counter & 0xFFFFFFFF


def bond_request(serial: bytes, wrapped_key: bytes, iv: bytes) -> TLV:
    return (
        TLV()
        .put(0x01)
        .put(0x03, b"\x00")
        .put(0x05, serial)
        .put(0x06, wrapped_key)
        .put(0x07, iv)
    )


def _signed(byte: int) -> int:
    return byte - 256 if byte >= 128 else byte


# ---------------------------------------------------------------------------
# 0x01/0x2C PIN code
# ---------------------------------------------------------------------------
def pincode_request() -> TLV:
    return TLV().put(0x01)


def decrypt_pincode(tlv: TLV, link: LinkInfo) -> bytes:
    message = tlv.get(0x01)
    iv = tlv.get(0x02)
    if message is None or iv is None:
        raise CryptoError("PIN response has no 0x01/0x02")
    key = digest_secret(link.auth_version)
    if link.encrypt_method == 1:
        return aes_gcm_open(message, key, iv)
    return aes_cbc_decrypt(message, key, iv)


# ---------------------------------------------------------------------------
# 0x01/0x28 HiChain pairing handshake
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class HiChainMessage:
    operation: int  # 1 = authenticate (PIN), 2 = bind (saved key)
    request_id: int
    self_auth_id: bytes  # ASCII hex of our 16 random bytes
    group_id: str = C.HICHain_GROUP_ID
    seed: bytes = field(default_factory=lambda: random_bytes(32))
    rand_self: bytes = field(default_factory=lambda: random_bytes(16))

    # -- helpers ---------------------------------------------------------
    def _wrap(self, message: int, payload: dict, *, device_level: bool) -> TLV:
        version = {"minVersion": "1.0.0", "currentVersion": "2.0.16"}
        body: dict = {
            "authForm": 0,
            "payload": {"version": version, **payload},
            "groupAndModuleVersion": "2.0.1",
            "message": (message | 0x10) if self.operation == 2 else message,
        }
        if self.operation == 1:
            device_id = self.self_auth_id.decode("ascii")
            body.update(
                {
                    "requestId": str(self.request_id),
                    "groupId": self.group_id,
                    "groupName": "health_group_name",
                    "groupOp": 2,
                    "groupType": 256,
                    "peerDeviceId": device_id,
                    "connDeviceId": device_id,
                    "appId": "com.huawei.health",
                    "ownerName": "",
                }
            )
        if device_level:
            body["isDeviceLevel"] = False
        return (
            TLV()
            .put(0x01, ordered_json(body))
            .put(0x02, bytes([self.operation]))
            .put(0x03, self.request_id.to_bytes(8, "big"))
        )

    # -- steps -----------------------------------------------------------
    def step_one(self) -> TLV:
        payload: dict = {
            "isoSalt": self.rand_self.hex().upper(),
            "peerAuthId": self.self_auth_id.decode("ascii"),
            "operationCode": self.operation,
            "seed": self.seed.hex().upper(),
            "peerUserType": 0,
        }
        if self.operation == 2:
            payload.update(
                {
                    "pkgName": "com.huawei.devicegroupmanage",
                    "serviceType": self.group_id,
                    "keyLength": 0x20,
                }
            )
        return self._wrap(1, payload, device_level=self.operation == 2)

    def step_two(self, token: bytes) -> TLV:
        return self._wrap(
            2,
            {"peerAuthId": self.self_auth_id.decode("ascii"), "token": token.hex().upper()},
            device_level=self.operation == 2,
        )

    def step_three(self, nonce: bytes, enc_data: bytes) -> TLV:
        return self._wrap(
            3,
            {"nonce": nonce.hex().upper(), "encData": enc_data.hex().upper()},
            device_level=False,
        )

    def step_four(self, nonce: bytes, enc_result: bytes) -> TLV:
        message = 4 if self.operation == 1 else 3
        return self._wrap(
            message,
            {
                "nonce": nonce.hex().upper(),
                "encResult": enc_result.hex().upper(),
                "operationCode": self.operation,
            },
            device_level=False,
        )


class HiChainFinished:
    """Marker returned when the watch reports the handshake result."""


@dataclass(slots=True)
class HiChainStep:
    step: int
    payload: dict


def parse_hichain_response(tlv: TLV) -> HiChainStep | HiChainFinished:
    code = tlv.integer(0x7F)
    if code is not None and code != C.RESULT_SUCCESS:
        raise CryptoError(f"HiChain 返回错误 0x{code:X}")
    kind = tlv.get(0x04)
    if not kind:
        raise CryptoError(f"HiChain 响应缺少类型字段: {tlv!r}")
    if kind[0] != 0x00:
        return HiChainFinished()
    raw = tlv.get(0x01)
    if not raw:
        raise CryptoError("HiChain 响应没有 JSON")
    import json

    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise CryptoError(f"HiChain JSON 解析失败: {exc}") from exc
    payload = value.get("payload") or {}
    if (err := payload.get("errorCode")) not in (None, 0):
        raise CryptoError(f"HiChain errorCode {err}")
    if "isoSalt" in payload:
        return HiChainStep(1, payload)
    if "returnCodeMac" in payload:
        return HiChainStep(2, payload)
    if "encAuthToken" in payload:
        return HiChainStep(3, payload)
    return HiChainStep(0, payload)


def hichain_derive_keys(
    pin_or_token: bytes, message: HiChainMessage, peer_payload: dict
) -> tuple[bytes, bytes, bytes]:
    """Verify the watch's token and derive the ISO session key.

    Returns ``(psk, iso_key, rand_peer)``.
    """
    try:
        rand_peer = bytes.fromhex(peer_payload["isoSalt"])
        auth_id_peer = peer_payload["peerAuthId"].encode("ascii")
        peer_token = bytes.fromhex(peer_payload["token"])
    except (KeyError, ValueError) as exc:
        raise CryptoError(f"HiChain 第 1 步响应缺字段: {exc}") from exc
    psk = hmac_sha256(pin_or_token, message.seed)
    expected = hmac_sha256(
        psk, rand_peer + message.rand_self + message.self_auth_id + auth_id_peer
    )
    if expected != peer_token:
        raise CryptoError("HiChain 校验失败：PIN 或已保存的配对密钥不正确")
    salt = message.rand_self + rand_peer
    iso_key = hkdf_sha256(psk, salt, "hichain_iso_session_key", 32)
    return psk, iso_key, rand_peer


def hichain_self_token(psk: bytes, message: HiChainMessage, rand_peer: bytes, auth_id_peer: bytes) -> bytes:
    return hmac_sha256(
        psk, message.rand_self + rand_peer + auth_id_peer + message.self_auth_id
    )


def hichain_return_key(iso_key: bytes, rand_self: bytes, rand_peer: bytes) -> bytes:
    return hkdf_sha256(iso_key, rand_self + rand_peer, "hichain_return_key", 32)


# ---------------------------------------------------------------------------
# 0x01/0x07 Product info, 0x01/0x08 battery, 0x01/0x02/0x03 capabilities
# ---------------------------------------------------------------------------
_PRODUCT_INFO_TAGS = (
    0x01, 0x02, 0x07, 0x09, 0x0A, 0x11, 0x12, 0x16,
    0x1A, 0x1D, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23,
)


def product_info_request() -> TLV:
    tlv = TLV()
    for tag in _PRODUCT_INFO_TAGS:
        tlv.put(tag)
    return tlv


def describe_product_info(tlv: TLV) -> dict[str, str]:
    def text(tag: int) -> str | None:
        raw = tlv.get(tag)
        if not raw:
            return None
        return raw.decode("utf-8", "replace").strip()

    result: dict[str, str] = {}
    for tag, key in ((0x0A, "model"), (0x11, "name"), (0x07, "firmware"), (0x03, "hardware"), (0x09, "serial")):
        value = text(tag)
        if value:
            result[key] = value
    return result


def battery_request() -> TLV:
    return TLV().put(0x01)


SUPPORTED_SERVICES = (
    0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A,
    0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12, 0x13, 0x14,
    0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1D, 0x20,
    0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x2A, 0x2B, 0x2D, 0x2E,
    0x30, 0x32, 0x33, 0x34, 0x35,
)


def supported_services_request() -> TLV:
    return TLV().put(0x01, bytes(SUPPORTED_SERVICES))


def parse_supported_services(tlv: TLV) -> list[int]:
    flags = tlv.get(0x02)
    if not flags:
        return []
    return [service for service, flag in zip(SUPPORTED_SERVICES, flags, strict=False) if flag == 1]


def supported_commands_request(asked: list[tuple[int, list[int]]]) -> TLV:
    inner = TLV()
    for service, commands in asked:
        inner.put(0x02, bytes([service])).put(0x03, bytes(commands))
    return TLV().put(0x81, inner.serialize())


def parse_supported_commands(tlv: TLV, asked: list[tuple[int, list[int]]]) -> dict[int, list[int]]:
    raw = tlv.get(0x81)
    if not raw:
        return {}
    try:
        container = TLV.parse(raw)
    except TLVError:
        return {}
    result: dict[int, list[int]] = {}
    service: int | None = None
    by_service = {svc: cmds for svc, cmds in asked}
    for tag, value in container.items:
        if tag == 0x02 and value:
            service = value[0]
        elif tag == 0x04 and service is not None and service in by_service:
            commands = by_service[service]
            result[service] = [cmd for index, cmd in enumerate(commands) if index < len(value) and value[index] == 1]
    return result


# ---------------------------------------------------------------------------
# 0x01/0x10 Phone info (the watch asks who we are)
# ---------------------------------------------------------------------------
def phone_info_requested_tags(tlv: TLV) -> list[int] | None:
    if len(tlv) == 1 and tlv.integer(0x7F) == C.RESULT_SUCCESS:
        return None
    return tlv.tags()


def phone_info_answer(tags: list[int]) -> TLV:
    tlv = TLV()
    for tag in tags:
        if tag in (0x00, 0x0F):
            continue
        if tag in (0x02, 0x04, 0x15):
            tlv.put(tag)
        elif tag == 0x08:
            tlv.put(tag, b"14")
        elif tag == 0x11:
            tlv.put(tag, be32(C.HUAWEI_HEALTH_VERSION))
        else:
            tlv.put(tag, b"\x00")
    return tlv


# ---------------------------------------------------------------------------
# 0x25 Music
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class MusicStorage:
    free_mb: int = 0
    max_songs: int = 0
    songs: int = 0
    frames: int = 0
    formats: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return f"剩余 {self.free_mb} MB，已存 {self.songs}/{self.max_songs} 首"


_FORMAT_BYTES = (
    ("mp3", "wav", "aac", "sbc", "msbc", "hwa", "cvsd"),
    ("pcm", "ape", "m4a", "flac", "opus", "ogg", "butt"),
    ("amr", "imy"),
)


def parse_music_formats(raw: bytes) -> list[str]:
    formats: list[str] = []
    for group, byte in enumerate(raw):
        if group >= len(_FORMAT_BYTES):
            break
        for bit, name in enumerate(_FORMAT_BYTES[group]):
            if byte & (1 << bit):
                formats.append(name)
        if not byte & 0x80:
            break
    return formats


def music_storage_request() -> TLV:
    return TLV().put(0x01).put(0x02).put(0x03).put(0x04).put(0x05)


def parse_music_storage(tlv: TLV) -> MusicStorage:
    raw_formats = tlv.get(0x03) or b""
    return MusicStorage(
        free_mb=tlv.integer(0x02) or 0,
        max_songs=tlv.integer(0x04) or 0,
        songs=tlv.integer(0x05) or 0,
        frames=tlv.integer(0x01) or 0,
        formats=parse_music_formats(raw_formats) if raw_formats else [],
    )


@dataclass(slots=True)
class MusicEntry:
    index: int
    title: str
    artist: str
    file_name: str


def music_list_request(start_frame: int = 0, end_index: int = 0xFFFF) -> TLV:
    return TLV().put(0x01, be16(start_frame & 0xFFFF)).put(0x04, be16(end_index & 0xFFFF))


def parse_music_list(tlv: TLV) -> tuple[int, int, list[MusicEntry]]:
    """Parse a 0x25/0x05 response: ``(start_frame, end_index, entries)``.

    Entries live in a 0x82 container as repeated 0x83 sub-TLVs carrying
    ``0x04`` index, ``0x05`` title, ``0x06`` artist and ``0x07`` file name.
    """
    start = tlv.integer(0x01) or 0
    end = tlv.integer(0x04) or 0
    entries: list[MusicEntry] = []
    container = tlv.get(0x82)
    if container:
        try:
            outer = TLV.parse(container)
        except TLVError:
            outer = None
        if outer is not None:
            for raw in outer.get_all(0x83):
                try:
                    item = TLV.parse(raw)
                except TLVError:
                    continue
                entries.append(
                    MusicEntry(
                        index=item.integer(0x04) or 0,
                        title=item.text(0x05) or "",
                        artist=item.text(0x06) or "",
                        file_name=item.text(0x07) or "",
                    )
                )
    return start, end, entries


def music_upload_info_reply(index: int, title: str, artist: str) -> TLV:
    return (
        TLV()
        .put(0x01, be16(index & 0xFFFF))
        .put(0x03, title[:80].encode("utf-8"))
        .put(0x04, artist[:60].encode("utf-8"))
    )


def music_control_ack() -> TLV:
    return TLV().put(0x7F, be32(C.RESULT_SUCCESS))


def music_screen_info(artist: str, title: str, playing: bool, volume: int, max_volume: int = 15) -> TLV:
    return (
        TLV()
        .put(0x01, artist[:90].encode("utf-8"))
        .put(0x02, title[:90].encode("utf-8"))
        .put(0x03, bytes([3 if playing else 2]))
        .put(0x04, bytes([max_volume & 0xFF]))
        .put(0x05, bytes([volume & 0xFF]))
    )


# ---------------------------------------------------------------------------
# 0x28 File upload
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class UploadParams:
    file_id: int = 0
    protocol_version: str = ""
    app_wait_time: int = 0
    bitmap_enable: int = 0
    unit_size: int = 0
    max_apply_data_size: int = 0
    interval: int = 0
    received_file_size: int = 0
    no_encrypt: int = 0

    @classmethod
    def from_tlv(cls, tlv: TLV) -> UploadParams:
        return cls(
            file_id=tlv.get(0x01)[0] if tlv.get(0x01) else 0,
            protocol_version=tlv.text(0x02) or "",
            app_wait_time=tlv.integer(0x03) or 0,
            bitmap_enable=tlv.integer(0x04) or 0,
            unit_size=tlv.integer(0x05) or 0,
            max_apply_data_size=tlv.integer(0x06) or 0,
            interval=tlv.integer(0x07) or 0,
            received_file_size=tlv.integer(0x08) or 0,
            no_encrypt=tlv.get(0x09)[0] if tlv.get(0x09) else 0,
        )


def upload_info_request(file_name: str, size: int, file_type: int) -> TLV:
    tlv = TLV().put(0x01, file_name.encode("utf-8")).put(0x02, be32(size)).put(0x03, bytes([file_type]))
    if file_type == C.FILE_TYPE_WATCHFACE:
        stem = file_name.split("_", 1)
        tlv.put(0x05, stem[0].encode()).put(0x06, (stem[1] if len(stem) > 1 else "1.0.0").encode())
    return tlv


def upload_hash_reply(file_id: int, digest: bytes) -> TLV:
    return TLV().put(0x01, bytes([file_id & 0xFF])).put(0x03, digest)


def upload_consult_ack(file_id: int, no_encrypt: bool) -> TLV:
    tlv = TLV().put(0x7F, be32(C.RESULT_SUCCESS)).put(0x01, bytes([file_id & 0xFF]))
    if no_encrypt:
        tlv.put(0x09, b"\x01")
    return tlv


def upload_result_reply(file_id: int) -> TLV:
    return TLV().put(0x7F, be32(C.RESULT_SUCCESS)).put(0x01, bytes([file_id & 0xFF]))


def chunk_payloads(data: bytes, offset: int, unit_size: int, file_id: int) -> list[bytes]:
    """Split ``data`` into ``[file id][index][offset BE32] + data`` units."""
    payloads: list[bytes] = []
    position = 0
    slice_start = offset
    index = 0
    while position < len(data):
        end = min(position + max(unit_size, 1), len(data))
        payloads.append(bytes([file_id & 0xFF, index & 0xFF]) + be32(slice_start) + data[position:end])
        slice_start += end - position
        position = end
        index = (index + 1) & 0xFF
    return payloads


def describe_upload_error(code: int) -> str:
    return C.UPLOAD_ERRORS.get(code, f"手表返回错误码 {code}")
