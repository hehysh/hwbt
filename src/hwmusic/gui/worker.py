"""Background asyncio worker so BLE work never blocks the Qt event loop."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine
from typing import Any

from PySide6.QtCore import QThread, Signal

from ..app import MusicApp
from ..store import Settings


class AsyncWorker(QThread):
    log = Signal(str, str)          # level, message
    event = Signal(str, object)     # name, payload
    failed = Signal(str)            # unhandled background error

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self.app: MusicApp | None = None

    # -- lifecycle ------------------------------------------------------
    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self.app = MusicApp(
            self.settings,
            log=lambda level, message: self.log.emit(level, message),
            on_event=lambda name, payload: self.event.emit(name, payload),
        )
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            finally:
                self._loop.close()

    def wait_ready(self, timeout: float = 5.0) -> None:
        self._ready.wait(timeout)

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        if self._loop is None:
            raise RuntimeError("worker is not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def cancel_upload(self) -> None:
        """Ask the asyncio loop to stop the running upload (thread-safe)."""
        if self._loop is None or self.app is None:
            return
        self._loop.call_soon_threadsafe(self.app.cancel_upload)

    def stop(self) -> None:
        if self._loop is None:
            return

        async def _shutdown() -> None:
            if self.app is not None:
                await self.app.disconnect()

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), self._loop).result(timeout=5)
        except Exception as exc:  # noqa: BLE001 - shutting down anyway
            self.log.emit("debug", f"关闭连接时出错: {exc}")
        self._loop.call_soon_threadsafe(self._loop.stop)
