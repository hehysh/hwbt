# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one-file, windowed Windows build.

Run from the repository root:

    pyinstaller packaging/hwmusic.spec --noconfirm --clean
"""

import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.dirname(os.path.abspath(SPECPATH))

hiddenimports = collect_submodules("bleak")

# Windows BLE backend: bleak talks to WinRT through these packages.
hiddenimports += [
    "winrt.runtime",
    "winrt.system",
    "winrt.windows.devices.bluetooth",
    "winrt.windows.devices.bluetooth.advertisement",
    "winrt.windows.devices.bluetooth.genericattributeprofile",
    "winrt.windows.devices.enumeration",
    "winrt.windows.devices.radios",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.storage.streams",
]
hiddenimports += [
    "hwmusic.gui.main",
    "hwmusic.cli",
]

a = Analysis(
    [os.path.join(ROOT, "run_gui.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    datas=[(os.path.join(ROOT, "src", "hwmusic", "gui", "icon.png"), "hwmusic/gui")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pytest", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="HuaweiMusic",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "packaging", "icon.ico"),
    version=os.path.join(ROOT, "packaging", "version_info.txt"),
)
