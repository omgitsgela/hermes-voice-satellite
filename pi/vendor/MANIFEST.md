# Vendor manifest

Everything in `pi/vendor/` exists so this project can be rebuilt and defended without depending on
anyone upstream. Read `docs/owning-the-satellite-stack.md` for why.

Captured 2026-09-23 from the working device (`wyoming1`).

| Item | Detail |
|---|---|
| `native-satellite-13bb024/` | `rhasspy/wyoming-satellite` at commit `13bb024`, tag `v1.4.1-4-g13bb024`, 2025-04-25. 46 files, 456 KB. Our core patch is already applied. Excludes `.git`, `.venv` and caches. |
| `seeed-voicecard-0.3/` | Seeed audio driver source, 53 files, 1.3 MB. Tree fingerprint (sha256 of sorted per-file sha256 values): `a77f9c72fca4aa38ad6e4775072b0d4aa385ebea0c36f904fce66e2c521aa61d`. Built against kernel `6.12.109+rpt-rpi-v8`. |
| `wyoming-satellite-core-satellite-py.patch` | The 7-line change to `wyoming_satellite/satellite.py`: play the done tone with `mute_microphone=True`. Apply to a clean checkout of `13bb024`. |
| `wyoming-satellite-local-changes.patch` | The complete divergence from upstream, both files, for reference. 437 lines. |
| `pi-venv-freeze.txt` | The satellite's Python environment, 16 packages. Key pins: `wyoming 1.5.4`, `numpy 1.25.2`, `flatbuffers 20181003210633`. |

## Notes carried from the device

- The driver is **DKMS-installed** and rebuilds itself on kernel upgrades (`dkms.conf` sets
  `AUTOINSTALL="yes"`). Verified 2026-09-23 by the loaded module's path under `updates/dkms/`. If a
  future kernel ever fails to build it, this vendored source is the rollback.
- The driver package supports more than our HAT: it ships device-tree overlays and asound state for
  **2, 4, 6 and 8 microphone** variants (`seeed-2mic-voicecard.dtbo`, `seeed-4mic-voicecard.dtbo`,
  `seeed-8mic-voicecard.dtbo`, `asound_4mic.conf`, `asound_6mic.conf`). Any ReSpeaker I2S HAT that
  matches one of those should work with the driver we already run, which is useful for the device
  evaluation in `docs/supported-devices.md`.
- The satellite source ships `sounds/timer_finished.wav`, `sounds/done.wav` and `sounds/awake.wav`,
  which are candidates for the timer and alarm tones in `docs/timers-and-alarms.md`.
- The wake model itself is not vendored: it is a 422 KB TFLite file, reproducible from the
  openWakeWord community library (model 2338), and it lives on the device at
  `/home/wyoming1/wake-models`.
