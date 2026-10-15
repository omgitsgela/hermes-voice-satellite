# LED phases

The satellite speaks in lights as well as in tone and voice. This document is the vocabulary: what
each phase means, what colour it is, and how long it lasts.

**Both satellites use the same vocabulary.** The Raspberry Pi satellite drives its 3 × APA102
from `pi/2mic_service.py`; `wyoming2` (the ESP32 + XVF3800) drives its 12 × WS2812 ring from
`firmware/esp32/components/wy_led/`. The colours and the meanings are copied deliberately, so
someone who learns the lights on one device has learned them on both. If you change a colour,
change it in all three places: here, `wy_led.h`, and `pi/2mic_service.py`.

## The phases

| Phase | Colour | Effect | Duration | What it means |
|---|---|---|---|---|
| **Boot** | white | breath (pulse) | until the host connects | "I am alive and starting up" |
| **Ready** | green | single, ×3 flash | 3 × 0.3 s on / 0.3 s off | first connection of this boot: "I am ready" |
| **Reconnect** | black | off | — | a reconnect is silent |
| **Wake** | rainbow | rainbow (circular sweep) | 400 ms | "I heard you" |
| **Voice started** | yellow | single | 500 ms | the microphone is open |
| **Listening** | black ring + white marker | **direction of arrival** | while the query runs | the bright point is where the person is |
| **Thinking** | violet (150, 70, 255) | breath (glow) | while the answer is worked out | "I am thinking" |
| **Speaking** | violet (150, 70, 255) | single | while the answer plays | the answer is audible |
| **Follow-up** | dim teal (0, 64, 64) | single | until the window closes | keep talking, no wake word needed |
| **Idle** | black | off | — | nothing is happening |
| **Trouble connecting** | red | single | until it reconnects | the link is genuinely gone |
| **Error** | red | single | until the host reconnects | something is actually wrong |

There is no separate "Transcript" look: on the reference satellite a transcript briefly shows green
and then the working glow begins, while here the arrival of the transcript **is** what starts the
thinking glow. The green one-second flash exists in the core (`wy_led_transcript`) but the live
mapping goes straight to the glow, because a beat of green between the marker and the violet reads
as noise rather than as progress.

A disconnect goes **dark immediately**. Whatever the ring was showing — speech, a flash, the ready
handshake — was about a session that no longer exists, and leaving it lit would claim otherwise for
the whole grace period. Red then appears only if the outage survives the 30 seconds.

**Red, once earned, is not replaced.** Only a reconnect releases it. A stray idle arriving after
the outage has gone red used to blank the one message the device was trying to send, so the room
read "everything is fine" while the link had been dead for minutes. A repeated disconnect does not
restart the 30-second clock either — the supervisor notifies twice and a retry loop notifies many
times, and each used to push the red further into the future.

**Error is not an outage.** Red at once says "the host said no"; red after 30 s says "I cannot
reach it". A refusal is followed almost immediately by ordinary disconnect handling, and that used
to overwrite the more important message with darkness.

## The two behaviours worth getting right

**Red means an outage, never a blink.** A cable that wobbles for half a second must not paint the
room red. So a disconnect shows *nothing*, and red only appears if the link stays away for
**30 seconds** (`WY_LED_DISCONNECT_GRACE_SECONDS` on the Pi,
`WY_LED_DISCONNECT_GRACE_MS` on the ESP32). Any reconnect cancels the pending red. This was
learned the hard way: a blink for every reconnect trains you to ignore red entirely.

**The startup look ends when the host connects.** The boot pulse is replaced by the green ready
flash at the moment the device is genuinely usable. A light show that outlives its welcome is
noise, and a pulse that runs forever reads as "something is broken" rather than "still starting".
On the Pi this is gated by a `/run` marker so a service restart cannot replay it; on the ESP32 the
state lives in RAM, so it is once per boot by construction.

## Direction finding

The ring runs one of five effects: off, breath, rainbow, single colour, and **direction of
arrival**. The XMOS computes the direction itself, from the four-microphone array, and the DoA
effect centres its bright marker on the *auto-selected beam* — the direction it has decided speech
is coming from. So "point at the person who is talking" needs no angle maths of our own.

On activation the ring runs **one animation of three stages**: a rainbow sweep for as long as the
ding rings (400 ms), a yellow "the microphone is open" beat (500 ms), then the direction marker for
the query. Sweep during the tone, acknowledge, point at the person — that progression is the whole
design intent.

The three stages are chained from `wy_led_wake()` rather than driven by three callers, because the
session announces "the microphone opened" at almost the same instant the animation starts. A naive
handler truncates the rainbow every single time. That announcement is accepted and politely ignored
while the chain runs, and it is genuinely needed when a stream opens with **no** wake animation at
all (a bridge-driven `wake-now`), where it is the only thing that shows.

**The sweep is measured from the moment the tone is queued, not from the moment it is heard.** The
playback pump has to prime its DMA and settle the amplifier first, which typically costs 50–150 ms,
so the audible tone runs later and longer than its 308 ms of rendered audio. The sweep is 400 ms
to cover the audible window rather than the render length. Making it exact would mean notifying on
playback *onset* rather than on enqueue, which is the honest fix and is not done yet.

The four beams can be read back (`AEC_AZIMUTH_VALUES`: focused 1, focused 2, free-running,
auto-selected, in radians), and `led_status_read_doa()` exposes the auto-selected one for
diagnostics.

## Which phases are live today

**Every phase is wired.** This is full parity with the reference satellite: there is no light that
one device can show and the other cannot.

A turn therefore reads: **rainbow** for the length of the ding, a brief **yellow** while the
microphone opens, the **bright marker** sitting on whoever is talking, a **violet glow** while the
answer is worked out, **violet** while it is said, then dark. A follow-up window shows dim teal;
a definite refusal from the host shows red at once.

**The wake is one animation, not three competing callers.** Rainbow, yellow and the direction
marker are chained from `wy_led_wake()` so the session's "the microphone opened" announcement
cannot restart or truncate the sweep. That announcement is accepted and politely ignored while the
chain runs, because a stream can also open without a wake animation (a bridge-driven `wake-now`),
where it is the only thing that shows.

**Error is not the same as an outage.** Red appears after 30 seconds for a link that has merely
gone away, and *immediately* for a definite refusal from the host. One says "I cannot reach it",
the other says "it said no".

The wiring runs through an injected callback at the session boundary (`wy_session_set_phase_notify`).
The Wyoming session announces *which phase* a turn is in and the glue decides what that looks like,
so the core never learns about colours and the session core never learns about lights. That matters:
both stay host-testable, and `scripts/test_esp32_phase.py` asserts the exact sequence a turn hands
over, including that thinking is announced **before** the answer starts. A glow that appeared after
the first word would claim the device is thinking while it is talking.

The bridge's own `led-busy` / `led-idle` / `led-followup` commands are honoured too. `led-idle` in
particular is how the bridge cleans up after aborted work and an expired follow-up window, which is
exactly what the device cannot work out for itself: a silent window closed by `run-satellite` now
goes dark instead of leaving the direction marker lit for a query that is not happening.

`WY_PHASE_LISTENING` is the one phase the session does not announce — the wake animation reaches it
on its own, and `scripts/test_esp32_led.py` covers that transition instead.

**One honest caveat on the tail.** Idle is announced at `audio-stop`, when the bridge stops sending,
not when the speaker falls silent: the playback ring still has to drain. The light therefore goes
dark a moment before the audio does. Announcing from the playback pump would be exact but would put
I2C work back on the realtime audio path, which was deliberately removed.

## What the hardware can and cannot do

The XMOS exposes **effect-level control, not per-pixel control**. There is no "make LED 7 full and
LED 6 half", so a fully bespoke per-pixel animation is not available without reflashing the XMOS
firmware, which we do not do. Everything in the table above is a choice among five built-in
effects, plus a colour and a speed.

The commands ride the **same I2C bus as the audio** (SDA 5 / SCL 6, address `0x2C`, resource 20).
There is no new wiring and no new peripheral: the LEDs are one more control write.

| Command | ID | Payload | Notes |
|---|---|---|---|
| `LED_EFFECT` | 12 | 1 byte | 0 off, 1 breath, 2 rainbow, 3 single, 4 doa |
| `LED_BRIGHTNESS` | 13 | 1 byte | 0–255 |
| `LED_GAMMIFY` | 14 | 1 byte | **provisional ID** |
| `LED_SPEED` | 15 | 1 byte | effect speed |
| `LED_COLOR` | 16 | 4 bytes | R, G, B, reserved — four bytes, not three |
| `LED_DOA_COLOR` | 17 | 6 bytes | base colour then marker colour |

The IDs 12, 13, 15, 16 are from Seeed's own published Arduino example. **14 and 17 are inferred**
from the `host_control` command ordering and must be confirmed with one live probe before they are
trusted. They are named constants in `wy_led.h`, so a correction is a one-line change.

## Brightness is on two scales

The fleet-wide setting `led_brightness` is **1–31**, the APA102 global-brightness scale the Pi
uses and the scale in `docs/configuration-plane.md`. This ring wants **0–255**. The device adapter
scales between them (`wy_led_scale_brightness`); the fleet contract does not change per model. 31
is full. There is no `0`: the strip's power rail is a GPIO, so 1 is the dimmest and the ring is
never silently off while a device is meant to be showing something.

## Changing a colour

1. Change the constant in `firmware/esp32/components/wy_led/wy_led.h`.
2. Change the matching value in `pi/2mic_service.py`.
3. Change the table above.
4. Run `.venv/bin/python -B scripts/test_esp32_led.py`, which asserts the palette **byte for byte**
   against this document's intent. A drift between the two satellites fails the suite rather than
   shipping unnoticed.
