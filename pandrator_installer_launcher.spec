# -*- mode: python ; coding: utf-8 -*-

import os

from pandrator_installer.build_support import (
    resolve_windows_runtime_libraries,
    windows_ctypes_library_directories,
)


runtime_directories = windows_ctypes_library_directories()
os.environ['PATH'] = os.pathsep.join(
    [*(str(directory) for directory in runtime_directories), os.environ.get('PATH', '')]
)
runtime_libraries = resolve_windows_runtime_libraries(runtime_directories)

a = Analysis(
    ['pandrator_installer_launcher.py'],
    pathex=[],
    binaries=[(str(library), '.') for library in runtime_libraries],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PandratorInstaller',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['pandrator.ico'],
)
