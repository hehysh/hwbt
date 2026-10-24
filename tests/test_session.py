from __future__ import annotations

import asyncio

import pytest

from hwmusic import constants as C
from hwmusic.protocol import MusicStorage
from hwmusic.session import HuaweiSession, SessionError
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
