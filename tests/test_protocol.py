from __future__ import annotations

import json

from hwmusic import constants as C
from hwmusic import protocol as P
from hwmusic.crypto import hkdf_sha256, hmac_sha256
from hwmusic.tlv import TLV, be16, be32


def test_link_info_parsing() -> None:
    nonce = bytes(range(16))
    tlv = (
        TLV()
        .put(0x01, be16(2))
        .put(0x02, be16(244))
        .put(0x03, be16(20))
        .put(0x05, bytes([0x00, 0x03]) + nonce)
        .put(0x07, b"\x04")
        .put(0x08, b"\x00")
        .put(0x0C, b"\x01")
    )
    link = P.LinkInfo.from_tlv(tlv)
    assert link.protocol_version == 2
    assert link.slice_size == 244
    assert link.auth_version == 3
    assert link.server_nonce == nonce
    assert link.device_support_type == 4
    assert link.uses_gcm is True


def test_link_info_cbc_default() -> None:
    link = P.LinkInfo.from_tlv(TLV().put(0x07, b"\x03").put(0x0C, b"\x00"))
    assert link.uses_gcm is False
    assert link.slice_size == C.DEFAULT_SLICE_SIZE


def test_auth_type_parsing() -> None:
    assert P.parse_auth_type(TLV().put(0x01, b"\x01")) == 0x186A0
    assert P.parse_auth_type(TLV().put(0x01, b"\x04")) == -1
    assert P.parse_auth_type(TLV().put(0x02, b"\x00")) == 0
    assert P.parse_auth_type(TLV().put(0x7F, b"\x05")) == 5
    assert P.is_hichain(0x186A0)
    assert P.is_hichain(5)  # 5 ^ 1 == 4
    assert P.is_hichain(6)  # 6 ^ 2 == 4
    assert not P.is_hichain(9)


def test_security_negotiation_request_shape() -> None:
    device_id = b"A" * 32
    tlv = P.security_negotiation_request(0x04, device_id, "SM-S918B", 1)
    assert tlv.get(0x01) == b"\x04"
    assert tlv.get(0x05) == device_id
    assert tlv.get(0x07) == b"SM-S918B"
    assert tlv.get(0x0D) == b"\x01"


def test_hichain_step_one_json() -> None:
    message = P.HiChainMessage(operation=1, request_id=1700000000000, self_auth_id=b"F" * 32)
    tlv = message.step_one()
    assert tlv.get(0x02) == b"\x01"
    assert tlv.get(0x03) == (1700000000000).to_bytes(8, "big")
    body = json.loads(tlv.get(0x01))
    assert body["authForm"] == 0
    assert body["message"] == 1
    assert body["groupId"] == C.HICHain_GROUP_ID
    assert body["appId"] == "com.huawei.health"
    assert body["peerDeviceId"] == "F" * 32
    payload = body["payload"]
    assert payload["operationCode"] == 1
    assert payload["isoSalt"] == message.rand_self.hex().upper()
    assert payload["seed"] == message.seed.hex().upper()
    assert next(iter(payload)) == "version"


def test_hichain_bind_message_numbers() -> None:
    message = P.HiChainMessage(operation=2, request_id=1, self_auth_id=b"B" * 32)
    body = json.loads(message.step_one().get(0x01))
    assert body["message"] == 0x11  # 1 | 0x10
    assert body["isDeviceLevel"] is False
    body = json.loads(message.step_four(b"\x00" * 12, b"\x01" * 20).get(0x01))
    assert body["message"] == 0x13  # 3 | 0x10
    assert body["payload"]["operationCode"] == 2


def test_hichain_authentication_messages() -> None:
    message = P.HiChainMessage(operation=1, request_id=1, self_auth_id=b"B" * 32)
    body = json.loads(message.step_four(b"\x00" * 12, b"\x01" * 20).get(0x01))
    assert body["message"] == 4
    assert "isDeviceLevel" not in body


def test_hichain_key_derivation_matches_watch_side() -> None:
    # Watch-side simulation of the HiChain step-1 token
    key = b"k" * 32
    message = P.HiChainMessage(operation=1, request_id=1, self_auth_id=b"C" * 32)
    rand_peer = bytes(range(16))
    auth_id_peer = b"D" * 32
    psk = hmac_sha256(key, message.seed)
    peer_token = hmac_sha256(psk, rand_peer + message.rand_self + message.self_auth_id + auth_id_peer)
    payload = {
        "isoSalt": rand_peer.hex().upper(),
        "peerAuthId": auth_id_peer.decode(),
        "token": peer_token.hex().upper(),
    }
    got_psk, iso_key, got_rand_peer = P.hichain_derive_keys(key, message, payload)
    assert got_psk == psk
    assert got_rand_peer == rand_peer
    assert iso_key == hkdf_sha256(psk, message.rand_self + rand_peer, "hichain_iso_session_key", 32)
    self_token = P.hichain_self_token(psk, message, rand_peer, auth_id_peer)
    assert self_token == hmac_sha256(psk, message.rand_self + rand_peer + auth_id_peer + message.self_auth_id)
    assert P.hichain_return_key(iso_key, message.rand_self, rand_peer) == hkdf_sha256(
        iso_key, message.rand_self + rand_peer, "hichain_return_key", 32
    )


def test_hichain_rejects_bad_token() -> None:
    message = P.HiChainMessage(operation=1, request_id=1, self_auth_id=b"C" * 32)
    payload = {
        "isoSalt": "00" * 16,
        "peerAuthId": "44" * 32,
        "token": "00" * 32,
    }
    try:
        P.hichain_derive_keys(b"k" * 32, message, payload)
    except Exception as exc:  # noqa: BLE001
        assert "校验失败" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("bad token accepted")


def test_parse_hichain_finished() -> None:
    assert isinstance(P.parse_hichain_response(TLV().put(0x04, b"\x01")), P.HiChainFinished)


def test_upload_info_tlv() -> None:
    tlv = P.upload_info_request("song.mp3", 1234, C.FILE_TYPE_MUSIC)
    assert tlv.get(0x01) == b"song.mp3"
    assert tlv.integer(0x02) == 1234
    assert tlv.get(0x03) == b"\x02"


def test_chunk_payloads_offsets() -> None:
    data = bytes(range(10))
    payloads = P.chunk_payloads(data, offset=100, unit_size=4, file_id=7)
    assert len(payloads) == 3
    assert payloads[0] == bytes([7, 0]) + be32(100) + bytes([0, 1, 2, 3])
    assert payloads[1] == bytes([7, 1]) + be32(104) + bytes([4, 5, 6, 7])
    assert payloads[2] == bytes([7, 2]) + be32(108) + bytes([8, 9])


def test_music_storage_parse() -> None:
    tlv = TLV().put(0x01, b"\x00").put(0x02, be32(4096)).put(0x04, be16(500)).put(0x05, be16(12))
    storage = P.parse_music_storage(tlv)
    assert (storage.free_mb, storage.max_songs, storage.songs) == (4096, 500, 12)
    assert storage.formats == []


def test_music_formats_parse() -> None:
    assert P.parse_music_formats(bytes([0x81, 0x0C])) == ["mp3", "m4a", "flac"]
    assert P.parse_music_formats(bytes([0x01])) == ["mp3"]
    assert P.parse_music_formats(b"") == []


def test_music_list_parse() -> None:
    inner = TLV()
    for index, title, artist, name in ((0, "A", "B", "a.mp3"), (7, "C", "", "c.flac")):
        inner.put(
            0x83,
            TLV().put(0x04, bytes([index])).put(0x05, title).put(0x06, artist).put(0x07, name).serialize(),
        )
    tlv = TLV().put(0x01, be16(0)).put(0x04, be16(0xFFFF)).put(0x82, inner.serialize())
    start, end, entries = P.parse_music_list(tlv)
    assert (start, end) == (0, 0xFFFF)
    assert [(e.index, e.title, e.artist, e.file_name) for e in entries] == [
        (0, "A", "B", "a.mp3"),
        (7, "C", "", "c.flac"),
    ]


def test_music_list_request_shape() -> None:
    tlv = P.music_list_request(3, 250)
    assert tlv.integer(0x01) == 3
    assert tlv.integer(0x04) == 250


def test_music_upload_info_reply() -> None:
    tlv = P.music_upload_info_reply(3, "标题", "歌手")
    assert tlv.get(0x01) == be16(3)
    assert tlv.text(0x03) == "标题"
    assert tlv.text(0x04) == "歌手"


def test_phone_info_answer() -> None:
    answer = P.phone_info_answer([0x08, 0x11, 0x02, 0x00])
    assert answer.get(0x08) == b"14"
    assert answer.integer(0x11) == C.HUAWEI_HEALTH_VERSION
    assert answer.get(0x02) == b""
    assert answer.get(0x00) is None


def test_supported_services_parse() -> None:
    flags = bytes(1 if svc == 0x25 else 0 for svc in P.SUPPORTED_SERVICES)
    assert P.parse_supported_services(TLV().put(0x02, flags)) == [0x25]
