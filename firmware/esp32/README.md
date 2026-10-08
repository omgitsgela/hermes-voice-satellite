# ESP32 firmware development

This directory contains an ESP-IDF diagnostic project and host-tested Wyoming components. The diagnostic has been built and boot-tested on a XIAO ESP32-S3; it is not yet a voice satellite.

## Diagnostic build

With ESP-IDF v6.1 activated, from this directory:

```sh
IDF_TARGET=esp32s3 idf.py build
```

This is a diagnostic, not a usable voice satellite. `app_main` runs a synthetic
loopback check through the real C sender, receiver, and header validator, logs
pass/fail, then performs a two-pattern read/write check over an explicitly allocated
1 MB PSRAM buffer. It frees that buffer, reports internal/external heap statistics
and minimum free main-task stack, then enters the USB provisioning console. It does not initialize microphone,
speaker, Wi-Fi, Bluetooth, or XMOS controls. Build defaults select 8 MB flash,
USB Serial/JTAG console, an 8 KB main task stack, and 8 MB Octal PSRAM at 80 MHz
with the SDK startup memory test enabled. Do not flash until preservation and
deployment gates have been satisfied.

The memory-pattern helper uses volatile accesses so the compiler cannot substitute
computed values for actual memory reads. This is a bring-up check, not exhaustive
RAM qualification or full-workload resource acceptance. ESP-IDF's stack high-water
API reports bytes, unlike upstream FreeRTOS's word-count convention.

Host tests: `.venv/bin/python -B scripts/test_esp32_selftest.py` from the repo root.
The host entry-point test uses a logging shim; it is not proof of MCU boot.


## Current milestone

`components/wyoming_wire/` transmits Wyoming JSON-line headers, separately serialized UTF-8 event data, and binary payloads. It also receives bounded header lines and exact-length bodies. It does not allocate heap memory. Transport callbacks handle socket, TLS, or USB I/O; they must use finite deadlines and return the number of bytes actually transferred.

Tests compile the real C implementation with `-Wall -Wextra -Werror` and check compatibility against Python Wyoming, including short transfers, embedded binary newlines, UTF-8 byte counts, EOF, oversized frames, and failed callbacks.

Run from the repository using an interpreter with the `wyoming` dependency and a host C compiler:

```sh
.venv/bin/python -B scripts/test_esp32_wire.py
```

## Control-session core

`components/wyoming_session/` is a portable, host-exercised control probe. It
answers `describe` with canonical `info` and `hermes.register`, validates a
matching device ID and protocol version in `hermes.register.ok`, and tracks
run/pause requests only after that acknowledgment. Registration is not audio
arming. `RUN_REQUESTED` is a logical state: the probe has no capture or playback
path and advertises no audio capabilities.

`wy_decode_control` validates separately serialized metadata, strict UTF-8,
finite numbers, decoded NUL, nesting limits, and duplicate keys throughout the
object tree. Body fields override inline fields as in Python Wyoming. The caller
owns the returned cJSON object and must delete it. This entry point rejects binary
payloads; it is not a general audio-event decoder.

The exact C core has exchanged control events with the project's actual Python
registry over an ephemeral loopback TCP fixture, with fragmented I/O. Refusal,
wrong-device/version acknowledgment, early run requests, framing failures and
transport errors end the session. Fresh initialization is required on reconnect.
Configuration, audio and unknown events currently fail closed rather than being
acknowledged as applied. This does not establish compatibility with the full
bridge configuration/lease/audio composition.

A session has caller-owned header/body workspace. Allocate it outside a small
RTOS task stack; JSON parsing additionally allocates temporary heap memory. One
reader and one serialized writer own the link. Adapters must impose an absolute
whole-event deadline, not reset a timeout after every short read. On any non-OK
return, close the transport; do not resume a partially read/written frame.

```sh
.venv/bin/python -B scripts/test_esp32_control_data.py
.venv/bin/python -B scripts/test_esp32_session.py
```

**USB composition:** `START_WYOMING` hands the existing USB driver from the
provisioning console to this core through `main/usb_wyoming.c`. No connection
starts at boot or on endpoint save. The host must issue that exact LF-terminated
command, wait for `PROVISION OK WYOMING CONTROL_ONLY RESET_REQUIRED`, then send
Wyoming frames on the same port. The ordinary provisioning CLI remains
staging-only and is not a gateway relay.

The adapter allocates session workspace in PSRAM, reads the factory MAC for a
stable `esp32-<hex>` ID, and generates a non-secret boot discriminator. The boot
value is not an authentication credential and its RNG entropy is not guaranteed
with the radio sources inactive. No eFuse, radio or audio setting is written.

It suppresses application logs before acknowledgment, handles short writes and
flushes the acknowledgment before reading frames. A connected idle USB host is
not EOF. After the first byte, one absolute five-second budget covers the frame
and its responses; physical USB disconnect or a protocol/I/O failure ends it.
The console then stays latched until reset, even after an allocation or
acknowledgment failure, so queued protocol bytes cannot become provisioning
commands. Ordinary status/settings commands are unavailable in that mode.

Host SDK shims exercise the actual adapter and console handoff, including idle,
short I/O, slow input/output, allocation, identity and acknowledgment failures.
Run `.venv/bin/python -B scripts/test_esp32_usb_link.py`. Audio payloads are still
rejected. A panic/reset may corrupt the byte stream; the host must fail closed
rather than treat boot output as protocol data.

**On-device acceptance (2026-10-03, application region only):** the build was
flashed to `0x10000` with readback proving the device matches the built artifact.
The pre-flash region read confirmed the bootloader and partition table were
untouched by the write, the 8 MB factory dump was re-verified immediately before
writing, and no chip erase, eFuse or security change was made.

With the laptop acting as the relay against a local canonical `RegistryServer`,
the device reported `esp32-<mac>` (protocol 1, no capabilities, 32-hex-character
boot discriminator), accepted `run-satellite` and `pause-satellite`, and still
answered `describe` afterwards. A listener that required a credential refused the
device, the refusal reached it, and it then ignored a later valid `describe`
frame, so it fails closed instead of retrying blindly. In both scenarios bytes
arriving after the link ended were not obeyed as provisioning commands, and a
real reset restored the console. The whole run passed twice.

The PC-to-gateway relay against the real bridge, Wi-Fi/TLS, enrollment, audio
payloads, wake, reconnect behaviour, combined-workload resources, factory restore
and Windows acceptance remain unverified. (Reconnect behaviour is proved by host
loopback tests of the persistent supervisor; it has not yet been exercised on this
device over a lost link.)

## Safety contract

- Exactly one reader and one serialized writer own a link.
- Close the link after any wire error. Never retry a partially transmitted frame on the same stream.
- Outbound event data must be a trusted serialized JSON object produced by the application JSON encoder. The wire layer does not validate that JSON.
- `components/wyoming_json/` validates bounded header envelopes before their declared lengths may be used: nonnegative finite integer lengths, metadata/payload bounds, event-type syntax, top-level duplicate-key rejection, complete input consumption, and object-valued inline data. It uses temporary cJSON heap allocations and a 1024-byte local buffer, unlike the heap-free wire core. Compile its pinned cJSON with `CJSON_NESTING_LIMIT=16`. Test it with `.venv/bin/python -B scripts/test_esp32_json.py`.
- The header validator alone is not a complete receive pipeline. Use `wy_decode_control` for strict control metadata decoding/merging and the session core for its supported-event dispatch/version acknowledgment. Audio-event schemas and general binary dispatch remain pending.
- Application limits: header buffer 1024 bytes including terminator; metadata 4096 bytes; one binary chunk 16384 bytes. Larger playback must be chunked, not buffered as an entire response.
- Receive-body callers provide the correct buffer capacity. The generic read helper caps length at the payload bound; metadata callers additionally enforce the smaller metadata bound.
- Plain Wyoming is trusted-network-only. No public plaintext listener.

## USB endpoint staging

The development console accepts LF-terminated printable ASCII commands on the
ESP32 programming USB port, not the XMOS audio USB port:

- `STATUS`: report `OK UNCONFIGURED` or `OK ENDPOINT <uri> STAGED`.
- `SET_ENDPOINT <uri>`: validate and persist a non-secret endpoint.
- `CLEAR_ENDPOINT`: persist an empty endpoint.

Responses begin `PROVISION `. An acknowledgment is `OK SAVED STAGED` only after
NVS commit succeeds and the value reads back exactly. Storage initialization,
commit, invalid stored data, or readback failures report `ERR STORAGE`. The
console never automatically erases NVS. It uses only namespace `hermes_dev`, key
`endpoint_v1`, preserving unrelated settings. Clearing is logical; NVS wear
leveling may retain older physical copies. No secrets belong in this field.

URIs use `tcp://host:port` (explicit plaintext for a trusted LAN) or
`tls://host:port`. Host syntax is ASCII DNS labels or dotted IPv4 spelling, with
at most 127 host characters, 63 characters per label, and a decimal port 1–65535.
No credentials, paths, query strings, IPv6 literals, or leading-zero ports are
accepted. Syntax validation is not DNS resolution or connectivity validation.
Staging a `tls://` URI does not establish a connection by itself, but it is the value the
register-on-boot gate and `START_GATEWAY` use when they do connect (see below).

Commands are at most 255 bytes. Invalid/control-byte/oversize lines and partial
lines older than five seconds are drained through LF before another command is
accepted. Send LF, not CRLF. There is one USB reader and one response writer;
responses have finite write deadlines. This is a development provisioning
console, not Wyoming audio over USB, and must not share the port with a monitor
or another client. Physical USB access can change these non-secret settings.

### The full command set

What the console answers, in the order a bring-up meets them. The Wi-Fi profile
commands and the gateway/audio commands are detailed in their own sections below.

| Command | What it does |
|---|---|
| `STATUS` | `OK UNCONFIGURED` or `OK ENDPOINT <uri> STAGED`. |
| `SET_ENDPOINT <uri>` / `CLEAR_ENDPOINT` | Stage or clear the gateway endpoint (NVS `hermes_dev`). |
| `SET_WIFI_SSID <ssid>` / `SET_WIFI_PSK <file>` / `CLEAR_WIFI` | Stage the one-shot join pair, or clear it. |
| `WIFI_STATUS` | `OK WIFI UNCONFIGURED`, or `OK WIFI ssid=<name> psk=staged\|missing`. The passphrase is only ever reported as present or missing. |
| `WIFI_PROFILE_SAVE` / `WIFI_PROFILES` / `WIFI_PROFILE_REMOVE <ssid>` / `WIFI_PROFILE_CLEAR` | Manage the saved-network list (up to four), which outlives a reboot and is what register-on-boot joins. |
| `WIFI_SCAN` | One bounded scan, framed `WIFI SCAN BEGIN` / `END`. |
| `WIFI_CONNECT` | One bounded join of the staged pair, framed `WIFI CONNECT BEGIN` / `END`. |
| `START_GATEWAY` | Join and register over Wi-Fi with the staged endpoint, framed `GATEWAY BEGIN` / `END`. |
| `GATEWAY_STATUS` | Read-only: what the persistent gateway task is doing right now. |
| `AUDIO_BRINGUP` | One bounded I2S/I2C bring-up, framed `AUDIO BRINGUP BEGIN` / `END`. |
| `TLS_STATUS` | What TLS material is provisioned, without dialling anything. |
| `ENROLL_STATUS` | The honest enrollment state: `OK ENROLL ABSENT authenticated=no detail=out_of_band` (no authenticated flow exists). |
| `WAKE_STATUS` | The local wake detector's state: model (`real`/`placeholder`), deaf, force-armed, and the scorer (`tflm` = real microWakeWord inference, `stub` = the sub-threshold placeholder). |
| `WAKE_FIRE` | Arm a one-shot forced detection for the live-test gate (no trained model needed). |
| `START_WYOMING` | Hand the USB port to the Wyoming control core (latches until reset). |

From the repo root, use a Python environment with `pyserial` installed:

```sh
python scripts/esp32_provision.py --port /dev/ttyACM0 status
python scripts/esp32_provision.py --port /dev/ttyACM0 set-endpoint tcp://192.0.2.1:10701
python scripts/esp32_provision.py --port /dev/ttyACM0 clear-endpoint
```

The example is a documentation-only address for staging tests, not a real gateway.
The client keeps DTR/RTS inactive before opening, uses bounded I/O, and filters
boot logs from provisioning acknowledgments. On a timeout or partial write,
close/resynchronize the port and query status before attempting another write.
Use a stable serial-by-id path on Linux when multiple serial devices are present.
Windows has not been exercised.

Focused tests: `scripts/test_esp32_provisioning.py`, `scripts/test_esp32_console.py`,
and `scripts/test_esp32_provision_tool.py`. Host storage/USB fault shims test the
actual adapter, but are not proof of real NVS reboot persistence.

The provisioning build has also been exercised on a physical XIAO ESP32-S3:
flash-region readback matched the built binaries; a synthetic endpoint survived
an MCU reset; clearing it survived another reset; invalid scheme/port, oversize
lines, and embedded NUL were rejected, followed by successful normal commands.
The command-line client read the final unconfigured state over real USB.
This is endpoint-staging acceptance only, not network connectivity, TLS,
audio transport, power-loss-at-commit qualification, or Windows validation.

## Transport and enrollment contracts

Two interfaces are deliberately narrow. This section states each contract exactly, because "not
implemented yet" and "not part of this transport" are different claims, and a bring-up must not
have to guess which it is looking at.

### USB audio transport contract

The development USB port carries **control and provisioning only**. There is no USB Audio Class:
no ALSA card appears for the device, and no microphone or speaker PCM crosses USB. A Wyoming
control session over USB (`START_WYOMING`) speaks `describe` / `hermes.register` /
`run-satellite` / `pause-satellite`; a frame that carries an audio payload is drained and is
**never** played or forwarded to the network. Audio is carried by the onboard path instead: the
XVF3800's I2S RX is downmixed to 16 kHz mono S16 and sent as Wyoming `audio-chunk` events over
the network link (microphone), and an inbound `audio-chunk` is resampled, volume-applied,
expanded to stereo-32 and written to I2S TX (playback). Both directions are bounded by the audio
component: `WY_AUDIO_CHUNK_SAMPLES` (1024 samples per chunk) and the wire's `WY_PAYLOAD_MAX`
(16 KiB); the capture path emits 160-sample chunks. Host proof: `test_esp32_usb_link.py` (the USB
session advertises `CONTROL_ONLY`), `test_esp32_audio.py` (the `audio-chunk` data field and the
exact 32-bit-stereo -> 16-bit-mono downmix) and `test_esp32_wire.py` (the 16 KiB payload bound).
What stays unverified until the live-test gate is the real I2S stream on the board, not this
contract.

### Secure-enrollment contract

There is **no authenticated enrollment flow**. The device's identity is a provisioned client
certificate plus the pinned server fingerprint, written out of band over USB by
`scripts/provision-device-tls.py`; the device presents the certificate in the mTLS handshake and
the bridge resolves its fingerprint to a device id. The device **must never claim an enrollment
it does not have**: its `hermes.register` reports only its real state (`protocol_version` and its
actual `capabilities`, e.g. `["microphone"]`) and never a credential or an "enrolled" flag it
cannot back. The provisioning console's `ENROLL_STATUS` reports the honest state
(`authenticated=no detail=out_of_band`). An authenticated enrollment exchange -- the device
proving possession of a factory or one-time secret to receive its certificate over the network --
is future work; until it exists, provisioning stays out of band and a listener refuses an unknown
fingerprint unless an explicit trusted-network opt-in is set.

## Relay it to a gateway from a PC

`scripts/usb_gateway_relay.py` owns the device's USB port on a PC, puts the device
into its link mode, and becomes a transparent pipe to a gateway's registry
listener. It is the PC-side counterpart of `pi/outbound_satellite.py`, with the
relay leg pointed at a serial port instead of a local satellite.

```sh
python scripts/usb_gateway_relay.py \
  --port /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_...-if00 \
  --gateway 192.0.2.1:10701 --ready-marker 'PROVISION READY' \
  --expect-device-id esp32-<mac hex> --stop-after-registration
```

It never speaks the protocol: it sends the one mode command, then moves bytes, and
writes nothing else to the device. It refuses to dial the gateway until the device
has acknowledged that command, so a half-booted device cannot appear as a
registered-but-silent satellite. A device banner is consumed as host bookkeeping
and never forwarded. Exit codes are 0 registered, 2 refused, 3 not registered,
4 device link failed, 5 identity mismatch.

Focused tests: `scripts/test_usb_gateway_relay.py`, which drives the relay with a
scripted device and a socketpair gateway and asserts byte-exact forwarding in both
directions.

Filed 2026-10-03 the relay registered this device with the running bridge over the
network: the bridge logged `device esp32-<mac hex> registered`, adopted it, and
cleared the slot on disconnect, with no errors and no lingering socket. Two
details that matter for the next step: the bridge immediately dispatches desired
state to a device it has not seen before, and it records a durable config entry
for it. The core now ignores well-formed control events it does not implement and
answers `ping` with `pong`, so a real bridge cannot end the session just by talking;
anything carrying a payload is still refused.

## Ask the device what it can see

`WIFI_SCAN` on the console performs one bounded scan: the station radio comes up, the
scan runs, the radio goes off again, and the report is framed by `PROVISION WIFI SCAN
BEGIN` / `PROVISION WIFI SCAN END`.

```sh
python scripts/run-wifi-scan.py WIFI_SCAN    # waits for the ready marker, then asks
```

Names are escaped (`\xNN` outside 0x21..0x7E) so every line splits on whitespace, hidden
networks are listed as `(hidden)` rather than dropped, the strongest network is first,
and anything that does not fit is reported as `TRUNCATED n`. Credentials are not involved:
the scan runs with no stored configuration and prints no key material.

Filed 2026-10-03 on hardware: the board reported the 2.4 GHz travel router as the
strongest network in range (`<ssid> ch=11 rssi=-40 auth=WPA2`) while also seeing ten
other networks, which is the check that matters, since a scan from a host machine is only
evidence about that host's radio.

## Join the network

`WIFI_CONNECT` stages nothing itself: it takes the SSID and passphrase already staged
through `SET_WIFI_SSID` / `SET_WIFI_PSK`, attempts one bounded association, and reports
the outcome between `PROVISION WIFI CONNECT BEGIN` / `PROVISION WIFI CONNECT END`.

```sh
python scripts/run-wifi-connect.py <psk-file> <ssid>
```

The passphrase is read from a file so it never appears in a process listing or shell
history, and it is never echoed: the console does not echo what it receives, and the
report carries no key material. The outcome line is machine-readable either way:

```
status=connected ssid=<ssid> ip=<device-ip> channel=11 rssi=-39
status=failed reason=15:4WAY_HANDSHAKE_TIMEOUT
```

A reason is named when this build knows the code, because the code is what separates a
wrong passphrase (`15`, `202`) from a network that is not there (`201`), and those two
failures need opposite fixes. An unknown code is reported as a bare number rather than a
guessed description. Half-staged credentials are refused outright (`ERR WIFI NOT_STAGED`):
no attempt reaches the radio until both fields are present.

The connection is deliberately not left standing. The device associates, obtains an
address, reports it, then disconnects and stops the radio, so every command is
self-contained and repeatable, and teardown is exercised on every run rather than being
assumed.

Filed 2026-10-03 on hardware: WPA2-PSK handshake completed with the travel router on
channel 11 at -40 dBm, address `<device-ip>` via `<router-gateway>`. That is the staged
passphrase proven correct by the access point, not by our own logs.

## Remember the networks

The staged pair (`SET_WIFI_SSID` / `SET_WIFI_PSK`) is one-shot: it is what `WIFI_CONNECT`
and a manual `START_GATEWAY` join. A device that should register by itself needs networks
that outlive a reboot, and those are the **saved profiles** — a different thing, a list of
up to four networks that a join tries before the staged pair:

```sh
python scripts/esp32_provision.py --port <usb> save-profile <ssid> <psk-file>
python scripts/esp32_provision.py --port <usb> list-profiles
python scripts/esp32_provision.py --port <usb> remove-profile <ssid>
python scripts/esp32_provision.py --port <usb> clear-profiles
```

The listing reports SSIDs and presence only (`WIFI PROFILE index=<n> ssid=<name> psk=present`);
a passphrase never leaves the device except as the bytes of `save-profile`, which reads it
from a file and never prints it. A WPA2 profile accepts an 8–63 character passphrase **or**
a 64-hex-digit raw key — a value too long to be a passphrase may still be a perfectly valid
key, so the validator checks the shape before refusing. Saving never consumes the staged
pair; re-staging never rewrites a saved profile; a re-save replaces in place; and a full
list refuses rather than evicting a network that works. An unreadable store is
`ERR STORAGE`, not "not configured": a device that cannot read its list must not look like
a device with no networks.

With more than one candidate, a join scans and takes the strongest one actually in range;
with one candidate it does not scan at all, so the single-network path keeps its reliability.
Every join and gateway run names what it used:
`profile source=saved|staged index=<n> ssid=<name>` — with several networks, "it
connected" says nothing without that line. A device that holds networks but can see none of
them answers `NO_KNOWN_NETWORK` and dials nothing.

## Register on boot, and ask the link what it is doing

A configured device no longer waits for a command. At boot, after the ready marker, the
console runs a **boot gate**: with a gateway endpoint staged and at least one joinable
network, it starts the persistent gateway task and says so; without either, it says why,
in the same words a manual `START_GATEWAY` would:

```
PROVISION GATEWAY BOOT
PROVISION profile source=saved index=1 ssid=<name>
PROVISION GATEWAY BOOT RUNNING
```

or

```
PROVISION GATEWAY BOOT IDLE reason=NO_NETWORK
```

The idle reasons, in the order the decision makes them: `WIFI_UNREADABLE` (the profile
store could not be read), `NO_NETWORK` (nothing saved or staged that can be joined),
`NO_ENDPOINT` (networks but nowhere to register), and `BAD_ENDPOINT` (the stored endpoint
does not parse); plus `NO_JOINABLE` (a join was attempted and could not associate) and
`FAILED reason=NO_TASK` (the decision said GO but the task would not start). Wi-Fi is
named before the endpoint, exactly the order a manual `START_GATEWAY` reports in, so a
boot line and a console command tell the same story for the same configuration. A
configured network whose access point is off at that moment is still a GO: the decision is
about what is **configured**, and the task's backoff is the retry that finds the AP when
it returns.

The task is the device half of item 1: it runs the persistent supervisor for the life of
the session — establish, register, pump — and re-dials, and re-associates, whenever the
link goes away. A bridge that **refuses** the registration is the one terminal case: the
task releases the radio and exits, and stopping is the correct thing.

Because a persistent task owns the radio while it runs, the radio commands defer to it:
`WIFI_SCAN` and `WIFI_CONNECT` answer `ERR WIFI BUSY` while the gateway is active, and a
manual `START_GATEWAY` answers `ERR GATEWAY ALREADY_RUNNING`. The queries that read state
(`STATUS`, `WIFI_STATUS`, `TLS_STATUS`, `GATEWAY_STATUS`) are always available, which is
what makes a long soak possible.

`GATEWAY_STATUS` is the read-only query the soak uses, and the one a person at the console
uses to answer "is it connected?" without starting anything:

```
PROVISION OK GATEWAY IDLE
PROVISION OK GATEWAY RUNNING last=session=registered
PROVISION OK GATEWAY RUNNING last=reconnect=registered reconnects=2
```

It carries the task's **last report line** — the supervisor's own words for what the link
did most recently — not the whole report, and not a guess: an empty `last=` is a real
answer (the task is active and has not yet appended a line), and the command dials
nothing, touches no radio, and never starts the task.

## Bring the audio hardware up (and stop there)

The XVF3800's microphones are processed by the XMOS XS3; its audio reaches the ESP32 over
I2S, and the codec is configured over I2C. `AUDIO_BRINGUP` proves that path is wired and
that the codec answers, and does nothing else — it is not capture and not playback:

```
PROVISION AUDIO BRINGUP BEGIN
PROVISION i2s=initialized i2c=codec_ok version=<major>.<minor>.<patch>
PROVISION AUDIO BRINGUP END
```

Two steps, then both buses are torn down. The I2S interface is initialized for the board
(ESP32 master, stereo, 32-bit, 16 kHz, on the documented pins) — created and initialized,
which is the interface bring-up; it is **never started**, so no stream runs and no sample
is read or written. The codec is probed over I2C and its firmware version read with the
vendor's own protocol (a three-byte write then a four-byte read, retrying on the codec's
retry status). The amplifier enable line is an XMOS GPO, not an ESP32 GPIO, and it is
never touched. A codec that answers without giving a version is not given one to show: the
report says `i2c=codec_ok` and stops there.

The report is deliberately silent about capture and playback, because they did not happen.
An I2S pin that is not where the wiring says it is reads `i2s=init_failed`; a board with
no codec reads `i2c=no_codec`; a bus that faults reads `i2c=bus_error`. Each is a
different next step, and naming the one that is true is the whole point of the command.

The pure report half (`components/audio_report`) is host-tested on its own; the console
half is host-tested against a stubbed bring-up; the firmware build proves the real I2S/I2C
driver calls are linked into the image. What remains unproved, until the board is flashed:
whether this particular board's codec answers, and on which pins.

## Soak the link

`scripts/run-soak.py` is how you find out whether a registered link **stays** registered.
It polls the read-only `GATEWAY_STATUS` command for a window, one sample per interval, and
judges the window:

```sh
python scripts/run-soak.py --port <usb> --duration 3600 --interval 5
```

Healthy means the task was active the whole window **and** the link registered at least
once. A sample whose last line is a backoff or a failed establish is **not** a failure:
a dropped link that the supervisor reconnects is exactly the property the persistent
session exists to prove, and the soak treats it as the expected behaviour it is. What
fails a soak: an `IDLE` sample (the task never ran or terminated), a `LOST` sample (the
console stopped answering — a flapping USB link, recorded rather than fatal), an
`UNEXPECTED` sample (the console changed its words), or a window in which the link never
registered. The verdict is one line and an exit code, so a soak runs equally well at a
console or as a scheduled job:

```
SOAK OK samples=721 running=721 idle=0 lost=0 unexpected=0 registered=720 backoffs=2 max_reconnects=2 window=3600.0s reason=ok
SOAK FAIL ... reason=idle_at_sample_412
```

The device must already be configured to register on boot for a soak to measure anything;
the script stages, joins, dials, and starts nothing. The verdict logic is pure and
host-tested; the polling glue is host-tested with a scripted connection and an injected
clock, so the loop is exercised on a host with no board and no pyserial. The first real
soak runs against the board at the flash gate.

## Register with the gateway over Wi-Fi

`START_GATEWAY` joins the network with the staged credentials and registers with the staged
endpoint over TCP, reporting each step as its own prefixed line:

```
PROVISION GATEWAY BEGIN
PROVISION GATEWAY status=connected ssid=<ssid> ip=<device-ip> channel=11 rssi=-38
PROVISION GATEWAY socket=connected host=<gateway-host> port=10701 transport=tcp
PROVISION GATEWAY session=registered result=ok device_id=esp32-68ee8f4b5988
PROVISION GATEWAY END
```

```sh
python scripts/run-gateway.py <psk-file> <ssid> tcp://<gateway-host>:10701
```

Unlike `START_WYOMING`, this does **not** latch the console. The protocol runs on a socket, so
the console stays usable for diagnostics while the link is up; the USB path has to latch because
the protocol *is* the console stream. The link is released before the block closes, so a caller
never inherits a socket it did not ask for.

It refuses rather than guessing. Nothing staged, half staged, or no endpoint gives
`ERR GATEWAY WIFI_NOT_STAGED` / `ERR GATEWAY NO_ENDPOINT` and dials nothing. The scheme on the endpoint
chooses the carrier: `tcp://` dials plaintext, `tls://` dials TLS, and an unrecognised transport refuses
without dialling anything at all. Never silently send plaintext to an endpoint the user marked as TLS:
that is a downgrade dressed up as a feature. A failed join reports the link failure and never reaches
the socket.

The whole session shares one deadline (25s) and the dial has its own (8s), because a peer that
accepts a connection and then stalls must not be able to hold the console open for ever.

Filed 2026-10-03 on hardware, against the live gateway: the device joined the travel router,
reached `<gateway-host>:10701` across it, registered as `esp32-68ee8f4b5988`, and the bridge's own
journal recorded `registered (ESP32 control probe), generation 2` followed by
`disconnected (generation 2, slot cleared)`. The identity is the same one the USB path uses, so
the gateway sees one device whichever transport it arrives on.

## Register over TLS

TLS is a first-class transport, chosen by the endpoint's scheme. Four things have to be on the device
for it to be used, and they land together:

| What | Where it comes from |
|---|---|
| the CA that signed the bridge's certificate | `<certs>/client-ca.crt` |
| the device's own certificate | `<certs>/<device>.crt` |
| the device's private key | `<certs>/<device>.key` |
| the fingerprint of the bridge's certificate to expect | computed from `<certs>/server.crt` |

`scripts/provision-device-tls.py` builds those into one blob using `scripts/tls_credential_blob.py`
(the single implementation of the format, shared with the host tests) and writes it at the head of the
`storage` partition over the **USB download path**, then reads it back byte for byte and checks that the
4 KB beyond the region did not change. It does not go through the console: the console is a line
protocol, and a private key typed into a line protocol is a key in a log. Two checks run before anything
is written, because both would otherwise fail silently — the device certificate's fingerprint must be the
one the bridge has on file for that device, and the offset comes from `partitions.csv` rather than being
repeated in the script.

```sh
python scripts/provision-device-tls.py --certs <certs> --device <device> \
  --host <gateway-host> --port 10702 --registry <device-credentials.json> --write
```

`TLS_STATUS` reports what is provisioned without dialling anything. It prints the whole fingerprint,
because comparing that value against the bridge's own is the entire check:

```
PROVISION OK TLS STAGED host=<gateway-host> port=10702 pin=<64 hex characters>
PROVISION OK TLS ABSENT reason=none provisioned
```

A `tls://` endpoint then reports each step as its own line, so a successful run says what actually
happened rather than only that it worked:

```
PROVISION GATEWAY status=connected ssid=<ssid> ip=<device-ip> channel=11 rssi=-38
PROVISION GATEWAY clock=unix=1791022861 synced=yes
PROVISION GATEWAY socket=connected host=<gateway-host> port=10702 transport=tls
PROVISION GATEWAY tls=handshake ok cipher=TLS-ECDHE-RSA-WITH-AES-256-GCM-SHA384
PROVISION GATEWAY tls=server_pin matched
PROVISION GATEWAY session=registered result=ok device_id=esp32-<mac hex>
PROVISION GATEWAY resource=stack_min_free=4020
```

```sh
python scripts/run-tls-gateway.py <psk-file> <ssid> tls://<gateway-host>:10702 <expected-pin>
```

Three decisions are visible in those lines.

**The bridge is identified by a fingerprint, not by a name.** The certificate the handshake produced is
hashed and compared against the provisioned pin. A name would be the weaker check: it admits any
certificate this CA signs for that name, where a fingerprint admits exactly one certificate. A name
check is also what would have to be re-implemented against an IP address.

**The comparison happens inside the handshake, not after it.** mbedTLS frees the peer's certificate as
soon as verification finishes (`MBEDTLS_SSL_KEEP_PEER_CERTIFICATE` is off in this configuration), so the
check runs in a verification callback while the certificate is still held, and a mismatch sets a
verification flag. The handshake fails there and then, rather than a wrong bridge being discovered after
a session was already established.

**The clock comes up before the handshake, and is reported whether or not it worked.** The board has no
battery-backed clock, and mbedTLS answers a certificate whose validity window the device cannot judge
with the same error it uses for a certificate that is genuinely wrong — so a clock problem would read as
a trust problem, and the fix would be looked for in the wrong place. `CONFIG_MBEDTLS_HAVE_TIME_DATE` is
enabled for the same reason: without it mbedTLS ignores the validity window entirely and would accept an
expired certificate.

Filed 2026-10-03 on hardware, against the live bridge: the device synced its clock over SNTP, handshook
TLS 1.2 with `TLS-ECDHE-RSA-WITH-AES-256-GCM-SHA384`, matched the pinned fingerprint of the bridge's
certificate, and registered as `esp32-<mac hex>`. The bridge's own journal recorded
`device esp32-<mac hex> registered (<stored name>), generation N` on its TLS listener. The negative case
was tested as well, by corrupting one byte of the pinned fingerprint in the credential region and reading
the region back to confirm the change: the device then reported
`tls=failed reason=server_pin presented=<the bridge's real fingerprint>` and did not register, and
restoring the region made it register again. That is what shows the pin is compared rather than merely
printed.

The mbedTLS contexts live in PSRAM and the handshake is the heaviest thing this firmware does on the
console task, so every run reports the task's remaining stack (`resource=stack_min_free`). The measured
runs left 4020 bytes. A `tls://` endpoint on the register-on-boot path runs the same handshake inside
the persistent task, so a TLS link also reconnects: the pin check and the clock sync run on every
dial, not only the first.

## Flash layout

The layout lives in `partitions.csv` and the build uses it (`CONFIG_PARTITION_TABLE_CUSTOM`), rather
than inheriting the SDK default or whatever the factory image happened to have.

| Partition | Offset | Size | What it is for |
|---|---|---|---|
| `nvs` | `0x9000` | 24 KB | Staged configuration, unchanged from the factory layout. |
| `phy_init` | `0xf000` | 4 KB | Radio calibration data, unchanged. |
| `factory` | `0x10000` | **2 MB** | The application. The Wi-Fi and gateway builds already used ~80% of the old 1 MB slot, which left no room for TLS or the wake detector. |
| `storage` | `0x210000` | **4 MB** | The first 8 KB holds the provisioned TLS material (see below). The rest is for wake-word models and audio assets when the audio path lands. |

Two properties are deliberate. **`nvs` and `factory` keep their offsets**, so changing the table moves
neither the staged credentials nor the running application: a table write is not a migration. And
**1.9 MB is left unallocated** rather than handed out, so a later decision (dual application slots for
over-the-air updates, for instance) has somewhere to go without disturbing anything above it.

The table was written on its own, with the reviewed layout parsed and checked before writing, a
readback compared byte for byte, and the bootloader and application regions re-read afterwards to
prove they did not move.

## Bundle the flash artifact

Flashing is the one act in this project with teeth, and it waits for a gate. `scripts/bundle-esp32-flash.py`
produces the reviewable artifact that gate runs against: the exact set of files that will be written to
the chip, at their exact offsets, with sizes and hashes, taken from the build's own flash map
(`flasher_args.json`) rather than a list that can drift from it.

```sh
python scripts/bundle-esp32-flash.py --build firmware/esp32/build                 # the manifest
python scripts/bundle-esp32-flash.py --build firmware/esp32/build \
  --bundle pending-flash.bundle.bin                                               # + the combined image
python scripts/bundle-esp32-flash.py --build firmware/esp32/build \
  --check pending-flash.manifest.txt                                              # re-hash after review
```

The manifest is self-contained: one `offset= name= size= sha256=` line per component, in flash-map
order. `--check` re-reads the build and re-hashes every file a reviewed manifest names; if a byte of
the app changed after review, it fails and names the file. The gate is then a hash comparison, not a
memory of what the build produced. `--bundle` places each component at its flash offset with zero
padding between, so a flasher can write the bundle straight to `0x0`, and reports the bundle's own
hash so it too can be pinned. The script refuses a missing or incomplete flash map rather than
guessing one: a file written at the wrong offset is not a recoverable mistake.

What flashing touches, in this layout: the bootloader at `0x0`, the partition table at `0x8000`, and
the application at `0x10000`. The `nvs`, `phy_init`, and `storage` partitions are data and are not
part of the flash map; the staged configuration, the radio calibration, and the provisioned TLS
material are therefore never touched by an application flash. The application is flashed to the
`factory` region only, with the readback that proves the written region matches the reviewed artifact
and the neighbouring regions did not move.

## Still pending

Done in this branch and pending the flash gate: a persistent session that outlives
registration (the supervisor, the register-on-boot task, and the soak that measures it);
the XMOS I2C/I2S bring-up (compiled and host-tested, not yet run on this board). What
remains genuinely open:

- **Device-side verification of this branch's code.** Everything above is proved by host
  tests and a clean build against the real driver headers; nothing in items 1–7 has been
  run on the board yet. The device still carries the multi-Wi-Fi build. The first flash is
  the gate that turns "compiled and host-green" into "on the hardware," and it bundles the
  manifest and hashes from `bundle-esp32-flash.py`.
- **A real soak on the board.** The harness is unit-tested; its first hour-long window
  against the running link has not happened.
- **Audio capture and playback.** The bring-up proves the path initializes and the codec
  answers; a stream, a sample, the amplifier, and the 4 Ω/3 W level have all been deferred
  on purpose. The XMOS audio path (the XS3's own firmware, the mic array) is a separate
  bring-up with its own checkpoint discipline.
- **A device-side XMOS readback.** The host tooling (`xmos-firmware.py`) is dry-run-only
  and reads through an injected driver; no readback of this board's XMOS image has been
  taken yet.
- **Wake inference and a selected licensed model**, and the storage it will need from the
  `storage` partition (after the 8 KB TLS region).
- **USB audio transport.**
- **Secure enrollment.** Credentials are provisioned out of band and the device-side
  material is written over USB; there is no authenticated enrollment flow.
- **A persistent PC relay service** (the relay is one bounded run) and full bridge
  configuration/lease interoperability (the probe does not implement the configuration
  plane, so the bridge holds desired-but-unreported state for a device it registers).
- **Audio-event schemas / general binary decoding.**
- **Combined-workload memory and stack measurements**, an installer, and Windows
  acceptance.

Host tests do not validate microphone or acoustic behavior. Diagnostic and provisioning
builds have been flashed, read back byte-for-byte, and boot-tested; that does not
establish production firmware, installer portability, or full voice functionality.
