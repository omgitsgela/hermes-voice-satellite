#!/usr/bin/env python3
"""Announce something out loud on the voice satellite, from Hermes or from a cron job.

    ~/hermes-voice-bridge/.venv/bin/python \
        ~/hermes-voice-bridge/scripts/announce.py --text "Freezer temperature is 40 degrees"

The token is read from state/announce-token.txt (or $VOICE_ANNOUNCE_TOKEN) and never printed.

Exit codes matter when a cron job uses this: 0 means the bridge accepted the announcement, 1
means it was refused (bad token, too long, queue full), 2 means the token itself was unreadable.
An accepted announcement is still best-effort delivery by design, so a drop is logged by the
bridge in the checkout's logs/announcements.jsonl rather than raised here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.config import (  # noqa: E402
    ANNOUNCE_MAX_CHARS,
    ANNOUNCE_PORT,
    ANNOUNCE_TOKEN_ENV,
    ANNOUNCE_TOKEN_FILE,
)


def read_token() -> str | None:
    token = os.environ.get(ANNOUNCE_TOKEN_ENV, "").strip()
    if token:
        return token
    try:
        return ANNOUNCE_TOKEN_FILE.read_text().strip() or None
    except OSError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--text", required=True, help="what to say, spoken verbatim")
    parser.add_argument("--source", default="hermes", help="label recorded in the log")
    parser.add_argument("--key", default=None,
                        help="dedup key: the same key and text inside 60s is ignored")
    parser.add_argument("--no-chime", action="store_true",
                        help="skip the wake tone (rarely wanted)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=ANNOUNCE_PORT)
    parser.add_argument("--quiet", action="store_true", help="print nothing on success")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be sent, including whether the token is readable")
    args = parser.parse_args()

    if len(args.text) > ANNOUNCE_MAX_CHARS:
        print(f"refusing: {len(args.text)} chars exceeds the {ANNOUNCE_MAX_CHARS} limit",
              file=sys.stderr)
        return 1

    token = read_token()
    if not token:
        print(f"cannot read the announce token from {ANNOUNCE_TOKEN_FILE}", file=sys.stderr)
        return 2

    url = f"http://{args.host}:{args.port}/announce"
    if args.dry_run:
        print(f"would POST {url}: {len(args.text)} chars, source={args.source}, "
              f"key={args.key or '-'}, chime={not args.no_chime}, token=readable")
        return 0

    body: dict[str, object] = {"text": args.text, "source": args.source,
                               "chime": not args.no_chime}
    if args.key:
        body["key"] = args.key

    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            payload = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        print(f"announce failed: HTTP {exc.code} {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"announce failed: {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        if payload.get("duplicate"):
            print(f"duplicate ignored: {args.text[:60]}")
        else:
            print(f"queued as {payload.get('id', '?')} (HTTP {status}, "
                  f"position {payload.get('position', '?')}): {args.text[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
