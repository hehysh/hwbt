from __future__ import annotations

from pathlib import Path

from hwmusic.store import Settings


def test_settings_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    settings = Settings(device_id="AB" * 16)
    settings.set_token("AA:BB:CC:DD:EE:FF", bytes(range(32)))
    settings.last_address = "AA:BB:CC:DD:EE:FF"
    settings.last_name = "HUAWEI WATCH GT 4"
    settings.debug = True
    settings.save(path)

    loaded = Settings.load(path)
    assert loaded.device_id == "AB" * 16
    assert loaded.token_for("aa:bb:cc:dd:ee:ff") == bytes(range(32))
    assert loaded.last_address == "AA:BB:CC:DD:EE:FF"
    assert loaded.last_name == "HUAWEI WATCH GT 4"
    assert loaded.debug is True


def test_missing_or_broken_settings_are_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json", "utf-8")
    settings = Settings.load(path)
    assert len(settings.device_id) == 32
    assert settings.device_id_bytes == settings.device_id.encode("ascii")
    assert settings.token_for("nope") is None


def test_forget_pairing(tmp_path: Path) -> None:
    settings = Settings(device_id="CD" * 16)
    settings.set_token("11:22", b"\x01" * 32)
    settings.set_token("11:22", None)
    assert settings.token_for("11:22") is None
