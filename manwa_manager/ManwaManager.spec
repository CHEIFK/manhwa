# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

binaries = []
datas = [('index.html', '.')]
hiddenimports = ['tkinter', 'bs4', 'PIL', 'curl_cffi']

for pkg in ['curl_cffi', 'bs4', 'PIL']:
    try:
        b, d, h = collect_all(pkg)
        binaries += b
        datas += d
        hiddenimports += h
    except Exception:
        pass

block_cipher = None

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
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ManwaManager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
