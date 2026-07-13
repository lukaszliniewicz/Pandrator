# -*- mode: python ; coding: utf-8 -*-

from pandrator_installer.build_support import resolve_openssl_runtime_pair


ssl_library, crypto_library = resolve_openssl_runtime_pair()
openssl_binaries = [
    (str(ssl_library), '.'),
    (str(crypto_library), '.'),
]


a = Analysis(
    ['pandrator_installer_launcher.py'],
    pathex=[],
    binaries=openssl_binaries,
    datas=[
        ('pandrator.png', '.'),
    ],
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
    [],
    exclude_binaries=True,
    name='PandratorInstaller',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PandratorInstaller',
)
