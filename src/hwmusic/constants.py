"""Protocol constants for the Huawei lite-wearable BLE protocol.

Values mirror Gadgetbridge (AGPL-3.0) ``devices/huawei/HuaweiConstants.java``
and ``packets/``; see docs/PROTOCOL.md for the frame layout.
"""

from __future__ import annotations

APP_NAME = "HuaweiMusic"
APP_DISPLAY_NAME = "华为手表传歌"

# --- GATT -------------------------------------------------------------------
SERVICE_UUID = "0000fe86-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fe01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fe02-0000-1000-8000-00805f9b34fb"

# Advertised name prefixes of Huawei wearables (scan filter)
NAME_PREFIXES = ("HUAWEI WATCH", "HUAWEI", "HONOR", "Watch")

MAGIC = 0x5A
RESULT_SUCCESS = 0x186A0  # 100000

# --- services ---------------------------------------------------------------
SERVICE_DEVICE_CONFIG = 0x01
SERVICE_MUSIC = 0x25
SERVICE_FILE_UPLOAD = 0x28

# DeviceConfig commands
CMD_LINK_PARAMS = 0x01
CMD_SUPPORTED_SERVICES = 0x02
CMD_SUPPORTED_COMMANDS = 0x03
CMD_PRODUCT_INFO = 0x07
CMD_BATTERY = 0x08
CMD_PHONE_INFO = 0x10
CMD_HICHain = 0x28
CMD_PIN_CODE = 0x2C
CMD_SECURITY_NEGOTIATION = 0x33
CMD_EXPAND_CAPABILITY = 0x37

# Music commands (service 0x25)
MUSIC_CMD_STATUS = 0x01
MUSIC_CMD_INFO = 0x02
MUSIC_CMD_CONTROL = 0x03
MUSIC_CMD_STORAGE = 0x04
MUSIC_CMD_LIST = 0x05
MUSIC_CMD_PLAYLISTS = 0x06
MUSIC_CMD_PLAYLIST_MUSICS = 0x07
MUSIC_CMD_OPERATION = 0x08
MUSIC_CMD_UPLOAD_INFO = 0x09

# File upload commands (service 0x28)
UPLOAD_CMD_INFO = 0x02
UPLOAD_CMD_HASH = 0x03
UPLOAD_CMD_CONSULT = 0x04
UPLOAD_CMD_CHUNK_PARAMS = 0x05
UPLOAD_CMD_CHUNK = 0x06
UPLOAD_CMD_RESULT = 0x07
UPLOAD_CMD_DEVICE_ERROR = 0x08

FILE_TYPE_WATCHFACE = 1
FILE_TYPE_MUSIC = 2
FILE_TYPE_BACKGROUND = 3
FILE_TYPE_APP = 6
FILE_TYPE_FIRMWARE = 7

# Music file types the watch can play
MUSIC_EXTENSIONS = ("mp3", "m4a", "aac", "flac", "wav", "opus")

# HiChain group (Huawei Health's device group)
HICHain_GROUP_ID = "7B0BC0CBCE474F6C238D9661C63400B797B166EA7849B3A370FC73A9A236E989"

# Identity we present to the watch
PHONE_MODEL = "SM-S918B"
HUAWEI_HEALTH_VERSION = 1_600_103_320

# Upload error codes seen on the watch
UPLOAD_ERRORS = {
    140004: "手表上的文件/表盘数量已达上限",
    140008: "该文件已在手表中",
    140009: "手表存储空间不足",
}

# Default slice size advertised by the watch (0xF4 = 244), clamped to the BLE MTU
DEFAULT_SLICE_SIZE = 0xF4
MIN_SLICE_SIZE = 20
