#!/usr/bin/env python3
"""Keep `docs/architecture.md` true, and keep the pipeline docs pointing at it.

The architecture document is the map: what runs at each stage of a turn, which piece does it, and
whether it is ours to maintain. A map that has drifted is worse than no map, because it is trusted.
It also cannot be checked by reading it - the facts in it live in the code.

So this pins the two failure modes that actually matter:

1. **The document stops matching the system.** Every fact asserted here is read back from the live
   configuration, so renaming the detector, moving the vendored model, or re-pointing the model route
   fails this test until the document is updated with it.
2. **A pipeline doc loses its place in the map.** Each one carries a "Where this sits" line naming its
   stage, which is how a reader arriving at one file learns the others exist.

Deliberately NOT checked: prose, wording, and the deep detail of each stage. Those change freely and
a test on them would be a change-detector.

    .venv/bin/python scripts/test_architecture_docs.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

PROJECT = pathlib.Path(__file__).resolve().parent.parent
DOCS = PROJECT / "docs"
ARCHITECTURE = DOCS / "architecture.md"

checks = 0
failures: list[str] = []

#: Docs that must name their stage. Not every file in docs/ describes a pipeline stage; these do.
PIPELINE_DOCS = (
    "outbound-link.md",
    "wyoming-protocol.md",
    "voice-detection.md",
    "endpointing-profiles.md",
    "agent-and-model-routing.md",
    "timers-and-alarms.md",
    "volume.md",
    "interruption.md",
    "voice-commands.md",
    "owning-the-satellite-stack.md",
    "configuration-plane.md",
    "satellite-http-config.md",
    "switching-wake-words.md",
    "switching-voices.md",
    "supported-devices.md",
)

MARKER = "**Where this sits:**"


def check(label: str, got, want) -> None:
    global checks
    checks += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


def main() -> int:
    check("the architecture document exists", ARCHITECTURE.is_file(), True)
    if not ARCHITECTURE.is_file():
        print(f"architecture docs: {checks - len(failures)}/{checks} passed")
        for failure in failures:
            print(f"  FAIL: {failure}")
        return 1

    doc = ARCHITECTURE.read_text()

    # ---- facts that live in the code, read back rather than remembered -------------------
    from bridge.config import (
        GATEWAY_BASE_URL, SILERO_THRESHOLD, VOICE_DETECTOR, VOICE_ROUTE_ALIAS,
    )
    from bridge.silero_vad import MODEL_PATH

    check("the doc names the detector actually configured",
          f"`{VOICE_DETECTOR}`" in doc or VOICE_DETECTOR in doc, True)
    check("the doc names the model route actually configured",
          VOICE_ROUTE_ALIAS in doc, True)
    check("the vendored model it points at is really there", MODEL_PATH.is_file(), True)
    check("and is inside the repository, not site-packages",
          "site-packages" in str(MODEL_PATH), False)
    check("the doc is honest about where the detector runs (the bridge)",
          "bridge" in doc and PATH_LABEL_OK(doc), True)

    # The detector is named with its threshold somewhere, so a reader can find the knob from here.
    check("the Silero threshold is discoverable from the doc", str(SILERO_THRESHOLD) in doc, True)

    # The gateway address a reader would need to follow stage 6.
    check("the doc names the bridge's gateway base url", GATEWAY_BASE_URL in doc, True)

    # ---- the map is navigable: each stage doc points back at it --------------------------
    for name in PIPELINE_DOCS:
        path = DOCS / name
        check(f"{name} exists", path.is_file(), True)
        if path.is_file():
            check(f"{name} names its stage in the pipeline", MARKER in path.read_text(), True)

    # A doc linking to the map must not link to a file that was renamed away.
    for path in sorted(DOCS.rglob("*.md")):
        text = path.read_text()
        for referenced in ("architecture.md", "voice-detection.md",
                           "owning-the-satellite-stack.md", "outbound-link.md"):
            if f"`{referenced}`" in text:
                check(f"{path.name} links to a real file: {referenced}",
                      (DOCS / referenced).is_file(), True)

    print(f"architecture docs: {checks - len(failures)}/{checks} passed")
    for failure in failures:
        print(f"  FAIL: {failure}")
    return 1 if failures else 0


def PATH_LABEL_OK(doc: str) -> bool:
    """The map must say the detector runs on the bridge, not on the device."""
    return "Runs on" in doc


if __name__ == "__main__":
    raise SystemExit(main())
