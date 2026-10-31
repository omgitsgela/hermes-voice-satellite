#!/usr/bin/env python3
"""Fire one cron-backed voice alarm through the authenticated announcement plane."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from satellite_gateway.config import ANNOUNCE_MAX_CHARS, ANNOUNCE_PORT  # noqa: E402
from scripts.announce import read_token  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", required=True, help="alarm wording, spoken verbatim")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=ANNOUNCE_PORT)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    text = " ".join(args.text.split())
    if not text or len(text) > ANNOUNCE_MAX_CHARS:
        print(f"alarm text must be 1-{ANNOUNCE_MAX_CHARS} characters", file=sys.stderr)
        return 1
    token = read_token()
    if not token:
        print("cannot read the announce token", file=sys.stderr)
        return 2

    body = {"text": text, "source": "alarm", "sound": "alarm", "key": f"alarm:{text}"}
    request = urllib.request.Request(
        f"http://{args.host}:{args.port}/announce",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode() or "{}")
            status = response.status
    except urllib.error.HTTPError as exc:
        print(f"alarm announcement refused: HTTP {exc.code}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        print(f"alarm announcement failed: {exc}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"alarm queued as {payload.get('id', '?')} (HTTP {status})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
