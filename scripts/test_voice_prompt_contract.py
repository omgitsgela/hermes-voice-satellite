#!/usr/bin/env python3
"""The voice prompt IS the contract: it must name every control, and forbid acting on a bare word.

Decided 2026-10-30, after the model turned follow-up mode on because it heard the single word
"Conversation." The gateway writes the instructions for every voice turn, so that prompt is the right
place to define what the device can do and what a request for it looks like.

The owner's position, recorded here because it shapes the tests: a strict schema is trusted to keep
the model from calling the wrong thing, the tool does NOT re-validate the transcript, and the prompt
defines the shapes so unusual phrasing still works. Adaptability is deliberate.

What this pins:
1. Every capability the voice surface exposes is named in the prompt (so the model knows it exists).
2. The prompt states the bare-word rule: naming a feature is not a request to change it.
3. The prompt tells the model how to answer "what can I say".

Run with the Gateway venv:

    .venv/bin/python scripts/test_voice_prompt_contract.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from satellite_gateway.config import VOICE_INSTRUCTIONS  # noqa: E402

checks = 0
failures: list[str] = []


def check(label: str, condition, detail: str = "") -> None:
    global checks
    checks += 1
    if not condition:
        failures.append(f"{label}{(': ' + detail) if detail else ''}")


def main() -> int:
    prompt = " ".join(VOICE_INSTRUCTIONS.split())
    lowered = prompt.lower()

    # 1) every control the tool can act on is described, in the person's language
    capabilities = {
        "volume": "volume",
        "follow-up mode": "follow up",
        "endpointing profile": "endpointing",
        "new conversation": "new conversation",
        "timers": "timer",
        "microphone gain": "microphone gain",
        "led brightness": "led brightness",
        "wake word / threshold": "wake threshold",
    }
    for label, needle in capabilities.items():
        check(f"the prompt names {label}", needle in lowered)

    # 2) the bare-word rule, which is the actual fix for the reported bug
    check("the prompt says a bare feature name is not a request",
          "is not a request to change it" in lowered)
    check("and names the exact word that misfired", "hearing 'conversation'" in lowered)
    check("and forbids changing a mode on a guess",
          "never change a mode on a guess" in lowered)

    # 3) starting a new conversation is an ACTION the model can take, not something it has to refuse
    check("the prompt names the reset action", "conversation_reset" in lowered)
    check("and forbids claiming it cannot be done",
          "never say you cannot reset the conversation" in lowered)

    # 4) the capability helper
    check("the prompt covers 'what can I say'", "what they can say" in lowered)
    check("and asks for a short spoken list, not a catalogue", "not a catalogue" in lowered)

    # 4) phrasing is explicitly NOT a script, so unusual words still work
    check("the prompt calls the examples intent, not a script",
          "examples of intent, not a script" in lowered)

    # 5) no self-introduction, no capability recitation (decided 2026-11-02: answers were
    #    opening with "Sure, I can help with that" style preambles instead of the answer)
    check("the prompt forbids opening with a self-introduction or capability recitation",
          "never open a reply by introducing yourself" in lowered)
    check("and allows capability answers only when explicitly asked",
          "only when the person explicitly asks who you are or what you can do" in lowered)

    print(f"voice prompt checks: {checks - len(failures)}/{checks} passed")
    for failure in failures:
        print(f"  FAIL: {failure}")
    return 1 if failures else 0


raise SystemExit(main())
