# Owning the satellite stack

**Role change, 2026-09-24 (issue #61).** The satellite stops being a Wyoming *server* that the bridge dials
and becomes a Wyoming *client* that dials the bridge. Nothing in the vendored stack changes, and the patches
below still apply exactly as they are; what changes is the outer transport, which is ours either way. The
device-side runner is `pi/outbound_satellite.py`, it is not wired into systemd yet, and the older server
shape stays available for rollback until the new path is proven on hardware.

Recorded 2026-09-23. **Why this file exists:** we run upstream voice satellite software that is now
archived, and we had already modified it. One modification was a 7-line edit to the satellite's own
core file living only as an uncommitted change on the Pi. If that card had died, the fix would have
been gone and the symptom would have come back with no obvious cause. Everything in the running
stack is now pinned, vendored, or written down here.

**Correction, 2026-09-23.** An earlier version of this file said the repo's copy of the satellite
core was stale. It was not. A sync script matched files by *filename* instead of by path, so it
wrote the bridge's client over the repo's copy of the satellite core (1397 lines replaced by 438).
The repo had been correct all along; the damage was self-inflicted by the sync method. Fixed, and
the lesson is why the live tree is now a git checkout rather than a hand-synced copy: **match paths,
never filenames.**

Direction set 2026-09-23: "at this point, we are taking inspiration from other projects, and
we will build and develop the future on our own."

## What is actually running, pinned

| Component | Version | Where it came from |
|---|---|---|
| Satellite software | `wyoming-satellite` commit `13bb024`, tag `v1.4.1-4-g13bb024`, dated 2025-04-25 | `github.com/rhasspy/wyoming-satellite` (**archived upstream**) |
| Wyoming library | `wyoming 1.5.4` | the satellite's venv, frozen in `pi/vendor/pi-venv-freeze.txt` |
| Wake engine | `wyoming-openwakeword` 2.1.0, TFLite models only | system packages plus our custom model dir |
| Wake model | a community TFLite model, e.g. `hey_hermes`, with its own threshold | the custom model directory, named by filename stem |
| Audio driver | Seeed `seeed-voicecard` 0.3, tree fingerprint `a77f9c72fca4aa38ad6e4775072b0d4aa385ebea0c36f904fce66e2c521aa61d` | `/usr/src/seeed-voicecard-0.3`, vendored here |
| Operating system | Debian 12 bookworm, arm64, 64-bit | Raspberry Pi OS on a Pi Zero 2 W |
| Kernel | `6.12.109+rpt-rpi-v8` | the driver is built against this exact kernel |
| Audio hardware | card `seeed2micvoicec`, codec `tlv320aic3x`, opened as `plughw:CARD=seeed2micvoicec` | the Seeed 2-mic HAT |
| LEDs | 3 x APA102 on `/dev/spidev0.1` (bus 0, device 1) | driven by our own service |

## Our divergence from upstream, in full

Captured as patches in `pi/vendor/`. Nothing here is upstream behaviour.

1. **`wyoming_satellite/satellite.py`, 7 lines.** The done tone is now played with
   `mute_microphone=True`, exactly as the awake tone already was. Without it the tone is captured as
   speech: on 2026-09-22 the follow-up window opened 0.6 s after playback, the tone arrived about a
   second later, was transcribed as "beep", and the device answered its own chime. This is the fix
   for that, and it existed only on the device until now.
2. **`examples/2mic_service.py`, +343 lines.** Our LED service, effectively entirely ours now:
   boot pulse, wake colour, working pulse, follow-up colour, and the red-after-30s disconnected
   state.
3. **Three example scripts we added:** `wake_shim.py` (holds 10400 so the microphone can be opened
   on demand for follow-up mode), `inject_wake.py`, `led_cmd.py` (lets the satellite's own TTS hooks
   change the LEDs over loopback).
4. **Two systemd drop-ins**, which is where the real configuration lives:
   - `wyoming-satellite.service.d`: `Requires=` and `After=` on `wake-shim.service`, so the shim is
     up first, and restarting it bounces the satellite. The drop-in carries the warning to check
     `systemctl is-active wyoming-satellite` afterwards.
   - `wyoming-openwakeword.service.d`: moves the wake service to 10401 and sets `--threshold 0.35`
     and `--custom-model-dir`. Both settings are in one file on purpose, because systemd takes the
     last `ExecStart` and a second drop-in would silently cancel the threshold.

## What we vendored

| Path | What it is |
|---|---|
| `pi/vendor/native-satellite-13bb024/` | the satellite source at the pinned commit, our changes applied, no `.git` |
| `pi/vendor/seeed-voicecard-0.3/` | the audio driver source, 53 files, 1.3 MB |
| `pi/vendor/wyoming-satellite-core-satellite-py.patch` | the 7-line core change, applyable to a clean checkout |
| `pi/vendor/wyoming-satellite-local-changes.patch` | the full divergence, for reference |
| `pi/vendor/pi-venv-freeze.txt` | the satellite venv, 16 packages, exact versions |
| `pi/systemd/*.service` and `*-overrides.conf` | the units and drop-ins as deployed |

Some of those units also exist as older single-file copies directly under `pi/`. **`pi/systemd/` is the
authoritative set** and is what the install steps below use; the older copies are kept for reference and
must not disagree with it. Since 2026-09-24 `scripts/test_pi_units_consistent.py` enforces that: it fails
if the tracked unit files give different values to `--wake-word-name` or `--custom-model-dir`, or if any of
them reintroduces `--vad`. The reason it exists is not theoretical: the legacy satellite copy still carried
the retired `ok_nabu` wake word and the old awake-tone mute, and the legacy wake-service copy was missing
the custom model directory, so a rebuild driven from those files would have produced a device that could
not be woken by the configured word. Consolidating the duplicated copies is issue #45.

Deliberately **not** vendored: the wake model binary (recorded above by name and source, since it is
a 422 KB blob reproducible from the community library), and the gold SD card image, which is the
real backstop and lives with the `remote-system-operations` procedure.

## Rebuilding this device from scratch

1. Flash Raspberry Pi OS 64-bit (bookworm). 64-bit matters: audio enhancements and local wake word
   detection require it.
2. Build and install the audio driver from `pi/vendor/seeed-voicecard-0.3` (`install.sh`). **The driver
   is DKMS-installed**, so a kernel upgrade rebuilds it automatically and the sound card returns on its
   own. Verified 2026-09-23: the loaded module is
   `/lib/modules/<kernel>/updates/dkms/snd-soc-seeed-voicecard.ko.xz`, `dkms status` lists
   `seeed-voicecard/0.3` for three kernels, and `/usr/src/seeed-voicecard-0.3/dkms.conf` sets
   `AUTOINSTALL="yes"`. Confirm with `/usr/sbin/dkms status`, noting the full path: `/usr/sbin` is not on
   the PATH in a non-login SSH, which is what made an earlier version of this document claim the
   opposite.
3. Check out the satellite at `13bb024`, or copy `pi/vendor/native-satellite-13bb024` into place,
   then apply the core patch if the source came from upstream.
4. Recreate the venv from `pi/vendor/pi-venv-freeze.txt`.
5. Copy the wake model into the custom model directory.
6. Install the units and drop-ins from `pi/systemd/`, then `daemon-reload`. Start order: LED service,
   wake engine, shim, then satellite.
7. Copy the three scripts into `examples/`.
8. Verify: `arecord -l` shows `seeed2micvoicec`; the wake word fires; the LEDs pulse on boot and go
   green once; a done tone plays without being transcribed.

## Maintenance watch items

- **Kernel upgrade does not break audio: the driver is already DKMS.** Verified 2026-09-23: the
  loaded module comes from `/lib/modules/<kernel>/updates/dkms/snd-soc-seeed-voicecard.ko.xz`, dkms
  3.0.10 is installed, `/usr/src/seeed-voicecard-0.3/dkms.conf` carries `AUTOINSTALL="yes"`, and the
  kernel post-install hook is in place, so a new kernel rebuilds the module automatically.
  **Earlier versions of this file said the driver was not DKMS and that a kernel upgrade would
  remove the sound card. That was wrong**, and it came from `which dkms` failing because `/usr/sbin`
  is not on the PATH in a non-login SSH. Always use `/usr/sbin/dkms` on this host.
  The vendored copy of the source in `pi/vendor/seeed-voicecard-0.3` still matters: it is the exact
  version DKMS builds, and it is the rollback if a future DKMS build fails.
- The upstream repo is archived, so there are no security fixes coming. Our exposure is limited by
  the device listening on only SSH and the Wyoming port, with everything else on loopback.
- The Pi venv pins are recorded here and nowhere else. Re-freeze after any deliberate change.
- **The device layer is ours now.** Direction set 2026-09-23: "We will maintain our own software stack to
  support the respeaker devices, including the LED drivers, and update code as needed to keep within
  kernel updates as they happen." The plan for several devices lives in
  `docs/supported-devices.md`.
