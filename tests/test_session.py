from __future__ import annotations

import asyncio

import pytest

from hwmusic import constants as C
from hwmusic.protocol import MusicStorage
from hwmusic.session import HuaweiSession, SessionError, UploadCancelled, UploadState
from hwmusic.tlv import TLV

from .fake_watch import FakeWatch, MockTransport

DEVICE_ID = b"0123456789ABCDEF0123456789ABCDEF"


def make_session(watch: FakeWatch, **kwargs) -> HuaweiSession:
    transport = MockTransport(watch)
    return HuaweiSession(
        transport,
        device_id=DEVICE_ID,
        log=lambda level, message: None,
        on_event=lambda name, data: None,
        **kwargs,
    )


async def pair(session: HuaweiSession, watch: FakeWatch) -> None:
    transport = session.transport
    await transport.connect()
    await session.start()
    await session.pair()


@pytest.mark.asyncio
async def test_pairing_establishes_session_key() -> None:
    watch = FakeWatch()
    session = make_session(watch)
    await pair(session, watch)
    assert session.paired is True
    assert session.session_key is not None
    assert session.session_key == watch.session_key
    assert len(session.session_key) == 32


@pytest.mark.asyncio
async def test_pairing_with_saved_token_skips_pin() -> None:
    watch = FakeWatch()
    session = make_session(watch)
    transport = session.transport
    await transport.connect()
    await session.start()
    token = await session.pair()

    # Reconnect with the stored token: the watch must not be asked for a PIN again
    watch.session_key = None
    watch.pin_requests = 0
    session2 = make_session(watch)
    await session2.transport.connect()
    await session2.start()
    await session2.pair(saved_token=token)
    assert session2.paired is True
    assert session2.session_key == watch.session_key
    assert watch.pin_requests == 0


@pytest.mark.asyncio
async def test_device_information() -> None:
    watch = FakeWatch()
    session = make_session(watch)
    await pair(session, watch)
    assert await session.battery() == 0x5A
    info = await session.product_info()
    assert info["model"] == "HUAWEI WATCH GT 4"
    assert info["firmware"] == "1.0.0.100"
    services = await session.supported_services()
    assert C.SERVICE_MUSIC in services
    storage = await session.music_storage()
    assert isinstance(storage, MusicStorage)
    assert storage.free_mb == 2048
    assert storage.songs == 2
    assert storage.frames == 2
    assert storage.formats == ["mp3", "m4a", "flac"]


@pytest.mark.asyncio
async def test_upload_music_roundtrip() -> None:
    watch = FakeWatch(unit_size=200, chunk_size=600)
    session = make_session(watch)
    await pair(session, watch)

    payload = bytes((index * 7) % 256 for index in range(5000))
    progress: list[float] = []
    await session.upload_music(
        payload,
        "song.mp3",
        title="测试歌曲",
        artist="测试歌手",
        progress=progress.append,
    )
    await asyncio.sleep(0.05)  # let queued acknowledgements run

    assert bytes(watch.received) == payload
    assert watch.upload_done is True
    assert watch.upload_name == "song.mp3"
    assert watch.upload_type == C.FILE_TYPE_MUSIC
    assert watch.song_info == ("测试歌曲", "测试歌手")
    assert progress and progress[-1] == 1.0


@pytest.mark.asyncio
async def test_music_list_returns_stored_songs() -> None:
    watch = FakeWatch()
    session = make_session(watch)
    await pair(session, watch)
    songs = await session.music_list()
    assert [(song.index, song.title, song.artist, song.file_name) for song in songs] == [
        (0, "晨跑", "测试歌手", "run.mp3"),
        (1, "夜曲", "测试歌手", "night.flac"),
    ]


@pytest.mark.asyncio
async def test_music_list_pages_one_song_at_a_time() -> None:
    class PagedWatch(FakeWatch):
        def _music_list(self, tlv) -> None:  # noqa: ANN001
            assert tlv is not None
            start = tlv.integer(0x01) or 0
            inner = TLV()
            for index, title, artist, name in self.songs:
                if index == start:
                    inner.put(
                        0x83,
                        TLV()
                        .put(0x04, bytes([index]))
                        .put(0x05, title)
                        .put(0x06, artist)
                        .put(0x07, name)
                        .serialize(),
                    )
            self.send(
                0x25,
                0x05,
                TLV().put(0x01, start.to_bytes(2, "big")).put(0x04, b"\xff\xff").put(0x82, inner.serialize()),
            )

    watch = PagedWatch()
    session = make_session(watch)
    await pair(session, watch)
    songs = await session.music_list()
    assert [song.title for song in songs] == ["晨跑", "夜曲"]


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_state() -> None:
    watch = FakeWatch()
    session = make_session(watch)
    with pytest.raises(SessionError):
        await session.upload_music(b"data", "song.mp3")


@pytest.mark.asyncio
async def test_upload_device_error_is_raised() -> None:
    watch = FakeWatch()
    session = make_session(watch)
    await pair(session, watch)

    # Make the watch reject the file info with "no space left"
    original = watch._upload_info

    def reject(tlv) -> None:
        watch.send(0x28, 0x02, TLV().put(0x7F, (140009).to_bytes(4, "big")))

    watch._upload_info = reject  # type: ignore[method-assign]
    with pytest.raises(SessionError) as excinfo:
        await session.upload_music(b"x" * 10, "song.mp3")
    assert "空间不足" in str(excinfo.value)
    watch._upload_info = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_cbc_pairing_and_upload() -> None:
    watch = FakeWatch(encrypt_method=0, support_type=3, unit_size=180, chunk_size=500)
    session = make_session(watch)
    await pair(session, watch)
    assert session.link is not None and session.link.uses_gcm is False

    payload = bytes((index * 3) % 256 for index in range(4000))
    await session.upload_music(payload, "cbc.mp3", title="CBC", artist="测试")
    await asyncio.sleep(0.05)
    assert bytes(watch.received) == payload
    assert watch.upload_done is True


@pytest.mark.asyncio
async def test_upload_with_unencrypted_chunks() -> None:
    watch = FakeWatch(unit_size=200, chunk_size=700, no_encrypt_chunks=True)
    session = make_session(watch)
    await pair(session, watch)

    payload = bytes(range(256)) * 20
    await session.upload_music(payload, "plain.mp3")
    await asyncio.sleep(0.05)
    assert bytes(watch.received) == payload
    assert watch.upload_done is True


def lite_watch(**kwargs) -> FakeWatch:
    """GT 2e-like peer: device type 2, auth version 1, PBKDF2 digest, AES-CBC."""
    return FakeWatch(
        auth_version=1, support_type=2, auth_algo=1, encrypt_method=0, **kwargs
    )


@pytest.mark.asyncio
async def test_hichain_lite_pairing_bonds_and_encrypts() -> None:
    watch = lite_watch()
    session = make_session(watch)
    tokens: list[bytes] = []
    await session.transport.connect()
    await session.start()
    token = await session.pair(token_saver=tokens.append)

    assert session.paired is True
    assert len(token) == 16
    assert session.session_key == token == watch.session_key
    assert tokens == [token]
    # The link is encrypted after bonding, so a normal request must work.
    assert await session.battery() == 0x5A


@pytest.mark.asyncio
async def test_hichain_lite_reconnect_reuses_the_saved_key() -> None:
    watch = lite_watch()
    session = make_session(watch)
    await session.transport.connect()
    await session.start()
    token = await session.pair()

    watch.session_key = None
    session2 = make_session(watch)
    await session2.transport.connect()
    await session2.start()
    await session2.pair(saved_token=token)

    assert session2.paired is True
    assert session2.session_key == token
    assert await session2.battery() == 0x5A


@pytest.mark.asyncio
async def test_hichain_lite_upload_round_trip() -> None:
    watch = lite_watch()
    session = make_session(watch)
    await session.transport.connect()
    await session.start()
    await session.pair()
    data = b"lite" * 100
    await session.upload_file(data, "lite.mp3", C.FILE_TYPE_MUSIC)
    assert bytes(watch.received) == data


class _RecordingTransport:
    """Transport that records writes and how many were in flight at once."""

    def __init__(self) -> None:
        self.max_write_size = 244
        self.writes: list[bytes] = []
        self.in_flight = 0
        self.peak = 0
        self.fail_at: int | None = None

    async def write(self, data: bytes) -> None:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.001)
            self.writes.append(data)
            if self.fail_at is not None and len(self.writes) == self.fail_at:
                raise RuntimeError("write failed")
        finally:
            self.in_flight -= 1


@pytest.mark.asyncio
async def test_write_all_pipelines_and_keeps_order() -> None:
    transport = _RecordingTransport()
    session = HuaweiSession(transport, device_id=DEVICE_ID)
    frames = [bytes([index]) for index in range(20)]
    await session._write_all(frames)

    assert transport.writes == frames
    assert transport.peak > 1, "writes should be pipelined"
    assert transport.peak <= session.write_window


@pytest.mark.asyncio
async def test_write_all_falls_back_to_serial_writes() -> None:
    transport = _RecordingTransport()
    transport.fail_at = 2
    session = HuaweiSession(transport, device_id=DEVICE_ID)
    await session._write_all([b"a", b"b", b"c", b"d"])

    # Everything before the failing frame reached the watch; the rest is
    # retried serially, so the tail is always the complete frame sequence.
    assert transport.writes[-3:] == [b"b", b"c", b"d"]
    assert session.write_window == 1


@pytest.mark.asyncio
async def test_report_speed_emits_an_event() -> None:
    events: list[tuple[str, dict]] = []
    watch = FakeWatch()
    session = HuaweiSession(
        MockTransport(watch),
        device_id=DEVICE_ID,
        on_event=lambda name, data: events.append((name, data)),
    )
    loop = asyncio.get_running_loop()
    state = UploadState(
        data=b"x" * 8192,
        digest=b"",
        name="speed.mp3",
        file_type=C.FILE_TYPE_MUSIC,
        progress=None,
        future=loop.create_future(),
    )
    state.last_report = loop.time() - 2.0
    state.sent = 2048

    session._report_speed(state)

    assert events and events[-1][0] == "speed"
    assert events[-1][1]["kbps"] == pytest.approx(1.0, abs=0.1)
    assert state.reported_bytes == 2048


@pytest.mark.asyncio
async def test_cancel_upload_stops_the_transfer() -> None:
    watch = lite_watch()
    session = make_session(watch)
    await session.transport.connect()
    await session.start()
    await session.pair()

    task = asyncio.ensure_future(
        session.upload_file(b"cancel-me" * 20000, "big.mp3", C.FILE_TYPE_MUSIC)
    )
    for _ in range(1000):
        if session._upload is not None and session._upload.sent > 0:
            break
        await asyncio.sleep(0)
    assert session._upload is not None

    session.cancel_upload()

    with pytest.raises(UploadCancelled):
        await task
    assert session._upload is None
    # Late chunk requests from the watch are ignored, not answered.
    session._handle_upload(C.UPLOAD_CMD_CHUNK_PARAMS, TLV().put(0x02, b"\x00\x00\x00\x00").put(0x03, b"\x00\x10"))
    assert session._upload is None


@pytest.mark.asyncio
async def test_cancel_without_upload_is_a_noop() -> None:
    watch = FakeWatch()
    session = make_session(watch)
    session.cancel_upload()  # must not raise
