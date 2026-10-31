# Voice commands

**Where this sits:** stage 6 think / bridge-local of the voice pipeline. The end-to-end map, and every other stage, is in `docs/architecture.md`.

What you can say to the satellite, what happens, and why some things are deliberately not
matched.

This documents the **bridge-local** commands: the ones the Gateway answers itself, instantly,
without calling a model. Everything else you say goes to the assistant as a normal question.

## Two classes of command, and why it matters

| | Bridge-local | Everything else |
|---|---|---|
| Answered by | the Gateway, on the spot | the assistant, over the gateway |
| Latency | immediate | a second or two |
| Works when the gateway is down | **yes** | no |

Bridge-local commands exist for the things that must not fail: changing the volume, ending a
conversation, and switching follow-up mode. If the internet is out or the model is slow, these
still work, because a device you cannot quieten or turn down is worse than one that cannot
answer a question.

The design rule behind all of them: **a whole-utterance match, never a careless substring.**
"stop" is an ordinary word, and a loose match would answer "stop the sprinklers" with
"Stopping." So the Gateway only acts when the whole utterance is a command, or matches a
narrowly-anchored pattern. Anything ambiguous falls through to the assistant, which is
slower but understands context. That trade is deliberate: a mis-heard command that changes
the room is worse than an extra second of thinking.

## Volume

Say any of these and the Gateway answers immediately with the resulting level.

| You say | Meaning |
|---|---|
| "set volume to 7" | level 7 on a 1-10 scale |
| "set volume to 40%" | 40 percent |
| "set volume to 65" | 65 percent — a bare number above 10 can only be a percentage |
| "volume up" / "volume down" | one step |
| "volume up by two" / "volume down by 3" | that many steps |
| "turn the volume up" / "turn the volume down" | one step |
| "louder" / "quieter" | one step |
| "what's the volume" | the current level; changes nothing |

### Both scales span what the hardware can actually do

The satellite's speaker cannot reproduce anything below a measured floor, so **both scales exist
only within the range that is audible**. On the current device that floor is 50% of the raw ALSA
range:

| You say | Raw ALSA | What it is |
|---|---|---|
| "volume 1%" or "level 1" | 50% | the quietest the device can be |
| "volume 50%" | 75% | halfway up what the hardware can do |
| "volume 100%" or "level 10" | 100% | full volume |

So "volume 30%" means 30% of what this speaker can do — not 30% of some raw scale that would be
inaudible. The two scales are the same range at different granularities, so "level 5" and
"volume 45%" cannot disagree.

### Zero is refused, on purpose

**"Set volume to 0" does not silence the device.** It is recognised, raised to the quietest
audible level, and the reply explains why:

> "Zero would be silent. Volume 1 out of 10."

This is not a limitation, it is the point. A voice device at zero volume is unreachable: you
cannot talk it back up, because you cannot hear it confirm anything. No reply, no tone, no
alarm. So there is a floor, it is above zero, and nothing you can say goes below it.

### Alarms override upward

An alarm may raise the room to a minimum level so it is always audible. That override is
**upward only**: if you have the volume louder than the minimum, an alarm leaves it exactly
alone. Lowering a level you chose would make the override a surprise rather than a safeguard.

## Follow-up mode

Follow-up mode makes the device keep listening for about fifteen seconds after each answer, so
a conversation continues without saying the wake word every time. Off by default, because it
changes how the device behaves in the room.

**Turn it on:**

> turn on follow up mode · turn follow up mode on · turn follow up on · follow up mode on ·
> follow up on · enable follow up mode · start follow up mode · switch on follow up mode

**Turn it off:**

> turn off follow up mode · turn follow up mode off · turn follow up off · follow up mode off ·
> follow up off · disable follow up mode · stop follow up mode · stop following up ·
> switch off follow up mode · end follow up mode

**Ask about it** (a question never changes the state):

> is follow up mode on · is follow up on · follow up mode status · check follow up mode

The status check is matched **before** the on-phrases, because "is follow up mode on" ends with
"follow up mode on" and checking the on-phrases first made the question turn the mode on.

## Ending a conversation

Ending the current follow-up conversation hands the device back to waiting for its wake word,
**without switching follow-up mode off** — the next conversation follows up again.

These are matched as a whole utterance: an optional lead-in (like "okay", "please", "will you")
followed by exactly one core. That is what lets "will you stop" and "stop listening to me" work
while leaving "stop the sprinklers" to the assistant.

**Cores:** stop · stop it · stop now · stop that · stop listening · stop listening to me ·
stop talking · stop talking to me · stop following me · stop replying · please stop · just stop ·
quiet · be quiet · hush · shush · shut up · silence · enough · that's enough · that's all ·
that is all · that'll be all · that's it · never mind · we're done · i'm done · goodbye · bye ·
good night

**Lead-ins:** ok · okay · please · hey · now · just · and · so · um · alright · all right ·
will you · could you · can you · would you · okay now · can you please · please just

So "okay stop", "will you stop", and "please just be quiet" all work. "Stop by the store later"
does not, because it is not one of these whole utterances.

## Starting a new conversation

Rotates the conversation, so the assistant starts fresh instead of carrying old context.

> new conversation · start a new conversation · start over · forget that ·
> forget this conversation · reset the conversation · clear the conversation ·
> fresh conversation · clean slate

Unlike the commands above, these are matched as substrings, because they are phrased more
freely and none of them is a likely fragment of an ordinary question.

## Silently ignored

Non-words are recognised and dropped **without a spoken reply**. This is a defence against the
device answering its own wake tone: a false wake can capture the chime, transcribe it as "beep",
and the device would otherwise answer out loud into the room. Silence is the correct reply to
something that was never speech.

> beep · beeps · beep beep · boop · boops · bleep · bleeps · blip · blips · ding · dings ·
> ding ding · tone · tones · buzz · buzzer · chime

Matched as a whole utterance, so "beep the horn" is still a question.

## Deliberately NOT matched

These probably do mean the device, but they fall through to the assistant on purpose:

| Not matched | Why |
|---|---|
| "turn it up" / "turn it down" | "it" could be music playing on the same device, and the Gateway cannot tell the difference |
| "volume of the box" | the word "volume" appears, but the intent is not the speaker level |
| "stop by the store later" | "stop" is an ordinary word; only whole-utterance forms end a conversation |

`turn the volume up` and `volume up` both work, because neither leaves the object ambiguous.

The test suites pin all of this: `scripts/test_volume.py` and `scripts/test_non_word.py`.

## Finding out what the device heard

The Gateway logs every turn:

```
  heard: set volume to 7
  said : Volume 7 out of 10.
```

```bash
journalctl --user -u hermes-voice-bridge -f | grep -E "heard|said|local control|volume"
```

`local control` and `volume control` in the log mean a Gateway-local command was recognised;
anything else went to the assistant.

## Adding a command

1. Add the phrase to the relevant list in `bridge/config.py`, or, for a new class, alongside
   the existing matcher in `bridge/loop.py`.
2. Decide the matching rule deliberately: whole-utterance for anything that changes the room,
   substring only for phrases that cannot plausibly appear inside a real question.
3. Add it to the test suite for that class, **including a negative case** proving a near-miss
   falls through. Every command here has one, because the failure that matters is not "it
   missed my phrase" but "it acted on something I did not say".
