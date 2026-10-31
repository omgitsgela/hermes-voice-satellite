# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build: one executable per platform, no Python required on the target machine.

Built from the checkout rather than installed wheels, so the packaged build contains exactly what a
developer runs. The web interface is data, not code, and has to be listed or the window comes up empty.
"""
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent          # the spec lives in packaging/

# Stripping is what took the Linux bundle from 241 MB to 24 MB, because the bundled interpreter
# carried its symbols. On Windows it is not supported and damaged the build: the executable could not
# load its own python3xx.dll ('Invalid access to memory location'). So: strip where it is safe, not
# everywhere.
STRIP = sys.platform != "win32" 
PACKAGES = ROOT / "packages"

pathex = [
    str(PACKAGES / name / "src")
    for name in ("installer", "gateway", "device-esp32", "device-pi")
]

# The interface file plus anything else the installer reads from its own package.
# (See STRIP above for why stripping is platform-conditional.)
datas = [
    (str(PACKAGES / "installer" / "src" / "hermes_installer" / "web"), "hermes_installer/web"),
    # The engine, not just the front end. Without these the bundle can report what it sees and then
    # has nothing to run: the gateway install, the device flash and the card writer are all scripts.
    (str(ROOT / "scripts"), "scripts"),
    (str(ROOT / "pi"), "pi"),
    (str(ROOT / "firmware" / "esp32" / "models"), "firmware/esp32/models"),
    (str(ROOT / "bridge.env.example"), "."),
    (str(ROOT / "requirements.txt"), "."),
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
    strip=STRIP,
    upx=False,
    console=True,          # the console path is a feature, not a fallback
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=STRIP,
    upx=False,
    name="hermes-installer",
)
