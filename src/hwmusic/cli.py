"""Command line interface: `hwmusic scan|info|upload`."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import __version__
from .app import MusicApp
from .session import SessionError
from .store import Settings
from .transport import scan

LEVEL_PREFIX = {"debug": "·", "info": "i", "ok": "✓", "warn": "!", "error": "✗"}


def _log(level: str, message: str) -> None:
    print(f"[{LEVEL_PREFIX.get(level, level)}] {message}", flush=True)


async def _pick_address(app: MusicApp, address: str | None, timeout: float) -> tuple[str, str]:
    if address:
        return address, ""
    if app.settings.last_address:
        return app.settings.last_address, app.settings.last_name
    _log("info", "未指定地址，正在扫描…")
    devices = await scan(timeout)
    if not devices:
        raise SessionError("没有扫描到华为手表，请确认蓝牙已开启且手表在附近")
    for device in devices:
        _log("info", f"发现: {device.label()}")
    return devices[0].address, devices[0].name


async def cmd_scan(args: argparse.Namespace) -> int:
    devices = await scan(args.timeout, show_all=args.all)
    if not devices:
        print("未发现设备")
        return 1
    for device in devices:
        print(f"{device.address}  {device.name}  RSSI={device.rssi}")
    return 0


async def _connect(args: argparse.Namespace) -> MusicApp:
    settings = Settings.load()
    if args.debug:
        settings.debug = True
    if getattr(args, "fast", False):
        settings.fast_mode = True
    app = MusicApp(settings, log=_log, on_event=lambda name, data: _log("debug", f"事件 {name}: {data}"))
    address, name = await _pick_address(app, args.address, args.timeout)
    _log("info", f"连接 {address}…")
    info = await app.connect(address, name, force_pair=getattr(args, "pair", False))
    _log(
        "ok",
        f"已连接: {info.name or name} 型号={info.model or '?'} 固件={info.firmware or '?'} "
        f"电量={info.battery if info.battery is not None else '?'}%",
    )
    if info.storage:
        _log("info", f"音乐存储: {info.storage.describe()}")
    if info.services is not None:
        _log("info", f"音乐服务: {'支持' if info.supports_music else '不支持'}")
    return app


async def cmd_info(args: argparse.Namespace) -> int:
    app = await _connect(args)
    try:
        return 0
    finally:
        await app.disconnect()


async def cmd_songs(args: argparse.Namespace) -> int:
    app = await _connect(args)
    try:
        songs = await app.music_list()
        if not songs:
            print("手表上没有歌曲（或该型号不返回歌曲列表）")
            return 0
        for song in songs:
            artist = song.artist or "未知歌手"
            print(f"{song.index:3d}  {song.title} — {artist}  [{song.file_name}]")
        print(f"共 {len(songs)} 首")
        return 0
    finally:
        await app.disconnect()


async def cmd_upload(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.files]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        _log("error", f"文件不存在: {', '.join(missing)}")
        return 2
    app = await _connect(args)
    try:
        def start(index: int, track) -> None:
            _log("info", f"[{index + 1}/{len(paths)}] {track.display}")

        last = -1

        def progress(index: int, fraction: float) -> None:
            nonlocal last
            percent = int(fraction * 100)
            if percent >= last + 10:
                last = percent
                print(f"    {percent}%", flush=True)

        results = await app.upload(paths, on_file_start=start, on_progress=progress)
        failed = [(track, error) for track, error in results if error]
        for track, error in results:
            print(f"{'✓' if not error else '✗'} {track.display}{(' — ' + error) if error else ''}")
        return 0 if not failed else 1
    finally:
        await app.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hwmusic",
        description="给华为 GT 系列手表上传音乐（无需华为运动健康）",
    )
    parser.add_argument("--version", action="version", version=f"hwmusic {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--address", "-a", help="手表 BLE 地址（默认扫描/上次使用的设备）")
        p.add_argument("--timeout", type=float, default=8.0, help="扫描超时秒数")
        p.add_argument("--debug", action="store_true", help="打印协议帧")
        p.add_argument("--fast", action="store_true", help="使用无响应写入（更快，可能丢包）")
        p.add_argument("--pair", action="store_true", help="忽略已保存的密钥，重新配对")

    p_scan = sub.add_parser("scan", help="扫描附近的手表")
    p_scan.add_argument("--timeout", type=float, default=8.0)
    p_scan.add_argument("--all", action="store_true", help="显示所有 BLE 设备")
    p_scan.set_defaults(func=cmd_scan)

    p_info = sub.add_parser("info", help="连接并显示设备信息")
    common(p_info)
    p_info.set_defaults(func=cmd_info)

    p_songs = sub.add_parser("songs", help="列出手表上的歌曲")
    common(p_songs)
    p_songs.set_defaults(func=cmd_songs)

    p_upload = sub.add_parser("upload", help="上传音乐文件")
    p_upload.add_argument("files", nargs="+", help="音频文件（mp3/m4a/aac/flac/wav/opus）")
    common(p_upload)
    p_upload.set_defaults(func=cmd_upload)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        _log("warn", "已取消")
        return 130
    except SessionError as exc:
        _log("error", str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
