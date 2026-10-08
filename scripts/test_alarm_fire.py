#!/usr/bin/env python3
"""Offline contract check for the cron-backed alarm announcer (issue #5)."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
# The child interpreter: the repo's own venv when there is one (it carries the bridge's deps),
# otherwise the interpreter running THIS test -- a fresh clone or an isolated worktree has no
# repo-local .venv, and the suite must still run there (sys.executable is what the other
# subprocess-based tests here spawn children with).
VENV_PY = ROOT / ".venv" / "bin" / "python"
PYTHON = str(VENV_PY) if VENV_PY.is_file() else sys.executable
received: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        received.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": json.loads(body),
        })
        response = json.dumps({"ok": True, "queued": True, "id": "an-test", "position": 1}).encode()
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_args):
        pass


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = dict(os.environ)
    env["VOICE_ANNOUNCE_TOKEN"] = "alarm-test-token"
    try:
        result = subprocess.run(
            [PYTHON, str(ROOT / "scripts" / "alarm_fire.py"),
             "--text", "Time to leave", "--host", "127.0.0.1", "--port", str(server.server_port)],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=10,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    failures = []
    if result.returncode != 0:
        failures.append(f"exit={result.returncode} stderr={result.stderr!r}")
    if len(received) != 1:
        failures.append(f"requests={received!r}")
    else:
        request = received[0]
        if request["path"] != "/announce":
            failures.append(f"path={request['path']!r}")
        if request["authorization"] != "Bearer alarm-test-token":
            failures.append("authorization header missing")
        if request["body"] != {
            "text": "Time to leave", "source": "alarm", "sound": "alarm", "key": "alarm:Time to leave",
        }:
            failures.append(f"body={request['body']!r}")

    print(f"alarm fire checks: {4 - len(failures)}/4 passed")
    for failure in failures:
        print("  FAIL:", failure)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
