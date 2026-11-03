# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec - 华硕主板 BIOS 下载工具 v2.3.0
import os
from PyInstaller.utils.hooks import collect_all

# curl_cffi 自带 .pyd + libcurl-impersonate 二进制，需要打包
curl_datas, curl_bins, curl_hidden = collect_all("curl_cffi")
# 加一些防御性 hidden import
extra_hidden = ["curl_cffi", "curl_cffi.requests"] + curl_hidden

block_cipher = None

a = Analysis(
    ['asus_bios_checker.py'],
    pathex=[],
    binaries=curl_bins,
    datas=curl_datas,
    hiddenimports=extra_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'numpy', 'pandas', 'scipy', 'matplotlib', 'PIL',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'openpyxl', 'xlsxwriter', 'docx', 'pptx',
        # 避免 venv 里其他工具（pythonnet 用于 CLR/.NET 桥接，本工具用不到）的 PyInstaller
        # 自动探测把 pythonnet + clr_loader + .NET runtime DLL 全量打包（约 8MB）
        'pythonnet', 'clr_loader', 'clr_loader.ffi',
        # pyreadline3 是 readline 的替代，本工具在 Windows GUI 用不到
        'pyreadline3', 'pyreadline3.py3k_compat',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='华硕主板BIOS下载工具',
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
    icon='asus_bios.ico',
    version='version_info.txt',
)
