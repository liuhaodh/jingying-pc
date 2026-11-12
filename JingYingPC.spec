# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all

datas = [('resolver.js', '.'), ('README.md', '.')]
binaries = []
hiddenimports = ['PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets']
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Windows 版本资源：由 build_release.py 按 config.APP_VERSION 生成，
# 让 exe 右键属性的 FileVersion / ProductVersion 不再为空。纯 ASCII 内容，
# 避免 PyInstaller 用 Windows 默认编码读取中文时乱码。
_spec_dir = globals().get('SPECPATH') or os.getcwd()
_version_info = os.path.join(_spec_dir, 'file_version_info.txt')
_version_kwargs = {'version': _version_info} if os.path.isfile(_version_info) else {}

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='JingYingPC',
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
    icon=['app.ico'],
    manifest='app.manifest',
    **_version_kwargs,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='JingYingPC',
)
