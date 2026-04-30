# -*- mode: python ; coding: utf-8 -*-
# ListScanner.spec
# Bundles Tesseract OCR so end-users need nothing extra.
# Requires Tesseract installed on the BUILD machine.

import os
import sys
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import tcl_tk


def _collect_tree(src, dest, exclude_names=()):
    items = []
    if not os.path.isdir(src):
        return items
    exclude_names = {name.lower() for name in exclude_names}
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d.lower() not in exclude_names]
        rel = os.path.relpath(root, src)
        target = dest if rel == "." else os.path.join(dest, rel)
        for name in files:
            if name.lower() in exclude_names:
                continue
            items.append((os.path.join(root, name), target))
    return items


_py_base = sys.base_prefix if hasattr(sys, "base_prefix") else sys.prefix
_tcl_root = os.path.join(_py_base, "tcl")
_tcl_data_dir = os.path.join(_tcl_root, "tcl8.6")
_tk_data_dir = os.path.join(_tcl_root, "tk8.6")

# This machine's Python can import _tkinter, but PyInstaller's Tcl/Tk probe
# fails before collection. Mark it available and bundle the data manually.
tcl_tk.tcltk_info.available = True
tcl_tk.tcltk_info.data_files = []
_tk_datas = (
    _collect_tree(_tcl_data_dir, "_tcl_data", {"demos", "tclconfig.sh"})
    + _collect_tree(_tk_data_dir, "_tk_data", {"demos", "tkconfig.sh"})
    + _collect_tree(os.path.join(_tcl_root, "tcl8"), "tcl8")
)
_tk_hiddenimports = collect_submodules("tkinter")

# ── Locate Tesseract on the build machine ─────────────────────────────────────
# build.bat sets TESSERACT_DIR; fall back to common locations if run manually.
_tess_dir = os.environ.get("TESSERACT_DIR", "").strip()

if not _tess_dir or not os.path.isfile(os.path.join(_tess_dir, "tesseract.exe")):
    _TESS_CANDIDATES = [
        r"C:\Program Files\Tesseract-OCR",
        r"C:\Program Files (x86)\Tesseract-OCR",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR"),
    ]
    _tess_dir = next(
        (_c for _c in _TESS_CANDIDATES
         if os.path.isfile(os.path.join(_c, "tesseract.exe"))),
        None,
    )

if not _tess_dir:
    raise SystemExit(
        "\n\nERROR: Tesseract OCR was not found on this machine.\n"
        "The build machine must have Tesseract installed so it can be bundled.\n"
        "Install from: https://github.com/UB-Mannheim/tesseract/wiki\n"
        "(Default install path: C:\\Program Files\\Tesseract-OCR)\n"
    )

print(f"[spec] Bundling Tesseract from: {_tess_dir}")

# Collect tesseract.exe + all DLLs
_tess_binaries = [
    (os.path.join(_tess_dir, f), "tesseract")
    for f in os.listdir(_tess_dir)
    if os.path.isfile(os.path.join(_tess_dir, f))
    and f.lower().endswith((".exe", ".dll"))
]

# Collect tessdata language files
_tessdata_dir = os.path.join(_tess_dir, "tessdata")
_tess_data = []
if os.path.isdir(_tessdata_dir):
    for f in os.listdir(_tessdata_dir):
        full = os.path.join(_tessdata_dir, f)
        if os.path.isfile(full):
            _tess_data.append((full, os.path.join("tesseract", "tessdata")))

print(f"[spec] {len(_tess_binaries)} binaries, {len(_tess_data)} tessdata files")

# ── Build ──────────────────────────────────────────────────────────────────────
a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=_tess_binaries,
    datas=_tess_data + _tk_datas,
    hiddenimports=_tk_hiddenimports,
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
    name='ListScanner',
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
)
