#!/usr/bin/env python3
"""Offline regression test for fail-closed NAS pruning (issue #53).

Run with the bridge venv:

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
    ordinary_file = mirror / "bridge" / "important.py"
    ordinary_file.parent.mkdir()
    ordinary_file.write_text("irreplaceable backup data\n")

    walked = []

    class NoWalkMirror:
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

print(f"prune failure checks: {4 - len(failures)}/4 passed")
for failure in failures:
    print("  FAIL:", failure)
sys.exit(1 if failures else 0)
