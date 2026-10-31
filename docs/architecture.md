# The voice pipeline, stage by stage

**What this document is for.** Every other file in `docs/` explains one part of the system in depth.
This one is the map: what runs at each stage of a turn, which piece does it, where that piece came
from, and whether it is ours to maintain. Read this first, then follow the pointer for the part you
are changing.

Verified against the running system on 2026-09-25. When a stage changes, change it here.

## Two directions, one device

A turn is two journeys in opposite directions, and the second one is why the first matters.

```
SPEAKING                                            →  the device hears a wake, tunnels audio to the Gateway,
                                                       the Gateway decides what was said

        ┌──────────────────────────── the device (Pi Zero 2 W) ──────────────────────────────────────┐
        │                                                                                             │
  1  mic  Seeed 2-mic HAT ──► ALSA card seeed2micvoicec ──► arecord 16 kHz mono S16_LE                 │
        │                          (tlv320aic3x codec, seeed-voicecard driver, DKMS)                  │
        │                                                                                             │
  2  wake  ──► wyoming-satellite ──► :10400 wake-shim ──► :10401 wyoming-openwakeword (hey_hermes)      │
        │      (until a wake fires, audio goes to the wake engine, NOT to the Gateway)                 │
        │                                                                                             │
  3  stream ──► wyoming-satellite ──► examples/outbound_satellite.py ──► TCP to the Gateway :10701      │
        │      (the device DIALS OUT; nothing dials in)                                               │
        └─────────────────────────────────────────────────────────────────────────────────────────────┘
                                                    │
                                        Wyoming protocol over TCP
                                                    ▼
        ┌────────────────────────────── the Gateway (Gateway host, bridge/loop.py) ────────────────────┐
        │                                                                                            │
  4  endpoint  endpointing.py ── VAD (silero_vad.py) ── energy gate ──► one trimmed utterance           │
        │                                                                                            │
  5  transcribe  faster-whisper (local) ──► text                                                     │
        │                                                                                            │
  6  think  gateway.py ──► Hermes API server :8642 ── route "voice-fast" ──► deepseek-flash           │
        │        (plus bridge-local commands and the timers path, which answer without a model)       │
        │                                                                                            │
  7  speak  tts.py ──► Piper  (voices/en_US-hfc_male-medium.onnx)                                     │
        │                                                                                            │
  8  play  ──► back down the same TCP link ──► the device's aplay ──► the HAT's speaker               │
        └────────────────────────────────────────────────────────────────────────────────────────────┘

ANSWERING   ←  the device speaks, flashes its LEDs, and goes back to listening for the wake
```

**The single most important structural fact:** the device is a Wyoming **client** that dials the
gateway, not a server the Gateway dials. That decides where configuration lives, what a reconnect
means, and why the Gateway holds a *lease* on the device rather than assuming it is always there.
See `docs/outbound-link.md`.

## One protocol implementation, two connection shapes

The direction change did **not** fork the protocol code, and knowing that saves a wrong turn when
reading it:

- `bridge/satellite.py` - `SatelliteClient`, "an async Wyoming client for one satellite connection".
  It owns the session: `run`, `pause`, `tts_stream`, `play_wav`, `capture`, `describe`, plus the two
  I/O primitives `send` and `next_event`.
- `bridge/lease.py` - `LeasedSatellite(**SatelliteClient**)`. It overrides **only** the connection
  lifecycle and those two I/O primitives, so the same session logic runs over a connection that
  arrived instead of one that was dialled.

So the Gateway can talk to a device either way, and everything above the transport - turn handling,
announcements, TTS, capture - is written once. `registry.py` decides which connection owns a device
id; `lease.py` runs a conversation on the one that dialled in.

**The older dial-out shape stays reachable as an explicit rollback path.** In registry mode it is disabled
by default so it cannot compete with the device's relay. Set `VOICE_DIRECT_DIAL_FALLBACK=1` to restore it;
even then the Gateway waits through the lease grace period before dialling. The code is not dead, and a reader
who finds both shapes should not delete one without reading `docs/outbound-link.md` first.

## The link to the gateway is a transport

A satellite reaches the gateway over whatever the installation can actually do, so the endpoint
names a **transport** rather than assuming a host and a port:

| Mode | Endpoint | When it is the right choice |
|---|---|---|
| Wi-Fi / IP | `tls://host:port` (or `tcp://host:port` on a trusted LAN) | the usual case: the satellite is wireless and the gateway is on the network |
| USB | `usb://localhost` | the satellite is cabled straight into the machine running the gateway, so the link is local by definition |
| Bluetooth | `bt://<address>` | there is no usable network path, but the radio is there |

Typing only an IP address or host name is enough for the network case: the installer fills in
`tls://` and the registry port and says what it resolved to. A port you give is always kept.

**Setup is a different thing from the link.** Every satellite is provisioned over USB first, and
that is the primary setup path: certificates, wake model, Wi-Fi and the endpoint all go over the
cable. The transport above is what the satellite uses *afterwards* to reach the gateway.

The full topology -- including the decisions to make on purpose when USB and Bluetooth land -- is in `transport-topology.md`.

Status: the network link is carried today. `usb://` and `bt://` are part of the vocabulary and the
installers understand them, but nothing carries them yet, so an installer asked for one says so
plainly rather than producing a satellite that cannot connect. Each becomes real by adding that
transport to the firmware and to the Gateway; the endpoint shape needs no further change.

## Every component, and who owns it

| Stage | Component | Runs on | Origin | Ours? |
|---|---|---|---|---|
| 1 capture | Seeed 2-mic HAT v2.0, `tlv320aic3x` codec | device | Seeed hardware | bought |
| 1 capture | `seeed-voicecard` 0.3 driver, DKMS | device | `respeaker/seeed-voicecard` | vendored in `pi/vendor/` |
| 1 capture | ALSA card `seeed2micvoicec`, opened `plughw:...` | device | kernel + driver | config |
| 2 wake | `wyoming-openwakeword` 2.1.0, TFLite | device | `rhasspy/wyoming-openwakeword` | upstream, mirrored |
| 2 wake | `hey_hermes` wake model, `--threshold 0.35` | device | openWakeWord community library | model named, not vendored |
| 2 wake | `wake_shim.py` :10400 | device | **written by us** | ours |
| 2 wake | `2mic_service.py` + 3 APA102 on SPI | device | upstream example, +343 lines ours | ours |
| 3 stream | `wyoming-satellite` `13bb024` + our 7-line patch | device | `rhasspy/wyoming-satellite` (**archived**) | vendored, patched |
| 3 stream | `outbound_satellite.py` | device | **written by us** | ours |
| 3 stream | `satellite.py` - `SatelliteClient`, the Wyoming protocol session | gateway | **written by us** | ours |
| 3 stream | `lease.py` - `LeasedSatellite`, the same client over a received connection | gateway | **written by us** | ours |
| 4 endpoint | `endpointing.py` + frame buffering | gateway | **written by us** | ours |
| 4 endpoint | **voice detection: Silero v6 ONNX** | gateway | `snakers4/silero-vad`, model **committed** | ours (vendored model) |
| 4 endpoint | energy gate (level backstop) | gateway | **written by us** | ours |
| 5 transcribe | `faster-whisper` `small.en`, int8, CPU | gateway | `SYSTRAN/faster-whisper` | dependency |
| 6 think | `gateway.py` over the Runs API | gateway | **written by us** | ours |
| 6 think | Hermes agent + API server at `http://127.0.0.1:8642` | Gateway host | Hermes Agent | platform |
| 6 think | route `voice-fast` → `deepseek-flash` | Gateway host | configured | ours (config) |
| 7 speak | `tts.py` + Piper `en_US-hfc_male-medium` | gateway | Piper | ours + model file |
| 8 play | `aplay` on the device's HAT | device | ALSA | config |
| — | `announce.py`, `registry.py`, `lease.py`, `timers.py`, `volume.py`, `config_plane.py` | gateway | **written by us** | ours |
| — | `hermes/plugins/voice_satellite`, `hermes_timers` | Gateway host | **written by us** | ours |

**Everything in the "ours" column is the point of the project.** The upstream satellite is archived,
so nothing above it will be fixed for us. `docs/owning-the-satellite-stack.md` covers the device half
of that in full; `docs/voice-detection.md` covers the detector.

### The knobs a reader usually wants, and where they live

| Knob | File | Value in force |
|---|---|---|
| which detector | `bridge/config.py` `VOICE_DETECTOR` | `silero` |
| detector sensitivity | `bridge/config.py` `SILERO_THRESHOLD` | `0.5` |
| energy gate: ratio, ceiling, floor clamps | `bridge/config.py` `VOICE_ENERGY_GATE` | see the comment above it |
| which model the voice surface uses | `~/.hermes/config.yaml` `gateway.api_server.model_routes.voice-fast` | `deepseek` / `deepseek-flash` |
| that route, selected by the Gateway | `bridge/config.py` `VOICE_ROUTE_ALIAS` | `voice-fast` |
| speech-to-text model | `bridge/loop.py` `stt_model` (a default argument, **not** a config constant) | `small.en`, int8, CPU |
| text-to-speech voice | `bridge/config.py` `PIPER_VOICE_NAME` (+ `PIPER_SPEAKER_SETTING`, `PIPER_LENGTH_SCALE`) | `en_US-hfc_male-medium` |
| the gateway the Gateway calls | `bridge/config.py` `GATEWAY_BASE_URL` | `http://127.0.0.1:8642` |

## What each stage actually decides

Worth reading before debugging, because a symptom at one stage is usually caused at the one before.

| Stage | The decision it makes | If it goes wrong |
|---|---|---|
| capture | is the microphone open, and at the right level | nothing works; wake never fires |
| wake | is this the wake phrase | the device ignores you (blue LED never comes) |
| stream | is the microphone being forwarded to the Gateway | the Gateway sees an empty room |
| endpoint | is this a voice, and is it loud enough | hears nothing, or turns of noise |
| transcribe | what words were those | answers the wrong question |
| think | what to do about it | slow, wrong, or no answer |
| speak | how it sounds | understandable but wrong voice |
| play | is it audible | the answer exists and you never hear it |

## The LED states, because they are the only diagnostic the device has

The device reports its state through three APA102s. Reading them tells you which stage you are at
before any log does:

| LED | Meaning |
|---|---|
| gentle white pulse | booting, until the Gateway connects |
| **blue**, 1 s | wake word detected |
| **yellow** | streaming: you are being listened to |
| **violet pulse** | the Gateway is thinking |
| **steady dim teal** | follow-up mode: listening without a wake word |
| dark | the answer started, or the turn ended |
| green flash x3 | connected |
| red held | disconnected |

**Violet is the one that matters for triage.** Blue → yellow → dark means the Gateway never reached
the thinking stage, so nothing was transcribed - the fault is at endpoint or transcribe, not think.
That is exactly how the 2026-09-25 office failure was caught.

## Where to read next

| For | Read |
|---|---|
| the device half, pinning and rebuilding | `owning-the-satellite-stack.md` |
| the transport, and why the device dials out | `outbound-link.md` |
| which transports the link can use (usb, bluetooth, network), and why setup is separate | `transport-topology.md` |
| voice detection and the energy gate | `voice-detection.md` |
| endpointing profiles (pauses, timeouts) | `endpointing-profiles.md` |
| which model the voice surface uses | `agent-and-model-routing.md` |
| the hardware | `hardware/`, `supported-devices.md` |
| what the satellite will say and do | `voice-commands.md` |
| timers, volume, interruptions | `timers-and-alarms.md`, `volume.md`, `interruption.md` |
| the wire protocol itself | `wyoming-protocol.md` |
| the device's config surface | `configuration-plane.md`, `satellite-http-config.md` |
| the wake word: switching, licensing | `switching-wake-words.md`, `wake-word-licensing.md` |
| the spoken voice: switching, speakers, licensing | `switching-voices.md` |
| the tones it speaks in | `prompt-tones.md` |
| the lights, and what each phase means | `leds.md` |

## The rule that holds all of it together

**Every component that decides whether the device hears its owner is vendored, pinned, or written by
us.** The rule, 2026-09-25: *"if we're going to use silero, we're going to use it locally, and
make it a repo we track."*

That is why the detector model is committed to this repository rather than read out of a package's
wheel, why the satellite source sits in `pi/vendor/`, and why the upstream projects are mirrored as
git bundles. `scripts/refresh-upstream-mirrors.py` is the maintained list; `pi/vendor/MANIFEST.md` and
`bridge/vendor/MANIFEST.md` record the vendored files and their checksums.
