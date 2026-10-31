"""Entry point for the packaged installer.

PyInstaller needs a script to analyse; this is the smallest honest one. Everything else lives in the
packages, so the packaged build runs exactly the code the checkout runs.

It also answers a question a bundle has to answer: the engine is shell scripts, and those call Python
helpers (esptool, the provisioners, the bundler). On a machine with no Python, "run this script" has no
interpreter to run it with. So the bundle can act as its own interpreter for the helpers it ships:

    hermes-voice-satellite --run-python <script.py> [args...]   run a helper file on the bundled runtime
    hermes-voice-satellite --run-module <module> [args...]      run -m style (esptool)
    hermes-voice-satellite --run-code "<code>" [args...]        run -c style
    hermes-voice-satellite --pairing-server [args...]           the pairing endpoint, without -m

Callers never have to know which one they have: the install scripts look at HERMES_INSTALLER_PY, and
the installer sets it only in a frozen build.
"""
from __future__ import annotations

import runpy
import sys


def _run(argv: list[str]) -> int:
    """Dispatch one of the helper modes, or fall through to the installer CLI."""
    if not argv:
        return -1  # not ours: let the CLI show its own usage

    mode, rest = argv[0], argv[1:]
    if mode in {"--run-python", "--run-module", "--run-code"} and not rest:
        sys.stderr.write(f"{mode} needs an argument\n")
        return 2

    if mode == "--run-python":
        target, *arguments = rest
        sys.argv = [target, *arguments]
        if target == "-":
            # `python - <<'EOF'` is how the install scripts run a multi-line helper without a
            # temporary file; keep that shape working on a bundled runtime too.
            exec(compile(sys.stdin.read(), "<stdin>", "exec"), {"__name__": "__main__"})  # noqa: S102
        else:
            runpy.run_path(target, run_name="__main__")
        return 0

    if mode == "--run-module":
        module, *arguments = rest
        sys.argv = [module, *arguments]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return 0

    if mode == "--run-code":
        source, *arguments = rest
        sys.argv = ["-c", *arguments]
        exec(compile(source, "<-c>", "exec"), {"__name__": "__main__"})  # noqa: S102 - the caller's own code
        return 0

    if mode == "--pairing-server":
        from hermes_gateway.pairing_server import main as pairing_main

        return pairing_main(rest) or 0

    return -1


if __name__ == "__main__":
    from hermes_installer.cli import main

    dispatched = _run(sys.argv[1:])
    sys.exit(main() if dispatched < 0 else dispatched)
