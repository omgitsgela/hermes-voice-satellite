"""Test the voice prompt's wording through the exact path the loop uses.

Builds a VoiceGateway with the live VOICE_INSTRUCTIONS (same code path the satellite uses) and
asks a device action, then reports whether the reply narrates verification. Prints text only, so
nothing is spoken out loud.
"""

import asyncio
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.config import (  # noqa: E402
    GATEWAY_BASE_URL,
    GATEWAY_ENV_FILE,
    VOICE_INSTRUCTIONS,
    VOICE_ROUTE_ALIAS,
)
from bridge.gateway import VoiceGateway, read_api_key  # noqa: E402

BANNED = ("verif", "confirm", "double check", "double-check", "checked", "validated",
          "i can see", "attribute")

# The rule, 2026-09-22: the voice surface must not volunteer anything about its own setup, and
# WHEN ASKED it must look the answer up (skill_view on hermes-voice-bridge) instead of guessing.
# These checks pin both halves. The expected values come from that skill's own summary section, so a
# pass means it read the skill and reported what is actually configured.
FACT_CHECKS = [
    ("What is your wake word threshold?",
     re.compile(r"0\.35"),
     "the threshold it actually runs"),
    ("Are your microphones at maximum sensitivity?",
     re.compile(r"4[01](\.\d+)?\s*(?:dB|decibel)|headroom|nowhere near|not at max|^\s*no\b", re.I),
     "the real preamp level rather than a guess"),
]

# A neutral question must not drag configuration into the reply at all.
NEUTRAL_QUESTION = "What is the capital of Australia?"
VOLUNTEER_WORDS = ("threshold", "sensitivity", "microphone", "preamp", "pga",
                   "my settings", "my configuration", "my config", "my hardware",
                   "my model", "wake word model", "my logs", "my own setup")



async def ask(gateway, question: str) -> None:
    print(f"\n--- asked: {question}")
    chunks = []
    async for delta in gateway.ask(question):
        chunks.append(delta)
    reply = "".join(chunks).strip()
    lowered = reply.lower()
    flags = sorted({word for word in BANNED if word in lowered})
    print(f"    reply : {reply}")
    print(f"    words : {len(reply.split())}")
    print(f"    verification narration: {flags if flags else 'none'}")


async def ask_for_facts(gateway, question: str, expected: re.Pattern, what: str) -> int:
    """Asked about its own setup, it must find and report the real value.

    The report: the device heard a person poorly and answered with confident inventions. The fix
    is not to refuse the question: it is to look it up. So this fails when the reply does not carry
    the real value, which it can only get by reading the skill.
    """
    print(f"\n--- asked: {question}")
    chunks = []
    async for delta in gateway.ask(question):
        chunks.append(delta)
    reply = "".join(chunks).strip()
    print(f"    reply  : {reply}")
    print(f"    expected {what}")
    if not expected.search(reply):
        print("    FAIL: it did not report the real value")
        return 1
    print("    OK: answered from the skill")
    return 0


async def ask_neutral(gateway, question: str) -> int:
    """A question that has nothing to do with its setup must stay that way."""
    print(f"\n--- asked: {question}")
    chunks = []
    async for delta in gateway.ask(question):
        chunks.append(delta)
    reply = "".join(chunks).strip()
    lowered = reply.lower()
    leaked = sorted({word for word in VOLUNTEER_WORDS if word in lowered})
    print(f"    reply  : {reply}")
    print(f"    configuration it volunteered: {leaked if leaked else 'none'}")
    if leaked:
        print("    FAIL: it volunteered configuration detail nobody asked for")
        return 1
    print("    OK: nothing volunteered")
    return 0


def check_prompt_clauses() -> int:
    """Static checks on the prompt text itself, so a regression fails without a gateway or a key.

    The prompt used to forbid `tool_search`, `tool_describe` and `tool_call` outright, which is the
    only route to a deferred tool, while the voice surface runs at full parity and depends on those
    tools being reachable (issue #18). Pin discovery, preserve the voice-only skill boundary, and
    require explicit permission for requested knowledge-base writes (issue #93).
    """
    problems = 0
    if "never call tool_search" in VOICE_INSTRUCTIONS:
        print("  FAIL: the prompt forbids tool_search again; deferred tools become unreachable")
        problems += 1
    for needed in ("tool_search", "tool_describe", "tool_call"):
        if needed not in VOICE_INSTRUCTIONS:
            print(f"  FAIL: the prompt no longer mentions {needed}")
            problems += 1
    blanket_write_bans = (
        "channel is READ-ONLY",
        "never write or modify files",
        "never run a command that changes anything",
    )
    for forbidden in blanket_write_bans:
        if forbidden in VOICE_INSTRUCTIONS:
            print(f"  FAIL: the prompt still blocks requested knowledge-base writes: {forbidden}")
            problems += 1
    for needed in (
        "knowledge base",
        "Obsidian",
        "smb-share-access",
        "obsidian",
        "state-changing request",
        "one concise clarifying question",
    ):
        if needed not in VOICE_INSTRUCTIONS:
            print(f"  FAIL: the prompt does not explain the knowledge-base write path: {needed}")
            problems += 1
    write_contract = (
        "preserve the existing note",
        "make the requested write, and verify it before confirming briefly",
        "Normal action gates, approvals, privacy rules, and safety boundaries still apply",
    )
    for needed in write_contract:
        if needed not in VOICE_INSTRUCTIONS:
            print(f"  FAIL: the operative knowledge-base write contract is missing: {needed}")
            problems += 1
    explicit_write_denials = (
        "do not make the requested write",
        "never make the requested write",
        "must not make the requested write",
    )
    for forbidden in explicit_write_denials:
        if forbidden in VOICE_INSTRUCTIONS.lower():
            print(f"  FAIL: the prompt explicitly denies the requested knowledge-base write: {forbidden}")
            problems += 1
    if "never call skill_manage" not in VOICE_INSTRUCTIONS.lower():
        print("  FAIL: the no-skill_manage rule is gone")
        problems += 1
    if problems == 0:
        print("  OK: discovery and requested KB writes permitted; skill_manage still refused")
    return problems


async def main() -> int:
    print("prompt in use now (excerpt):")
    for line in VOICE_INSTRUCTIONS.split("  "):
        if "smart home action" in line or "double checked" in line:
            print("   ", line.strip())
    gateway = VoiceGateway(
        base_url=GATEWAY_BASE_URL,
        api_key=read_api_key(GATEWAY_ENV_FILE),
        # A fresh session every run: an answer carried over from a previous run would look like a
        # pass without the model having read anything.
        session_id=f"voice-prompt-test-{os.getpid()}",
        model=VOICE_ROUTE_ALIAS,
        instructions=VOICE_INSTRUCTIONS,
        run_timeout=120.0,
    )
    problems = check_prompt_clauses()
    async with gateway:
        # The action questions ACTUATE the real devices in the house. They are the ones that test
        # the no-narration rule properly, since only an action reply invites "I verified it", but a
        # check script that turns an outlet off every time it runs is a hazard rather than a test.
        # Opt in with --include-actions.
        if "--include-actions" in sys.argv:
            await ask(gateway, "Turn off the Christmas outlet")
            await ask(gateway, "Turn off the Christmas outlet")
            await ask(gateway, "What is the porch thermometer reading?")
        for question, expected, what in FACT_CHECKS:
            problems += await ask_for_facts(gateway, question, expected, what)
        problems += await ask_neutral(gateway, NEUTRAL_QUESTION)
    print(f"\n=== {'ALL CHECKS PASSED' if problems == 0 else f'{problems} PROBLEM(S)'} ===")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
