# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build: one executable per platform, no Python required on the target machine.

Built from the checkout rather than installed wheels, so the packaged build contains exactly what a
developer runs. The web interface is data, not code, and has to be listed or the window comes up empty.
"""
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent          # the spec lives in packaging/
PACKAGES = ROOT / "packages"

pathex = [
    str(PACKAGES / name / "src")
    for name in ("installer", "gateway", "device-esp32", "device-pi")
]

# The interface file plus anything else the installer reads from its own package.
datas = [
    (str(PACKAGES / "installer" / "src" / "hermes_installer" / "web"), "hermes_installer/web"),
]

hiddenimports = [
    # Serial ports are imported lazily, so the analysis cannot see them.
    "serial",
    "serial.tools",
    "serial.tools.list_ports",
    # Discovered by entry point at runtime, which the analysis cannot follow either.
    "hermes_gateway",
    "hermes_gateway.pairing_server",
    "hermes_gateway.unit",
    "hermes_device_esp32",
    "hermes_device_pi",
]

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=pathex,
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="hermes-installer",
    debug=False,
    strip=True,
    upx=False,
    console=True,          # the console path is a feature, not a fallback
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=True,
    upx=False,
    name="hermes-installer",
)
