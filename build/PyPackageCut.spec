# -*- mode: python ; coding: utf-8 -*-
"""
PyPackageCut 的 PyInstaller 打包配置（单文件 exe）。

用法：
    pyinstaller build/PyPackageCut.spec

产物：dist/PyPackageCut.exe

说明：
- 源码位于 script/ 目录（cutcore.py 为核心引擎，main.py 为主界面）。
- 本 spec 将 main.py 作为入口，连同 cutcore.py / main1.py 一并打包。
- 若需保留项目自带的“PyPackageCut.int 引导 + 嵌入式 runtime”分发方式，
  可直接复制 script/、runtime/、site-packages/ 与 PyPackageCut.int 到发布目录，
  无需重新打包（详见 README 的“发布与分发”小节）。
"""
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT_DIR = os.path.join(ROOT, "script")


def _data(p, *sub):
    path = os.path.join(SCRIPT_DIR, *sub)
    return (path, os.path.join("script", *sub[:-1])) if len(sub) > 1 else (path, "script")


a = Analysis(
    [os.path.join(SCRIPT_DIR, "main.py")],
    pathex=[SCRIPT_DIR],
    binaries=[],
    datas=[
        (os.path.join(SCRIPT_DIR, "cutcore.py"), "script"),
        (os.path.join(SCRIPT_DIR, "main1.py"), "script"),
    ],
    hiddenimports=[
        "PyQt5.QtWidgets",
        "PyQt5.QtCore",
        "PyQt5.QtGui",
        "psutil",
        "ctypes",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="PyPackageCut",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon=(os.path.join(SCRIPT_DIR, "file.ico")
          if os.path.exists(os.path.join(SCRIPT_DIR, "file.ico")) else None),
)
