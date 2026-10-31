# Voice controls: what a person can say, and what answers it

This is the contract for the voice surface. It is enforced in two places and documented here so the
two can be checked against each other: the **fixed phrase matchers** in `gateway/loop.py`, and the
**model's tool surface** plus the **instructions the gateway sends** (`VOICE_INSTRUCTIONS` in
`gateway/config.py`).

The split matters. A control that lives only in a phrase matcher can misfire on nothing — but it can
also only fire on an exact phrase. A control that lives only in the model can understand any phrasing —
but it can also act on a word it merely recognised. Where both exist, the matcher answers first and
the model never sees the turn.

## The controls

| Control | Answered by | Examples that reach it |
|---|---|---|
| Volume (this device) | matcher + model tool | "volume 5", "turn it down", "what's the volume" |
| Follow-up mode | matcher + model tool | "turn on follow up mode", "keep listening after you answer", "stop following up", "is follow-up on" |
| Endpointing profile | matcher + model tool | "use the pause tolerant setting", "use the standard setting", "which profile is active" |
| New conversation | matcher + model tool | "new conversation", "start over", "forget this conversation", or **"yes"** after the assistant offers one |
| Timers / alarms | model tool | "set a timer for ten minutes", "cancel the timer", "what timers are running" |
| Device settings | model tool | microphone gain, LED brightness, wake word, wake threshold |
| Stop / end conversation | matcher | "stop", "that's all", "never mind", "goodbye" |

## The two rules that make it predictable

**1. A bare feature name is not a request to change it.** Hearing "conversation" on its own is not an
instruction to switch follow-up mode on or to start a new conversation. This is written into
`VOICE_INSTRUCTIONS` and exists because of a real failure on 2026-10-30: the transcript arrived as
"Conversation." and the model turned follow-up mode on, leaving the device listening when nobody had
asked. If an utterance only names a feature, the assistant asks one short question or does nothing.

**2. The examples are intent, not a script.** The phrase list tells the model what the controls *are*;
it does not restrict how a person asks. Unusual wording still reaches the right control, which is the
whole reason the model has these tools at all.

## Resetting the conversation

Two paths, deliberately:

- **Exact phrases** ("new conversation", "start over") are matched by the Gateway and rotate the session
  immediately, without a model call.
- **A confirmed offer** — the assistant asks "did you want me to start a new conversation?", the person
  says "yes please" — goes through the `conversation_reset` action of the `voice_satellite` tool, which
  calls `POST /settings {"conversation_reset": true}` and rotates the same way.

Before that action existed, the second path had nowhere to go and the assistant had to answer "I can't
reset the conversation from this side" after asking the question itself.

## Auto-expiry

Hermes' own auto-expiring conversations are disabled, so the satellite would otherwise carry a single
conversation indefinitely. The Gateway rotates on idle instead: `--idle-reset-minutes`, **set to 60** for
this installation by the drop-in
`~/.config/systemd/user/hermes-voice-gateway.service.d/20-idle-reset.conf`. The log line to confirm it
is `resumed conversation is N min old; idle window is 60 min`.

## Where each piece lives

| Piece | File |
|---|---|
| Fixed phrase lists (follow-up, stop, reset) | `gateway/config.py` |
| Matcher order (status before set, deadbands) | `gateway/loop.py::_local_command` |
| Instructions sent with every voice turn | `gateway/config.py::VOICE_INSTRUCTIONS` |
| Model-facing tool schema and description | `hermes/plugins/voice_satellite/__init__.py` |
| Tool dispatch | `hermes/plugins/voice_satellite/voice_tool.py` |
| `/settings` endpoint (follow-up, profile, reset) | `gateway/announce.py` |
| Wake arbitration between satellites | `VOICE_WAKE_CLAIM_WINDOW_MS` in `gateway.env` |

## Host settings this installation runs with

These are host-local (not in the repo) and are recorded here so a rebuild can reproduce them:

- `VOICE_WAKE_CLAIM_WINDOW_MS=500` in `gateway.env` — without it the first satellite to report a wake
  takes the turn, which let a satellite in another room answer a request meant for this one (and take
  the volume command with it).
- `20-idle-reset.conf` drop-in — the 60 minute idle window above.
- `VOICE_DUMP_UTTERANCES=1` in `gateway.env` — **off by default**, kept as a diagnostic: it writes each
  utterance to a WAV under `audio/` so a missing word can be examined instead of guessed at.
