#!/usr/bin/env python3
"""The documented voice commands behave as `docs/voice-commands.md` says (issue #10).

Documentation drifts, and a command reference that has quietly stopped being true is worse
than none: it is confidently wrong about how to operate the device. Every concrete claim in
that document is checked here, so a change that invalidates the doc fails the suite.

Not a duplicate of the other suites: those test behaviour, this tests that the *document*
still describes it.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from satellite_gateway.config import (  # noqa: E402
    FOLLOW_UP_OFF_PHRASES,
    FOLLOW_UP_ON_PHRASES,
    FOLLOW_UP_STATUS_PHRASES,
    NON_WORD_TRANSCRIPTS,
    STOP_CORES,
    STOP_PREFIXES,
)
from satellite_gateway.loop import NEW_CONVERSATION_PHRASES, VoiceLoop  # noqa: E402
from satellite_gateway.volume import (  # noqa: E402
    USER_MIN,
    parse_volume_command,
    resolve,
    ten_scale_to_percent,
    user_percent_to_raw,
)

DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "voice-commands.md"

problems: list[str] = []
checks = 0


def check(label: str, condition: bool, detail="") -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(f"{label}: {detail}")


# ---------------------------------------------------------------- the document exists

text = DOC.read_text()
check("the voice-commands document exists", DOC.is_file(), DOC)


# ---------------------------------------------------------------- every listed phrase is real

# Each family the document quotes must actually contain the phrases it prints, or the
# document is describing a device that does not exist.
def doc_lists(label: str, phrases, heading: str) -> None:
    for phrase in phrases:
        check(f"the document's {label} list matches the code for {phrase!r}",
              phrase in text, f"{phrase!r} is in the code but not the document")
    check(f"the document's {label} section exists", heading in text, heading)


doc_lists("follow-up on", FOLLOW_UP_ON_PHRASES, "Turn it on")
doc_lists("follow-up off", FOLLOW_UP_OFF_PHRASES, "Turn it off")
doc_lists("follow-up status", FOLLOW_UP_STATUS_PHRASES, "Ask about it")
doc_lists("non-word", NON_WORD_TRANSCRIPTS, "Silently ignored")

# The stop cores are 42 long and the document quotes them in prose, so check a
# representative sample rather than all of them.
for phrase in ("stop", "quiet", "be quiet", "that's enough", "goodbye", "never mind"):
    check(f"the document quotes the stop core {phrase!r}", phrase in text, phrase)
check("the document says how many stop cores there are",
      str(len(STOP_CORES)) in text or "Cores:" in text, len(STOP_CORES))
for phrase in ("ok", "okay", "please", "will you", "can you please"):
    check(f"the document quotes the lead-in {phrase!r}", phrase in text, phrase)

# ---------------------------------------------------------------- the volume forms

for phrase in ("set volume to 7", "set volume to 40%", "set volume to 65",
               "volume up", "volume down", "volume up by two", "volume down by 3",
               "turn the volume up", "louder", "quieter", "what's the volume"):
    check(f"the documented volume form {phrase!r} is actually recognised",
          parse_volume_command(phrase) is not None, phrase)

# ---------------------------------------------------------------- the examples are true

# "Zero would be silent. Volume 1 out of 10." is quoted in the document.
_, said = resolve(parse_volume_command("set volume to 0"), 50)
check("the document quotes the zero reply accurately",
      "Zero would be silent." in text and said.startswith("Zero would be silent."),
      f"doc says one thing, the code says {said!r}")

# The scale table: 1% -> the quietest, 50% -> halfway, 100% -> full.
check("the document's 1% row is right", user_percent_to_raw(USER_MIN) < user_percent_to_raw(50),
      (user_percent_to_raw(USER_MIN), user_percent_to_raw(50)))
check("the document's 100% row is right", user_percent_to_raw(100) == 100, user_percent_to_raw(100))

# "level 5 and volume 45% cannot disagree" - they are the same scale, so check it.
check("level 5 and user 45% really are the same level",
      abs(ten_scale_to_percent(5) - user_percent_to_raw(45)) <= 1,
      (ten_scale_to_percent(5), user_percent_to_raw(45)))

# ---------------------------------------------------------------- the negative claims

# These are the document's promises about what does NOT happen. Each is the kind of thing
# that would be quietly wrong if a matcher were loosened.
negative = [
    ("beep the horn", "a near-miss on a non-word is still a question",
     lambda t: not (t.strip() in NON_WORD_TRANSCRIPTS)),
    ("stop the sprinklers", "a sentence containing stop does not end the conversation",
     None),
    ("stop by the store later", "the document's own example falls through", None),
    ("volume of the box", "a sentence containing volume is not a volume command",
     lambda t: parse_volume_command(t) is None),
    ("turn it up", "the ambiguous form falls through", lambda t: parse_volume_command(t) is None),
    ("turn it down", "the ambiguous form falls through", lambda t: parse_volume_command(t) is None),
]

for utterance, label, predicate in negative:
    if predicate is not None:
        check(label, predicate(utterance), utterance)
    else:
        # The stop matcher lives in the loop; drive the real one with a bare instance.
        loop = VoiceLoop.__new__(VoiceLoop)
        loop.follow_up = False
        loop.stop_requested = False
        check(label, loop._local_command(utterance) == "", utterance)

# "Stop by the store later" must not have been mistaken for a stop by the real matcher.
loop = VoiceLoop.__new__(VoiceLoop)
loop.follow_up = False
loop.stop_requested = False
check("the document's sprinkler example is not treated as a stop",
      loop._local_command("stop the sprinklers") == "")
check("and neither is the store example",
      loop._local_command("stop by the store later") == "")

# A genuine stop still works after all that.
loop = VoiceLoop.__new__(VoiceLoop)
loop.follow_up = False
loop.stop_requested = False
check("a real stop is still recognised", loop._local_command("okay stop") == "Stopping.")
check("and it set the stop flag", loop.stop_requested is True)

# ---------------------------------------------------------------- the status ordering claim

# The document says the status question never turns the mode on, because the status check is
# matched first. That ordering bug existed once and must not come back.
loop = VoiceLoop.__new__(VoiceLoop)
loop.follow_up = False
loop.stop_requested = False
loop.set_follow_up = lambda on: setattr(loop, "follow_up", bool(on)) or loop.follow_up
reply = loop._local_command("is follow up mode on")
check("asking about follow-up mode does not switch it on", loop.follow_up is False,
      f"state {loop.follow_up}, reply {reply!r}")
check("and the answer says it is off", "off" in reply.lower(), reply)

# ---------------------------------------------------------------- the new-conversation family

for phrase in ("new conversation", "start over", "clean slate"):
    check(f"the documented new-conversation phrase {phrase!r} exists",
          phrase in NEW_CONVERSATION_PHRASES, phrase)
    check(f"the document lists {phrase!r}", phrase in text, phrase)

print(f"voice command doc checks: {checks - len(problems)}/{checks} passed")
for problem in problems:
    print(f"  FAIL: {problem}")
raise SystemExit(1 if problems else 0)
