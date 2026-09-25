#!/usr/bin/env python3
"""The device files must import in the DEVICE's layout, not the repo's (#10).

The outbound runner is installed as a plain script:

    /home/wyoming1/wyoming-satellite/.venv/bin/python \
        /home/wyoming1/wyoming-satellite/examples/outbound_satellite.py

So its helper modules sit BESIDE it in `examples/` with no package around them. A bare
`from pi.volume_control import ...` works in the repo, where `pi/` is a package on
sys.path, and fails on the device with ImportError - a bug that every in-repo test passes
straight through, because in the repo the import is valid.

This suite copies the shipped files into a flat directory with no package and imports
them the way the device does. It is the only check here that would have caught that.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Exactly the files a device needs for the volume feature, and their real install names.
SHIPPED = {
    "outbound_satellite.py": ROOT / "pi" / "outbound_satellite.py",
    "config_agent.py": ROOT / "pi" / "config_agent.py",
    "device_apply.py": ROOT / "pi" / "device_apply.py",
    "volume_control.py": ROOT / "pi" / "volume_control.py",
}

problems: list[str] = []
checks = 0


def check(label: str, condition: bool, detail="") -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(f"{label}: {detail}")


for name, source in SHIPPED.items():
    check(f"{name} exists in the repo", source.is_file(), source)

with tempfile.TemporaryDirectory() as tmp:
    rootfs = pathlib.Path(tmp)
    flat = rootfs / "home/wyoming1/wyoming-satellite/examples"
    flat.mkdir(parents=True)
    # Exercise the real installer, never a hand-maintained copy of its inventory.
    for deployment in ("fresh", "update"):
        if deployment == "update":
            for name in SHIPPED:
                (flat / name).write_text("# stale installed artifact\n")
        installed = subprocess.run(
            ["sh", str(ROOT / "scripts/deploy-to-card.sh"), str(rootfs)],
            capture_output=True, text=True, timeout=30,
        )
        check(f"{deployment} installer succeeds", installed.returncode == 0, installed.stderr)
        for name, source in SHIPPED.items():
            target = flat / name
            check(f"{deployment} installs current {name}",
                  target.is_file() and target.read_bytes() == source.read_bytes())

    check("the simulated device directory contains no package structure",
          not (flat / "__init__.py").exists() and not (flat / "pi").exists())

    # Import each module the way the device does: flat, as a top-level module.
    for name in SHIPPED:
        module = name[:-3]
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}; print('ok')"],
            capture_output=True, text=True, cwd=flat, timeout=90,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "", "HOME": tmp},
        )
        if result.returncode == 0:
            check(f"{module} imports in the flat device layout", True)
        else:
            tail = (result.stderr or "").strip().splitlines()
            check(f"{module} imports in the flat device layout", False,
                  tail[-1] if tail else "no output")

    # And the thing that actually matters: can the runner build its config agent, with the
    # apply wiring, using only what is in that directory?
    probe = flat / "probe.py"
    probe.write_text(
        "import json, pathlib, tempfile, asyncio\n"
        "from wyoming.event import Event\n"
        "import outbound_satellite as runner\n"
        "from volume_control import AlsaVolume\n"
        "\n"
        "class FakeAmixer:\n"
        "    def __init__(self): self.calls = []\n"
        "    def __call__(self, argv, **kw):\n"
        "        self.calls.append(list(argv))\n"
        "        class R: returncode = 0; stdout = ''; stderr = ''\n"
        "        return R()\n"
        "\n"
        "async def main():\n"
        "    with tempfile.TemporaryDirectory() as t:\n"
        "        fake = FakeAmixer()\n"
        "        cfg = runner.LinkConfig(device_id='wyoming1', host='127.0.0.1', port=1,\n"
        "                                relay=False,\n"
        "                                config_state_path=pathlib.Path(t)/'c.json')\n"
        "        link = runner.BridgeLink(cfg, alsa_volume=AlsaVolume(\n"
        "            card='seeed2micvoicec', control='PCM', floor=25, runner=fake))\n"
        "        report = await link.config_agent.apply_event(Event(\n"
        "            type='hermes.config.desired',\n"
        "            data={'schema_version':1,'device_id':'wyoming1','generation':1,\n"
        "                  'settings':{'playback_volume':70}}))\n"
        "        print(json.dumps({'actual': report.data['actual'],\n"
        "                          'health': report.data['health'],\n"
        "                          'calls': len(fake.calls)}))\n"
        "\n"
        "asyncio.run(main())\n"
    )
    result = subprocess.run(
        [sys.executable, str(probe)],
        capture_output=True, text=True, cwd=flat, timeout=120,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "", "HOME": tmp},
    )
    if result.returncode == 0:
        try:
            data = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            data = None
        check("the runner builds its agent and applies a volume change in the device layout",
              data is not None and data.get("actual", {}).get("playback_volume") == 70
              and data.get("health") == "healthy",
              f"stdout={result.stdout!r} stderr={result.stderr[-300:]!r}")
        check("the volume actually reached a command in the device layout",
              data is not None and data.get("calls", 0) >= 2,
              data)
    else:
        tail = (result.stderr or "").strip().splitlines()
        check("the runner builds its agent and applies a volume change in the device layout",
              False, tail[-1] if tail else "no output")

print(f"device layout checks: {checks - len(problems)}/{checks} passed")
for problem in problems:
    print(f"  FAIL: {problem}")
raise SystemExit(1 if problems else 0)
