# -*- mode: python ; coding: utf-8 -*-
"""One-file Windows build.

curl_cffi is the reason this is a spec file and not a command line. It wraps
libcurl and ships native .dll payloads plus a CA bundle that PyInstaller's
module analysis does not find, and a build missing them does not fail at build
time — it fails at the first dibycap request, in the operator's hands.
collect_all pulls submodules, data files, and binaries together.

console=True is not optional. src/output.py reconfigures sys.stdout and writes
OSC escapes to it; a windowed build leaves sys.stdout as None and the first
log line raises.

UPX is off. A UPX-packed binary trips Windows Defender far more often than an
unpacked one, and shaving ~8 MB off a tool that runs for hours is not worth a
support conversation about a virus warning.

assets/icon.ico carries all seven sizes from 256 down to 16 in the one file.
Windows picks the size it needs and scales nothing; a single-size .ico looks
correct in Explorer and blurred in the taskbar, and neither the build nor the
tests would say so.
"""

import os
import sys

sys.path.insert(0, os.path.abspath("."))

from PyInstaller.utils.hooks import collect_all      # noqa: E402

from src.version import __version__                  # noqa: E402

datas, binaries, hiddenimports = [], [], []
for package in ("curl_cffi", "certifi"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

# The Windows version resource, generated here rather than checked in, so the
# .exe's Properties tab can never disagree with src/version.py.
_major, _minor, _patch = (int(part) for part in __version__.split("."))
_version_resource = os.path.join("build", "version_info.txt")
os.makedirs("build", exist_ok=True)
with open(_version_resource, "w", encoding="utf-8") as handle:
    handle.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({_major}, {_minor}, {_patch}, 0),
    prodvers=({_major}, {_minor}, {_patch}, 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'errored-app'),
      StringStruct('FileDescription', 'FarmsyncSolver'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'FarmsyncSolver'),
      StringStruct('OriginalFilename', 'FarmsyncSolver.exe'),
      StringStruct('ProductName', 'FarmsyncSolver'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""")

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing the app needs, and pytest drags in a large dependency tree.
    excludes=["tests", "pytest", "_pytest", "tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="FarmsyncSolver",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    version=_version_resource,
    icon=os.path.join("assets", "icon.ico"),
)
