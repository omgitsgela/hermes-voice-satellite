# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses semantic versioning.
Released versions are tagged `v<major>.<minor>.<patch>` and carry the standalone installer and the
ESP32 flash bundle as release assets.

## [1.0.0] - 2026-11-01

The first release. A wake-word voice frontend for Hermes Agent: satellites hear the wake word, the
Satellite Gateway does the recognition, the model call and the speech, and the device answers
through its own speaker. The same speaker carries announcements queued behind a live conversation.

### Added

- **Satellite Gateway**: endpointing with named profiles, faster-whisper transcription, Piper
  speech, the Hermes run API over SSE, announcements on an authenticated endpoint, timers, volume
  control, interruption (steer or stop a running task), and a configuration plane with
  desired/reported state per device.
- **Device-initiated connections**: devices dial the Gateway (plaintext registry or TLS with a
  per-device client certificate), register, are leased and supervised. The older direct-dial shape
  remains as an explicit rollback (`VOICE_DIRECT_DIAL_FALLBACK=1`).
- **ESP32-S3 satellite firmware**: on-device microWakeWord scoring through TFLite-Micro, 48 kHz I2S
  capture and playback, the XVF3800 controls (microphone gain, AEC, LEDs), a USB provisioning
  console, register-on-boot, and a flash bundle whose manifest pins every region.
- **Pi satellite**: the vendored, patched `wyoming-satellite` stack plus the outbound registry
  runner, the wake shim, the LED service and their units.
- **Installer** (`hermes-voice-satellite`): a graphical interface with a console path, gateway and
  workbench roles, per-device voices, fleet management over the network, pairing with a three-answer
  identity check, and per-platform standalone builds that need no Python on the target machine.
- **Model and policy as one unit**: a provisioned wake model now carries its own inference policy
  (cutoff, window, step, tensor arena) in the model blob, so switching models is a provisioning
  step rather than a firmware rebuild.
- **Upgrade path**: `scripts/update-gateway.sh` fast-forwards an existing checkout, updates
  dependencies and restarts the service without touching the host configuration or TLS material.

### Changed

- The bundled wake model is `hey_hermes` (microwakeword.com community library), the model the
  project is built around. Its terms are personal and non-commercial only, matching the repository's
  own licence. The Apache-2.0 `okay_nabu` model ships alongside it for commercial deployments; see
  `docs/wake-word-licensing.md`.
- The firmware's compiled default policy and the installer's default wake word are now the same
  model, and the Gateway seeds every new device with `hey_hermes` regardless of platform.
- The firmware README describes the firmware that exists (a satellite) instead of an early
  diagnostic milestone.

### Fixed

- Installer copy that denied features the same installer offered (remote settings, per-device
  voices).
- A satellite named "Living Room" refused as though no name were given.
- A device package that failed to import vanished from the device list with no explanation.
- A failed microphone write named the retired `mic_gain` key.
- README verify commands that named a unit and an environment template that do not exist.
- Documentation drift: the roadmap's duplicated 48 kHz entry, its incomplete issue index, and the
  installer guide's suite count and build directory.

### Known gaps

- No authenticated enrolment flow on the device: credentials are provisioned over USB.
- `usb://` and `bt://` transports exist as installer vocabulary only.
- The wake model is not yet trained by this project (issue #110).
- Acoustic acceptance is per board and is the field-test runbook's job.
