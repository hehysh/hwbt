"""High-level controller used by both the CLI and the GUI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import constants as C
from .crypto import CryptoError
from .metadata import Track, is_supported, read_track
from .protocol import MusicStorage
from .session import HuaweiSession, SessionError, UploadCancelled
from .store import Settings
from .transport import BleTransport, ScanResult, scan

LogFn = Callable[[str, str], None]
EventFn = Callable[[str, dict], None]

_WRITE_HINT = (
    "手表拒绝了这次写入（ATT 0x03 Write Not Permitted）。程序已经在「带响应 / 无响应」"
    "两种写入方式之间自动切换重试，仍然失败，通常不是配对问题。请依次检查：\n"
    "1) 手机：华为运动健康 → 设备 → 断开连接；仍不行就「解除配对」。\n"
    "2) 手表：不要停在「连接新手机」的二维码界面，按上键回到表盘。\n"
    "3) 电脑：不要在 Windows 蓝牙设置里配对手表——GT 2e 会因此进入二维码模式并停止广播，"
    "本程序会自己完成华为的配对。若之前配过对，请在「设置 → 蓝牙和其他设备」里删除该手表，"
    "再重启手表重试。\n"
    "4) 勾选「调试日志」后重连并导出日志，里面会记录 FE01 特征的写入属性。\n"
    "原始错误："
)

_BOND_HINT = (
    "手表要求先完成蓝牙加密（系统级配对）。请依次确认：\n"
    "1) 手机：华为运动健康 → 设备 → 断开连接；仍不行就「解除配对」。\n"
    "2) 电脑：设置 → 蓝牙和其他设备 → 添加设备 → 蓝牙 → 选中手表完成配对"
    "（手表上如弹出提示请确认）。\n"
    "3) 回到本工具点「连接」。\n"
    "原始错误："
)


def describe_connection_error(exc: BaseException) -> str:
    """Turn cryptic WinRT/BlueZ errors into something actionable."""
    lowered = str(exc).lower()
    if "not permitted" in lowered or "0x03" in lowered:
        return _WRITE_HINT + str(exc)
    if "insufficient" in lowered or "0x80070005" in lowered:
        return _BOND_HINT + str(exc)
    if isinstance(exc, SessionError):
        return str(exc)
    return f"连接失败：{exc}"


@dataclass(slots=True)
class DeviceInfo:
    name: str = ""
    model: str = ""
    firmware: str = ""
    serial: str = ""
    battery: int | None = None
    storage: MusicStorage | None = None
    services: list[int] | None = None
    udid: str = ""

    @property
    def supports_music(self) -> bool:
        return self.services is None or C.SERVICE_MUSIC in self.services


class MusicApp:
    def __init__(self, settings: Settings, log: LogFn | None = None, on_event: EventFn | None = None) -> None:
        self.settings = settings
        self._log = log or (lambda level, message: None)
        self._on_event = on_event or (lambda name, data: None)
        self.transport: BleTransport | None = None
        self.session: HuaweiSession | None = None
        self.info = DeviceInfo()

    # ------------------------------------------------------------------
    async def _pair_session(self, address: str, *, force_pair: bool) -> None:
        if self.session is None:
            raise SessionError("尚未连接手表")
        token = None if force_pair else self.settings.token_for(address)
        try:
            await self.session.pair(saved_token=token, token_saver=lambda t: self._save_token(address, t))
        except CryptoError as exc:
            if token is None:
                raise SessionError(str(exc)) from exc
            self._log("error", f"已保存的配对密钥失效（{exc}），重新配对…")
            self._save_token(address, None)
            await self.session.pair(token_saver=lambda t: self._save_token(address, t))
        except SessionError as exc:
            if token is not None and "超时" in str(exc):
                self._log("error", "使用已保存密钥连接超时，尝试完整配对…")
                self._save_token(address, None)
                await self.session.pair(token_saver=lambda t: self._save_token(address, t))
            else:
                raise

    async def scan(self, timeout: float = 8.0, show_all: bool = False) -> list[ScanResult]:
        return await scan(timeout, show_all=show_all)

    async def connect(self, address: str, name: str = "", *, force_pair: bool = False) -> DeviceInfo:
        await self.disconnect()
        self.transport = BleTransport(address, name)
        self.transport.use_response = not self.settings.fast_mode
        self.transport.on_status = lambda message: self._log("info", message)
        try:
            await self.transport.connect()
        except Exception as exc:  # noqa: BLE001 - bleak raises many backend errors
            raise SessionError(
                f"无法连接手表：{exc}\n"
                "请检查：① 手机上的华为运动健康已断开/解除配对；"
                "② 手表没有停在「连接新手机」的二维码界面；"
                "③ Windows「设置 → 蓝牙和其他设备」里已成功配对这块手表。"
            ) from exc
        self.transport.on_disconnect(lambda: self._on_event("disconnected", {}))
        self.session = HuaweiSession(
            self.transport,
            device_id=self.settings.device_id_bytes,
            log=self._log,
            on_event=self._on_event,
            debug=self.settings.debug,
        )
        await self.session.start()

        try:
            await self._pair_session(address, force_pair=force_pair)
        except Exception as exc:  # noqa: BLE001 - surface backend errors with a hint
            raise SessionError(describe_connection_error(exc)) from exc

        self.settings.last_address = address
        self.settings.last_name = name
        self.settings.save()
        self._on_event("paired", {"address": address})

        info = DeviceInfo(name=name)
        try:
            info.battery = await self.session.battery()
        except SessionError:
            pass
        try:
            details = await self.session.product_info()
            info.model = details.get("model", "")
            info.firmware = details.get("firmware", "")
            info.serial = details.get("serial", "")
            info.name = details.get("name") or name
        except SessionError as exc:
            self._log("error", f"读取设备信息失败: {exc}")
        try:
            info.services = await self.session.supported_services()
        except SessionError:
            info.services = None
        try:
            info.storage = await self.session.music_storage()
        except SessionError as exc:
            self._log("info", f"读取音乐存储失败: {exc}")
        self.info = info
        self._on_event("info", {"info": info})
        return info

    def _save_token(self, address: str, token: bytes | None) -> None:
        self.settings.set_token(address, token)
        self.settings.save()

    async def refresh_storage(self) -> MusicStorage | None:
        if self.session is None or not self.session.paired:
            raise SessionError("尚未连接手表")
        self.info.storage = await self.session.music_storage()
        return self.info.storage

    async def music_list(self) -> list:
        if self.session is None or not self.session.paired:
            raise SessionError("尚未连接手表")
        return await self.session.music_list()

    async def upload(
        self,
        paths: list[Path],
        *,
        on_file_start: Callable[[int, Track], None] | None = None,
        on_progress: Callable[[int, float], None] | None = None,
    ) -> list[tuple[Track, str | None]]:
        """Upload tracks; returns ``[(track, error_or_None), ...]``."""
        if self.session is None or not self.session.paired:
            raise SessionError("尚未连接手表")
        if not self.info.supports_music:
            raise SessionError("该手表没有音乐服务（0x25），无法存歌")

        results: list[tuple[Track, str | None]] = []
        cancelled = False
        for index, raw_path in enumerate(paths):
            path = Path(raw_path)
            if not is_supported(path):
                results.append(
                    (read_track(path), f"不支持的格式（仅支持 {'/'.join(C.MUSIC_EXTENSIONS)}）")
                )
                continue
            track = read_track(path)
            if on_file_start:
                on_file_start(index, track)
            self._log("info", f"[{index + 1}/{len(paths)}] {track.display} ({track.size / 1024 / 1024:.1f} MB)")
            try:
                data = path.read_bytes()
                await self.session.upload_music(
                    data,
                    path.name,
                    title=track.title,
                    artist=track.artist,
                    progress=(lambda fraction, i=index: on_progress(i, fraction) if on_progress else None),
                )
                results.append((track, None))
            except UploadCancelled:
                cancelled = True
                results.append((track, "已取消上传"))
                for rest in paths[index + 1 :]:
                    results.append((read_track(Path(rest)), "已取消上传"))
                break
            except (SessionError, OSError) as exc:
                self._log("error", f"{track.display}: {exc}")
                results.append((track, str(exc)))
        if not cancelled:
            try:
                await self.refresh_storage()
            except SessionError as exc:
                self._log("debug", f"刷新容量失败: {exc}")
        return results

    def cancel_upload(self) -> None:
        """Stop the upload that is currently running, if any."""
        if self.session is not None:
            self.session.cancel_upload()

    async def disconnect(self) -> None:
        if self.session is not None:
            try:
                await self.session.close()
            except Exception as exc:  # noqa: BLE001 - closing must never fail
                self._log("debug", f"关闭会话时出错: {exc}")
        elif self.transport is not None:
            try:
                await self.transport.disconnect()
            except Exception as exc:  # noqa: BLE001
                self._log("debug", f"断开蓝牙时出错: {exc}")
        self.session = None
        self.transport = None

    async def forget_pairing(self, address: str) -> None:
        self._save_token(address, None)
