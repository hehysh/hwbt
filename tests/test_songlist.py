from __future__ import annotations

import datetime as dt

from hwmusic.protocol import MusicEntry
from hwmusic.songlist import render_song_list, song_label

STAMP = dt.datetime(2026, 10, 31, 9, 5)


def entry(index: int, title: str, artist: str, file_name: str) -> MusicEntry:
    return MusicEntry(index=index, title=title, artist=artist, file_name=file_name)


def test_song_label_joins_title_and_artist() -> None:
    assert song_label(entry(1, "夜曲", "周杰伦", "0001.mp3")) == "夜曲 - 周杰伦"


def test_song_label_falls_back_to_the_file_name() -> None:
    assert song_label(entry(1, "", "", "0001.mp3")) == "0001.mp3"
    assert song_label(entry(1, "", "   ", "")) == "（未命名）"


def test_song_label_without_artist_is_just_the_title() -> None:
    assert song_label(entry(1, "Intro", "", "0001.mp3")) == "Intro"


def test_render_song_list_writes_a_header_and_numbered_lines() -> None:
    songs = [
        entry(0, "夜曲", "周杰伦", "0001.mp3"),
        entry(1, "晴天", "", "0002.mp3"),
    ]

    text = render_song_list(songs, device="HUAWEI WATCH GT 2e", stamp=STAMP)
    lines = text.splitlines()

    assert lines[0] == "华为手表歌曲列表"
    assert lines[1] == "导出时间：2026-10-31 09:05"
    assert lines[2] == "设备：HUAWEI WATCH GT 2e"
    assert lines[3] == "共 2 首"
    assert lines[4] == ""
    assert lines[5] == "01. 夜曲 - 周杰伦"
    assert lines[6] == "02. 晴天"
    assert text.endswith("\n")


def test_render_song_list_handles_an_empty_watch() -> None:
    text = render_song_list([], stamp=STAMP)

    assert "共 0 首" in text
    assert text.splitlines()[-1] == ""


def test_render_song_list_omits_the_device_when_unknown() -> None:
    text = render_song_list([entry(0, "A", "B", "a.mp3")], stamp=STAMP)

    assert "设备：" not in text


def test_numbers_beyond_nine_stay_aligned() -> None:
    songs = [entry(index, f"Song {index}", "X", f"{index}.mp3") for index in range(12)]

    text = render_song_list(songs, stamp=STAMP)

    assert "09. Song 8 - X" in text
    assert "10. Song 9 - X" in text
    assert "12. Song 11 - X" in text
