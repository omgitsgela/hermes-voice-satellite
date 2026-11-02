# Hermes Voice Satellite installer

The setup and configuration tool for **Hermes Voice Satellite**: a graphical installer for the
Gateway and its satellites, with a console path for headless Linux, that a person can run without a
terminal, a Python install, or any knowledge of what a Wyoming protocol is.

### Vocabulary

| Term | Means |
|---|---|
| **Hermes Voice Satellite** | The project, and the name of the installer command: `hermes-voice-satellite`. |
| **Satellite Gateway** | The service the satellites talk to and that talks to the Hermes API. One component with two roles, never two services. Always said in full. |
| **Satellite** | A physical device: an ESP32 with a microphone array, or a Raspberry Pi with a HAT. |
| **the Hermes gateway** | Hermes' own messaging integration (Telegram and the rest), and its API. A different program that this project talks to, never this project's service. |

**An unqualified "gateway" in this project always means the Hermes one.** That is why the service is
called the Satellite Gateway everywhere: in the interface, in the console, in log lines, and in these
documents. The earlier name was "bridge", and both earlier names survive only as compatibility
aliases: the environment file falls back to `gateway.env` and then `bridge.env`, and the device
variables are still `VOICE_GATEWAY_*`.

It is also a **configuration tool**: run it again, point it at a gateway you already set up, and
change what can be changed without a cable.

## Two roles

- **Gateway**: run where Hermes runs. Installs the Satellite Gateway, its environment, the registry TLS
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

## The gateway's pairing endpoint

The gateway holds the certificate authority and the words; a workbench on another machine holds only
the words and an address. The endpoint is how the second half gets a device identity.

```
POST /pair   (TLS)
  {"words": "five words", "device": "esp32-0011223344"}
  -> 200 {"ok": true, "ca_pem": ..., "cert_pem": ..., "key_pem": ...,
          "server_pin": "<sha256 hex>", "dial_address": "tls://host:10702"}
  -> 401 wrong words    429 too many attempts    400 bad request
```

Run it with `python -m hermes_gateway.pairing_server --state <state> --dial tls://host:10702`, or
`--check` to verify the words and certificate material without starting anything.

It listens on **port 10704 by default, not the registry port**. Devices dial the registry on 10702;
only a workbench pairing a device talks to this listener, and the two must not contend for the same
socket. The gateway setup prints both numbers, and the installer asks for the pairing port.

The response includes the gateway's **public certificate** as well as its pin, because the
provisioning step recomputes the pin from the certificate rather than trusting a number. A workbench
writes the four files the provisioner reads, named for the device's own identity:

```
client-ca.crt   <device>.crt   <device>.key   server.crt
```

That directory is created `0700`, the files `0600`, and it is removed when the session ends.

Three rules are load-bearing, not decoration:

- **The words are compared in constant time**, and they are never logged, echoed, or repeated in an
  error. A wrong guess gets the same answer whatever it guessed.
- **Failures are rate limited per source**, with a lockout after a handful of attempts. The words are
  the only thing between the internet and the certificate authority, so an unlimited guess rate would
  make the entropy meaningless.
- **The device id is validated before it reaches the filesystem**, because it becomes a filename.

The endpoint does not decide whether a device is welcome beyond the words: anyone holding them can
enrol. Rotating the words is the remedy, and enrolling devices the owner no longer wants is what
revoking their certificates is for.

## The local interface proves itself

The interface is served on loopback, but loopback is not authorisation: any page in the same browser
can POST to it, and a DNS-rebinding request can arrive with a hostile `Host`. So every API call
carries a per-launch token that exists only in this process and in the page it served, the `Host`
header must be a loopback name, and a JSON body is required with the JSON content type, because form
and text encodings can be sent cross-origin without a preflight. A request without the token gets
403, and the wrong content type gets 415.

## What each device actually runs

The device scripts are not interchangeable, and the first draft of this installer treated two of
them as if they were:

- **ESP32**: `scripts/install-esp32.sh --port <usb> --flash|--provision ...`. The port flag is
  `--port`, an explicit action is required, and provisioning also needs `--device-id`, the board's
  own identity rather than the friendly name.
- **Pi**: `scripts/deploy-to-card.sh <mount> [user]` writes onto the mounted card. `install-pi.sh` is
  the *on-device* installer: it writes into `/etc/systemd/system` and calls the host's `systemctl`,
  so pointing it at a mounted card is wrong. The card's real values live in
  `gateway-link.env`, which the script deliberately never overwrites, so the driver fills it and then
  re-runs the script: the second run is what enables the unit, which keeps that rule in one place.

## Choosing a wake word, and choosing a voice

`hermes_installer.catalogue` reads the published `rhasspy/piper-voices/voices.json` library when
browsing voices. Search by name/model ID/language and filter by language. The checked-in snapshot
remains available offline; a failed refresh never removes it. HFC female remains the default, with
HFC male beside it: both have bundled offline samples and are marked as fast choices.

Wake-word browsing refreshes free microWakeWord GitHub models and openWakeWord release assets,
scoped to the selected device's model format. A custom ONNX/TFLite file remains accepted regardless
of catalogue membership or published measurements.

- **A device is only offered wake words in its own format.** openWakeWord ONNX for a Pi, microWakeWord
  TFLite for an ESP32. They are not interchangeable, and the catalogue refuses a model in the wrong
  format rather than letting it fail on the device.
- **Both figures are shown, never one score.** Recall and false activations per hour, with the caveat
  that both were measured by the trainer on their own audio. A single quality number would imply a
  precision nobody has.
- **Our own phrase is first, and says so.** `hey hermes` exists in both formats, is the default, and
  carries no third-party trademark problem. The rest are listed honestly: `alexa` and `hey jarvis` are
  named as other people's marks, not recommendations.
- **A download is SHA-256 verified or refused.** Both Piper model and configuration are checked
  against `piper-checksums.json`, at its pinned upstream revision; existing files are rechecked too.
  The live index uses MD5, which is not silently accepted as SHA-256. A new voice without a trust
  snapshot is visible but its download is refused. Maintainers refresh the snapshot with
  `scripts/refresh-piper-checksums.py`: LFS SHA-256 authenticates model weights and Git blob IDs
  authenticate small configurations before recording their SHA-256. No weights are fetched by
  that refresh. Free wake downloads likewise require a trusted SHA-256 (snapshot maintained with
  `scripts/refresh-wake-checksums.py`); unpinned new entries are refused, and custom files stay available.
- **Voices default to HFC female**, with HFC male beside it. **The two default voices ship their
  sample clips**, 87 KB and 93 KB, served from the package itself, so the choice is audible with no
  network at all (`web/samples/NOTICE.md` records where they came from). Every other voice plays its
  published sample on demand.
- **A voice of your own is supported**: point at a Piper `.onnx` with its `.json` beside it, hear a line
  synthesised through it before deciding, and only then is it copied to the gateway's voice directory
  on the local Satellite Gateway computer. Copying a model and selecting a shared voice are separate
  actions: downloading or installing a model alone does not change the current voice.

The public API at `openwakeword.com/api` is used for *measurements only*: search and detail are
anonymous, while downloading a model from that library needs an account. Its numbers are surfaced for
comparison; the download path stays on the free sources.

## Guided pages and offline preview

The web interface begins with a welcome splash, checks for Hermes early, and separates each decision
into a short page. Each page explains what will happen. Back/Continue stay outside the scrolling
content, page changes fade out/in, and reduced-motion users get an immediate transition. On a small
screen a long catalogue or expanded technical details may still scroll inside the page; navigation
does not move. Heading focus, a polite status region, native labelled controls and an ordered
`aria-current` progress list are retained. The two approved hardware SVG drawings are unchanged.

Voice choices come from `/api/voices`: search by name, model ID or language, filter by language,
listen to each sample, then deliberately download via `/api/voice`. An offline sample clip is not
an installed model; both `.onnx` and `.onnx.json` must exist before a voice is marked installed.
The five-entry snapshot is only the offline fallback, not the live library. Local cable/card voice
selection can still update the fallback voice for a local Satellite Gateway's next restart. For
per-device selection, choose **Manage satellites over the network**: models download on the target
Satellite Gateway, not the workbench. Several model/config pairs coexist in its `voices/` directory;
assignments take effect on the next synthesis without a service restart.

Build a self-contained click-through file from the exact shipped markup/script:

```sh
python scripts/build-installer-preview.py --output <preview.html>
```

Only its API transport is replaced with an offline fixture. All five catalogue samples are embedded
MP3 data URLs; generation fetches uncached sample clips, but the resulting file needs no server,
CDN, network or adjacent asset folder. The preview has a small badge, not different product copy.
It never contacts hardware or a running Satellite Gateway. It cannot synthesize an arbitrary Piper
model in a browser: that action reports the missing local runtime rather than substituting a bundled
clip and claiming it is the uploaded voice. Use the real installer to validate a custom voice.

Optional development-only browser acceptance (Playwright is not an installer runtime dependency):

```sh
python -m pip install playwright
python -m playwright install chromium
python packages/installer/tests/browser_ux.py --preview <preview.html>
```

These tests drive both the generated file and the shipped interface with offline API fixtures.
They cover both device types, management and installation routes, back/forward, retry, focus,
reduced motion, voice search/download, real sample playback and no outbound network requests.
Backend command plans, no-flash management and console decisions run in the ordinary local gate.
Browser fixture success is not a claim that hardware was flashed or a live service reconfigured.

### Network-first fleet management

Choose **Manage satellites over the network** (console: **Manage enrolled satellites over the
network**) before selecting any cable or device. Enter the Satellite Gateway address and pairing
port, compare its certificate-derived identity code, and then supply its pairing words. This does
not enroll another device or replace any certificate. Words stay in session memory; Start over
clears the authenticated target. Each request rechecks the certificate identity before sending words.

The pairing TLS endpoint proxies only an allowlisted configuration API to the host's loopback
configuration plane. Its bearer token never leaves the Satellite Gateway. `VOICE_CONFIG_LOCAL_URL`
on the pairing service overrides the local configuration-plane address if the default loopback port
8765 is changed. Do not expose the token-authenticated HTTP plane merely to use the installer remotely.

The fleet page and console show enrolled devices (including certificates enrolled before first
connection), names, connected/offline state,
health, remote-capable controls and cable/card-only capabilities from each device profile. Explicit
profile IDs are supported; existing devices are identified by their firmware metadata, not their
chosen name. Playback/microphone volume, LED brightness and wake threshold are editable remotely;
wake-word selection is remote only where the profile declares it (currently Pi). The ESP32 profile
requires its cable for wake-word selection. Wi-Fi, dial address, TLS credentials and wake model files
are never offered as remote writes. Selecting a wake word does not install its model.

Writes use the displayed generation, reject stale generations, and read the exact device back.
Accepted/dispatched does not mean applied: reported values, generation and health decide convergence.
Offline devices remain pending. Dry-run sessions may browse but refuse remote writes and model
downloads. Refresh reported state to observe later acknowledgements; errors
remain visible rather than turning a pending write into a success claim.

The fleet voice picker searches the target host's live library and installed models. The explicit
**Approve and download** action shows the model size and records consent; assignment is a separate
operation on the chosen satellite. `voice=default` uses the host fallback. `voice` is host-owned,
never sent to Pi/ESP32 configuration agents, so old device software does not reject the new field.
Streaming replies and announcements choose their destination satellite's voice through the TTS
provider interface. Piper is the implemented provider; paid/hosted providers are not integrated.

### Dependency consent before installation

The welcome page and gateway review show a structured inventory (`what`, `why`, class, package list,
size where known). The console prints the same inventory. Unknown sizes are explicitly unknown.

1. **Bundled in the executable:** Python runtime, pyserial, cryptography, stdlib web layer and assets.
2. **Provided by the host:** the Hermes host's Python for the Satellite Gateway venv and host elevation
   for disk/service writes. The installer does not download a Python or an elevation mechanism.
3. **Installed only with consent:** the exact pinned `requirements.txt` package list and the default
   Piper model. Optional voice/model downloads have their own explicit actions and sizes.

A default-off confirmation is required before any gateway install command executes. Confirmation is
bound to the current inventory's SHA-256 and recorded with the full inventory in the session run log;
changing the package list invalidates it. Accepted consent is also saved as private, mode-0600 JSONL
in `~/.cache/hermes-voice-satellite/installer-runs/`; GUI and console show that log path. Failure to
record consent blocks installation. The host runtime is preferred from Hermes’ installed venv;
`HERMES_HOST_PYTHON` (or `HERMES_PYTHON`) explicitly chooses its Python, with host `python3` as the
compatibility fallback. No Python runtime is downloaded for the host. Previewing commands does not
grant consent. Direct engine
scripts remain manual operator tools; installer GUI and console share this backend consent guard.

### Verification boundary

The standalone preview uses offline fleet/device-report fixtures, including a connected device that
converges and an offline device that stays pending. Browser acceptance exercises authentication,
remote editing, voice download consent/assignment and dependency refusal/approval in both the shipped
UI and standalone file. `scripts/test_installer_fleet_network.py` additionally exercises real local TLS
pairing transport and real token-authenticated HTTP handlers, with fixture device acknowledgements.
Neither result claims live-device execution, a service restart or deployment.

## Changing a device later

Two entries, deliberately separated by what is actually reachable:

- **"Change a device I have already set up"** in the interface, and the same choice in the console,
  changes Wi-Fi and/or the wake word on the connected device or its card. The person chooses which
  settings to replace, reviews the command plan, and explicitly applies it. Existing credentials,
  pairing and firmware are preserved. Nothing selected means nothing to apply. The ESP32 path uses
  targeted console commands, not the first-time installer (which requires a complete credential set).
- **Manage satellites over the network** edits remote-capable volume, LED and wake settings through
  the authenticated Satellite Gateway and assigns a separate voice per satellite. No cable, firmware
  flash, certificate replacement or live-service restart is part of this route.

## What this machine can do

`status` reports the platform honestly, and both frontends show it before the first choice: whether
device flashing is possible at all (it needs a shell, so not on Windows), whether the gateway can be
supervised (systemd, so Linux), and whether the process is elevated. A step that needs an administrator
refuses with that reason rather than failing halfway through a write.

Everything a setup needs is checked **before** anything is written. `preflight` returns every reason at
once (no gateway address, no device identity, no Wi-Fi password, the wrong card partition, a missing
model file), and an install with problems writes nothing at all.

## Pairing a device that is not on the gateway's machine

Pairing returns one more thing than the certificate, key, CA and pin: the **fingerprint the gateway
registered for that device**. The provisioner refuses to write credentials unless it can confirm the
certificate is the one the gateway has on file, and on another machine there is no registry file to
check against. So the workbench writes the one line for its own device, from the value the gateway
just issued, and passes it as `--registry`. It never receives the gateway's registry, so it cannot see
any other device.

## Re-enrolling a device

A device that was reflashed has no certificate of its own any more, and the enrolment script refuses an
existing id. Pairing therefore takes an explicit `"replace": true`, which is passed through as `--force`.
A device is never silently replaced: taking over another device's identity has to be asked for.

## Local acceptance gate

There is no CI. GitHub hosts the code and issues; tests run locally. Run
`bash scripts/check-all.sh` and require its final `GATE PASSED` line before merging.
`--build` compiles firmware; it is not needed for installer interface work.
`--bundle` additionally builds and exercises the standalone executable.

## Running the pairing endpoint as a service

`python -m hermes_gateway.pairing_server --write-unit <path>` renders a systemd user unit from the
same configuration the endpoint uses, so the two cannot disagree about the state directory, the dial
address or the port, and prints the three `systemctl --user` commands to enable it. Nobody should have
to keep a long command alive in a terminal for a gateway to work.

## Building the standalone installer

```sh
scripts/build-installer.sh --check   # what would be used
scripts/build-installer.sh           # build into dist/hermes-voice-satellite/
```

The result runs with no Python, no virtualenv and no pip on the target machine, and it carries the
engine it needs: the install scripts, the Pi templates and the firmware model are inside the bundle,
and a frozen build resolves them from its own directory rather than the current one. `--check-assets`
verifies exactly that, and `scripts/check-all.sh --bundle` runs it against a packaged build with the
Python environment stripped. There is no CI: GitHub holds this project's code and issues, and every
test runs on the machine that made the change. Sizes with those assets: about **30 MB** locally, 20-21 MB before they were included. Locally, on an interpreter built
with symbols, the same build was 24 MB stripped and **241 MB unstripped**, which is why the spec sets
`strip` — and why it is set per platform, because stripping is not supported on Windows and produced a
binary there that could not load its own Python DLL.

Two honest notes for whoever ships it:

- **Windows and macOS need their own build**, run on that platform. PyInstaller does not cross-compile.
- **Windows SmartScreen will warn on an unsigned build.** Options and current costs are in the review
  notes: Store MSIX (free, needs a listing), Azure Artifact Signing (about $9.99/month for individuals
  in the US and Canada), or an OV certificate ($150 to $300/year). EV no longer bypasses SmartScreen.

The bundle contains the interface file as data, so a build that forgets `datas` produces an installer
whose window is empty; `--status` in a clean environment is the check that catches it.

## Reviewing this

```sh
bash scripts/test-installer.sh            # eight suites, offline, a few minutes
bash scripts/build-installer.sh           # a standalone bundle in dist/hermes-voice-satellite/
hermes-voice-satellite --gui              # the interface (add --dry-run to preview only)
hermes-voice-satellite                    # the same flow, in a console
hermes-voice-satellite --status           # what it detects on this machine, as JSON
python -m hermes_installer.cli            # the same, with no console script installed
python -m hermes_gateway.pairing_server --help
```

`hermes-installer` is still installed as an alias for scripts written against the older command.

Worth looking at specifically, because these are the parts where a wrong decision would be expensive:

1. **The device stage starts with the device, not the gateway.** A certificate is issued for the board's
   own identity, and that identity can only be read while the device is connected.
2. **The identity check is three answers, not a warning.** Match, don't match, and "I don't have my
   code" each lead somewhere sensible, and changing the address invalidates everything downstream.
3. **The capability badges come from the profile.** What a person is told can change remotely is the
   same data the driver obeys; a profile that claims a cable-only setting is remote is rejected.
4. **The wake-word list shows both figures** with the caveat, and offers a file of your own.
5. **`--dry-run` is a ceiling.** Nothing a client sends can turn a preview into a real install.

## Status of this first draft

Everything in this document is implemented and tested: the profile contract and registry, the
identity code, the pairing client with its confirmation gate, the pairing endpoint on the gateway with
rate limiting, bounded source tracking, serialised enrolment and explicit re-enrolment reachable from
both frontends, the credential store with the remember checkbox, the drivers for both devices against
their real scripts, device installation as two checked stages, the wake-word and voice catalogues
including bundled default samples, live community measurements, a verified voice download and a
voice of your own with a synthesised preview, the "change a device later" path, the platform report,
the console flow, the web UI with its token-gated API, per-platform builds, and the offline
suite (`scripts/test-installer.sh`); one of its tests pairs the shipping client with the real
endpoint over real TLS.

A device setup is two stages because flashing is not staging: the flash writes the software, and the
provision step puts Wi-Fi, the dial address, the credentials and the model where the device looks for
them. Success means both.

A session started in preview mode is a hard ceiling: no client request can turn it into a real run,
and the gateway install is previewed before it is executed rather than after.

What still needs **your** hardware rather than more code: flashing a real board, writing a real card,
and hearing a downloaded voice through the gateway's speaker. Pairing no longer needs a second machine
to be *proved*: `run_e2e_pairing_tests.py` serves the endpoint with a generated certificate and pairs the
shipping client against it over real TLS, which covers the wire contract and the certificate handshake.
The device steps are tested at the argv boundary, which is as far as an emulator can honestly go.

Delegation: the gateway and device steps currently run the existing `scripts/install-*.sh`, which
are the engine and already idempotent with `--dry-run`. Absorbing them into the package is tracked
work, and until then this document should not pretend they are already absorbed.
