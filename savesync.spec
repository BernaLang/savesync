# -*- mode: python ; coding: utf-8 -*-

# Import version from savesync.py
import sys
sys.path.insert(0, '.')
from savesync import VERSION

a = Analysis(
    ['savesync.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.png', '.')],  # Bundle icon.png with the exe
    hiddenimports=[
        'pydrive2',
        'pydrive2.auth',
        'pydrive2.drive',
        'oauth2client',
        'oauth2client.client',
        'oauth2client.file',
        'oauth2client.tools',
        'httplib2',
        'googleapiclient',
        'winshell',
        'win32com',
        'win32com.client',
    ],
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
    name=f'SaveSync_v{VERSION}',  # Version in filename
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console window (GUI-only)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',  # Custom app icon (Windows requires .ico format)
)
