# Hermes Voice Satellite

A wake-word voice frontend for [Hermes Agent](https://github.com/NousResearch/hermes-agent): a small
satellite device in the room listens for a wake word, streams audio to a host, and the host does the
speech recognition, the model call and the speech synthesis, then speaks the answer back through the
device's own speaker. The same speaker also carries house announcements from Hermes and from home
automation, queued behind any conversation already in progress.

Built for one specific installation (a Pi Zero 2 W called `wyoming1` in the house, with the host
`autumn` running Hermes), but laid out so the same setup can be deployed somewhere else.

## Architecture

```
  Pi Zero 2 W  ("wyoming1", the satellite: mic, speaker, LEDs only)
    wyoming-satellite      mic capture, playback, wake-word client
    wyoming-openwakeword   the wake model itself (TFLite, custom model supported)
    wake-shim              proxy in front of the wake service; can inject a synthetic wake
    2mic_leds              LED feedback, single writer, APA102 over SPI
                 |
                 | wyoming protocol, tcp/10700
                 v
  autumn  (host: everything that needs a CPU)
    hermes-voice-bridge    user service, ~/hermes-voice-bridge
      endpointing -> faster-whisper STT -> Hermes run API -> Piper TTS
      announcements on :8765, token authenticated, queued politely
                 |
                 | HTTP loopback only: POST /v1/runs + SSE events
                 v
  Hermes Agent
      gateway API server bound to 127.0.0.1:8642, with a "voice" model route
      plugins: voice_satellite, hubitat, google, geolocation
      a small set of core patches, see hermes/patches/
```

The device is inert unless the bridge dials in. Nothing about the voice surface is reachable from the
LAN: the API server is loopback, the wake shim's control port is loopback, and the only network
listener is the announcement endpoint, which requires a token.

## Repository layout

| Path | What it is |
|---|---|
| `bridge/` | The bridge itself: endpointing, STT, TTS, gateway client, satellite client, announcements |
| `scripts/` | Operational scripts: live checks, tests, chime generation, capture tools |
| `pi/` | The device side: wake shim, LED service, systemd units, and `.orig` copies of the upstream files we patch |
| `sounds/` | Chimes and tones the device plays |
| `reference/` | Upstream reference copies of the files the device patches, for diffing |
| `hubitat/` | `HermesAnnouncer.groovy`, the Hubitat driver that speaks through this system |
| `hermes/plugins/` | The Hermes plugins the voice surface uses, verbatim from a working install |
| `hermes/patches/` | The one remaining core patch, as a diff, plus a checker |
| `hermes/config/` | The voice-surface configuration keys, with placeholders instead of secrets |
| `hermes/systemd/` | The bridge's user unit |
| `skills/` | The skills that document how to operate and repair this system |
| `docs/` | Restart safety, portability and extension notes |

## Requirements

Hardware, as built:

- A Raspberry Pi Zero 2 W with a **Seeed ReSpeaker 2-mic HAT** (`plughw:CARD=seeed2micvoicec`), WiFi only
- A host machine that stays on, with enough CPU for `faster-whisper small.en` and Piper
- A Hermes Agent install on that host, version current within a release or so

Software: Python 3 with the pinned set in `requirements.txt` (faster-whisper, piper-tts, wyoming,
aiohttp, silero VAD, webrtcvad fallback). The Piper voice `en_US-hfc_male-medium` (61 MB, not committed
here) comes from the Piper voices release; the custom wake model is a community TFLite file, see
`skills/hermes-voice-bridge/references/switching-wake-words.md`.

## Install: the Hermes side

1. Copy the plugins in: `~/.hermes/plugins/{voice_satellite,hubitat,google,geolocation}`.
2. Enable them. A plugin that merely sits in the directory is **not loaded**:
   `hermes plugins enable voice_satellite` and so on for each.
3. Apply the configuration keys from `hermes/config/voice-surface.example.yaml` to
   `~/.hermes/config.yaml`, including the `platform_toolsets.api_server` list.
4. Apply the core patches: see `hermes/patches/README.md`.
5. Restart the gateway and verify the surface resolves as expected (see Verify, below).

## Install: the device side

1. Install the satellite per upstream `wyoming-satellite`, with the Seeed 2-mic HAT.
2. Copy `pi/*.py` and the unit files into place, then enable `wake-shim`, `wyoming-openwakeword`,
   `2mic_leds` and `wyoming-satellite`. Mind the `Requires=` relationships: restarting the shim or the
   LED service bounces the satellite, and the satellite does not come back on its own.
3. Put the wake model in the directory named by `--custom-model-dir` in the wake service drop-in, named
   after the wake word you want (`<name>.tflite`).
4. Copy `sounds/` onto the device under `/home/wyoming1/sounds/`.

## Verify

```bash
# the bridge is alive and connected
systemctl --user status hermes-voice-bridge
journalctl --user -u hermes-voice-bridge -n 30

# what the voice surface actually resolves (it runs at parity with a normal conversation,
# so the tool_search bridge is expected here)
cd ~/.hermes/hermes-agent && ./venv/bin/python - <<'PY'
import sys; sys.path.insert(0, '/home/angela/.hermes/hermes-agent')
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

`hermes update` does not preserve local source changes. Our patches are uncommitted working-tree edits,
so an update stashes them and the system runs unpatched until they are re-applied. Run
`hermes/patches/check-patches.sh` after every update; it is read-only and tells you which patches are
missing. See `docs/restart-safety-and-portability.md` for the planned hardening.

## Status and known gaps

- Verified working as of 2026-09-22/23: wake word, follow-up mode, announcements, LED states.
- **The voice surface runs at full parity with a normal Hermes conversation** (24 toolsets, 52 tools,
  ~19k tokens of schema) on the gateway's default model, so it inherits the normal fallback chain.
  It is no longer narrowed, and it no longer pins a voice-specific model route.
- **One satellite only.** `SATELLITE_HOST` is a single address and the session is a single session.
- Machine-specific values are still hardcoded in `bridge/config.py`.
- `voices/`, `logs/`, `audio/` and `state/` are intentionally not committed.
