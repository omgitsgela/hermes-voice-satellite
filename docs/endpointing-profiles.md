# Endpointing profiles: accommodating hesitant or slower speech

**Where this sits:** stage 4 endpoint of the voice pipeline. The end-to-end map, and every other stage, is in `docs/architecture.md`.

Some people pause in the middle of a sentence: to find a word, to organise a thought, or simply to
breathe. A voice assistant tuned for conversational pace treats a long pause as "they have finished"
and answers early, cutting the rest of the sentence off.

This document describes the optional, explicit endpointing profile that gives a speaker more time.

## What a profile is

Utterance endpointing is the logic that decides when the person has stopped speaking, so their audio
can be transcribed and answered. This project does that with a voice-activity detector: it counts how
long the signal has been non-speech and closes the utterance after a configured silence.

A **profile** is a named, validated set of those timing values. It is applied when the endpointer for
a capture is constructed, so every capture path (ordinary turns, follow-up windows and interruption
captures) behaves the same way.

Two profiles ship:

| Profile | Silence before the utterance closes | Longest utterance | No-speech window |
|---|---|---|---|
| `standard` (default) | the installation's own configured value | the installation's own configured value | the installation's own configured value |
| `pause_tolerant` | 1.8 s | 25 s | 12 s |

`standard` deliberately contains no values of its own: it preserves whatever the host was configured
with, including the `--silence-ms` command-line flag, so the default behaviour of an existing
installation is unchanged.

`pause_tolerant` changes all three bounds together, and the reason matters. If only the silence
window grew, a speaker who pauses would hold the microphone open for longer but a genuinely finished
utterance would still be cut by the length cap, and a silent room would still close at the ordinary
no-speech timeout, which would look like the setting doing nothing. The profile keeps **finite**
limits: this is a longer wait, not an open microphone.

## Turning it on

Three ways, all authenticated and all reversible:

**By voice or by asking the assistant.** The assistant has a tool for it in a normal conversation, and
any natural wording works:

> "wait a bit longer when I'm talking"
> "stop waiting so long after I finish"
> "are you using the slower listening mode?"

**From the Gateway settings endpoint**, with the same bearer token announcements use:

```
GET  /settings
  -> {"ok": true, "follow_up": false, "endpointing_profile": "standard"}

POST /settings  {"endpointing_profile": "pause_tolerant"}
  -> {"ok": true, "endpointing_profile": "pause_tolerant"}

POST /settings  {"endpointing_profile": "standard"}
  -> {"ok": true, "endpointing_profile": "standard"}
```

**By editing the state file** on the host, for a script or a scheduled job:
`state/endpointing-profile` holds one profile name per line. An unreadable or unrecognised value falls
back to `standard`, so a bad file cannot leave the device waiting indefinitely.

The profile is persisted, so it survives a Gateway restart and a host reboot. Only one setting may be
sent per request, and the value is validated against the shipped profile table; an unknown name is
rejected with 400 rather than silently stored.

## What it does not change

- **The wake word.** Nothing about detection or threshold is involved.
- **The no-speech bound.** A silent room still closes; it is not an indefinite listen.
- **The hard ceiling.** An utterance still ends at a maximum length, so a stuck open microphone
  cannot record without end.
- **The default.** Follow-up mode and the standard profile are what a fresh installation uses.

## Testing

Offline, no device required:

```
scripts/test_endpointing_profiles.py       # profile table, persistence, failure paths, settings API
hermes/plugins/voice_satellite/test_voice_control.py   # the assistant-facing actions and read-back
```

The endpointing test drives a scripted voice-activity detector through a two-part utterance separated
by a 1.2-second pause and asserts that the standard profile ends at the pause while the pause-tolerant
profile carries the whole sentence. That is the behaviour the profile exists for, and it is checked
without a recording or a piece of hardware.

A live check on a real device is still worth doing before relying on it in a room: say a sentence with
a deliberate long pause and confirm the device waits for the second half.

## Design notes

- **Profile names are an interface.** They are published in the assistant's tool schema, accepted by
  the settings endpoint and written to a state file. Renaming one breaks a caller, so add a new name
  rather than renaming an existing one.
- **The pause-tolerant bound is a compromise, not a maximum.** 1.8 seconds accommodates a
  word-finding pause without making every turn in a noisy room feel slow, because a noisy room
  supplies enough speech-like frames that the silence counter rarely accumulates anyway. If a room
  turns out to need more, change the value and re-run the tests; the profile table is one place.
- **A profile is applied at construction, so a change takes effect on the next capture.** Nothing is
  threaded through the turn, and an in-flight utterance finishes under the settings it started with.
- **This is an accessibility mode, not a second conversation mode.** It is independent of follow-up
  mode: a profile changes how long an utterance is allowed to be, follow-up mode changes whether a new
  capture is opened at all.
