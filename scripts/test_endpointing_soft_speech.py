#!/usr/bin/env python3
"""A quiet word in a loud room must not end the turn (Option A, decided 2026-10-30).

The problem this addresses, measured on the device:

    quiet room   floor 129-172    speech median 1278   gate 517    clean transcript
    loud moment  floor 489-638    quiet "new" ~700    gate 1000   first word filtered out
                                 loud "conversation" 1759         rest transcribed

The gate's ceiling is clamped so it can never rise above a person -- that is deliberate, because a
learned threshold above the speaker is how the busy-office case went deaf. The defect was what the
endpointer did with a gated frame: it counted it as SILENCE, so the turn ended at the first quiet
word.

A gated frame where the detector says speech is now SOFT speech: it holds the utterance open and
stays in the captured audio, but it does not count as accepted speech.

Option A, chosen deliberately: soft speech alone does NOT make a turn. A lone quiet word is still
rejected, because the room floor and that word measured within 15 percent of each other -- letting
soft-only audio trigger would have the device answering the fan. The quiet word survives when the
person keeps talking, which is exactly the case that broke.

Run with the Gateway venv:

    .venv/bin/python scripts/test_endpointing_soft_speech.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gateway.endpointing import Endpointer  # noqa: E402

FRAME_MS = 20
checks = 0
failures: list[str] = []


def check(label: str, condition, detail: str = "") -> None:
    global checks
    checks += 1
    if not condition:
        failures.append(f"{label}{(': ' + detail) if detail else ''}")


class ScriptedVad:
    """Answers from a prepared list, so the audio content itself is irrelevant."""

    def __init__(self, decisions: list[bool]):
        self._decisions = decisions
        self._i = 0

    def is_speech(self, frame: bytes, rate: int) -> bool:
        value = self._decisions[self._i] if self._i < len(self._decisions) else False
        self._i += 1
        return value


def frame(level: int) -> bytes:
    """A square wave whose RMS is `level`, so the gate's level test is predictable."""
    return (int(level).to_bytes(2, "little", signed=True)) * 320


def run(decisions: list[bool], levels: list[int],
        no_speech_timeout_seconds: float = 60.0) -> list:
    ep = Endpointer(no_speech_timeout_seconds=no_speech_timeout_seconds)
    ep._vad = ScriptedVad(decisions)
    ep.begin_activation()
    results = []
    for decision, level in zip(decisions, levels):
        result = ep.process(frame(level))
        if result is not None:
            results.append(result)
    return results


QUIET_WORD = 150        # below the gate (threshold_min 250, and higher once the floor is learned)
LOUD_SPEECH = 4000      # comfortably over any gate
ROOM = 60               # the room's own level


def main() -> int:
    # ---- case A: a quiet word at the ONSET, then normal speech (her exact "new conversation")
    # This one is carried by the PRE-ROLL, not by the soft branch: before the first accepted frame
    # everything sits in the pre-roll buffer, so the window has to be long enough to hold a word.
    onset_quiet = 25    # 0.5 s
    loud = 30           # 0.6 s
    tail = 45           # 0.9 s of room, enough to end the utterance (silence_ms 700)
    results = run([True] * onset_quiet + [True] * loud + [False] * tail,
                  [QUIET_WORD] * onset_quiet + [LOUD_SPEECH] * loud + [ROOM] * tail)
    check("an onset utterance was produced", len(results) == 1, f"got {len(results)}")
    if results:
        result = results[0]
        check("and it is not rejected", not result.rejected, f"rejected={result.rejected}")
        # 0.5 s of quiet word + 0.6 s of speech + a 0.2 s tail = 1.3 s. A 300 ms pre-roll would
        # keep only the last 0.3 s of that word, giving 1.1 s, so this threshold is what makes the
        # pre-roll window a real requirement rather than a coincidence.
        check("the whole quiet onset word is in the captured audio", result.duration >= 1.25,
              f"clip is {result.duration:.2f}s for 1.3s expected")
        check("but only the loud part counts as speech",
              abs(result.speech_seconds - loud * FRAME_MS / 1000) < 0.05,
              f"counted {result.speech_seconds:.2f}s")

    # ---- case B: a quiet stretch MID-utterance. This one needs the soft branch: without it the
    # quiet stretch counts as silence and the turn ends in the middle of the sentence.
    quiet_mid = 50      # 1.0 s, longer than the 700 ms silence hangover
    results = run([True] * loud + [True] * quiet_mid + [True] * loud + [False] * tail,
                  [LOUD_SPEECH] * loud + [QUIET_WORD] * quiet_mid
                  + [LOUD_SPEECH] * loud + [ROOM] * tail)
    check("a quiet stretch mid-sentence does not split the turn", len(results) == 1,
          f"got {len(results)} result(s)")
    if results:
        check("and the whole sentence is in the clip", results[0].duration >= 2.0,
              f"clip is {results[0].duration:.2f}s for 2.2s of speech")

    # ---- Option A: a lone quiet word must NOT make a turn
    soft_only = 120     # 2.4 s, past the 2.0 s soft-run cap
    results = run([True] * soft_only, [QUIET_WORD] * soft_only)
    check("a lone quiet word produces no turn", len(results) == 0,
          f"got {len(results)} result(s)")
    # A stream of soft-only audio must still CLOSE rather than hold the microphone open. Before the
    # first accepted frame this is the no-speech budget's job (8 s by default), not the soft-run cap,
    # which only applies inside an utterance that already has speech.
    long_run = 450      # 9 s, past the 8 s no-speech budget
    results = run([True] * long_run, [QUIET_WORD] * long_run, no_speech_timeout_seconds=8.0)
    check("a long run of soft-only audio closes as rejected",
          bool(results) and results[0].rejected, f"results={results}")

    # ---- regression: ordinary loud speech is unaffected
    decisions = [True] * 40 + [False] * 45
    levels = [LOUD_SPEECH] * 40 + [ROOM] * 45
    results = run(decisions, levels)
    check("ordinary speech still makes a turn", len(results) == 1 and not results[0].rejected)

    # ---- regression: a room of VAD-positive noise still cannot make a turn
    decisions = [True] * 60 + [False] * 10
    levels = [QUIET_WORD] * 60 + [ROOM] * 10
    results = run(decisions, levels)
    check("VAD-positive noise is still refused",
          all(r.rejected for r in results) or not results, f"results={results}")

    print(f"soft-speech checks: {checks - len(failures)}/{checks} passed")
    for failure in failures:
        print(f"  FAIL: {failure}")
    return 1 if failures else 0


raise SystemExit(main())
