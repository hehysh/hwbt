"""Plain-text rendering of the watch's song list.

Kept separate from the GUI so the CLI and the tests can use it too.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable
from typing import Protocol


class _Song(Protocol):
    index: int
    title: str
    artist: str
    file_name: str


def song_label(song: _Song) -> str:
    """One readable line for a song: ``标题 - 歌手`` (or the file name)."""
    title = (song.title or "").strip() or (song.file_name or "").strip() or "（未命名）"
    artist = (song.artist or "").strip()
    return f"{title} - {artist}" if artist else title


def render_song_list(
    songs: Iterable[_Song], *, device: str = "", stamp: _dt.datetime | None = None
) -> str:
    """Render the song list as UTF-8 text, one song per line.

    The first lines are a small header so an exported file still makes sense
    months later; the numbered lines are what people actually copy out.
    """
    items = list(songs)
    when = (stamp or _dt.datetime.now()).strftime("%Y-%m-%d %H:%M")
    lines = ["华为手表歌曲列表", f"导出时间：{when}"]
    if device:
        lines.append(f"设备：{device}")
    lines.append(f"共 {len(items)} 首")
    lines.append("")
    for number, song in enumerate(items, start=1):
        lines.append(f"{number:02d}. {song_label(song)}")
    return "\n".join(lines) + "\n"
