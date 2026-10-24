"""Main window of the Huawei watch music uploader."""

from __future__ import annotations

import datetime as _dt
import html
import platform
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from .. import constants as C
from ..app import DeviceInfo
from ..metadata import Track, is_supported, read_track
from ..store import Settings, config_dir
from .worker import AsyncWorker

ACCENT = "#C7000B"

STYLE = f"""
QWidget {{ font-size: 13px; color: #1f2328; }}
QMainWindow, #root {{ background: #f5f6f8; }}
QFrame#card {{ background: white; border: 1px solid #e6e8eb; border-radius: 12px; }}
QLabel#title {{ font-size: 20px; font-weight: 600; }}
QLabel#subtitle {{ color: #6b7280; }}
QLabel#chip {{ background: #f1f3f5; border-radius: 10px; padding: 4px 10px; color: #374151; }}
QLabel#chipOk {{ background: #e8f7ee; border-radius: 10px; padding: 4px 10px; color: #146c2e; }}
QLabel#chipWarn {{ background: #fdecec; border-radius: 10px; padding: 4px 10px; color: #a11212; }}
QFrame#dropzone {{ background: #fbfbfc; border: 2px dashed #c9ced6; border-radius: 12px; }}
QFrame#dropzone[dragging="true"] {{ border-color: {ACCENT}; background: #fff5f5; }}
QPushButton {{ background: white; border: 1px solid #d5d9de; border-radius: 8px; padding: 7px 14px; }}
QPushButton:hover {{ background: #f6f7f8; }}
QPushButton:disabled {{ color: #a0a6ad; background: #f4f5f6; }}
QPushButton#primary {{ background: {ACCENT}; border: 1px solid {ACCENT}; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #a80009; }}
QPushButton#primary:disabled {{ background: #e3b6b9; border-color: #e3b6b9; }}
QComboBox {{ background: white; border: 1px solid #d5d9de; border-radius: 8px; padding: 6px 10px; min-width: 260px; }}
QTableWidget {{ background: white; border: none; gridline-color: #f0f1f3; }}
QHeaderView::section {{ background: #fafbfc; border: none; border-bottom: 1px solid #e6e8eb; padding: 8px; color: #6b7280; }}
QProgressBar {{ border: 1px solid #e6e8eb; border-radius: 6px; background: #f4f5f6; height: 16px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
QTextEdit {{ background: #0f172a; color: #e2e8f0; border-radius: 8px; font-family: Consolas, 'Cascadia Mono', monospace; font-size: 12px; }}
QGroupBox {{ border: none; margin-top: 6px; }}
"""


class DropZone(QFrame):
    files_dropped = Signal(list)
    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("dropzone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        title = QLabel("把音乐拖到这里，或点击选择文件")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("支持 " + " / ".join(ext.upper() for ext in C.MUSIC_EXTENSIONS))
        hint.setObjectName("subtitle")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addWidget(hint)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setProperty("dragging", True)
            self.style().unpolish(self)
            self.style().polish(self)

    def dragLeaveEvent(self, _event) -> None:
        self.setProperty("dragging", False)
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event: QDropEvent) -> None:
        self.setProperty("dragging", False)
        self.style().unpolish(self)
        self.style().polish(self)
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
        event.acceptProposedAction()

    def mousePressEvent(self, _event) -> None:
        self.clicked.emit()


class WatchSongsDialog(QDialog):
    """Read-only list of the songs stored on the watch."""

    def __init__(self, parent: QWidget | None, songs: list) -> None:
        super().__init__(parent)
        self.setWindowTitle("手表上的歌曲")
        self.resize(620, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        summary = f"共 {len(songs)} 首" if songs else "手表上没有歌曲，或该型号不返回歌曲列表"
        label = QLabel(summary)
        label.setObjectName("subtitle")
        layout.addWidget(label)

        table = QTableWidget(len(songs), 4)
        table.setHorizontalHeaderLabels(["#", "标题", "歌手", "文件名"])
        table.verticalHeader().setVisible(False)
        table.setShowGrid(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header = table.horizontalHeader()
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for column in range(3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for row, song in enumerate(songs):
            for column, value in enumerate(
                (str(song.index), song.title, song.artist or "—", song.file_name)
            ):
                table.setItem(row, column, QTableWidgetItem(value))
        layout.addWidget(table, 1)

        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)


class MainWindow(QMainWindow):
    file_start = Signal(int, object)
    file_progress = Signal(int, float)
    upload_done = Signal(object)
    scan_done = Signal(object)
    connect_done = Signal(object)
    disconnect_done = Signal()
    storage_ready = Signal(object)
    watch_songs_ready = Signal(object)
    op_failed = Signal(str)

    def __init__(self, worker: AsyncWorker, settings: Settings) -> None:
        super().__init__()
        self.worker = worker
        self.settings = settings
        self.tracks: list[Track] = []
        self.connected = False
        self.uploading = False
        self._force_pair = False
        self._log_path = config_dir() / "app.log"
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self._log_path.exists() and self._log_path.stat().st_size > 2 * 1024 * 1024:
                self._log_path.replace(self._log_path.with_suffix(".log.1"))
        except OSError:
            pass

        self.setWindowTitle(f"{C.APP_DISPLAY_NAME} {__version__}")
        self.resize(940, 780)
        self.setStyleSheet(STYLE)
        self._build_ui()
        self._connect_signals()
        self._restore_state()
        self.append_log("info", f"{C.APP_DISPLAY_NAME} {__version__} 启动")

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel(C.APP_DISPLAY_NAME)
        title.setObjectName("title")
        subtitle = QLabel("通过蓝牙直接给 GT 系列手表传歌 · 无需华为运动健康")
        subtitle.setObjectName("subtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch(1)
        self.chip_state = QLabel("未连接")
        self.chip_state.setObjectName("chip")
        header.addWidget(self.chip_state)
        outer.addLayout(header)

        outer.addWidget(self._build_device_card())
        outer.addWidget(self._build_info_card())
        outer.addWidget(self._build_files_card(), 1)
        outer.addWidget(self._build_log_card())

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        outer.addWidget(self.progress)

        self.statusBar().showMessage("就绪")

    def _build_device_card(self) -> QFrame:
        card, layout = self._card()
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("设备"))
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(300)
        row.addWidget(self.device_combo, 1)
        self.btn_scan = QPushButton("扫描")
        self.btn_connect = QPushButton("连接")
        self.btn_connect.setObjectName("primary")
        self.btn_pair = QPushButton("重新配对")
        row.addWidget(self.btn_scan)
        row.addWidget(self.btn_connect)
        row.addWidget(self.btn_pair)
        layout.addLayout(row)

        row2 = QHBoxLayout()
        self.fast_mode = QCheckBox("快速模式（无响应写入，可能更快但可能丢包）")
        self.debug_mode = QCheckBox("调试日志（打印协议帧）")
        row2.addWidget(self.fast_mode)
        row2.addWidget(self.debug_mode)
        row2.addStretch(1)
        layout.addLayout(row2)
        return card

    def _build_info_card(self) -> QFrame:
        card, layout = self._card()
        row = QHBoxLayout()
        row.setSpacing(8)
        self.chip_model = QLabel("型号 —")
        self.chip_firmware = QLabel("固件 —")
        self.chip_battery = QLabel("电量 —")
        self.chip_storage = QLabel("存储 —")
        for chip in (self.chip_model, self.chip_firmware, self.chip_battery, self.chip_storage):
            chip.setObjectName("chip")
            row.addWidget(chip)
        row.addStretch(1)
        layout.addLayout(row)
        return card

    def _build_files_card(self) -> QFrame:
        card, layout = self._card()
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("音乐列表"))
        toolbar.addStretch(1)
        self.btn_add = QPushButton("添加文件")
        self.btn_clear = QPushButton("清空")
        self.btn_upload = QPushButton("开始上传")
        self.btn_upload.setObjectName("primary")
        self.btn_cancel = QPushButton("取消上传")
        self.btn_cancel.setEnabled(False)
        self.btn_refresh = QPushButton("刷新容量")
        self.btn_songs = QPushButton("手表歌曲")
        for button in (
            self.btn_add,
            self.btn_clear,
            self.btn_refresh,
            self.btn_songs,
            self.btn_upload,
            self.btn_cancel,
        ):
            toolbar.addWidget(button)
        layout.addLayout(toolbar)

        self.drop = DropZone()
        layout.addWidget(self.drop)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["文件", "标题", "歌手", "时长", "大小", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 230)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in range(2, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(180)
        layout.addWidget(self.table, 1)
        return card

    def _build_log_card(self) -> QWidget:
        group = QGroupBox("日志")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(0, 0, 0, 0)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(130)
        self.log_view.setMaximumHeight(180)
        layout.addWidget(self.log_view)
        row = QHBoxLayout()
        self.btn_export = QPushButton("导出诊断日志")
        self.btn_clear_log = QPushButton("清空日志")
        row.addStretch(1)
        row.addWidget(self.btn_clear_log)
        row.addWidget(self.btn_export)
        layout.addLayout(row)
        return group

    @staticmethod
    def _card() -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        return card, layout

    # ------------------------------------------------------------------
    # signals
    # ------------------------------------------------------------------
    def _connect_signals(self) -> None:
        self.btn_scan.clicked.connect(self.scan_devices)
        self.btn_connect.clicked.connect(self.toggle_connection)
        self.btn_pair.clicked.connect(self.repair)
        self.btn_add.clicked.connect(self.pick_files)
        self.btn_clear.clicked.connect(self.clear_files)
        self.btn_upload.clicked.connect(self.start_upload)
        self.btn_cancel.clicked.connect(self.cancel_upload)
        self.btn_refresh.clicked.connect(self.refresh_storage)
        self.btn_songs.clicked.connect(self.fetch_watch_songs)
        self.btn_export.clicked.connect(self.export_log)
        self.btn_clear_log.clicked.connect(self.log_view.clear)
        self.drop.files_dropped.connect(self.add_files)
        self.drop.clicked.connect(self.pick_files)
        self.fast_mode.toggled.connect(self._fast_mode_changed)
        self.debug_mode.toggled.connect(self._debug_mode_changed)

        self.file_start.connect(self._on_file_start)
        self.file_progress.connect(self._on_file_progress)
        self.upload_done.connect(self._on_upload_done)
        self.scan_done.connect(self._on_scan_done)
        self.connect_done.connect(self._on_connect_done)
        self.storage_ready.connect(self._on_storage)
        self.watch_songs_ready.connect(self._on_watch_songs)
        self.disconnect_done.connect(self._on_disconnected)
        self.op_failed.connect(self._on_failed)
        self.worker.event.connect(self._on_worker_event)
        self.worker.log.connect(self.append_log)

    def _restore_state(self) -> None:
        self.fast_mode.setChecked(self.settings.fast_mode)
        self.debug_mode.setChecked(self.settings.debug)
        if self.settings.last_address:
            label = f"{self.settings.last_name or '上次的设备'}  ({self.settings.last_address})"
            self.device_combo.addItem(label, self.settings.last_address)
        self._update_buttons()

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def scan_devices(self) -> None:
        self.btn_scan.setEnabled(False)
        self.statusBar().showMessage("正在扫描附近的华为手表…")
        future = self.worker.submit(self.worker.app.scan(8.0))
        future.add_done_callback(self._deliver(self.scan_done, "扫描失败"))

    def _on_scan_done(self, devices: list) -> None:
        self.btn_scan.setEnabled(True)
        current = self.device_combo.currentData()
        self.device_combo.clear()
        if not devices:
            self.statusBar().showMessage("没有发现设备")
            self.append_log("warn", "没有扫描到华为手表：请确认蓝牙已开启、手表在附近且未连接手机")
            if self.settings.last_address:
                label = f"{self.settings.last_name or '上次的设备'}  ({self.settings.last_address})"
                self.device_combo.addItem(label, self.settings.last_address)
                self.append_log(
                    "info",
                    "手表可能已与 Windows 配对/连接而停止广播。可直接点「连接」尝试上次的地址。",
                )
            return
        for device in devices:
            self.device_combo.addItem(device.label(), device.address)
        index = self.device_combo.findData(current) if current else -1
        if index >= 0:
            self.device_combo.setCurrentIndex(index)
        self.statusBar().showMessage(f"发现 {len(devices)} 个设备")

    def toggle_connection(self) -> None:
        if self.connected:
            self.disconnect_device()
        else:
            self.connect_device()

    def connect_device(self, *, force_pair: bool = False) -> None:
        address = self.device_combo.currentData()
        if not address:
            QMessageBox.information(self, "提示", "请先扫描并选择手表。")
            return
        name = self.device_combo.currentText()
        self._set_busy(True, "正在连接…")
        self.statusBar().showMessage("正在连接手表…（不需要在 Windows 里配对手表）")

        async def _connect():
            if force_pair:
                await self.worker.app.forget_pairing(address)
            return await self.worker.app.connect(address, name, force_pair=force_pair)

        future = self.worker.submit(_connect())
        future.add_done_callback(self._deliver(self.connect_done, "连接失败"))

    def _on_connect_done(self, info: DeviceInfo) -> None:
        self.connected = True
        self._set_busy(False, "已连接")
        self.chip_state.setText("已连接")
        self.chip_state.setObjectName("chipOk")
        self._restyle(self.chip_state)
        self.btn_connect.setText("断开")
        self._on_info(info)
        self.statusBar().showMessage(f"已连接 {info.name or ''}")
        self.append_log("ok", f"已连接 {info.name or ''}")

    def disconnect_device(self) -> None:
        if self.uploading:
            QMessageBox.information(self, "正在上传", "请等待上传完成后再断开。")
            return
        self._set_busy(True, "正在断开…")

        async def _disconnect():
            await self.worker.app.disconnect()

        future = self.worker.submit(_disconnect())

        def done(future) -> None:
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                self.op_failed.emit(f"断开失败: {exc}")
            else:
                self.disconnect_done.emit()

        future.add_done_callback(done)

    def repair(self) -> None:
        address = self.device_combo.currentData()
        if not address:
            QMessageBox.information(self, "提示", "请先选择手表。")
            return
        answer = QMessageBox.question(
            self,
            "重新配对",
            "将删除本机保存的配对密钥并重新配对。\n手表上可能会出现确认提示，需要点“确认”。\n\n继续吗？",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.connect_device(force_pair=True)

    def refresh_storage(self) -> None:
        if not self.connected:
            return

        async def _refresh():
            return await self.worker.app.refresh_storage()

        future = self.worker.submit(_refresh())
        future.add_done_callback(self._deliver(self.storage_ready, "读取容量失败"))

    def fetch_watch_songs(self) -> None:
        if not self.connected:
            return
        self.btn_songs.setEnabled(False)
        self.statusBar().showMessage("正在读取手表上的歌曲…")

        async def _list():
            return await self.worker.app.music_list()

        future = self.worker.submit(_list())
        future.add_done_callback(self._deliver(self.watch_songs_ready, "读取歌曲列表失败"))

    def pick_files(self) -> None:
        patterns = " ".join(f"*.{ext}" for ext in C.MUSIC_EXTENSIONS)
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择音乐文件", "", f"音频文件 ({patterns});;所有文件 (*)"
        )
        if files:
            self.add_files(files)

    def add_files(self, paths: list[str]) -> None:
        added = 0
        for raw in paths:
            path = Path(raw)
            if path.is_dir():
                for child in sorted(path.iterdir()):
                    if child.is_file() and is_supported(child):
                        self._append_track(child)
                        added += 1
                continue
            if not path.is_file():
                continue
            if not is_supported(path):
                self.append_log("warn", f"跳过不支持的格式: {path.name}")
                continue
            self._append_track(path)
            added += 1
        if added:
            self.append_log("info", f"已添加 {added} 个文件")
        self._update_buttons()

    def _append_track(self, path: Path) -> None:
        track = read_track(path)
        self.tracks.append(track)
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [
            track.path.name,
            track.title,
            track.artist or "—",
            track.duration_text,
            f"{track.size / 1024 / 1024:.1f} MB",
            "待上传",
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setToolTip(str(track.path))
            self.table.setItem(row, column, item)

    def clear_files(self) -> None:
        if self.uploading:
            return
        self.tracks.clear()
        self.table.setRowCount(0)
        self._update_buttons()

    def start_upload(self) -> None:
        if not self.connected:
            QMessageBox.information(self, "提示", "请先连接手表。")
            return
        if not self.tracks:
            QMessageBox.information(self, "提示", "请先添加音乐文件。")
            return
        if not self.worker.app.info.supports_music:
            QMessageBox.warning(self, "无法上传", "这块手表没有音乐存储服务，无法传歌。")
            return

        self.uploading = True
        self._update_buttons()
        self.progress.setFormat("%p%")
        self.progress.setValue(0)
        for row in range(self.table.rowCount()):
            self._set_status(row, "等待中")
        paths = [track.path for track in self.tracks]
        self.append_log("info", f"开始上传 {len(paths)} 个文件")

        future = self.worker.submit(
            self.worker.app.upload(
                paths,
                on_file_start=lambda index, track: self.file_start.emit(index, track),
                on_progress=lambda index, fraction: self.file_progress.emit(index, fraction),
            )
        )
        future.add_done_callback(self._deliver(self.upload_done, "上传失败"))

    def _on_file_start(self, index: int, track: Track) -> None:
        self._set_status(index, "上传中 0%")
        self.progress.setValue(0)
        self.statusBar().showMessage(f"正在上传: {track.display}")

    def cancel_upload(self) -> None:
        if not self.uploading:
            return
        self.btn_cancel.setEnabled(False)
        self.append_log("warn", "正在取消上传…")
        self.worker.cancel_upload()

    def _on_file_progress(self, index: int, fraction: float) -> None:
        percent = int(fraction * 100)
        self._set_status(index, f"上传中 {percent}%")
        self.progress.setValue(percent)

    def _on_upload_done(self, results: list) -> None:
        self.uploading = False
        self.progress.setFormat("%p%")
        self._update_buttons()
        cancelled = any(error and "取消" in str(error) for _track, error in results)
        failed = 0
        for index, (_track, error) in enumerate(results):
            if error and "取消" in str(error):
                self._set_status(index, "已取消")
            elif error:
                failed += 1
                self._set_status(index, f"失败: {error}")
            else:
                self._set_status(index, "完成")
        if cancelled:
            self.progress.setValue(self.progress.value())
            self.append_log("warn", "上传已取消；手表可能仍在等待数据，请等几秒再继续操作")
            self.statusBar().showMessage("已取消上传")
            QMessageBox.information(
                self,
                "已取消",
                "上传已取消。手表上可能残留未完成的文件，可在手表音乐列表中删除。",
            )
            return
        self.progress.setValue(100 if not failed else self.progress.value())
        summary = f"上传结束：成功 {len(results) - failed}，失败 {failed}"
        self.append_log("ok" if not failed else "warn", summary)
        self.statusBar().showMessage(summary)
        if self.worker.app.info.storage:
            self._update_storage(self.worker.app.info.storage)
        if failed:
            QMessageBox.warning(self, "部分文件失败", summary + "\n详情见日志。")
        else:
            QMessageBox.information(self, "完成", summary)

    def _on_disconnected(self) -> None:
        self.connected = False
        self.chip_state.setText("未连接")
        self.chip_state.setObjectName("chip")
        self._restyle(self.chip_state)
        self.btn_connect.setText("连接")
        self._set_busy(False, "已断开")
        self.append_log("info", "已断开连接")

    def _on_storage(self, storage) -> None:
        if storage is not None:
            self._update_storage(storage)

    def _on_watch_songs(self, songs: list) -> None:
        self.btn_songs.setEnabled(self.connected and not self.uploading)
        self.statusBar().showMessage(f"手表上有 {len(songs)} 首歌曲")
        self.append_log("info", f"读取到手表歌曲 {len(songs)} 首")
        WatchSongsDialog(self, songs).exec()

    def _on_info(self, info: DeviceInfo) -> None:
        self.chip_model.setText(f"型号 {info.model or '—'}")
        self.chip_firmware.setText(f"固件 {info.firmware or '—'}")
        self.chip_battery.setText(f"电量 {info.battery if info.battery is not None else '—'}%")
        if info.storage:
            self._update_storage(info.storage)
        if info.services is not None:
            ok = info.supports_music
            self.chip_storage.setObjectName("chip" if ok else "chipWarn")
            self._restyle(self.chip_storage)

    def _update_storage(self, storage) -> None:
        self.chip_storage.setText(
            f"存储 {storage.free_mb} MB · {storage.songs}/{storage.max_songs} 首"
        )
        details = []
        if storage.formats:
            details.append("支持格式: " + " / ".join(fmt.upper() for fmt in storage.formats))
        if storage.frames:
            details.append(f"歌曲帧数: {storage.frames}")
        self.chip_storage.setToolTip("\n".join(details))

    def _on_worker_event(self, name: str, payload: object) -> None:
        if name == "disconnected":
            self.connected = False
            self.chip_state.setText("已断开")
            self.chip_state.setObjectName("chipWarn")
            self._restyle(self.chip_state)
            self.btn_connect.setText("连接")
            self._update_buttons()
            self.append_log("warn", "蓝牙连接已断开")
        elif name == "music_screen_opened":
            self.append_log("info", "手表打开了音乐界面")
        elif name == "udid":
            self.append_log("debug", f"手表 UDID: {payload.get('udid', '')}")
        elif name == "speed":
            kbps = 0.0
            if isinstance(payload, dict):
                try:
                    kbps = float(payload.get("kbps", 0.0))
                except (TypeError, ValueError):
                    kbps = 0.0
            self.progress.setFormat(f"%p%  ·  {kbps:.1f} KB/s")

    def _on_failed(self, message: str) -> None:
        self._set_busy(False, "操作失败")
        self.uploading = False
        self._update_buttons()
        self.append_log("error", message)
        self.statusBar().showMessage(message)
        QMessageBox.critical(self, "出错了", message)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _deliver(self, signal, prefix: str):
        def callback(future) -> None:
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                self.op_failed.emit(f"{prefix}: {exc}")
            else:
                signal.emit(result)

        return callback

    def _set_status(self, row: int, text: str) -> None:
        if row < self.table.rowCount():
            item = self.table.item(row, 5)
            if item is None:
                item = QTableWidgetItem()
                self.table.setItem(row, 5, item)
            item.setText(text)

    def _set_busy(self, busy: bool, message: str) -> None:
        for widget in (
            self.btn_scan,
            self.btn_connect,
            self.btn_pair,
            self.btn_upload,
            self.btn_cancel,
            self.btn_refresh,
            self.btn_songs,
        ):
            widget.setEnabled(not busy)
        self.statusBar().showMessage(message)
        if not busy:
            self._update_buttons()

    def _update_buttons(self) -> None:
        has_files = bool(self.tracks)
        self.btn_upload.setEnabled(self.connected and has_files and not self.uploading)
        self.btn_cancel.setEnabled(self.uploading)
        self.btn_add.setEnabled(not self.uploading)
        self.btn_clear.setEnabled(has_files and not self.uploading)
        self.btn_refresh.setEnabled(self.connected and not self.uploading)
        self.btn_songs.setEnabled(self.connected and not self.uploading)
        self.btn_scan.setEnabled(not self.uploading)
        self.btn_connect.setEnabled(not self.uploading)
        self.btn_pair.setEnabled(not self.uploading)
        self.btn_connect.setText("断开" if self.connected else "连接")

    @staticmethod
    def _restyle(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def append_log(self, level: str, message: str) -> None:
        colors = {
            "debug": "#8b949e",
            "info": "#c9d1d9",
            "ok": "#3fb950",
            "warn": "#d29922",
            "error": "#f85149",
        }
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        color = colors.get(level, "#c9d1d9")
        self.log_view.append(
            f'<span style="color:#6e7681">[{stamp}]</span> '
            f'<span style="color:{color}">{html.escape(message)}</span>'
        )
        try:
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp} [{level}] {message}\n")
        except OSError:
            pass

    def export_log(self) -> None:
        default = str(Path.home() / f"hwmusic-diagnostic-{_dt.datetime.now():%Y%m%d-%H%M%S}.txt")
        path, _ = QFileDialog.getSaveFileName(self, "保存诊断日志", default, "文本文件 (*.txt)")
        if not path:
            return
        header = [
            f"{C.APP_DISPLAY_NAME} {__version__}",
            f"时间: {_dt.datetime.now().isoformat(timespec='seconds')}",
            f"系统: {platform.platform()}",
            f"Python: {sys.version.split()[0]}",
            f"设备: {self.device_combo.currentText()} ({self.device_combo.currentData()})",
            f"已连接: {self.connected}",
            "-" * 60,
        ]
        try:
            Path(path).write_text("\n".join(header) + "\n" + self.log_view.toPlainText(), "utf-8")
            self.statusBar().showMessage(f"诊断日志已保存: {path}")
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", str(exc))

    def _fast_mode_changed(self, checked: bool) -> None:
        self.settings.fast_mode = checked
        self.settings.save()
        self.append_log("info", f"快速模式: {'开' if checked else '关'}（下次连接生效）")

    def _debug_mode_changed(self, checked: bool) -> None:
        self.settings.debug = checked
        self.settings.save()
        if self.worker.app is not None:
            self.worker.app.settings.debug = checked
            if self.worker.app.session is not None:
                self.worker.app.session.debug = checked

    def closeEvent(self, event) -> None:
        if self.uploading:
            answer = QMessageBox.question(self, "正在上传", "上传尚未完成，确定退出吗？")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.worker.stop()
        self.worker.wait(4000)
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("HuaweiMusic")
    app.setApplicationDisplayName(C.APP_DISPLAY_NAME)
    if sys.platform == "win32":
        app.setFont(QFont("Microsoft YaHei UI", 10))
    icon_path = Path(__file__).with_name("icon.png")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    settings = Settings.load()
    worker = AsyncWorker(settings)
    worker.start()
    worker.wait_ready()

    window = MainWindow(worker, settings)
    window.show()
    if "--smoke-test" in sys.argv:
        # Used by packaging checks: start the real UI, then exit cleanly.
        from PySide6.QtCore import QTimer

        QTimer.singleShot(1500, window.close)
        QTimer.singleShot(2000, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
