# Hermes Voice Satellite

A wake-word voice frontend for [Hermes Agent](https://github.com/NousResearch/hermes-agent): a small
satellite device in the room listens for a wake word, streams audio to a host, and the host does the
speech recognition, the model call and the speech synthesis, then speaks the answer back through the
device's own speaker. The same speaker also carries house announcements from Hermes and from home
automation, queued behind any conversation already in progress.

Written for one installation (a Pi Zero 2 W in the house, with a host running Hermes) and laid out so
the same setup can be deployed elsewhere. Names, addresses and paths in this document are placeholders:
substitute your own.

## Why a voice frontend

A terminal is the natural interface to an agent, but it assumes you are at a screen with something to
type into. The satellite is for the moments you are not:

- **Your hands are occupied.** Cooking, soldering, working on something on a ladder, holding a child:
  the wake word plus a sentence replaces typing, and the answer comes back without touching a device.
  The voice surface runs at full parity with a normal conversation (see Status), so the same requests
  that work in a terminal — a question, a draft, a lookup, a knowledge-base update, or a home-automation
  action — work here. Its spoken prompt explicitly routes requested Obsidian writes through the storage and
  vault skills rather than treating the whole microphone channel as read-only.
- **The house can talk.** A cron job, a home-automation rule, or any other process can POST an
  announcement to the bridge, and the satellite speaks it through its own speaker, queued politely
  behind any conversation in progress. A freezer that crossed a threshold, a door left open, a backup
  that failed: it reaches you in the room instead of as a notification you see days later. The sound
  of that speaker becoming active *is* the signal.
- **You can interrupt a running task.** Wake the satellite while a run is in flight and the device
  steers or stops it: a stop phrase ends the run and acknowledges it, or new information is passed to
  the task so it carries on with the correction. See `docs/interruption.md`.
- **Multi-turn without the wake word.** With follow-up mode on, the microphone reopens after each
  answer, so a short exchange — the answer, a correction, a follow-up question — stays one
  conversation instead of repeating the wake word each time.
- **One satellite per room.** Each satellite is a small device plus a speaker; the heavy lifting stays
  on the host. A kitchen satellite for questions mid-recipe, a workshop one for hands-occupied work,
  a hallway one that doubles as the announcement speaker for the whole house.

What this is not: a better way to read long output. Spoken answers are kept short by design; when you
need the full result you come back to the terminal. The satellite is for the interaction, the
notification, and the moment your hands are full.

## Architecture

```
  Raspberry Pi Zero 2 W  (the satellite: microphone, speaker, LEDs only)
    wyoming-satellite      mic capture, playback, wake-word client
    wyoming-openwakeword   the wake model itself (TFLite, custom model supported)
    wake-shim              proxy in front of the wake service; can inject a synthetic wake
    2mic_leds              LED feedback, single writer, APA102 over SPI
                 |
                 | wyoming protocol, tcp/10700
                 v
  <host>  (everything that needs a CPU)
    hermes-voice-bridge    user service, ~/hermes-voice-bridge
      endpointing -> faster-whisper STT -> Hermes run API -> Piper TTS
      announcements on :8765, token authenticated, queued politely
                 |
                 | HTTP loopback only: POST /v1/runs + SSE events
                 v
  Hermes Agent
      gateway API server bound to 127.0.0.1:8642; the voice surface follows its default model
      plugins: voice_satellite, plus whatever else your install enables
      a small set of core patches, see hermes/patches/
```

The device is inert unless the bridge dials in, and the bridge is not reachable from the LAN: the API
server is loopback, the wake shim's control port is loopback, and the announcement endpoint requires a
token.

**Two connection shapes exist in this design, and the second one is being built.** Today the **device is the
server**: it listens on 10700 and the bridge dials it, which is why the device needs a reachable address.
The decided direction (issue #61) **inverts that**: satellites dial the bridge, the bridge runs a Wyoming
server with a registry of connected devices, and nothing has to reach the device at all. The event
vocabulary is identical either way. A local bridge stays the most private option; a bridge on a public
endpoint terminates TLS in the bridge itself and authenticates each device with its own credential.

**The satellite's own Wyoming listener is a different matter.** `wyoming-satellite` binds
`tcp://0.0.0.0:10700`, so any device on the LAN can reach it, and Wyoming has no authentication or
encryption by design: it is meant for a trusted network and must never be exposed to the internet. Treat
port 10700 on the satellite as open to the local network, meaning a peer on that network can speak to the
device and receive its audio. Everything else in the stack is loopback only.

## Repository layout

| Path | What it is |
|---|---|
| `bridge/` | The bridge itself: endpointing, STT, TTS, gateway client, satellite client, announcements |
| `scripts/` | Operational scripts: live checks, tests, chime generation, capture tools |
| `pi/` | The device side: wake shim, LED service, the systemd units, and the device's configuration template (`pi/systemd/satellite.env`) |
| `sounds/` | Chimes and tones the device plays |
| `reference/` | Upstream reference copies of the files the device patches, for diffing |
| `hubitat/` | `HermesAnnouncer.groovy`: an optional Hubitat driver that announces through this system |
| `hermes/plugins/` | The Hermes plugins the voice surface uses, verbatim from a working install |
| `hermes/patches/` | The one remaining core patch, as a diff, plus a checker |
| `hermes/config/` | The voice-surface configuration keys, with placeholders instead of secrets |
| `hermes/systemd/` | The bridge's user unit |
| `docs/` | Operating guides: hardware, restart safety, wake-word changes, timers and device support |


## Requirements

Hardware, as built:

- The wake word is whatever model you install. This build uses `hey_hermes`; see `docs/hardware/`
- A Raspberry Pi Zero 2 W with a **Seeed ReSpeaker 2-mic HAT** (`plughw:CARD=<your card id>`), WiFi only
- A host machine that stays on, with enough CPU for `faster-whisper small.en` and Piper. It is reached
  by SSH and runs the bridge as a user service.
- A Hermes Agent install on that host, version current within a release or so

Software: Python 3.11 is the tested bridge runtime. Use a dedicated virtual environment
with `requirements.txt` (faster-whisper, piper-tts, wyoming, aiohttp, silero VAD,
webrtcvad fallback). Hermes Agent is installed separately; the bridge calls its HTTP API
and does not need the `hermes-agent` Python distribution in its virtual environment.
The Piper voice `en_US-hfc_male-medium` (63 MB, single speaker, not committed
here) is in the Piper catalog and is fetched by `piper.download_voices` — or, for the curated
external voices this host has auditioned, by `scripts/fetch-voice.py` with pinned checksums.
`VOICE_PIPER_SPEAKER` selects a speaker for multi-speaker voices and is left unset here;
`docs/switching-voices.md` covers changing either. The custom wake model is a community TFLite file, see
`docs/switching-wake-words.md`.

## Install

The end-to-end guide is [`docs/installation.md`](docs/installation.md): Hermes, then the gateway,
then a satellite (ESP32 or Pi), and where the prebuilt images fit. It is the order this section
follows, and the installers it documents.

### The bridge host

On Debian/Ubuntu, install Python 3.11 with venv support, Git, `libsndfile1` and
`libgomp1`. If your Python release lacks wheels for a dependency, compiling it also
needs `build-essential` and that interpreter's development headers. `alsa-utils` is
required on an ALSA satellite and by the device-control offline checks, not for
bridge-only audio synthesis. Newer Python releases are not validated by this guide.

```sh
git clone https://github.com/omgitsgela/hermes-voice-satellite.git ~/hermes-voice-bridge
cd ~/hermes-voice-bridge
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m piper.download_voices --download-dir voices en_US-hfc_male-medium
cp bridge.env.example bridge.env
```

Set your host, device identity, session, registry and announcement bind values in
`bridge.env`. If using a registry-only deployment, enable it, bind it to a trusted
interface, and set `VOICE_DIRECT_DIAL_FALLBACK=0`. See `docs/outbound-link.md` for
credentials and the explicit trusted-network override. Never expose raw Wyoming
on a public interface. Set `VOICE_ROUTE_ALIAS=` if your Gateway has no named route.
Provide your own Gateway's `API_SERVER_KEY` through its environment file or the
bridge service environment. Do not copy another installation's keys or state.

Install the unmodified user unit after configuring the separate Hermes Gateway:

```sh
mkdir -p ~/.config/systemd/user
cp hermes/systemd/hermes-voice-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-voice-bridge.service
```

The supplied unit expects `~/hermes-voice-bridge`; for a different clone directory,
use a systemd drop-in overriding `WorkingDirectory`, `EnvironmentFile` and
`ExecStart`, not source edits. User services need lingering for unattended boot.
Verify listeners, registration and actual audio delivery, not just `active`.
See `docs/portability-acceptance.md` for the fresh-machine proof and its explicit
synthetic-Gateway and hardware-test boundaries.

### The Hermes side

1. Copy the plugin in: `~/.hermes/plugins/voice_satellite`. It is the only plugin this project needs;
   the rest of the voice surface comes from your own Hermes install and its own plugins.
2. Enable them. A plugin that merely sits in the directory is **not loaded**:
   `hermes plugins enable voice_satellite` and so on for each.
3. Apply the configuration keys from `hermes/config/voice-surface.example.yaml` to
   `~/.hermes/config.yaml`. The surface deliberately has **no** `platform_toolsets.api_server` entry:
   it inherits the default toolset list, which is how it runs at full parity.
4. Write `bridge.env` at the project root from `bridge.env.example` and set at least
   `VOICE_SATELLITE_HOST` to the address of this installation's satellite. This is the host side's one
   configuration file: `bridge/config.py` reads it (a real environment variable always wins over the
   file, and the file wins over the default in the code), and the user unit points at it too, so one
   file is the whole host-side configuration.
5. Apply the core patch: see `hermes/patches/README.md`.
6. Restart the gateway and verify the surface resolves as expected (see Verify, below).

### The device side

The device-side installers do all of this: `scripts/install-esp32.sh` (build, bundle, flash, and
provision a board — see [`docs/installation.md`](docs/installation.md)) and `scripts/install-pi.sh`
(the satellite venv, units, `satellite.env`, and the registry link). The manual steps below are what
those scripts do, for reference and for a device you want to wire by hand.

1. Install the satellite per upstream `wyoming-satellite`, with the Seeed 2-mic HAT.
2. Copy `pi/*.py` into the satellite's `examples/` directory, and install the units from `pi/systemd/`
   to `/etc/systemd/system/`.
3. Copy `pi/systemd/satellite.env` to `/etc/voice-satellite/satellite.env` and fill in the `<...>`
   values: the install location, the device's name, the sounds and wake-model directories, and the
   wake word. Every unit reads this one file through `EnvironmentFile=`; none of the unit files carry a
   path or an identity of their own, so changing where the device lives is a change to this file plus a
   `daemon-reload` and a restart, not an edit to any unit.
4. Put the wake model in the directory named by `VS_WAKE_MODELS` in that file, named after the wake
   word you want (`<name>.tflite`). The model's stem must equal `VS_WAKE_WORD`: the satellite and the
   wake shim both take the word from there, and `scripts/test_pi_units_consistent.py` fails if the two
   units ever disagree.
5. Copy `sounds/` onto the device under the path named by `VS_SOUNDS` in the file, e.g. `~/sounds/`.
6. Enable `wake-shim`, `wyoming-openwakeword`, `2mic_leds` and `wyoming-satellite`. Mind the
   `Requires=` relationships: restarting the shim or the LED service bounces the satellite, and the
   satellite does not come back on its own.

## Verify

```bash
# the bridge is alive and connected
systemctl --user status hermes-voice-bridge
journalctl --user -u hermes-voice-bridge -n 30

# what the voice surface actually resolves (it runs at parity with a normal conversation,
# so the tool_search bridge is expected here)
cd ~/.hermes/hermes-agent && ./venv/bin/python - <<'PY'
import sys; sys.path.insert(0, '~/.hermes/hermes-agent')
from hermes_cli.plugins import discover_plugins; discover_plugins(force=True)
import model_tools
from hermes_cli.config import load_config
from hermes_cli.tools_config import _get_platform_tools
from tools.tool_search import is_deferrable_tool_name
ts = _get_platform_tools(load_config(), 'api_server')
names = [d["function"]["name"] for d in model_tools.get_tool_definitions(enabled_toolsets=ts, quiet_mode=True)]
print(sorted(names)); print("deferrable:", [n for n in names if is_deferrable_tool_name(n)])
PY

# the patches are all present
hermes/patches/check-patches.sh

# the live end-to-end checks
.venv/bin/python scripts/check_voice_prompt.py
.venv/bin/python scripts/test_follow_up.py
python3 ~/.hermes/plugins/voice_satellite/test_voice_control_live.py
```

Speaking to the device is the only real test of the wake word. A synthesised voice cannot do it:
Piper saying the wake word scores around 0.001 against any wake model.

## Secrets

This repository deliberately contains **no secrets**. `SECRETS.md` lists every credential the system
needs, where it lives, and what it is for. Read it before deploying.

## Licensing

Two separate things, and only one of them has restrictions:

- **Wake word models.** The recommended default is a community-library model from the hosted
  openWakeWord trainer, and that site's terms license such models for **personal and non-commercial use
  only**. The pre-trained models that ship with the openWakeWord software are Apache-2.0 and carry no
  such restriction. Before deploying commercially, read
  [`docs/wake-word-licensing.md`](docs/wake-word-licensing.md): it sets out which source carries which
  terms, and the one case that needs care, which is compiling a model into a published firmware image.
- **This repository's own code.** Licensed under the **PolyForm Noncommercial License 1.0.0**: see
  [`LICENSE`](LICENSE). Personal, hobby, research and non-profit use, which also means this project as
  shipped is a non-commercial one. [`THIRD-PARTY.md`](THIRD-PARTY.md) sets out what is redistributed here
  under other licences (the GPL-3.0 audio driver, the MIT vendored satellite) and what is fetched at
  install time instead.

## Surviving Hermes updates

`hermes update` does not preserve local source changes, and our remaining patch is an uncommitted
working-tree edit, so an update stashes it and the check reports it missing. It is **one** patch,
`cron/jobs.py`, which keeps a scheduled job from drifting to the bare-`custom` provider fallback when it
snapshots its model. It is tracked upstream as #109765 with a fix proposed in #109773, so it can be retired
once a release carries that.

Run `hermes/patches/check-patches.sh` after every update; it is read-only and reports what is missing. See
`docs/restart-safety-and-portability.md` for the planned hardening.

## Keeping the repository and your installation in step

A change is not finished when it works on your machine. Four things move together, in one pass:

1. **The code**, committed and pushed here. This repository is authoritative for content: the bridge,
   the device files, the document that covers the job, and one issue per work item.
2. **The running system**, meaning your device and your host, deployed and restarted as the change
   needs, then verified as running code rather than as a file on disk. A fix that is committed but
   never deployed leaves the room on old code; a value changed on the device or in your host's config
   but never committed is drift that the next rebuild or restore loses silently.
3. **The documentation**, in the guide that covers the job, with the roadmap's issue index updated
   when a work item is added. Written generally, as the rest of this repository is.
4. **Your own private notes**, wherever you keep them: the real addresses, ids, model names and build
   details belong there, not here.

If one of the four cannot happen in the same session, say so and do it first next time. A running
system that no longer matches this repository looks healthy right up until something is rebuilt from
it.

## Documentation

- [`docs/timers-and-alarms.md`](docs/timers-and-alarms.md) timers and alarms: design spec, not built yet
- [`docs/satellite-http-config.md`](docs/satellite-http-config.md) configuration server for the satellite: feasibility research, not built
- [`docs/owning-the-satellite-stack.md`](docs/owning-the-satellite-stack.md) what is pinned and vendored, our divergence from upstream, and how to rebuild the device
- [`docs/feature-roadmap.md`](docs/feature-roadmap.md) features worth building ourselves, diffed against the successor project, with the reasoning for staying on Wyoming
- [`docs/supported-devices.md`](docs/supported-devices.md) microphone devices worth buying and testing, with the compatibility reasoning and a test checklist
- [`docs/hardware/`](docs/hardware/README.md) per-model hardware: identity, enclosure, wiring, service arguments and bring-up tests
- [`docs/interruption.md`](docs/interruption.md) talking to a task that is already running: steer it, stop it, or ignore noise
- [`docs/endpointing-profiles.md`](docs/endpointing-profiles.md) opt-in pause-tolerant listening for hesitant or slower speech
- [`docs/agent-and-model-routing.md`](docs/agent-and-model-routing.md) choose either a model route alias or a complete named Hermes Bot/profile for every satellite conversation
- [`docs/wake-word-licensing.md`](docs/wake-word-licensing.md) which wake word models may be used commercially, and the terms attached to each source
- [`docs/outbound-link.md`](docs/outbound-link.md) the device dials the bridge: the bootstrap file it carries, installing the service so it dials on boot, and what it does not do yet

## Status and known gaps

- **Connection direction is being inverted (issue #61).** Satellites dial the bridge, which runs a Wyoming
  server and keeps a device registry, so a device needs no reachable address and no port forwarding anywhere.
  Decided 2026-09-24, reviewed before implementation, and **partly built**: a device can dial in, register and
  be tracked, the bridge stays up and keeps listening with no device present instead of exiting for a restart,
  and the device-side service that dials on every boot is in `pi/systemd/` with `scripts/deploy-to-card.sh`
  installing it. What is **not** built yet: leasing a registered device, so a device that dials in is tracked
  but carries no audio, and the older dial-in path is still the one that carries a conversation. The migration
  keeps both shapes available until the new path is proven on one device.

- Verified working as of 2026-09-22/23: wake word, follow-up mode, announcements, LED states.
- Behaviour worth knowing: **follow-up mode** keeps listening for about **fifteen seconds** after an answer,
  so a conversation continues without repeating the wake word. **Announcements** queue behind any
  conversation in progress, open with a distinct wake tone, and are spoken only when the room is quiet.
- **The voice surface runs at full parity with a normal Hermes conversation** on the gateway's default
  model, so it inherits the normal fallback chain. Measured 2026-09-24 with this repository's own Verify
  block: **21 toolsets, 19 tools, about 8.4k tokens of tool schema**.
  It is no longer narrowed, and it no longer pins a voice-specific model route.
- **One satellite only.** The bridge dials one configured address and the session is a single session.
- The host side's configuration is `bridge.env` (template `bridge.env.example`); the device side's is
  `/etc/voice-satellite/satellite.env` (template `pi/systemd/satellite.env`). The repository itself
  carries no installation-specific value: `scripts/check-no-personal-data.sh` fails if one gets back in.
- `voices/`, `logs/`, `audio/` and `state/` are intentionally not committed.

