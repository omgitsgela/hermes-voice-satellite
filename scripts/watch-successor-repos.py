#!/usr/bin/env python3
"""Deterministic digest of the successor projects, for a change-detecting cron monitor.

Watched because we are not switching to them, but we do want their ideas: this prints a fingerprint
of the parts that interest us, so a monthly job can wake an agent ONLY when something actually
changed. Steady state costs nothing and says nothing.

Determinism matters more than completeness here: no timestamps, no counts that drift, nothing that
changes without a real upstream change, or the monitor would fire every single month.

Watched repositories:
  OHF-Voice/linux-voice-assistant   the active successor to the archived Wyoming satellite

Watched within it: the docs that map to features we lack (echo cancellation, the peripheral API),
the wakeword and sound asset directories, and the changelog.
"""

from __future__ import annotations

import json
import sys
import urllib.request

# What to watch, per repository. The transport repository matters more to us than the application
# built on it: we depend on the protocol itself, and it is where new event types appear (timers, for
# one, arrived there long before we had a use for them).
WATCH = {
    "OHF-Voice/wyoming": {"dirs": ["wyoming"], "files": ["README.md", "SECURITY.md"]},
    "OHF-Voice/linux-voice-assistant": {
        "dirs": ["docs", "wakewords", "sounds", "linux_voice_assistant"],
        "files": ["CHANGELOG.md", "README.md"],
    },
}
API = "https://api.github.com"
TIMEOUT = 20


def get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-successor-watch",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def main() -> int:
    lines: list[str] = []
    for repo, spec in WATCH.items():
        try:
            head = get(f"{API}/repos/{repo}/commits?per_page=1")[0]["sha"][:10]
        except Exception as exc:  # noqa: BLE001 - a network hiccup must be visible, not silent
            print(f"{repo}: fetch failed: {type(exc).__name__}")
            return 1
        lines.append(f"repo {repo} @ {head}")
        for d in spec["dirs"]:
            try:
                items = get(f"{API}/repos/{repo}/contents/{d}")
            except Exception:
                continue
            names = sorted(str(i.get("name")) for i in items)
            lines.append(f"  {d}/ ({len(names)}): {' '.join(names)}")
        for f in spec["files"]:
            try:
                meta = get(f"{API}/repos/{repo}/contents/{f}")
            except Exception:
                continue
            lines.append(f"  {f}: {meta.get('size')} bytes, blob {str(meta.get('sha'))[:10]}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
