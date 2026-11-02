# Quick reference: using the satellite by voice

One page for anyone new to the device. Everything here is enforced and tested in the code; the
long-form documents are named at the end of each section.

## Starting a conversation

Say the wake word (this build's default is **"hey hermes"**), then your sentence. The LED and the
activation tone tell you it heard you. With follow-up mode on, the mic reopens for about 15 seconds
after each answer, so a short exchange stays one conversation without repeating the wake word.

## Volume (answered instantly, works even if the internet is down)

| You say | What happens |
|---|---|
| "set volume to 7" | level 7 on a 1-10 scale |
| "set volume to 40%" | 40 percent of what the speaker can actually do |
| "set volume to 65" | a bare number above 10 can only be a percentage |
| "volume up" / "louder" / "quieter" | one step up or down |
| "volume up by two" | that many steps |
| "what's the volume" | states the level, changes nothing |

Zero is refused on purpose: the device raises it to the quietest audible level and says so, because
a silent voice device cannot be talked back up. Alarms may raise the volume so they are always
heard, and never lower a level you chose. Details: `docs/volume.md`, `docs/voice-commands.md`.

## Follow-up mode

- On: "turn on follow up mode" · "follow up on" · "keep listening after you answer"
- Off: "turn off follow up mode" · "stop following up"
- Status (never changes anything): "is follow up mode on"

## Ending or restarting a conversation

- End: "stop" · "quiet" · "that's all" · "never mind" · "goodbye" (whole-utterance matches only, so
  "stop the sprinklers" goes to the assistant instead of shutting the device up)
- Fresh start: "new conversation" · "start over" · "forget this conversation" · or say "yes" when
  the assistant offers one
- Idle conversations rotate automatically after 60 minutes.

## Timers and alarms

- "set a timer for five minutes" · "set a timer for three minutes called check the popcorn"
- "how long is left on the popcorn timer?" · "what timers are running?"
- "cancel the popcorn timer" · "cancel all timers"
- On expiry the satellite says "Your timer is done." (or names it).
- Alarms are separate: set them with a weekday or time phrase ("wake me at 6:30 on weekdays");
  a bare "6:30" is refused because the day is ambiguous. Details: `docs/timers-and-alarms.md`.

## Device settings (through the assistant, one short line back)

Say it naturally; the assistant has tools for this satellite's own settings:

- microphone gain ("turn the microphone up a bit")
- LED brightness ("make the lights dimmer")
- wake word and wake threshold ("lower the wake sensitivity")
- endpointing profile: "use the pause tolerant setting" / "use the standard setting" /
  "which profile is active" (how long a pause ends your turn)

Rules that protect you: a bare feature name is not a request ("conversation" alone does nothing),
a question never changes state, the assistant never invents a value it didn't hear or read from the
device, and a request about one device is never answered by touching another. Details:
`docs/voice-controls.md`, `docs/endpointing-profiles.md`, `docs/configuration-plane.md`.

## Interrupting a running task

Wake the satellite while the assistant is working: a stop phrase ("stop", "that's all") ends the
run and acknowledges it; anything else is passed to the running task as a correction, so it carries
on with your new information. Details: `docs/interruption.md`.

## Announcements (the house talking to you)

Any process — a cron job, a home-automation rule, Hermes itself — can POST to the Gateway's
announcement endpoint and the satellite speaks it, queued politely behind any live conversation.
That speaker activating *is* the signal. Details: `docs/architecture.md` and the announcement
routes in `gateway/announce.py`.

## What deliberately does NOT happen

- "turn it up/down" is not volume control — "it" could be anything; say "volume".
- Non-words ("beep", "ding", "chime") are silently dropped; the device never answers its own tone.
- Partial or garbled speech ("...um, seven") never changes a device; you get one short question.
- Raw Wyoming on the satellite's port 10700 is open to your LAN by design: never expose it to the
  internet. Details: `docs/voice-commands.md`, `docs/outbound-link.md`.

## Seeing what the device heard

```bash
journalctl --user -u hermes-voice-satellite-gateway -f | grep -E "heard|said|local control"
```

## Changing how the device is configured (host side)

- One host config file: `satellite-gateway.env` at the checkout root (voice, wake, routes, registry).
- Change the voice: `scripts/switch-voice.py <voice>` (both surfaces, with `--rollback`).
- Change the wake word: `docs/switching-wake-words.md` (model file and selector move together).
- Upgrade an existing install: `scripts/update-gateway.sh`.
- Per-device desired settings (wake word, threshold, and so on) go through the configuration plane:
  `docs/configuration-plane.md`.

## Where the whole picture lives

`docs/architecture.md` is the map of a turn: capture, wake, stream, endpointing, transcription,
model call, speech, playback. Every other doc names its stage in that pipeline.
