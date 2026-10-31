# The installer

A graphical setup tool for the voice gateway and its satellites, with a console path for headless
Linux, that a person can run without a terminal, a Python install, or any knowledge of what a
Wyoming protocol is.

It is also a **configuration tool**: run it again, point it at a gateway you already set up, and
change what can be changed without a cable.

## Two roles

- **Gateway**: run where Hermes runs. Installs the bridge, its environment, the registry TLS
  material, the enrolment words, and the Hermes-side plugin and keys.
- **Workbench**: run where the physical device is. Flashes a satellite, stages its Wi-Fi and dial
  address, and can change settings later.

They are separate because a gateway can live on a VPS while a satellite is flashed on a kitchen
table, and that case must not be an afterthought.

## Packages

| Package | What it is |
|---|---|
| `hermes-voice-installer` | Core: identity code, device profile contract, pairing, credential storage, drivers, session, console CLI, local web UI |
| `hermes-voice-gateway-install` | Gateway side: the identity code from the certificate, the pairing card, Hermes detection, the gateway install |
| `hermes-device-esp32-xvf3800` | ESP32-S3 with reSpeaker XVF3800: profile and driver |
| `hermes-device-pi` | Raspberry Pi satellite, flashed by writing its SD card: profile and driver |

Device packages register themselves through the `hermes_installer.devices` entry point. A
first-party device that is merely importable is picked up too, so a checkout works without an
install step.

## Running it

```sh
python3 -m hermes_installer.cli --status     # what was detected, as JSON
python3 -m hermes_installer.cli              # console flow
python3 -m hermes_installer.cli --gui        # local web UI in a browser
python3 -m hermes_installer.cli --dry-run    # every command shown, none executed
scripts/test-installer.sh                    # the offline test suite
```

The console flow is the reference for what the GUI does, because both drive the same session
object. A rule implemented in one appears in the other without being written twice.

## The device profile: the public contract

A device is a profile plus a driver. Adding a device is a package and a file, not a fork of the
installer. This is the format; a third-party device should only have to fill it in.

```json
{
  "id": "vendor-board-01",
  "display_name": "Vendor Board 01",
  "vendor": "Vendor",
  "description": "One line a person will read in the device list.",
  "flashing": "usb_serial",
  "wake_formats": ["microwakeword_tflite"],
  "voice": "local_piper",
  "wake_default": "hey_hermes",
  "first_party": false,
  "settings": {
    "playback_volume": { "apply": "immediate", "remote": true,  "help": "How loud it speaks." },
    "wifi":            { "apply": "cable",     "remote": false, "help": "Staged over the cable." }
  }
}
```

| Field | Values | Meaning |
|---|---|---|
| `flashing` | `usb_serial`, `sd_card` | How the device's software gets written to it. `sd_card` implies a card reader. |
| `wake_formats` | `microwakeword_tflite`, `openwakeword_onnx` | Which wake-word catalogue applies. The two are **not** interchangeable. |
| `voice` | `local_piper`, `none` | Whether it has a speaker the gateway can speak through. |
| `settings.*.apply` | `immediate`, `service_restart`, `next_turn`, `cable` | How a change reaches the device. |
| `settings.*.remote` | `true`, `false` | Whether a paired installer can change it over the network. A `cable` setting cannot claim to be remote, and the profile is rejected if it tries. |

The UI renders its capability badges from `remote` and `apply`, so what a person is told can be
changed remotely is the same data the code obeys. That is the point of putting it in the profile.

### The three driver verbs

```python
def flash(self, request, runner) -> StepResult     # write the software and point it at a gateway
def provision(self, request, runner) -> StepResult # stage Wi-Fi, address, model
def verify(self, request, runner) -> StepResult    # read the device's own report
```

Drivers **build `Command` objects** and the runner decides whether to run them, which is why every
flow can be tested with no hardware attached and why the UI can preview exactly what it will run.
A command that needs a secret names a file, never a value: the passphrase is read from that file by
the underlying script and never appears in an argument, a log, or an error.

## Pairing, and the identity code

The gateway generates two things once, and the person is told to save both:

- **the identity code**, eight characters like `XK7F-9Q2M`, derived from the gateway certificate's
  SHA-256, in Crockford base32 so it contains no I, L, O or U;
- **the pairing words**, five words from a 2048-word list.

Later, the config tool is given the gateway's address and asks the gateway to prove itself:

1. connect and complete the TLS handshake,
2. derive the identity code **locally from the certificate that was actually received**,
3. show it and wait for the person to confirm it matches what they wrote down,
4. only then send the words.

The order is the whole security property. The words guard the gateway's certificate authority, so
they must never go to a host that has not proved itself, and the proof is computed locally rather
than displayed from a response, because a wrong gateway could simply claim the right value.

If the person says the code does not match, nothing is sent and nothing is stored.

## What needs a cable, and why the tool says so

The configuration plane carries playback volume, microphone gain, LED brightness, the wake
threshold and wake word, locale and update channel. It does **not** carry Wi-Fi profiles, the dial
address, TLS credentials or the wake model file. On a Pi, Wi-Fi additionally means taking the card
out.

So the profile marks those as `cable`, the UI shows them differently, and the USB path is offered
when a device is plugged in. **USB provisioning needs no credentials at all**: physical access to a
device is ownership of it, which also means nobody can lock themselves out of their own gateway by
running the installer locally.

## Status of this first draft

Working: the profile contract and registry, the identity code, the pairing client with the
confirmation gate, the credential store with the remember checkbox, the drivers for both devices,
the console flow, the web UI, and 63 offline tests.

Not yet: the **pairing endpoint on the gateway** (the client exists, the server side does not), the
wake-word and voice catalogues, Piper voice previews, and the packaging into per-OS executables.

Delegation: the gateway and device steps currently run the existing `scripts/install-*.sh`, which
are the engine and already idempotent with `--dry-run`. Absorbing them into the package is tracked
work, and until then this document should not pretend they are already absorbed.
