# Hermes Voice Satellite

A wake-word voice frontend for [Hermes Agent](https://github.com/NousResearch/hermes-agent): a small
satellite device in the room listens for a wake word, streams audio to a host, and the host does the
speech recognition, the model call and the speech synthesis, then speaks the answer back through the
device's own speaker. The same speaker also carries house announcements from Hermes and from home
automation, queued behind any conversation already in progress.

Written for one installation (a Pi Zero 2 W in the house, with a host running Hermes) and laid out so
the same setup can be deployed elsewhere. Names, addresses and paths in this document are placeholders:
substitute your own.

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
| `pi/` | The device side: wake shim, LED service, systemd units, and `.orig` copies of the upstream files we patch |
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

Software: Python 3 with the pinned set in `requirements.txt` (faster-whisper, piper-tts, wyoming,
aiohttp, silero VAD, webrtcvad fallback). The Piper voice `en_US-hfc_male-medium` (61 MB, not committed
here) comes from the Piper voices release; the custom wake model is a community TFLite file, see
`docs/switching-wake-words.md`.

## Install: the Hermes side

1. Copy the plugin in: `~/.hermes/plugins/voice_satellite`. It is the only plugin this project needs;
   the rest of the voice surface comes from your own Hermes install and its own plugins.
2. Enable them. A plugin that merely sits in the directory is **not loaded**:
   `hermes plugins enable voice_satellite` and so on for each.
3. Apply the configuration keys from `hermes/config/voice-surface.example.yaml` to
   `~/.hermes/config.yaml`. The surface deliberately has **no** `platform_toolsets.api_server` entry:
   it inherits the default toolset list, which is how it runs at full parity.
4. Apply the core patch: see `hermes/patches/README.md`.
5. Restart the gateway and verify the surface resolves as expected (see Verify, below).

## Install: the device side

1. Install the satellite per upstream `wyoming-satellite`, with the Seeed 2-mic HAT.
2. Copy `pi/*.py` and the unit files into place, then enable `wake-shim`, `wyoming-openwakeword`,
   `2mic_leds` and `wyoming-satellite`. Mind the `Requires=` relationships: restarting the shim or the
   LED service bounces the satellite, and the satellite does not come back on its own.
3. Put the wake model in the directory named by `--custom-model-dir` in the wake service drop-in, named
   after the wake word you want (`<name>.tflite`).
4. Copy `sounds/` onto the device under the path your wake service expects, e.g. `~/sounds/`.

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

## Surviving Hermes updates

`hermes update` does not preserve local source changes, and our remaining patch is an uncommitted
working-tree edit, so an update stashes it and the check reports it missing. It is **one** patch,
`cron/jobs.py`, which keeps a scheduled job from drifting to the bare-`custom` provider fallback when it
snapshots its model. It is tracked upstream as #109765 with a fix proposed in #109773, so it can be retired
once a release carries that.

Run `hermes/patches/check-patches.sh` after every update; it is read-only and reports what is missing. See
`docs/restart-safety-and-portability.md` for the planned hardening.
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

## Status and known gaps

- Verified working as of 2026-09-22/23: wake word, follow-up mode, announcements, LED states.
- Behaviour worth knowing: **follow-up mode** keeps listening for about **fifteen seconds** after an answer,
  so a conversation continues without repeating the wake word. **Announcements** queue behind any
  conversation in progress, open with a distinct wake tone, and are spoken only when the room is quiet.
- **The voice surface runs at full parity with a normal Hermes conversation** on the gateway's default
  model, so it inherits the normal fallback chain. Measured 2026-09-24 with this repository's own Verify
  block: **21 toolsets, 19 tools, about 8.4k tokens of tool schema**.
  It is no longer narrowed, and it no longer pins a voice-specific model route.
- **One satellite only.** `SATELLITE_HOST` is a single address and the session is a single session.
- Machine-specific values are still hardcoded in `bridge/config.py`.
- `voices/`, `logs/`, `audio/` and `state/` are intentionally not committed.

