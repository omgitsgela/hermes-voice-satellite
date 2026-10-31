"""What a shell script needs to know about the process that launched it.

The engine is shell scripts, and those scripts call Python helpers. In a checkout that means "python3"
or the checkout's venv. In a packaged build there may be no Python at all, so the packaged executable
offers itself as the runtime (`--run-python`, `--run-module`, `--run-code`) and announces itself here.

Scripts read `HERMES_INSTALLER_PY` and add the matching dispatch flag. Nothing changes for a normal
checkout, which is why the scripts keep working exactly as before.
"""
from __future__ import annotations

import os
import sys


def helper_environment() -> dict[str, str] | None:
    """Extra environment for the engine's subprocesses, or None outside a packaged build."""
    if not getattr(sys, "frozen", False):
        return None
    return {
        "HERMES_INSTALLER_PY": sys.executable,
        "HERMES_INSTALLER_FROZEN": "1",
        # A frozen child must not re-trigger the installer's own single-instance behaviour or try to
        # write its unpacked files into a read-only bundle directory.
        "PYTHONUTF8": os.environ.get("PYTHONUTF8", "1"),
    }


def merge(base: dict[str, str] | None = None) -> dict[str, str] | None:
    """The environment for a subprocess: the caller's, plus the bundle's, if there is one."""
    extra = helper_environment()
    if extra is None and base is None:
        return None
    merged = dict(os.environ)
    if base:
        merged.update(base)
    if extra:
        merged.update(extra)
    return merged
