"""Audio metadata helpers (title/artist, duration, size checks)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import constants as C

try:  # optional: only used to read tags
    from mutagen import File as MutagenFile  # type: ignore
except Exception:  # noqa: BLE001 - mutagen is optional
    MutagenFile = None  # type: ignore[assignment]


@dataclass(slots=True)
class Track:
    path: Path
    title: str
    artist: str
    duration: float | None = None
    size: int = 0
    extension: str = ""

    @property
    def display(self) -> str:
        return f"{self.title} — {self.artist}" if self.artist else self.title

    @property
    def duration_text(self) -> str:
        if not self.duration:
            return "--:--"
        minutes, seconds = divmod(int(self.duration), 60)
        return f"{minutes}:{seconds:02d}"


def is_supported(path: Path) -> bool:
    return path.suffix.lower().lstrip(".") in C.MUSIC_EXTENSIONS


def read_track(path: Path) -> Track:
    path = Path(path)
    title = path.stem
    artist = ""
    duration: float | None = None
    if MutagenFile is not None:
        try:
            audio = MutagenFile(path, easy=True)
            if audio is not None:
                tags = audio.tags or {}
                if tags.get("title"):
                    title = str(tags["title"][0]).strip() or title
                if tags.get("artist"):
                    artist = str(tags["artist"][0]).strip()
                if getattr(audio, "info", None) is not None:
                    duration = float(audio.info.length)
        except Exception:  # noqa: BLE001 - broken tags must not break uploads
            duration = None
    return Track(
        path=path,
        title=title[:80],
        artist=artist[:60],
        duration=duration,
        size=path.stat().st_size,
        extension=path.suffix.lower().lstrip("."),
    )
