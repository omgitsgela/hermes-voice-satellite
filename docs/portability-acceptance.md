# Fresh-machine portability acceptance

Issue #45 separates source generalisation from proof that another installation
can actually start. The bridge calls a separately installed Hermes Gateway over
HTTP; it does not import or install the `hermes-agent` Python distribution.
Follow the bridge-host installation steps in `README.md`.

## Acceptance recorded for the clean-install change

A fresh independent Linux VPS, a new non-root account, a source clone transferred
by Git bundle, new dependency environments, independent configuration and a new
synthetic device identity were used. No production credentials, device state or
private calibration were copied. The existing reference satellite was untouched.

Verified:

- Dependencies installed on Python 3.11.15 with the bridge-only manifest.
- The repository's user systemd unit ran from the new user's home without editing
  its source or the unit. Configuration selected non-default loopback ports for
  the Gateway fixture, registry and announcement endpoint, and a new session ID.
- A real loopback Wyoming connection completed `describe`, registration,
  configuration delivery and `run-satellite` using the synthetic device identity.
- Announcement authentication rejected the unauthenticated POST with 401 and
  accepted the authenticated POST with 202. Acceptance alone was not the proof:
  the synthetic speaker actually received `audio-start`, PCM and `audio-stop`.
- Real Piper synthesis delivered 104,960 bytes of non-silent 22,050 Hz,
  16-bit mono audio. Real `small.en` Whisper transcribed the received WAV as
  "The fresh machine portability test is successful."
- All 69 standalone offline scripts exited successfully in an unconfigured source
  checkout. Three scripts reported skipped private recording checks. This count
  includes the bridge-dependency guard added by this change.
- Restoring the hard-coded announcement bind or the Hermes distribution dependency
  made the corresponding regression checks fail.

The offline architecture-document test compares the configured Gateway URL with
prose describing the reference defaults. Run that source-document check without
installation-specific `bridge.env`; a custom port is not an architecture defect.
The volume-range test now checks the configured floor rather than assuming the
reference installation's private calibration.

## Boundaries of this proof

The Gateway was a **synthetic health fixture**, not a model-backed Hermes agent.
The satellite was a **synthetic Wyoming speaker**, not a microphone or physical
speaker. This proves bridge installation, service startup, independent configuration,
registration, authentication and real synthesis/transport/transcription, not a full
spoken model/tool interaction, wake-word performance or physical-device playback.
Those require their own integration and acoustic acceptance.

The VPS's system Python 3.14 could install dependencies after native build tools
were added, but its offline suite had asynchronous-test failures and a timeout.
Python 3.11 is the validated runtime here; this change does not claim 3.14 support.
ALSA device-control tests also require `alsa-utils`, even when commands are stubbed.

On a public host, explicitly bind announcements and raw Wyoming to loopback or a
trusted private interface. `VOICE_ANNOUNCE_HOST` and `VOICE_ANNOUNCE_PORT` are now
configurable through environment or `bridge.env`; the LAN default is preserved
for compatibility. Transport security remains separate work under #61.
