# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


root = Path(SPEC).resolve().parent
package_root_value = os.environ.get("PANDRATOR_MANAGER_WHEEL_ROOT")
entrypoint_value = os.environ.get("PANDRATOR_MANAGER_BOOTSTRAP_ENTRY")
if not package_root_value or not entrypoint_value:
    raise RuntimeError(
        "Build through scripts/build_manager_bootstrap.py so PyInstaller "
        "consumes a validated pandrator-manager wheel."
    )
package_root = Path(package_root_value).resolve(strict=True)
entrypoint = Path(entrypoint_value).resolve(strict=True)
manager_package = package_root / "pandrator_manager"
if not manager_package.is_dir() or manager_package.is_symlink():
    raise RuntimeError(f"Unsafe or missing wheel package root: {manager_package}")
recovery_static = manager_package / "recovery_ui" / "static"
tray_icon = manager_package / "tray" / "pandrator-tray.png"
if not tray_icon.is_file() or tray_icon.is_symlink():
    raise RuntimeError(f"Unsafe or missing tray icon: {tray_icon}")
tray_backend = (
    "pystray._win32"
    if os.name == "nt"
    else "pystray._darwin"
    if sys.platform == "darwin"
    else "pystray._xorg"
)

a = Analysis(
    [str(entrypoint)],
    pathex=[str(package_root)],
    binaries=[],
    datas=[
        (str(recovery_static), "pandrator_manager/recovery_ui/static"),
        (str(tray_icon), "pandrator_manager/tray"),
    ],
    hiddenimports=[
        "PIL.Image",
        "pystray",
        tray_backend,
        *collect_submodules("dbus_next"),
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "tkinter",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PandratorManagerBootstrap",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Windows is a desktop bootstrap: using the GUI subsystem prevents the
    # setup, daemon, and tray entry points from creating console windows.
    # Linux retains stdout for AppImage diagnostics and shell operation.
    console=os.name != "nt",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=(str(root / "pandrator.ico") if (root / "pandrator.ico").is_file() else None),
)
