"""Session state machine: pairing, encrypted requests and file upload."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from . import constants as C
from . import protocol as P
from .crypto import (
    CryptoError,
    aes_cbc_decrypt,
    aes_cbc_encrypt,
    aes_gcm_open,
    aes_gcm_seal,
    digest_lite,
    hmac_sha256,
    initialization_vector,
    random_bytes,
    sha256,
)
from .packet import FrameError, Packet, PacketParser, build_frames, hexdump
from .tlv import TLV
from .transport import SliceTooLarge

LOG = logging.getLogger("hwmusic.session")

LogFn = Callable[[str, str], None]  # (level, message)
EventFn = Callable[[str, dict], None]
ProgressFn = Callable[[float], None]


class Transport(Protocol):
    """Minimal BLE interface the session needs."""

    max_write_size: int

    async def connect(self, address: str) -> None: ...
    async def start_notify(self, callback: Callable[[bytes], None]) -> None: ...
    async def write(self, data: bytes) -> None: ...
    async def disconnect(self) -> None: ...
    async def stop_notify(self) -> None: ...


class SessionError(Exception):
    """Any protocol-level failure with a user-facing message."""


class NotPairedError(SessionError):
    pass


class WatchError(SessionError):
    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(P.describe_upload_error(code))


class UploadCancelled(SessionError):
    """The user stopped the upload before the watch finished receiving it."""


@dataclass(slots=True)
class UploadState:
    data: bytes
    digest: bytes
    name: str
    file_type: int
    progress: ProgressFn | None
    future: asyncio.Future
    file_id: int = 0
    unit_size: int = 0
    encrypt: bool = True
    last_activity: float = 0.0
    sent: int = 0
    last_report: float = 0.0
    reported_bytes: int = 0
    chunks: int = 0


class HuaweiSession:
    def __init__(
        self,
        transport: Transport,
        *,
        device_id: bytes,
        log: LogFn | None = None,
        on_event: EventFn | None = None,
        debug: bool = False,
    ) -> None:
        self.transport = transport
        self.device_id = device_id
        self._log = log or (lambda level, message: None)
        self._on_event = on_event or (lambda name, data: None)
        self.debug = debug

        self.parser = PacketParser()
        self.link: P.LinkInfo | None = None
        self.session_key: bytes | None = None
        self._encrypt_counter = 0
        self.paired = False
        self.watch_name: str | None = None

        self._waiters: dict[tuple[int, int], asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        # How many GATT writes to keep in flight. Windows serialises GATT
        # operations anyway, but not awaiting each completion lets the stack
        # pack several write commands into one connection event, which is
        # where most of the upload time goes.
        self.write_window = 16
        self._upload: UploadState | None = None
        self._chunk_task: asyncio.Task | None = None
        self._music_metadata: dict[str, tuple[str, str]] = {}
        self._closed = False
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Subscribe to watch notifications (call right after connecting)."""
        await self.transport.start_notify(self._on_notify)

    def _on_notify(self, data: bytes) -> None:
        """Transport callback (may run in another thread); schedule handling."""
        try:
            packets = self.parser.feed(data)
        except FrameError as exc:
            self._log("error", f"收到损坏的数据帧: {exc}")
            return
        for packet in packets:
            try:
                packet = self._decrypt(packet)
            except CryptoError as exc:
                self._log("error", f"解密失败: {exc}")
                continue
            if self.debug:
                self._log(
                    "debug",
                    f"← 0x{packet.service:02X}/0x{packet.command:02X} {len(packet.body)}B "
                    f"{repr(packet.tlv) if packet.tlv else hexdump(packet.body)}",
                )
            self._dispatch(packet)

    def _decrypt(self, packet: Packet) -> Packet:
        if not packet.is_encrypted or self.session_key is None or self.link is None:
            return packet
        tlv = packet.tlv
        assert tlv is not None
        iv = tlv.get(0x7D)
        cipher = tlv.get(0x7E)
        if iv is None or cipher is None:
            raise CryptoError("加密包缺少 IV 或密文")
        plain = (
            aes_gcm_open(cipher, self.session_key, iv)
            if self.link.uses_gcm
            else aes_cbc_decrypt(cipher, self.session_key, iv)
        )
        return Packet.from_body(packet.service, packet.command, plain)

    def _dispatch(self, packet: Packet) -> None:
        key = (packet.service, packet.command)
        waiter = self._waiters.get(key)
        if waiter is not None and not waiter.done():
            waiter.set_result(packet)
            return
        try:
            self._handle_unsolicited(packet)
        except Exception as exc:  # noqa: BLE001 - never break the notify loop
            self._log("error", f"处理手表主动请求失败 (0x{packet.service:02X}/0x{packet.command:02X}): {exc}")

    def _handle_unsolicited(self, packet: Packet) -> None:
        tlv = packet.tlv
        if tlv is None:
            return

        if packet.service == C.SERVICE_FILE_UPLOAD and self._upload is not None:
            self._handle_upload(packet.command, tlv)
            return

        if packet.service == C.SERVICE_DEVICE_CONFIG and packet.command == C.CMD_PHONE_INFO:
            tags = P.phone_info_requested_tags(tlv)
            if tags:
                self.send(C.SERVICE_DEVICE_CONFIG, C.CMD_PHONE_INFO, P.phone_info_answer(tags))
                self._log("info", "手表询问手机信息，已按 Android 14 回复")
            return

        if packet.service == C.SERVICE_MUSIC:
            self._handle_music(packet.command, tlv)

    def _handle_music(self, command: int, tlv: TLV) -> None:
        if command == C.MUSIC_CMD_STATUS:
            if (code := tlv.integer(0x7F)) is not None and code != C.RESULT_SUCCESS:
                return
            self.send(C.SERVICE_MUSIC, C.MUSIC_CMD_STATUS, P.music_control_ack())
            self._on_event("music_screen_opened", {})
        elif command == C.MUSIC_CMD_UPLOAD_INFO:
            index = tlv.integer(0x01) or 0
            name = tlv.text(0x02) or ""
            title, artist = self._music_metadata.get(name, (name.rsplit(".", 1)[0], ""))
            self.send(
                C.SERVICE_MUSIC,
                C.MUSIC_CMD_UPLOAD_INFO,
                P.music_upload_info_reply(index, title, artist),
            )
            self._log("info", f"手表登记歌曲信息: #{index} {name} → {title} / {artist or '未知歌手'}")
        elif command == C.MUSIC_CMD_CONTROL:
            self.send(C.SERVICE_MUSIC, C.MUSIC_CMD_CONTROL, P.music_control_ack())
            self._on_event("music_control", {"button": tlv.get(0x01), "volume": tlv.integer(0x02)})

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------
    def _slice_size(self) -> int:
        link_size = self.link.slice_size if self.link else C.DEFAULT_SLICE_SIZE
        mtu_size = max(self.transport.max_write_size, C.MIN_SLICE_SIZE)
        return max(C.MIN_SLICE_SIZE, min(link_size or C.DEFAULT_SLICE_SIZE, mtu_size))

    def _encrypt_body(self, body: bytes) -> bytes:
        if self.session_key is None or self.link is None:
            raise NotPairedError("尚未与手表完成配对")
        if self.link.uses_gcm:
            iv = random_bytes(16)
        else:
            # CBC links expect the trailing counter to advance (Gadgetbridge
            # does the same); the watch uses it to order our packets.
            iv, self._encrypt_counter = initialization_vector(self._encrypt_counter)
        cipher = (
            aes_gcm_seal(body, self.session_key, iv)
            if self.link.uses_gcm
            else aes_cbc_encrypt(body, self.session_key, iv)
        )
        return TLV().put(0x7C, b"\x01").put(0x7D, iv).put(0x7E, cipher).serialize()

    def _frames(
        self,
        service: int,
        command: int,
        tlv: TLV,
        encrypted: bool,
        sliced: bool,
        *,
        auto_slice: bool = True,
    ) -> list[bytes]:
        body = tlv.serialize()
        if encrypted:
            body = self._encrypt_body(body)
        slice_size = self._slice_size()
        if auto_slice and len(body) + 8 > slice_size:
            sliced = True
        return build_frames(service, command, body, slice_size=slice_size, sliced=sliced)

    def send(self, service: int, command: int, tlv: TLV, *, encrypted: bool = True) -> None:
        """Fire-and-forget write (acknowledgements)."""
        try:
            frames = self._frames(service, command, tlv, encrypted, sliced=False)
        except Exception as exc:  # noqa: BLE001
            self._log("error", f"组包失败: {exc}")
            return
        task = asyncio.ensure_future(self._write_all(frames))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write_all(self, frames: list[bytes]) -> None:
        async with self._write_lock:
            window = max(1, self.write_window)
            if window == 1 or len(frames) == 1:
                for frame in frames:
                    await self.transport.write(frame)
                return
            start = 0
            while start < len(frames):
                batch = frames[start : start + window]
                results = await asyncio.gather(
                    *(self.transport.write(frame) for frame in batch),
                    return_exceptions=True,
                )
                errors = [item for item in results if isinstance(item, BaseException)]
                if not errors:
                    start += len(batch)
                    continue
                # Some Windows stacks accept only one outstanding GATT
                # operation. Fall back to plain serial writes, resuming at the
                # first frame that failed: everything before it definitely
                # reached the watch, and a repeat of a later unit carries the
                # same absolute offset, so it lands on the same bytes.
                first_error = next(
                    index for index, item in enumerate(results) if isinstance(item, BaseException)
                )
                self.write_window = 1
                self._log("warn", f"并发写入不受支持（{errors[0]}），改为逐帧发送")
                for frame in frames[start + first_error :]:
                    await self.transport.write(frame)
                return

    async def request(
        self,
        service: int,
        command: int,
        tlv: TLV,
        *,
        label: str = "",
        encrypted: bool = True,
        sliced: bool = False,
        timeout: float = 10.0,
    ) -> Packet:
        key = (service, command)
        if key in self._waiters:
            raise SessionError(f"上一个 {label or key} 请求还没有结束")
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._waiters[key] = future
        try:
            frames = self._frames(service, command, tlv, encrypted, sliced)
            if self.debug:
                self._log(
                    "debug",
                    f"→ 0x{service:02X}/0x{command:02X} {sum(len(f) for f in frames)}B "
                    f"({len(frames)} 帧){' 加密' if encrypted else ''} {hexdump(tlv.serialize()) if tlv.serialize() else ''}",
                )
            try:
                await self._write_all(frames)
            except SliceTooLarge:
                # The link could not carry our frames; the transport already shrank
                # max_write_size, so rebuild once with the smaller slice size.
                frames = self._frames(service, command, tlv, encrypted, sliced)
                await self._write_all(frames)
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise SessionError(f"{label or '请求'}超时：手表没有响应") from exc
        finally:
            self._waiters.pop(key, None)

    # ------------------------------------------------------------------
    # pairing
    # ------------------------------------------------------------------
    async def pair(self, *, saved_token: bytes | None = None, token_saver: Callable[[bytes], None] | None = None) -> bytes:
        """Run LinkParams → SecurityNegotiation → pairing (HiChain or Lite).

        Returns the permanent auth token (saved by the caller).
        """
        self.paired = False
        self.session_key = None
        self._encrypt_counter = 0

        self._log("info", "读取链路参数…")
        response = await self.request(
            C.SERVICE_DEVICE_CONFIG, C.CMD_LINK_PARAMS, P.link_params_request(), label="链路参数", encrypted=False
        )
        if response.tlv is None:
            raise SessionError("链路参数响应为空")
        link = P.LinkInfo.from_tlv(response.tlv)
        self.link = link
        self._log("info", f"链路: {link.describe()}")

        if link.device_support_type not in (1, 2, 3, 4):
            raise SessionError(f"不支持的手表类型 {link.device_support_type}（需要 HiChain 配对）")

        self._log("info", "安全协商…")
        response = await self.request(
            C.SERVICE_DEVICE_CONFIG,
            C.CMD_SECURITY_NEGOTIATION,
            P.security_negotiation_request(
                auth_mode=0x04 if link.device_support_type == 4 else 0x02,
                device_id=self.device_id,
                phone_model=C.PHONE_MODEL,
                encrypt_method=link.encrypt_method,
            ),
            label="安全协商",
            encrypted=False,
        )
        if response.tlv is None:
            raise SessionError("安全协商响应为空")
        auth_type = P.parse_auth_type(response.tlv)
        udid = response.tlv.get(0x05)
        if udid and len(udid) == 64:
            self._on_event("udid", {"udid": udid.decode("ascii", "replace")})
        self._log("info", f"配对类型: 0x{auth_type:X}")
        if P.is_hichain(auth_type):
            return await self._pair_hichain(link, saved_token, token_saver)
        if P.is_hichain_lite(auth_type):
            return await self._pair_lite(link, saved_token, token_saver)
        raise SessionError(
            f"该手表使用不支持的配对方式 0x{auth_type:X}（支持 HiChain 与 HiChain Lite）"
        )

    async def _pair_hichain(
        self, link: P.LinkInfo, saved_token: bytes | None, token_saver: Callable[[bytes], None] | None
    ) -> bytes:
        if saved_token is None:
            self._log("info", "请求 PIN 码（手表上如弹出配对提示请点确认）…")
            pin_response = await self.request(
                C.SERVICE_DEVICE_CONFIG,
                C.CMD_PIN_CODE,
                P.pincode_request(),
                label="PIN 码",
                encrypted=False,
                timeout=45.0,
            )
            if pin_response.tlv is None:
                raise SessionError("PIN 响应为空")
            pin = P.decrypt_pincode(pin_response.tlv, link)
            self._log("info", f"PIN 已解密（{len(pin)} 字节）")
            key = sha256(pin.hex().upper().encode("ascii"))
            token = await self._hichain(operation=1, key=key, timeout=90.0)
            if token_saver and len(token) == 32:
                token_saver(token)
            self._log("ok", "已获得永久配对密钥并保存")
        else:
            token = saved_token
            self._log("info", "已有保存的配对密钥，跳过 PIN")

        self._log("info", "HiChain 绑定，协商会话密钥…")
        session_key = await self._hichain(operation=2, key=token, timeout=60.0)
        self.session_key = session_key
        self.paired = True
        self._log("ok", "配对完成，连接已加密")
        return token

    async def _pair_lite(
        self, link: P.LinkInfo, saved_token: bytes | None, token_saver: Callable[[bytes], None] | None
    ) -> bytes:
        """HiChain Lite: PIN challenge, then bond with a locally generated key.

        Gadgetbridge calls this "HiChain Lite"; it is what the GT 2e and other
        device support types 1-3 speak instead of the ISO HiChain handshake.
        """
        secret_key = saved_token if saved_token and len(saved_token) == 16 else random_bytes(16)
        key = secret_key
        if link.auth_version != 0x02:
            self._log("info", "HiChain Lite：读取 PIN 码（手表上如弹出配对提示请点确认）…")
            pin_response = await self.request(
                C.SERVICE_DEVICE_CONFIG,
                C.CMD_PIN_CODE,
                P.pincode_request(),
                label="PIN 码",
                encrypted=False,
                timeout=45.0,
            )
            if pin_response.tlv is None:
                raise SessionError("PIN 响应为空")
            key = P.decrypt_pincode(pin_response.tlv, link)
            self._log("info", f"PIN 已解密（{len(key)} 字节）")

        client_nonce = random_bytes(16)
        double_nonce = link.server_nonce + client_nonce
        challenge = digest_lite(
            b"\x01\x00", key, double_nonce, auth_version=link.auth_version, auth_algo=link.auth_algo
        )
        first_key = challenge[32:48]

        self._log("info", "HiChain Lite：校验手表身份…")
        response = await self.request(
            C.SERVICE_DEVICE_CONFIG,
            C.CMD_AUTH,
            P.auth_request(
                challenge[:32], P.auth_nonce(link.auth_version, client_nonce), link.auth_algo
            ),
            label="认证",
            encrypted=False,
            timeout=45.0,
        )
        if response.tlv is None:
            raise SessionError("认证响应为空")
        expected = digest_lite(
            b"\x01\x10", key, double_nonce, auth_version=link.auth_version, auth_algo=link.auth_algo
        )[:32]
        actual = response.tlv.get(0x01) or b""
        if actual != expected:
            raise CryptoError("HiChain Lite 校验失败：PIN 不匹配或手表拒绝了本次连接")

        serial, mac = self.identity()
        response = await self.request(
            C.SERVICE_DEVICE_CONFIG,
            C.CMD_BOND_PARAMS,
            P.bond_params_request(serial, mac),
            label="绑定参数",
            encrypted=False,
        )
        status, counter = P.parse_bond_params(response.tlv) if response.tlv else (0, 0)
        if status not in (0, 1):
            self._log("warn", f"绑定参数返回状态 {status}")

        iv, counter = initialization_vector(counter)
        wrapped = (
            aes_gcm_seal(secret_key, first_key, iv)
            if link.encrypt_method == 1
            else aes_cbc_encrypt(secret_key, first_key, iv)
        )
        self._log("info", "HiChain Lite：提交配对密钥…")
        try:
            await self.request(
                C.SERVICE_DEVICE_CONFIG,
                C.CMD_BOND,
                P.bond_request(serial, wrapped, iv),
                label="绑定",
                encrypted=False,
                timeout=20.0,
            )
        except SessionError as exc:
            if "超时" not in str(exc):
                raise
            self._log("warn", "手表没有确认绑定，继续尝试加密通信")

        self.session_key = secret_key
        self._encrypt_counter = counter
        if token_saver:
            token_saver(secret_key)
        self.paired = True
        self._log("ok", "配对完成，连接已加密")
        return secret_key

    def identity(self) -> tuple[bytes, bytes]:
        """A stable serial + MAC pair for this installation.

        Gadgetbridge sends the phone's Bluetooth MAC here; we derive one from
        the persisted device id so the watch always sees the same peer.
        """
        raw = self.device_id.decode("ascii", "ignore")
        hexid = (raw + "0" * 12)[:12].upper()
        mac = ":".join(hexid[i : i + 2] for i in range(0, 12, 2))
        return hexid[6:12].encode("ascii"), mac.encode("ascii")

    async def _hichain(self, *, operation: int, key: bytes, timeout: float) -> bytes:
        message = P.HiChainMessage(
            operation=operation,
            request_id=int(time.time() * 1000),
            self_auth_id=self.device_id,
        )
        name = "HiChain 认证" if operation == 1 else "HiChain 绑定"
        step = await self._hichain_step(message.step_one(), f"{name} 第 1 步", timeout)
        if not isinstance(step, P.HiChainStep) or step.step != 1:
            raise SessionError(f"{name} 第 1 步响应异常")
        psk, iso_key, rand_peer = P.hichain_derive_keys(key, message, step.payload)
        auth_id_peer = step.payload["peerAuthId"].encode("ascii")
        self._log("info", f"{name}: 手表令牌校验通过")

        step = await self._hichain_step(
            message.step_two(P.hichain_self_token(psk, message, rand_peer, auth_id_peer)),
            f"{name} 第 2 步",
            20.0,
        )
        if not isinstance(step, P.HiChainStep) or step.step != 2:
            raise SessionError(f"{name} 第 2 步响应异常")
        mac = bytes.fromhex(step.payload.get("returnCodeMac", ""))
        if mac != hmac_sha256(psk, b"\x00\x00\x00\x00"):
            raise SessionError(f"{name}: returnCodeMac 校验失败")

        auth_token: bytes | None = None
        if operation == 1:
            nonce = random_bytes(12)
            challenge = random_bytes(16)
            enc_data = aes_gcm_seal(challenge, iso_key, nonce, b"hichain_iso_exchange")
            step = await self._hichain_step(
                message.step_three(nonce, enc_data), f"{name} 第 3 步", 20.0
            )
            if not isinstance(step, P.HiChainStep) or step.step != 3:
                raise SessionError(f"{name} 第 3 步响应异常")
            enc_token = bytes.fromhex(step.payload.get("encAuthToken", ""))
            token_nonce = bytes.fromhex(step.payload.get("nonce", ""))
            auth_token = aes_gcm_open(enc_token, iso_key, token_nonce, challenge)

        nonce = random_bytes(12)
        enc_result = aes_gcm_seal(b"\x00\x00\x00\x00", iso_key, nonce, b"hichain_iso_result")
        step = await self._hichain_step(message.step_four(nonce, enc_result), f"{name} 第 4 步", 20.0)
        if not isinstance(step, P.HiChainFinished):
            raise SessionError(f"{name} 未正常结束")

        if auth_token is not None:
            return auth_token
        return P.hichain_return_key(iso_key, message.rand_self, rand_peer)

    async def _hichain_step(self, tlv: TLV, label: str, timeout: float) -> P.HiChainStep | P.HiChainFinished:
        response = await self.request(
            C.SERVICE_DEVICE_CONFIG,
            C.CMD_HICHain,
            tlv,
            label=label,
            encrypted=False,
            sliced=True,
            timeout=timeout,
        )
        if response.tlv is None:
            raise SessionError(f"{label}: 响应为空")
        return P.parse_hichain_response(response.tlv)

    # ------------------------------------------------------------------
    # information
    # ------------------------------------------------------------------
    async def battery(self) -> int:
        response = await self.request(C.SERVICE_DEVICE_CONFIG, C.CMD_BATTERY, P.battery_request(), label="电量")
        value = response.tlv.integer(0x01) if response.tlv else None
        if value is None:
            raise SessionError("电量响应异常")
        return value

    async def product_info(self) -> dict[str, str]:
        response = await self.request(
            C.SERVICE_DEVICE_CONFIG, C.CMD_PRODUCT_INFO, P.product_info_request(), label="设备信息"
        )
        return P.describe_product_info(response.tlv) if response.tlv else {}

    async def supported_services(self) -> list[int]:
        response = await self.request(
            C.SERVICE_DEVICE_CONFIG, C.CMD_SUPPORTED_SERVICES, P.supported_services_request(), label="服务列表"
        )
        return P.parse_supported_services(response.tlv) if response.tlv else []

    async def supported_commands(self, asked: list[tuple[int, list[int]]]) -> dict[int, list[int]]:
        response = await self.request(
            C.SERVICE_DEVICE_CONFIG,
            C.CMD_SUPPORTED_COMMANDS,
            P.supported_commands_request(asked),
            label="命令列表",
        )
        return P.parse_supported_commands(response.tlv, asked) if response.tlv else {}

    async def music_storage(self) -> P.MusicStorage:
        response = await self.request(
            C.SERVICE_MUSIC, C.MUSIC_CMD_STORAGE, P.music_storage_request(), label="音乐存储"
        )
        if response.tlv is None:
            raise SessionError("音乐存储响应为空")
        return P.parse_music_storage(response.tlv)

    async def music_list(self, *, max_pages: int = 16) -> list[P.MusicEntry]:
        """Read the songs stored on the watch (0x25/0x05).

        Most GT watches return the whole list for one request; a few answer
        one frame at a time, in which case we keep asking until it stops
        producing new entries.
        """
        entries: list[P.MusicEntry] = []
        seen: set[tuple[int, str]] = set()
        start = 0
        end = 0xFFFF
        for _page in range(max_pages):
            response = await self.request(
                C.SERVICE_MUSIC,
                C.MUSIC_CMD_LIST,
                P.music_list_request(start, end),
                label="歌曲列表",
            )
            if response.tlv is None:
                break
            _start, _end, page = P.parse_music_list(response.tlv)
            fresh = 0
            for entry in page:
                key = (entry.index, entry.file_name or entry.title)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(entry)
                fresh += 1
            if fresh == 0 or len(page) != 1:
                break
            start += 1
        entries.sort(key=lambda item: item.index)
        return entries

    # ------------------------------------------------------------------
    # music upload
    # ------------------------------------------------------------------
    def register_metadata(self, file_name: str, title: str, artist: str) -> None:
        self._music_metadata[file_name] = (title, artist)

    async def upload_music(
        self,
        data: bytes,
        file_name: str,
        *,
        title: str = "",
        artist: str = "",
        progress: ProgressFn | None = None,
        timeout: float = 900.0,
    ) -> None:
        self.register_metadata(file_name, title or file_name.rsplit(".", 1)[0], artist)
        await self.upload_file(
            data, file_name, C.FILE_TYPE_MUSIC, progress=progress, timeout=timeout
        )

    async def upload_file(
        self,
        data: bytes,
        file_name: str,
        file_type: int,
        *,
        progress: ProgressFn | None = None,
        timeout: float = 900.0,
    ) -> None:
        if not self.paired:
            raise NotPairedError("尚未与手表完成配对")
        if self._upload is not None:
            raise SessionError("已有文件正在上传")
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._upload = UploadState(
            data=data,
            digest=sha256(data),
            name=file_name,
            file_type=file_type,
            progress=progress,
            future=future,
            last_activity=loop.time(),
        )
        watchdog = asyncio.ensure_future(self._upload_watchdog(self._upload))
        self._tasks.add(watchdog)
        watchdog.add_done_callback(self._tasks.discard)
        try:
            self._log("info", f"上传 {file_name}（{len(data) / 1024:.0f} KB）…")
            self._log(
                "info",
                f"发送通道: 单帧 {self._slice_size()}B"
                f"（{'带响应' if self.transport.use_response else '无响应'}写入，"
                f"窗口 {self.write_window} 帧，"
                f"{'已请求高吞吐连接参数' if getattr(self.transport, 'high_throughput', False) else '系统默认连接参数'}）",
            )
            response = await self.request(
                C.SERVICE_FILE_UPLOAD,
                C.UPLOAD_CMD_INFO,
                P.upload_info_request(file_name, len(data), file_type),
                label="文件信息",
                timeout=30.0,
            )
            code = response.result_code
            if code is not None and code != C.RESULT_SUCCESS:
                raise WatchError(code)
            await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise SessionError("上传超时：手表长时间没有请求数据") from exc
        finally:
            watchdog.cancel()
            self._chunk_task = None
            self._upload = None

    def cancel_upload(self) -> None:
        """Stop the current upload (the watch offers no abort command).

        The watch may keep waiting for the chunk it asked for until its own
        timeout, so the caller should tell the user to wait a few seconds
        before starting another transfer.
        """
        state = self._upload
        if state is None or state.future.done():
            return
        task = self._chunk_task
        self._chunk_task = None
        if task is not None and not task.done():
            task.cancel()
        # Drop the state first: any chunk request already in flight from the
        # watch is then ignored instead of being answered after the cancel.
        self._upload = None
        if not state.future.done():
            state.future.set_exception(UploadCancelled("已取消上传"))
        self._log("warn", f"已取消上传 {state.name}（已发送 {state.sent / 1024:.0f} KB）")

    async def _upload_watchdog(self, state: UploadState) -> None:
        """Fail the upload when the watch stops asking for data."""
        loop = asyncio.get_running_loop()
        while self._upload is state and not state.future.done():
            await asyncio.sleep(5.0)
            if self._upload is not state or state.future.done():
                return
            if loop.time() - state.last_activity > 60.0:
                self._fail_upload(SessionError("上传超时：手表 60 秒没有请求数据"))
                return

    def _handle_upload(self, command: int, tlv: TLV) -> None:
        state = self._upload
        if state is None:
            return
        loop = asyncio.get_running_loop()
        state.last_activity = loop.time()

        if command == C.UPLOAD_CMD_HASH:
            raw = tlv.get(0x01)
            state.file_id = raw[0] if raw else 0
            self._log("info", f"手表请求文件校验值（file id {state.file_id}）")
            self.send(
                C.SERVICE_FILE_UPLOAD,
                C.UPLOAD_CMD_HASH,
                P.upload_hash_reply(state.file_id, state.digest),
            )
        elif command == C.UPLOAD_CMD_CONSULT:
            params = P.UploadParams.from_tlv(tlv)
            state.unit_size = params.unit_size
            state.encrypt = params.no_encrypt != 1
            if state.unit_size <= 0:
                self._fail_upload(SessionError("手表返回的传输单元大小为 0"))
                return
            self._log(
                "info",
                f"传输参数: 单元 {state.unit_size}B，"
                f"{'不额外加密' if not state.encrypt else '分块加密'}，"
                f"单帧上限 {self._slice_size()}B",
            )
            self._log(
                "debug",
                f"手表传输参数: 协议 {params.protocol_version or '?'}, "
                f"wait {params.app_wait_time}, 单次上限 {params.max_apply_data_size}B, "
                f"interval {params.interval}, 单帧 {self.transport.max_write_size}B",
            )
            self.send(
                C.SERVICE_FILE_UPLOAD,
                C.UPLOAD_CMD_CONSULT,
                P.upload_consult_ack(state.file_id, not state.encrypt),
            )
        elif command == C.UPLOAD_CMD_CHUNK_PARAMS:
            offset = tlv.integer(0x02) or 0
            size = tlv.integer(0x03) or 0
            if size <= 0 or state.unit_size <= 0 or offset > len(state.data):
                self._fail_upload(SessionError(f"手表请求了无效的数据段 offset={offset} size={size}"))
                return
            if self.debug:
                self._log("debug", f"手表请求数据段: offset={offset} size={size}")
            chunk = state.data[offset : offset + size]
            task = asyncio.ensure_future(self._send_chunk(state, chunk, offset))
            self._chunk_task = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif command == C.UPLOAD_CMD_RESULT:
            self.send(
                C.SERVICE_FILE_UPLOAD,
                C.UPLOAD_CMD_RESULT,
                P.upload_result_reply(state.file_id),
            )
            if state.progress:
                state.progress(1.0)
            self._log("ok", f"{state.name} 上传完成")
            self._finish_upload(None)
        elif command == C.UPLOAD_CMD_DEVICE_ERROR:
            code = tlv.integer(0x7F) or -1
            self._fail_upload(WatchError(code))

    async def _send_chunk(self, state: UploadState, chunk: bytes, offset: int) -> None:
        try:
            await self._write_chunk_frames(state, chunk, offset)
        except SliceTooLarge:
            try:
                await self._write_chunk_frames(state, chunk, offset)
            except Exception as exc:  # noqa: BLE001
                self._fail_upload(SessionError(f"发送数据块失败: {exc}"))
                return
        except Exception as exc:  # noqa: BLE001
            self._fail_upload(SessionError(f"发送数据块失败: {exc}"))
            return
        state.sent = offset + len(chunk)
        if state.progress and state.data:
            state.progress(min(1.0, state.sent / len(state.data)))
        self._report_speed(state)

    def _report_speed(self, state: UploadState) -> None:
        """Log the achieved throughput every couple of seconds."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        if state.last_report == 0.0:
            state.last_report = now
            state.reported_bytes = state.sent
            return
        elapsed = now - state.last_report
        if elapsed < 2.0:
            return
        delta = state.sent - state.reported_bytes
        kbps = delta / elapsed / 1024
        state.last_report = now
        state.reported_bytes = state.sent
        self._log("info", f"上传速度 {kbps:.1f} KB/s")
        self._on_event("speed", {"kbps": round(kbps, 1)})

    async def _write_chunk_frames(self, state: UploadState, chunk: bytes, offset: int) -> None:
        payloads = P.chunk_payloads(chunk, offset, state.unit_size, state.file_id)
        frames: list[bytes] = []
        for payload in payloads:
            body = self._encrypt_body(payload) if state.encrypt else payload
            frames.extend(
                build_frames(
                    C.SERVICE_FILE_UPLOAD,
                    C.UPLOAD_CMD_CHUNK,
                    body,
                    slice_size=self._slice_size(),
                    sliced=len(body) + 8 > self._slice_size(),
                )
            )
        if self.debug and state.chunks < 3:
            # Shows how much of each ATT write is real payload: if the watch
            # asks for 200-byte units but the link allows 514, we can never
            # fill a packet and the round trips, not the MTU, are the limit.
            air = sum(len(frame) for frame in frames)
            self._log(
                "debug",
                f"数据块: {len(chunk)}B → {len(payloads)} 个单元 / {len(frames)} 帧，"
                f"平均 {air / max(1, len(frames)):.0f}B/帧"
                f"（单帧上限 {self._slice_size()}B）",
            )
        state.chunks += 1
        await self._write_all(frames)

    def _fail_upload(self, error: Exception) -> None:
        self._log("error", str(error))
        self._finish_upload(error)

    def _finish_upload(self, error: Exception | None) -> None:
        state = self._upload
        if state is None or state.future.done():
            return
        if error is not None:
            state.future.set_exception(error)
        else:
            state.future.set_result(None)

    # ------------------------------------------------------------------
    async def close(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        await self.transport.disconnect()
