#!/usr/bin/env python3
"""Offline regression test for fail-closed NAS pruning (issue #53).

Run with the Gateway venv:

    .venv/bin/python scripts/test_prune_nas_mirror.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("prune_nas_mirror", HERE / "prune-nas-mirror.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class FailedGitResult:
    returncode = 128
    stdout = ""
    stderr = "fatal: not a git repository"


with tempfile.TemporaryDirectory() as tmp:
    mirror = pathlib.Path(tmp)
    # The prune refuses to touch anything unless the mirror marker is present; this fixture
    # is a real mirror whose git read-back fails, not a wrong directory. The code directory
    # is deliberately named like the CURRENT tree (satellite_gateway/, not Gateway/): an
    # anchor that still looked for the old name would refuse a real mirror here and this
    # test would see the git failure never reached.
    (mirror / "SYNC-INFO.txt").write_text("commit: fixture\n")
    ordinary_file = mirror / "satellite_gateway" / "important.py"
    ordinary_file.parent.mkdir()
    ordinary_file.write_text("irreplaceable backup data\n")

    walked = []

    class NoWalkMirror:
        def __truediv__(self, name):
            return mirror / name

        def rglob(self, _pattern):
            walked.append(True)
            return ()

    mod.mirror_root = lambda: NoWalkMirror()
    original_run = mod.subprocess.run
    mod.subprocess.run = lambda *args, **kwargs: FailedGitResult()

    old_argv = sys.argv
    sys.argv = [str(HERE / "prune-nas-mirror.py"), "--apply"]
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            result = mod.main()
    finally:
        sys.argv = old_argv
        mod.subprocess.run = original_run

    failures = []
    if result == 0:
        failures.append("apply returned success after git ls-files failed")
    if walked:
        failures.append("apply walked the mirror after git ls-files failed")
    if not ordinary_file.is_file():
        failures.append("apply deleted a mirror file after git ls-files failed")
    if "fatal: not a git repository" not in stderr.getvalue():
        failures.append("stderr did not report the git ls-files failure")

phase_one_failures = len(failures)
print(f"prune failure checks: {4 - phase_one_failures}/4 passed")

# Phase two: an --apply prune must delete a file the repository no longer tracks and keep every
# protected one. PROTECTED_TOP in prune-nas-mirror.py is the only thing standing between the hourly
# job and the device image and the enclosure files, so the behaviour is pinned here rather than left
# to inspection of the constant.
with tempfile.TemporaryDirectory() as tmp:
    mirror = pathlib.Path(tmp)
    (mirror / "satellite_gateway").mkdir()
    (mirror / "satellite_gateway" / "tracked.py").write_text("still tracked\n")
    (mirror / "satellite_gateway" / "stale.py").write_text("no longer tracked\n")
    protected = [
        "private/device-images/satellite-sdcard.img.zst",
        "private/device-images/token-copy.txt",
        "private/makerworld-enclosure/case.3mf",
        "private/makerworld-enclosure/DO-NOT-PUBLISH.md",
        "upstream/wyoming.bundle",
        "upstream/MANIFEST.md",
        "release-assets/v1.0.0/flash.bundle.bin",
        "release-assets/v1.0.0/hermes-voice-satellite-1.0.0-linux-x86_64.tar.gz",
    ]
    generated = ["SYNC-INFO.txt", "README-what-this-is.md", "hermes-voice-satellite.bundle"]
    for rel in protected + generated:
        path = mirror / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep me\n")

    protected_failures = 0
    mod.mirror_root = lambda: mirror
    # The fixture this check builds is <mirror>/satellite_gateway/tracked.py, so the stubbed "tracked" set has
    # to name the same path. It said "Gateway/tracked.py" -- a different file -- so the guard had
    # nothing to protect and the check failed on its own inconsistency.
    mod.tracked_files = lambda: {"satellite_gateway/tracked.py"}
    sys.argv = [str(HERE / "prune-nas-mirror.py"), "--apply"]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = mod.main()
    finally:
        sys.argv = old_argv

    if result != 0:
        failures.append("apply returned failure while a protected file existed")
        protected_failures += 1
    if not (mirror / "satellite_gateway" / "tracked.py").is_file():
        failures.append("apply deleted a file the repository still tracks")
        protected_failures += 1
    if (mirror / "satellite_gateway" / "stale.py").exists():
        failures.append("apply kept a file the repository no longer tracks")
        protected_failures += 1
    for rel in protected + generated:
        if not (mirror / rel).is_file():
            failures.append(f"apply deleted a protected file: {rel}")
            protected_failures += 1

    total = len(protected) + len(generated) + 3
    print(f"prune protected checks: {total - protected_failures}/{total} passed")

for failure in failures:
    print("  FAIL:", failure)
sys.exit(1 if failures else 0)
