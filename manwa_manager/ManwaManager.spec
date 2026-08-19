# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all

if "SPECPATH" in globals():
    SPEC_DIR = SPECPATH
elif "SPEC" in globals():
    SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
elif "__file__" in globals():
    SPEC_DIR = os.path.dirname(os.path.abspath(__file__))
else:
    SPEC_DIR = os.path.abspath(".")

binaries = []
datas = [(os.path.join(SPEC_DIR, "index.html"), ".")]
hiddenimports = [
    "tkinter",
    "tkinter.filedialog",
    "bs4",
    "PIL",
    "PIL.Image",
    "PIL.PngImagePlugin",
    "PIL.JpegImagePlugin",
    "PIL.WebPImagePlugin",
    "PIL.GifImagePlugin",
    "curl_cffi",
    "curl_cffi.requests",
]

for pkg in ["curl_cffi", "bs4", "PIL"]:
    try:
        b, d, h = collect_all(pkg)
        binaries += b
        datas += d
        hiddenimports += h
    except Exception as e:
        print(f"Notice: collect_all for {pkg}: {e}")

block_cipher = None

a = Analysis(
    [os.path.join(SPEC_DIR, "app.py")],
    pathex=[SPEC_DIR],
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
    name="ManwaManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
