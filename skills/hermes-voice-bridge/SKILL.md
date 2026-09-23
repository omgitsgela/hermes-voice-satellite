---
name: hermes-voice-bridge
description: "Use for the Hermes voice satellite bridge and announcements."
version: 1.0.0
author: angela
platforms: [linux]
metadata:
  hermes:
    tags: [voice, wyoming, satellite, announcements, hubitat]
---

# Hermes Voice Bridge

Load this before changing, restarting, debugging, or extending Angela's voice satellite
frontend (wake word, spoken answers, or house announcements). Project history and evidence:
Obsidian `10 Projects/Hermes Voice Satellite/Hermes Voice Satellite.md`.

## If you are the voice assistant, answering about your own setup

This section is written to be read by the voice surface itself, so that a question about its own
hardware or settings gets a real answer instead of a guess. Angela's rule, 2026-09-22: do not
volunteer anything about your own configuration, but when you are asked, look it up and answer.
This is the authoritative summary; the detail and the traps are further down this skill.

- **Wake word:** `ok_nabu` (spoken as "Ok Nabu"), detected on the Pi by wyoming-openwakeword.
  Threshold **0.35**, lowered from openWakeWord's default 0.5 on 2026-09-22 because a lower-pitched
  voice was hard to trigger. It lives in a systemd drop-in on the Pi, not in the service's unit.
- **Microphones:** the Seeed 2-mic HAT on the Pi (`plughw:CARD=seeed2micvoicec`), mono at 16 kHz,
  with the codec's PGA capture preamp at **81 of 119**, which is **40.5 dB of a possible 59.5 dB**.
  It is at 40.5 dB gain, nowhere near maximum. There have been no measurements of one voice against
  another, so no claim about pitch or loudness can be made from data.
- **Speech recognition:** faster-whisper `small.en`, running on `autumn`, not on the Pi.
- **What does your thinking:** the model behind the voice surface is set by the voice route in
  `~/.hermes/config.yaml` (`gateway.api_server.model_routes.voice`). As of 2026-09-22 that is
  `deepseek-flash`. It is configured state, not a fixed fact.
- **Your voice:** Piper `en_US-hfc_male-medium`.
- **What runs where:** the Pi (`wyoming1`, 192.168.0.149) does wake word, microphone and speaker
  only. `autumn` (192.168.0.142) runs the endpointing, the speech recognition, the model call and
  the speech synthesis.
- **Follow-up mode:** off unless someone turns it on. With it on, the device listens again straight
  after each answer, so a conversation continues without the wake word. About **15 seconds** is the
  hard cap; in practice the endpointer's own no-speech window closes it sooner in a silent room.
  Control it with the `voice_satellite` tool, or by saying "turn on follow up mode".
- **Indicators:** blue flash for the wake word, yellow while being listened to, a violet pulse while
  the host is thinking, green flash three times when the host connects.
- **House announcements** arrive at `http://192.168.0.142:8765/announce` and queue behind any
  conversation already in progress.
- **Where her home is:** the Cherry Creek area of Denver. She **works** at CDOT TOC in Golden, so
  Golden is her workplace and not her home: something "nearby" means near home. The street address
  and coordinates are in the `local-service-location-research` skill.

### Business lookups: use `places_search`, never a web search

**Corrected 2026-09-22, after the first write-up of this was wrong.** Asked for the hours of a nearby
Las Delicias, the voice surface answered "Las Delicias North on Conifer Road" with a full set of
hours. That looked like an invention and was not: **Las Delicias North is real** (7610 Conifer Rd,
Denver 80221) and the hours it read out matched that location exactly. Its one error was calling it
the closest, because it had no distance data at all. The nearest is **Las Delicias Glendale, 4301 E
Kentucky Ave, 0.5 miles from home**; North is 9.9 miles away. So the failure was ranking, not
fabrication, and the fix is distance data rather than more caution.

That data now exists. The `google` plugin (the same gog-backed tool as her tasks and calendar) has a
**`places_search`** action: it calls the Places v1 API directly, biased to her home unless another
origin is named with `near`, and returns each place with its address, distance in miles, business
status and real weekday opening hours. Use it for any "nearest", "near me" or "what are their hours"
question, and do not state hours or claim proximity from web search while it is available.

Why the plugin calls the API directly instead of shelling out to gog: **`gog maps places search`
returns only name, address and id, and `maps places details` returns no opening hours and no
coordinates** (verified 2026-09-22). gog's `maps geocode` is still used, to resolve a named `near`
origin. Live checks: `~/.hermes/plugins/google/test_places_live.py`.


## What runs where

- **The Pi** (`wyoming1`, 192.168.0.149) does wake word, mic capture and playback only. It is
  inert unless a server dials in: `event_from_wake` returns early while `server_id is None`.
- **`autumn`** (192.168.0.142) runs everything else in `~/hermes-voice-bridge/`: endpointing,
  local STT, the model call, and Piper TTS.
- The bridge is a user service: `systemctl --user restart hermes-voice-bridge.service`, logs at
  `journalctl --user -u hermes-voice-bridge.service`, events under `logs/`.
- Model routing for the voice surface is `gateway.api_server.model_routes.voice` in
  `~/.hermes/config.yaml`. As of 2026-09-22 it is **`deepseek` / `deepseek-flash`** (Angela's request),
  previously Cerebras `qwen-3.8-27b`. Treat it as live state, never as a fact: read the config.
  **A change here needs a gateway restart to take effect** (the API server reads routes at startup,
  so the config can be new while the route is still the old model), and `hermes config set` refuses
  this path as an unrecognised key, so edit the file surgically with a backup and a parsed read-back.
  Do not put that API server on the LAN.

## Announcements (the house can speak)

```
POST http://<autumn>:8765/announce      Authorization: Bearer <state/announce-token.txt>
{"text": "...", "source": "hubitat", "key": "dedup-key", "chime": true}
```

From Hermes or a cron job, use the helper with absolute paths so isolated runs have no ambiguity:

```
/home/angela/hermes-voice-bridge/.venv/bin/python /home/angela/hermes-voice-bridge/scripts/announce.py \
  --text "Freezer temperature is 40 degrees" --source cron --key freezer-40 --quiet
```

Exit 0 accepted, 1 refused, 2 token unreadable. Rules that matter:

- **Reserved for important notifications.** Do not wire chatty status updates to it. The point of
  the feature is that a sound from this speaker means something.
- **A 202 means queued, not spoken.** Delivery is best-effort; failures are dropped and recorded in
  `logs/announcements.jsonl`, never retried.
- **Never put the token in a chat message, a note, or printed output.** It lives in
  `state/announce-token.txt` (mode 600). Read it with a shell substitution; never echo it.
- 400 characters is the limit; longer text is a bug, not a feature.
- Text is spoken verbatim. Never route an announcement through a model to "tidy" it.

Hubitat calls the same endpoint through the `Hermes Announcer` driver (hub driver id 1791), which
declares `Speech Synthesis` so Rule Machine's speak action maps onto `speak(String)`. Source of
truth: `~/hermes-voice-bridge/hubitat/HermesAnnouncer.groovy`. Redeploy by POSTing the record to
`/driver/saveOrUpdateJson` and verifying the stored source hash, which the hub exposes at
`/driver/list/single/data/{id}` without needing a login session.

## The traps that cost real time

- **A second connection to the satellite gets no sound.** The satellite only arms its snd service
  for a client that has sent `RunSatellite`, and sending `RunSatellite` on a second connection risks
  it delivering wake-word detections to the wrong client. Any new speaker (announcements, alerts)
  must share the conversation loop's connection and serialise through
  `SatelliteClient.PLAYBACK_LOCK`, because there is one aplay.
- **Do not wait for `Played` on a shared connection.** The loop's capture consumes events and will
  swallow it. Time the playout locally instead.
- **Gate speech on `turn_started_at`, and treat it as expirable.** Announcements must not talk over
  a conversation, but a turn that dies must not wedge them either, so ignore the flag once it is
  older than `ANNOUNCE_TURN_MAX_AGE`.
- **Never hold a playback lock across a long timeout.** A stalled playback holding the lock stalls
  every later answer, including Angela's conversations. Bound the wait and reset the connection.
- **A name imported inside one method is not visible in another.** This cost real time on
  2026-09-22: the release of `PLAYBACK_LOCK` lived in `TtsStream.__aexit__` and referenced a name
  imported only in `__aenter__`, so it raised `NameError` inside its own `finally`, the lock never
  came back, and every later answer blocked forever. To Angela that looked like a stuck device: it
  woke, listened, transcribed and said nothing. The same trap then caught an `asyncio.wait_for` added
  to a module that imported `asyncio` only inside a method. Run
  `scripts/check_playback_lock.py` after touching playback; it is silent and takes a second.
- **Gate service restarts on the check passing.** Restarting in the same command as a failing test
  once pushed broken code live. Run the check first, then restart only if it passed.
- **Make a lock acquire fail loudly.** `TtsStream` waits 30s for `PLAYBACK_LOCK` and raises a named
  error, so a wedged lock appears in the log instead of as silence.
- **Verify audio actually reached the speaker.** The bridge logs "playing <file>"; the Pi's journal
  shows `Playing raw data 'stdin'` when aplay starts. A send without that line means silence.
- **Session rotation is the bridge's job, not Hermes'.** Hermes' own `session_reset` settings are
  dead config (the docs state gateway conversations never reset). The bridge rotates on a spoken
  "new conversation" and after the idle window, and both must stay silent: the device must not
  announce its own housekeeping.
- **The voice-surface toolset is deliberately narrow** (`web`, `skills_read`, `hubitat`, `google`,
  `geolocation`, `voice_satellite`) and now assembles to exactly 8 tools with **no `tool_search`
  bridge**: any single deferrable tool drags that bridge in (three tools, ~760 tokens, and a detour
  that has cost seconds of silence), so every tool the surface can use is listed in
  `_HERMES_CORE_TOOLS`. `vision` was removed for the same reason: a voice surface never sees an image.
  Verify through `_get_platform_tools` plus `is_deferrable_tool_name`, never the raw saved list.

## The voice prompt is tuned for the ear, not the page

`VOICE_INSTRUCTIONS` in `bridge/config.py` is the behavioural contract for the voice surface: at
most two short sentences, no markdown or lists, no follow-up questions, and for a smart home action
**one line naming the action with no verification narration**. Angela asked for that last part
explicitly: the reply arriving at all is the confirmation, so saying "I double checked" wastes the
only thing a voice channel is short of. The check itself still happens, silently.

The `angela-hubitat` skill carries a matching line for the voice surface: if you change either the
prompt or that skill line, keep the two in step, or a future session will reintroduce the narration
by following the skill.

**It does not volunteer its own configuration, but it answers when asked** (Angela's rule,
2026-09-22, after Michael asked why it heard him poorly and got three confident inventions: "her
microphone is already running at maximum sensitivity", "the current setting is even tuned to the most
sensitive setting", and an offer to dig into logs it cannot read).

**The first fix was wrong and is worth remembering.** The prompt was changed to forbid analysing its
own configuration at all and to decline when asked. Angela corrected it the same day: being asked is
exactly when it *should* look, and refusing was not what she wanted. What she wanted was no
*volunteering*. The prompt now says never to bring up its own setup, hardware, settings, models, logs
or way of working, and never to open a reply with a caveat about its own limits -- but when someone
does ask, to look it up with `skill_view` on this skill and answer from what it finds, numbers
included, without guessing at a setting or a cause.

That makes **the summary section at the top of this file load-bearing**: it is the reference the voice
surface answers from. Update it whenever a value changes, or the device will confidently recite a
stale number. It carries the wake threshold, the mic preamp level, the STT and TTS models, what runs
where, the LED states and the announcement endpoint.

`scripts/check_voice_prompt.py` guards both halves. Six questions, in a fresh session each run so
nothing can be inherited from an earlier run:

- "What is your wake word threshold?" must produce **0.35**.
- "Are your microphones at maximum sensitivity?" must produce the **real preamp level**, not a guess.
- A neutral question must not drag configuration into the reply at all.
- The smart-home questions still check for verification narration.

Lessons from building those checks:

- **Do not match wording with phrase lists.** Three live runs produced "I can't peek at my own
  microphone", "I can't see my own microphone" and "The mic preamp is at about 41 dB" -- all correct,
  and the first two read as failures against a phrase list. Assert on the value you expect instead;
  a number that can only come from the skill is a far stronger signal than any phrasing.
- **A persistent test session hides real failures.** The first version reused one session id, so an
  answer could be carried over from an earlier run and look like a pass. Use a fresh session.
- **Watch the words, not the intent, when it answers a neutral question.** Canberra is the whole
  correct reply; any mention of threshold, mic, model or settings there is the volunteering bug.


To test prompt wording without talking to the device:

```
/home/angela/hermes-voice-bridge/.venv/bin/python /home/angela/hermes-voice-bridge/scripts/check_voice_prompt.py
```

It drives the real gateway path in its own session and prints text only, so nothing is spoken and
her conversation history is untouched.

## The LEDs (the device's only non-audio feedback)

The LEDs are three APA102s on SPI, owned by `2mic_leds.service` running the Pi's
`~/wyoming-satellite/examples/2mic_service.py`. It is a **single writer**: never write SPI from
another process, or frames interleave and pixels corrupt. Every state change goes through its
`LEDController`, which owns one solid colour plus one pulse, so any incoming event cancels an
animation instead of fighting it.

What each state means, and who drives it:

| State | Trigger |
|---|---|
| Gentle white pulse | This boot, until the host connects (`SatelliteConnected` cancels it) |
| Blue, 1 s | `Detection`, wake word |
| Yellow | `StreamingStarted`, and `VoiceStarted`: Angela is being listened to |
| **Violet pulse** | `led-busy` from the bridge: the host is thinking |
| **Steady dim teal** | `led-followup` from the bridge: listening for a follow-up reply, no wake word |
| Dark | `led-idle` from the bridge when the answer starts, or any idle event |
| Green flash x3 | `SatelliteConnected` |
| Red held | `SatelliteDisconnected` |

**The bridge publishes the "thinking" state, not the Pi.** This was got wrong first: driving the
pulse from `StreamingStopped` and ending it with `--tts-start-command` looked right and failed,
because that hook fires for **any** audio the bridge sends, and the bridge plays a filler ("One
moment.") as soon as a tool call begins. The filler lands exactly when thinking starts, so the
pulse died one second in and the lights stayed dark through the whole research step. No timer can
fix it either: a fast answer returns in about 2.5 s, so "ignore the first idle" would break that
case instead. Only the bridge knows which audio is the answer, so `bridge/loop.py` sends `led-busy`
before the gateway call, `led-idle` when the first real answer audio goes out, and a safety
`led-idle` in the turn's `finally`. `StreamingStopped` maps back to dark, since it also fires for a
rejected blip or an empty transcript where no work follows.

The satellite carries unrecognised server events straight through to the LED service
(`satellite.py`, `event_from_server`: `if forward_event: await self.forward_event(event)`), so the
bridge's events reach the LEDs with **no extra listener and nothing new on the LAN**. Keep
`--tts-played-command` as a safety net after any playback; do NOT re-add `--tts-start-command`.

Rules learned here:

- **Gate a boot indicator on the boot, not on a service start.** The startup pulse needs both
  `/run/wyoming-led-boot-pulse` and an uptime window, or hand restarts and package updates look like
  power cycles. Same lesson as the power-on tone.
- **Cap every animation.** A turn that dies without replying, or a stream that stops with no answer,
  must not leave the lights breathing all night. Caps: 120 s working, 180 s startup, and the cap logs
  a warning.
- **Log the pulse start and stop at INFO.** Cancelling an animation is otherwise invisible, and that
  start/stop pair in `journalctl -u 2mic_leds` is the only proof the end-of-turn hook fired.
- **Restarting `2mic_leds` makes the bridge re-arm** (a brief stream, and the connected tone), which
  shows up as `led-idle` events. Two extra `led-idle` lines after a restart are expected, not a bug.
- **The Pi needs the venv interpreter to run the service by hand**
  (`examples/.venv/bin/python3`); `/usr/bin/python3` has no `wyoming` module. `led_cmd.py` is the
  opposite: it needs only the standard library.
- **`wyoming-satellite.service` has `Requires=` on `2mic_leds.service`.** Stopping or restarting the
  LED service therefore stops or restarts the satellite too, and a following `start` of the LED
  service does **not** bring the satellite back: a pulse demo once left the whole device dead. After
  any LED-service stop or restart, check `systemctl is-active wyoming-satellite.service`, start it if
  needed, and confirm `Waiting for wake word` in `journalctl -u wyoming-satellite`.

Editing and testing:

```
~/hermes-voice-bridge/pi-led/                       # source of truth on autumn
 2mic_service.py  led_cmd.py  test_led_controller.py
 wyoming-satellite.service  2mic_leds.service      # the units
 *.orig                                             # what the Pi shipped with
/home/angela/hermes-voice-bridge/.venv/bin/python test_led_controller.py   # offline, no device
```

The tests stub `gpiozero` and `spidev`, so they check the animation, cancellation and the boot gate
without touching the hardware. They run fast and should be run before any redeploy. A forced pulse
demo, on the Pi, with the unit stopped first: `--force-startup-pulse --startup-pulse-seconds 20`.
Colour and brightness are CLI arguments (`--pulse-low`, `--pulse-high`, `--pulse-period`), so
tuning needs no code change.

`examples/2mic_service.py` is a **modified upstream example file**: a `git pull` in that checkout
will want to overwrite it, and `~/led-backup-20260922/` plus the patched copy on autumn are the way
back.

**Also modified upstream code, same warning (2026-09-22):**

- `wyoming_satellite/satellite.py` line 872: `_play_wav(done_wav, mute_microphone=True)`. The patched
  copy is at `pi-led/satellite.py` on autumn, and the pre-change copy is in
  `~/backup-20260922-followup/` on the Pi. Without it the done tone is transcribed and answered.
- `examples/led_cmd.py`: gained `followup`, for testing the follow-up state by hand
  (`python3 led_cmd.py followup`). Same backup directory.
- `examples/2mic_service.py`: gained `led-followup` and `_FOLLOWUP = (0, 64, 64)`.

Deploy order that keeps the device alive: copy the files, then `restart 2mic_leds.service`, then
`restart wyoming-satellite.service` (because the satellite `Requires=` the LED service and does not
come back on its own), then confirm three services active and the mic recording.

## The activation window (a blip must not end it)

`capture_after_wake` in `bridge/loop.py` keeps listening through anything that is not an utterance.This exists because of a real report from Michael testing the device (2026-09-22): he said the wake
word, the device chimed, and it cut him off before he spoke. The journal showed a `REJECTED`
endpoint of 0.28 s carrying 0.08 s of "speech" about a second after each wake, followed by him
saying the wake word again.

The cause is the satellite's own wake chime leaking into the mic. Those few frames of noise trip
the endpointer's speech detection, the 700 ms silence timer then closes the utterance while the
speaker is still drawing breath, and the old code stopped the mic and abandoned the activation.
Note the satellite runs with **no VAD flags at all**: the bridge's endpointer is the only thing that
ends a stream, and the `--mic-seconds-to-mute-after-awake-wav 0.1` mute does not cover the chime.

Rules:

- **A rejected blip never ends the activation.** Discard it, keep the mic open, and let the
  endpointer start a fresh utterance (its reset also drops the noise so it cannot glue itself to
  the real speech that follows).
- **A full `no_speech` window does end it.** Silence across the whole window is an empty room, not a
  blip, and listening again would just burn another eight seconds.
- **Bound the wait with `CAPTURE_ACTIVATION_SECONDS`** (30 s), or a wake with nobody there holds the
  mic open indefinitely while blips keep resetting the endpointer.
- `scripts/test_blip_activation.py` guards this; it drives the decision with scripted endpoints and
  needs no device.

## Follow-up mode (keep listening after an answer)

Angela's request, 2026-09-22: "enable a new control mode 'turn on/off follow up mode', which would
cause the device to immediately listen for responses for about 15 seconds on its own after talking
to it when enabled". Then, immediately after: "'turn on follow up mode' should work too. if you give
the agent a tool call script, it should be able to execute that on its own smartly."

**Two control paths, on purpose:**

1. **The assistant, through the `voice_satellite` tool.** The general path, and the one that handles
   any wording: "keep listening after you answer", "stop following up", "are you still listening for
   a reply?". The plugin lives at `~/.hermes/plugins/voice_satellite/` and calls the bridge's
   authenticated `/settings` endpoint.
2. **The bridge, by matching the literal phrases.** Instant, no model call, and it keeps working
   when the gateway is down. `_local_command` in `bridge/loop.py`, phrase sets in `bridge/config.py`.

The exact phrase therefore never reaches the model: the bridge answers it first, and only wording
outside the phrase sets reaches the tool. That is intended, not a gap.

State and behaviour:

- **Off by default**, persisted in `state/follow-up-on` and read at startup, so a restart keeps the
  choice and a cron job or a script can flip it without touching code.
- **The chain**: after a turn, `follow_up_chain` opens a window per answer, bounded by
  `FOLLOW_UP_MAX_TURNS` (10), and stops the moment the mode is turned off. Silence closes it.
- **15 s is a cap, not a promise of 15 s of silence.** The endpointer's own no-speech window closes a
  quiet room sooner. Raise that only if Angela asks for a longer silent tolerance.
- **The window holds the active-turn flag**, so an announcement arriving mid-window waits instead of
  being caught by the open mic and answered. That is the reason for it, not politeness.
- **The lights need no change for it**: the window opens the mic, so the satellite's existing yellow
  "being listened to" state covers it.
- **Turning it on is a behaviour change in the room.** It is hers to switch; do not enable it on her
  behalf.

Live checks, both of which must pass before believing a change here:

```
.venv/bin/python scripts/test_follow_up.py                               # controls, window, chain, bounds
python3 ~/.hermes/plugins/voice_satellite/test_voice_control_live.py     # a real change over the wire
```

The live one reads the change back from the bridge instead of trusting the POST, and it leaves the
mode off. A bug this caught in the first build: the status phrase "is follow up mode on" *contains*
the on phrase "follow up mode on", so asking the question turned the mode on. Status is checked
first now, and a question never changes state.

### Angela reported it broken while it was working (2026-09-22) — read this before "fixing" it again

She said follow-up mode "isn't working properly ... after you give an answer you're just shutting
off", with the mode genuinely on. The journal shows the windows opening and capturing every one of
her follow-ups, so the mechanism worked. Three real problems sat behind that report, and one of them
was the assistant, not the bridge:

1. **No sign it was listening.** The answer ends, `led-idle` goes up, and the window opens in the
dark. To a person watching, the device looks switched off. *Fix: the window publishes
`led-followup`, a steady dim teal that stops pulsing precisely so it cannot be read as "working".*
2. **The device's own done tone was captured as an utterance.** The satellite plays `done.wav`
   (~1.1 s) when the bridge ends an utterance, and `_play_wav(self.settings.snd.done_wav)` was
   called WITHOUT `mute_microphone=True`, unlike the awake tone. The window opens 0.6 s after
   playback, the tone arrives about a second later, and the endpointer returned 0.28 s of "speech"
   which was transcribed as **"beep"** and answered. *Fix: one keyword on the Pi's
   `wyoming_satellite/satellite.py` line 872, giving the done tone the same mic mute the awake tone
   already had. Note there is no `--mic-seconds-to-mute-after-done-wav` flag; the mute mechanism is
   `_play_wav(mute_microphone=True)` and it covers the tone plus
   `seconds_to_mute_after_awake_wav`.*
3. **A rejected blip closed the window.** `capture_follow_up` treated `rejected` as "nothing said",
   so any noise ended the turn the person was about to take. *Fix: discard it and keep listening,
   bounded by the window deadline, exactly as `capture_after_wake` already does.*

And the fourth problem, which is the one to remember: **the assistant invented a limitation.** Asked
about it, it said "Follow-up mode is a setting on the satellite itself, and I can't change it from my
end, so it stays off even after I answer. You'll need to toggle it on the device." Every clause of
that is false, it had the `voice_satellite` tool, and the mode was on. The prompt now says that a
complaint about behaviour is a question about the device: check the real state, never claim a device
setting is something it cannot change.

Lesson worth carrying: **when she says a feature is broken, read her words against the journal before
trusting either one.** The user's report and the log disagreed here, and the truth was three separate
small defects plus one false answer, none of which was "the feature does not work".

Also: `capture_follow_up` must NOT clear `turn_started_at` on the hand-over path. It did in the first
version of the loop, which briefly reopened the announcement gate while the turn was starting. Clear
it only when the window closes with nothing to answer; a test covers this.

### THE HARD LIMIT: a wake-word satellite never streams without a wake (proven 2026-09-22)

**Follow-up mode cannot work in the current architecture, and the reason is in the satellite's design,
not in the bridge.** Everything below was measured on the device, not inferred.

- `WakeStreamingSatellite.event_from_server` handles `Transcript`, `Error`, `RunSatellite` and
  `PauseSatellite` by setting `is_streaming = False`, then calls `trigger_streaming_stop()`,
  `_send_wake_detect()` and logs **"Waiting for wake word"**. Streaming begins from exactly one thing:
  a wake-word `Detection` arriving from the wake service.
- **`--vad` is a no-op here.** With a local wake word in use, the satellite logs
  `WARNING:root:VAD is enabled but will not be used` / `VAD is not used with local wake word detection`.
  VAD streaming lives in `VadStreamingSatellite`, which does not run the wake word at all.
  So "just turn on VAD" is not a fix, and a `--vad` drop-in should not be left on the device.
- Measured with `scripts/diag_followup_stream.py`, which drives the satellite directly with the bridge
  stopped: after `Transcript('')`, with a real spoken wav played out of the device's own speaker three
  times (each confirmed by the satellite), the bridge's side received **0 AudioChunk events, 0.00 s of
  audio**, both with and without `--vad`.

**This also explains the earlier run that looked like it worked.** Each follow-up that was captured
had an awake chime in the Pi journal during the window, i.e. a wake detection really happened (the
bridge absorbs `Detection` events while capturing, so they never appear as "wake word:" in its own
log). Those were wake-word turns wearing a follow-up's clothes. Follow-up mode has never once worked
on its own.

**Angela chose option 3, and it is built and deployed (details below).** The alternatives, kept for
reference:

1. **Move wake detection to `autumn` (recommended).** The satellite's mic audio already flows to the
   wake service continuously, which is how local wake detection works; point `--wake-uri` at a wake
   service the bridge hosts, running openWakeWord on the host. The bridge then owns the wake signal and
   can emit a `Detection` whenever it wants the microphone open, which is exactly what a follow-up
   window needs. The device stays in wake mode, so no audio reaches the bridge outside a capture and
   there is no backpressure problem. Costs: the device needs the host to be woken at all (it already
   needs it for STT and the model), and the 0.35 threshold moves to the host.
2. **Hold the microphone open while follow-up mode is on** (stop sending `Transcript` until the chain
   ends). No new service, but the bridge would have to drain a continuous audio stream even while
   answering a slow question, and the satellite stalls its own event loop when its writes block. That
   risks making the existing playback underruns worse, so it needs a real consumer/drain design first.
3. **A shim wake service on the Pi** that proxies wyoming-openwakeword and can inject a synthetic
   `Detection` on request. Keeps the wake model on the device and its tuning untouched, at the cost of
   one more local service and a control path from the bridge to it.

Do not re-derive this. Start from the measurement above, and do not spend a session re-testing VAD.

### The wake shim, as built (option 3, 2026-09-22)

The bridge now owns "open the microphone". A small proxy sits in front of the wake service and can
write a synthetic `Detection` into the satellite's connection, which to the satellite is exactly a
wake: it chimes, starts streaming and hands the audio to the bridge.

Topology, all loopback on the Pi, all three ports on 127.0.0.1:

| Port | Who listens | Who connects |
|---|---|---|
| 10400 | `wake_shim.py` | the satellite (its `--wake-uri`, deliberately unchanged) |
| 10401 | `wyoming-openwakeword` | the shim, upstream |
| 10402 | `wake_shim.py` control | only `2mic_service.py` |

The control chain, which adds **nothing to the LAN**: the bridge sends a `wake-now` event over the
satellite connection it already owns; the satellite forwards unrecognised server events to the
event/LED service, which is the only path the bridge has to anything on the Pi; that service writes
`wake` to the shim's loopback control port; the shim injects the detection.

Files, all under `pi-led/` on autumn and deployed to the Pi:

- `wake_shim.py` -> `/home/wyoming1/wyoming-satellite/examples/wake_shim.py`
- `wake-shim.service` -> `/etc/systemd/system/wake-shim.service`, run with the **satellite's venv**
  (it needs the `wyoming` package to write well-formed events)
- `wyoming-openwakeword-threshold.conf` -> replaces the Pi's `10-threshold.conf`. **The port and the
  threshold live in one file on purpose**: systemd takes the LAST `ExecStart`, so a separate
  drop-in for the port would silently cancel the 0.35 threshold.
- `wyoming-satellite-requires-shim.conf` -> adds `Requires=wake-shim.service` so the shim is up first.
  Same trap as the LED service: restarting the shim bounces the satellite.
- `inject_wake.py` -> the manual trigger, for testing without speaking to the device.
- `test_wake_shim.py` -> offline checks: byte-faithful satellite audio, intact relayed events,
  valid injected detections, an injection landing safely beside a relayed event, no-satellite
  reporting, and loopback-only defaults.

The upstream direction is relayed **event at a time** rather than byte-copied, so an injected
detection can never land inside a relayed event and corrupt the stream. The satellite direction is a
plain byte copy, because nothing else writes that way.

**A silent follow-up window must hand the satellite back.** A streaming satellite is not listening
for its wake word and its lights stay yellow, so `capture_follow_up` now sends `RunSatellite` on the
paths where no utterance was captured. `RunSatellite` stops the stream and re-arms detection and,
unlike `Transcript`, plays no tone, so a window that heard nothing stays silent. Angela saw the
symptom directly on 2026-09-22: "the satellite right now is stuck on yellow led", after an injection
opened the microphone with the bridge stopped and nothing closed it.

**The injection is intermittently acted on, so the bridge asks twice.** Measured 2026-09-22: with
identical commands, one follow-up window opened the microphone (the yellow LED and the first audio
chunk both appeared) and the next did nothing. `capture_follow_up` therefore sends `wake-now` at the
window's start and again after `FOLLOW_UP_WAKE_RETRY_SECONDS` (1.5 s), cancelling the retry on a
hand-over. This is free: a satellite that is already streaming returns from the wake handler *before*
`trigger_detection`, so a second request while streaming produces no chime and no light change. It can
only matter when the first request did not take. Two checks in `scripts/test_follow_up.py` pin both
halves.

Known and unresolved: the latency between a successful injection and the first audio chunk is
unmeasured. My first attempt at measuring it was invalid (the test client had two concurrent readers
on the stream, which swallows chunks). If Angela reports that the first word of her reply is clipped,
measure that gap before anything else. A valid measurement needs one reader only: do not play a wav
while also counting chunks, because `play_wav` waits for its own `Played` event and eats chunk events
in the meantime.

Also expected behaviour, not a bug: the injection is indistinguishable from a real wake, so **the
awake chime plays each time a follow-up window opens** and the lights flash blue then yellow. If that
reads as chatty, the options are a second silent wake wav for injection, which the satellite cannot
distinguish, or dropping the per-window chime by other means.

**A spoken "stop" ends the conversation, not the mode** (Angela, 2026-09-22: "'ok nabu stop' should be
a way to stop the current follow up conversation, but leave it open to retrigger again"). `STOP_PHRASES`
in `bridge/config.py` covers the forms a person actually uses (stop, stop it, that's all, never mind,
we're done, goodbye, and the wake-word variants). `_local_command` sets `stop_requested`, the chain
consumes the flag, re-arms the wake word and ends, and `follow_up` is left exactly as it was, so the
next conversation follows up again.

Two traps, both hit while building it:

- **Match the whole utterance, never a substring.** "stop" is an ordinary word, and a substring match
  answered "stop the sprinklers" with "Stopping." instead of letting the model handle it. Matching is
  now an optional lead-in from `STOP_PREFIXES` plus exactly one `STOP_CORES` entry, which is what a
  person actually says: "okay stop", "will you stop", "stop listening to me". Sentences with an object
  ("stop the sprinklers", "stop by the store later") never match, and a test drives six of them.
- **Normalise BOTH sides, and collapse whitespace while doing it.** `normalise()` folds punctuation
  into a space, so "Okay, stop." became **"okay  stop" with two spaces** and silently missed the list.
  Angela hit exactly this on 2026-09-22: it answered "Okay, stop." with "Understood, I'll stop the
  Monty Python bit." and carried on. The phrase lists are written the way a person writes them and the
  comparison normalises each entry, which is the only way the two can meet.

**And the one that mattered most: a stop request must never switch the mode off.** Before the matcher
could catch her words, "Stop listening to me!" reached the model, which reasonably read it as "turn
follow-up off" and called the `voice_satellite` tool. She asked for the conversation to end and got the
feature disabled instead. `VOICE_INSTRUCTIONS` now states that a request to stop, be quiet or finish
means end this conversation, with one short line and no question, and that follow-up mode must not be
touched for it. A stop is a conversation-level thing; only the mode itself may change the mode.

**Verify a restart actually happened by its timestamp.** A shell syntax error in the deploying command
let the guard suites run and the restart silently not, so a fix that had passed every test was not
live and the bug appeared to persist (`systemctl show <unit> -p ActiveEnterTimestamp` settles it in one
line). Do not conclude a fix failed until the running process is known to have the new code.

## Reading a slow turn: the log now separates waiting from working

The turn line carries the breakdown, because one number was actively misleading and cost real diagnosis
time on 2026-09-22:

```
turn: process 14.0s (idle 356.5s, listen 8.0s, stt 1.1s, first delta 2.6s, first audio 2.7s; whole 370.5s), ...
```

- **`idle`** is the wait for the wake word. The old single `total` included this, so a turn that sat
  for six minutes read as a turn that took six minutes to answer. Angela asked why her last answer was
  slow and the first thing the log said was `total 356.50s`.
- **`listen`** is wake word to completed utterance, and it is the number that matters most here. If a
  turn feels slow, check this before touching the model: on 2026-09-22 two of her turns ended
  `ended_by=max_length` after listening the full 15 s, because a TV in the room never left the 700 ms
  silence the endpointer needs to close an utterance. The delay was the room, not the model.
- **`process`** is from the completed utterance to the end of the turn: STT plus the model plus
  playback, the part that is actually the answering.

Related: `--idle-reset-minutes` now defaults to **20** rather than 60, so a voice conversation that has
been left alone starts fresh sooner instead of carrying a long history forward. Context size is a
smaller lever than it looks, though: the provider reports ~99% cache hits on these turns, so a 34k
context was not the reason the first token took 3 s.

## Wake word tuning

Detections come from wyoming-openwakeword on the Pi. A false wake is usually ambient speech, not a
self-trigger; confirm which by reading the bridge journal (a `wake word:` line followed by what was
heard) before changing anything.

The service runs with **no threshold flag, so it is on openWakeWord's default 0.5**, and
`--trigger-level` defaults to 1. Do not repeat the old claim that this is "the most sensitive
possible": a LOWER threshold raises sensitivity, so there is headroom. When one particular voice
wakes it only reluctantly (Michael's lower voice, 2026-09-22), the levers in order of cost are:

- **Lower the threshold** (`--threshold 0.35` on wyoming-openwakeword): more sensitive, more false
  wakes, one flag to revert.
- **Raise the codec's mic preamp**, `PGA Capture Volume` on card 1 (`amixer -c 1 cget numid=34`),
  which sits at 81 of 119, i.e. 40.5 dB of a possible 59.5 dB. This lifts the wake score and the VAD
  signal together, at the cost of noise floor and clipping on loud speech.
- **Retrain, or add a custom verifier model** from recordings of that voice. A real build job, not a
  config change.

Changing wake sensitivity, or adding a second wake word, is a behaviour change: propose it and wait
for Angela's word.

### The premade models, and what the wake service can actually be told to do (verified 2026-09-22)

The installed `wyoming-openwakeword` 2.1.0 offers exactly **five** ready models. Ask the install, never
a list from the web:

```
/home/wyoming1/wyoming-openwakeword/.venv/bin/python3 -c \
  "from pyopen_wakeword import Model; print(sorted(m.value for m in Model))"
# ['alexa', 'hey_jarvis', 'hey_mycroft', 'hey_rhasspy', 'okay_nabu']
```

The satellite asks for one by name (`--wake-word-name ok_nabu`), so switching is that flag plus a
threshold retune. Upstream also ships `timer` and `weather` phrase models; the full `--help` is the
authoritative list of what the service accepts, and it is short:

- `--custom-model-dir PATH` is **the only way to load a trained model**. A custom model is named by
  its file's basename, so `myword.tflite` is requested as `--wake-word-name myword`.
- Tuning is `--threshold`, `--trigger-level`, `--refractory-seconds` (default 2). That is all.
- **There is no flag for custom verifier models, Silero VAD, or Speex noise suppression**, even though
  upstream openWakeWord supports all three. Anything advanced needs either a patched service or our
  own, and **the wake shim already sits in that exact audio path**, which makes it the natural place.
  Do not promise a verifier or VAD "setting"; there is no such setting today.

Every model has its own baseline (upstream tunes for 0.5), so a switch always means re-testing the
threshold, and the 0.35 in the drop-in was tuned for `okay_nabu` and Michael's lower voice.

### Changing the wake word, and voice imprint, without guesswork

Three different desires get called "voice detection" and they need different builds:

1. **Make the wake word easier for one particular voice.** Either a **custom verifier model** or
a
   wake model trained with that voice's speech in the data. The verifier is much cheaper than it
   sounds: the official doc asks for **at least 3 wake-word clips per target speaker, ~10 s of that
   speaker's other speech, and optionally ~5 s of room noise or past false activations**. It is a
   logistic-regression filter over the shared features (`openwakeword.train_custom_verifier`),
   packaged as a `.pkl`, and set with `custom_verifier_models={...}` plus
   `custom_verifier_threshold` (about 0.3) when constructing the model.
2. **Limit who can wake it.** Same verifier, but read the official warning as binding: it "is not
   intended to perform the task of speaker verification directly" and needs the deployment acoustics
   to resemble the recorded ones. It also makes the device **less likely to respond to new voices**,
   which is exactly wrong if guests or a not-yet-enrolled household member should be able to wake it.
   Treat it as a false-activation filter, never as security.
3. **Know who is talking.** This is real speaker identification, not a wake-word trick: ECAPA-TDNN
   (SpeechBrain, 0.80% EER on VoxCeleb1-test, 192-dim embeddings, cosine distance) or the lighter
   Resemblyzer, enrolled from seconds of speech (a working practitioner tool uses 5 s). Run it on
   **autumn, not the Pi**, and note that **the bridge already holds every utterance's audio**, so it
   needs no change to the wake service at all. This scales to several people; the verifier explicitly
   should not.

Training a new wake word phrase is a modest job, not a research project: 100% synthetic positives from
Piper TTS plus augmentation with background noise and room impulse responses, so nobody has to record
themselves. Either the official Colab notebook (free tier, roughly 30-60 minutes, exports `.tflite`
and `.onnx`) or the free hosted trainer at openwakeword.com, which can additionally **fine-tune a Piper
voice on Angela's own speech** so the target pronunciation is hers. Practical notes: 3-4 syllables, an
uncommon phrase, English only, and expect a few iterations.

### Browsing the community library, and the rule that follows from it

The community wake-word library lives at openwakeword.com. **The listing is public even though the site
shows a sign-in wall**: the page itself calls this endpoint, and it answers HTTP 200 with no account
(observed 2026-09-22, 1,787 words / 3,169 models at the time):

```
https://openwakeword.com/api/models/community?page=1&per_page=50&sort=recent
# -> {"groups": [...], "total_words": N, "page": N, "per_page": N, "has_more": bool}
```

Walk pages until `has_more` is false. Each group carries `word`, `languages`, `model_count`,
`total_downloads`, `best_rating`, and per-model `has_tflite`, `clean_recall` and
`false_activations_per_hour`. Downloading the file itself does need a free account; reading the
library does not. Script: `/tmp/oww_library.py` (recreate as needed, it is a throwaway).

**Filter on `has_tflite` and the quality fields, not on the name or the download count.** This device
loads TFLite through `--custom-model-dir`, and the library is unmoderated user uploads: it contains
duplicates, junk entries, and models reporting a `false_activations_per_hour` several times upstream's
<0.5/hour target (one sampled entry reported 2.4/hour with `clean_recall` 0.71). A famous name and a
5-star rating are not evidence of a good model.

**Never choose a wake word that collides with the stop phrases.** A published community model is
literally named "stop", and `STOP_PHRASES` in `bridge/config.py` matches whole utterances. A wake word
or response that sounds like "stop", "that's all", "we're done" or "goodbye" would make the device end
its own conversation instead of listening. Check any candidate against `STOP_PHRASES` before installing.

### Switching to a custom wake word: the parts that are not obvious (hay_hermes, 2026-09-22)

**The wake word is now `hey_hermes`** (Angela's choice, from the community library), previously
`ok_nabu`. Threshold deliberately left at 0.35 so only one thing changed; the model's own benchmark
point is 0.5. Rollback is two file restores, listed at the end of this section.

**Angela may swap wake words again, so use the tools instead of re-deriving this:**
`references/switching-wake-words.md` is the runbook (built-in vs downloaded vs trained, the naming
rule, verification order, traps, rollback), `scripts/switch-wake-word.py` does a switch safely (dry
run by default, backups, refuses blind edits, checks the TFLite header, pre-flights the command line,
restarts in the order that works, then verifies the relay chain), and `scripts/probe-wake-model.py`
scores a model with the service's own pipeline before anything is deployed. Copy them to the Pi and
run them there.

Mechanism, read from the service source rather than guessed:

- The wake service takes `--custom-model-dir` (**`action="append"`, repeatable**) and globs
  `*.tflite` in it. The wake word name is the **file stem**, and a stem matching
  `^([^_]+)_v[0-9.]+$` has the version stripped, so `hey_hermes.tflite` is requested as `hey_hermes`.
  The downloaded file's original name (`Hey_Hermes_20260707_013715.tflite`) would have produced an
  unusable name, which is why it is renamed on install.
- The satellite passes `--wake-word-name` straight into its wake request with **no validation**, and
  the service's `Describe` advertises **built-in models only**. So a name that does not match a file
  stem is silently skipped, the service falls back to `DEFAULT_MODEL`, and the device quietly keeps
  its old wake word. Nothing errors. Always confirm the name equals the filename stem.
- `--custom-model-dir` belongs in the same drop-in as the port and threshold, for the reason that
  file already documents: systemd takes the last `ExecStart`.

Pre-flight that caught nothing but proved everything: run the new command line by hand on a scratch
port with `--debug` and look for `Found custom model <name> at <path>` then `Ready`, before touching
the live service.

Verification that works without a human voice, all of it done on the device with the service's own
package (`OpenWakeWordFeatures.from_builtin()` -> `OpenWakeWord.from_model(path)`, 80 ms frames):

| Test | Result |
|---|---|
| custom model on 3 s of silence | 0.0005 |
| built-in `okay_nabu` on the repo's `okay_nabu.wav` | **0.9655** (validates the method) |
| custom `hey_hermes` on that same `okay_nabu` recording | **0.0007** (proves it discriminates, and is not a repackaged built-in) |

What still needs a person: only a real voice proves detection. Piper-synthesised wake words score
around 0.001, so they cannot be used to test this, and a wake model cannot be tested without audio.
Watch the bridge log for `wake word: hey_hermes` once someone speaks it.

**Lifecycle trap found while deploying this.** Restarting the wake service makes the **shim** churn:
`relaying to the wake service` immediately followed by `relay ended: EOF` in a tight loop, during
which the satellite's microphone audio never reaches the wake model and the wake word cannot fire at
all. The shim does not recover on its own. Fix, in this order: `restart wake-shim` (which bounces the
satellite because it `Requires=` it), then **`start` wyoming-satellite** (it does not come back by
itself). Confirm two established legs (`satellite->10400` and `shim->10401`) and no EOF lines in the
shim log before believing it.

**`WARNING:root:Failed to get info from wake service` in the satellite log is benign and
pre-existing.** It is a timeout on the startup `Describe` through the shim (counted 16 times in one
day, including before any wake-word change, alongside 101 successful detections). Do not chase it.

Installed state and rollback:

- Model: `/home/wyoming1/wake-models/hey_hermes.tflite`, 422,132 bytes,
  sha256 `26569aa8d82c287dfab1fcd5dfc29ad87022e4cfaf93ada4e65f555eea884322`. Community model id 2338,
  V3, 0.36 false activations/hour, which is the only one of the four "Hey Hermes" uploads under
  upstream's 0.5/hour target. The widely-downloaded one is the 0.87/hour model and is the only rated
  one, at 1.0.
- Wake service drop-in: `/etc/systemd/system/wyoming-openwakeword.service.d/10-threshold.conf`
- Satellite unit: `/etc/systemd/system/wyoming-satellite.service`
- Both were backed up as `*.bak-hermes-20260922-212534` on the Pi. Revert = restore both, set
  `--wake-word-name 'ok_nabu'`, `daemon-reload`, restart the wake service, then `restart wake-shim`
  and `start wyoming-satellite`. `ok_nabu` needs no model work, it is a built-in.

### What is actually set, and how to change it

**Applied 2026-09-22 by Angela's request: `--threshold 0.35`** (was the default 0.5), after Michael
found the wake word hard to trigger. It lives in a drop-in,
`/etc/systemd/system/wyoming-openwakeword.service.d/10-threshold.conf`, which clears `ExecStart` and
re-declares it with the lower threshold. A drop-in rather than the unit itself, so an update of the
unit cannot silently undo it. **Revert:** delete that file, `daemon-reload`, restart the service.
Confirm the live value with `systemctl show wyoming-openwakeword -p ExecStart`; the unit only exposes
it there, not in `systemctl cat`, because the drop-in supplies the whole line.

- **Piper TTS cannot be used to probe wake sensitivity.** Feeding `en_US-hfc_male-medium` saying
  "okay nabu" to `okay_nabu` peaks at **0.001**, at normal pitch and at two pitch shifts, while the
  upstream `tests/okay_nabu.wav` scores **0.965** through the identical pipeline. TTS is useless as a
  stand-in voice; only real recordings of the person tell you anything.
- **The model name is `okay_nabu`, not `ok_nabu`.** `ok_nabu` is what the satellite passes as
  `--wake-word-name`; `Model("ok_nabu")` raises `ValueError: 'ok_nabu' is not a valid Model`. List the
  real names with `[m.value for m in Model]`.
- To score a real recording with the service's own pipeline: `OpenWakeWordFeatures.from_builtin()`,
  `OpenWakeWord.from_builtin(Model("okay_nabu"))`, feed 1280-sample chunks, take the peak of the
  returned probabilities, and compare against the threshold. Run it with the service's own venv,
  `/home/wyoming1/wyoming-openwakeword/.venv/bin/python3`, or the imports fail.

## Safety

- Reading state, logs, and this project's config is routine. Changing wake sensitivity, the voice
  toolset, or model routing is a behaviour change: propose it and wait for Angela's word.
- The gold card image and its restore procedure live in the `remote-system-operations` skill
  (`references/sd-card-gold-image.md`). The device gets hard power cuts routinely.
