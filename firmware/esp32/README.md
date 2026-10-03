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
payloads, wake, reconnect behavior, combined-workload resources, factory restore
and Windows acceptance remain unverified.

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
A staged `tls://` URI does not establish that TLS or enrollment is implemented.
**Neither URI scheme activates any network connection in this build.**

Commands are at most 255 bytes. Invalid/control-byte/oversize lines and partial
lines older than five seconds are drained through LF before another command is
accepted. Send LF, not CRLF. There is one USB reader and one response writer;
responses have finite write deadlines. This is a development provisioning
console, not Wyoming audio over USB, and must not share the port with a monitor
or another client. Physical USB access can change these non-secret settings.

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
runs left 4020 bytes. This is a registration probe: it stops at registration, so run/pause handling,
reconnect-under-loss and audio over this transport are unproven.

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

## Still pending

Audio-event schemas/general binary decoding; a persistent PC relay service (the relay is one bounded run, and it is what a real deployment would replace with a network link on the device); full bridge configuration/lease interoperability (the probe does not implement the configuration plane, so the bridge holds desired-but-unreported state for a device it registers); secure enrollment (credentials can be staged, a join is proven, and registration reaches the live gateway over plaintext TCP and over mutual TLS, but there is no authenticated enrollment flow: certificates are provisioned out of band, and the device-side material is written over USB rather than exchanged); a session that outlives registration (it currently stops at registration, so run/pause handling and reconnect-under-loss are unproven); USB audio transport; XMOS I2C/I2S adapters; capture/playback; wake inference and selected licensed model; installer; combined workload memory/stack measurements.

Host tests do not validate microphone or acoustic behavior. Diagnostic builds have
been flashed, read back byte-for-byte, and boot-tested; this does not establish
production firmware, installer portability, reconnect stability, or full voice functionality.
