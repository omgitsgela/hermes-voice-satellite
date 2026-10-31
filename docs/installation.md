# Installation

The whole system, end to end, in the order you actually build it:

1. a host that runs **Hermes** (the agent your voice talks to)
2. the **gateway** on that host (the Gateway: speech, sessions, and the registry that devices dial)
3. one or more **satellites**: an ESP32 board (Seeed XVF3800) and/or a Raspberry Pi
4. **prebuilt images** where a build toolchain would be a tax

Each installer has a `--dry-run` that prints exactly what it would do without writing (the gateway
dry-run is the one exception: it edits a throwaway copy of `bridge.env` so it can show you the
file you will end up with). Re-running an installer does not clobber a tuned `bridge.env` or a
live registry CA — the managed `bridge.env` keys move in place to the current flag values and the
CA is reused, never regenerated, because a regenerated CA strands every enrolled device. The
venv/dependencies step rebuilds only when its import check fails, so it is conditional rather than
strictly idempotent. The installers live in `scripts/`:

| Script | Where it runs | What it does |
|---|---|---|
| `install-gateway.sh` | the gateway host, next to Hermes | venv, `bridge.env`, registry TLS, user unit, Hermes plugin + config |
| `install-esp32.sh` | a machine with the ESP32 attached by USB | build, bundle, flash, and provision a board |
| `install-pi.sh` | the Pi itself (as root) | venv, units, `satellite.env`, the registry link, services |
| `issue-registry-tls.py` | the gateway host | mint the CA / server certificate, enroll devices, verify the set |

The order matters only at the registry: a device cannot be provisioned until the gateway has minted
its certificate, so the gateway comes first. Nothing else blocks on anything.

**Guided setup:** running any installer with **no arguments** starts an interactive guided setup
instead of failing on a missing flag. The script detects what it can (the host LAN IP, attached
USB serial ports, an existing checkout or registry state directory, existing Wi-Fi PSK files,
whether `hermes` is on PATH), prompts for every remaining value with a sane default shown in
`[brackets]` (bare Enter accepts it), validates each answer with the same rules the flags use,
prints a summary of exactly what it will do, and asks for an explicit yes/no before writing
anything. `--guided --dry-run` walks the same prompts but previews instead of writing. The guided
flow is for a person at a terminal: with a non-interactive stdin it refuses and points at the
flags, so every command in this document remains the automation path and behaves exactly as
before. A first ESP32 board needs no certificate folder: the guided flow enrols it with the
gateway's five-word setup code (see *Set up a board (plug and play)* below).

---

## 1. Install Hermes

The gateway talks to Hermes through its HTTP API and its plugin system; it does not need
`hermes-agent` inside the Gateway virtualenv. You need a current Hermes Agent install (desktop app or
CLI) on the host, at a release within a few of the current one.

```sh
# if you do not have it yet
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
hermes --version          # the gateway installer checks that the binary is on PATH before step 5
```

The gateway installer (below) installs and enables this project's one plugin,
`hermes/plugins/voice_satellite`, and applies the voice-surface config keys. You do not need to do
any of that by hand; it is in step 5 of the gateway install. See `README.md`, *Install: the Hermes
side*, for what those keys do and why the surface deliberately has no toolset override.

## 2. Install the gateway (the host side)

On the host that stays on:

```sh
git clone https://github.com/omgitsgela/hermes-voice-satellite.git ~/hermes-voice-bridge
cd ~/hermes-voice-bridge

# look first: what would it do
scripts/install-gateway.sh --base ~/hermes-voice-bridge \
    --device-reachable-as 192.0.2.10 --dry-run

# then for real
scripts/install-gateway.sh --base ~/hermes-voice-bridge --device-reachable-as 192.0.2.10
```

`--device-reachable-as` is the address a satellite will dial on this host: the LAN IP, or a name the
device can resolve. It is refused if it is `127.0.0.1`, `localhost` or `0.0.0.0`, because that is a
certificate no device could ever pin.

What the installer does, in order:

1. **venv** — `~/hermes-voice-bridge/.venv` with `requirements.txt` (faster-whisper, Piper,
   wyoming, silero VAD) plus the Piper voice `en_US-hfc_male-medium` from the catalog (a curated
   external voice is fetched instead by `scripts/fetch-voice.py` with pinned checksums; its
   speaker is selected by `VOICE_PIPER_SPEAKER`; see `docs/switching-voices.md`). To change the
   voice after install, run `scripts/switch-voice.py <voice>` — it updates both the Gateway and
   the Hermes gateway, validates the 22,050 Hz audio contract, and can `--rollback`.
2. **bridge.env** — from `bridge.env.example`, with the registry link values appended: the listener
   enabled, bound to the reachable address, the TLS certificate paths, and the satellite dial
   (`VOICE_SATELLITE_HOST/PORT`, loopback by default for the old shape).
3. **registry TLS** — `scripts/issue-registry-tls.py init` mints the client CA and the Gateway's
   server certificate (SAN = the reachable address) under `state/registry-tls/`, then `verify`
   re-checks the set. A re-run reuses the CA: regenerating it would strand every enrolled device.
4. **systemd user unit** — `hermes-voice-bridge.service` installed to `~/.config/systemd/user/`,
   enabled and started, plus lingering so it survives logout. The unit reads `bridge.env` as its
   `EnvironmentFile`, so the step-2 values are already visible to the service: no drop-in needed.
5. **Hermes side** — the plugin installed and enabled, the voice-surface keys applied to
   `~/.hermes/config.yaml` (with a timestamped backup, and no key the installer does not manage is
   touched), then a reminder to restart the gateway.

### Enrolling a device (still on the gateway host)

A device is a satellite by having three things: a certificate, a credential entry, and a way to
provision. The issuer signs the first two and prints the command for the third:

```sh
.venv/bin/python scripts/issue-registry-tls.py --state state enroll --new kitchen-esp32 \
    --endpoint-host 192.0.2.10 --endpoint-port 10702
```

That prints the exact `install-esp32.sh --provision` / `install-pi.sh` command for that device. The
registry's credential file (`state/device-credentials.json`) is updated with the device
certificate's fingerprint, computed the same way the provisioner computes it, so a device enrolled
here is accepted by the exact lookup the Gateway's registry builds.

### Verifying the gateway

```sh
systemctl --user status hermes-voice-bridge
journalctl --user -u hermes-voice-bridge -n 30
ss -ltnup | grep -E '1070[0-9]|8765'
```

The registry listens on `10701` (plaintext, trusted LAN) and `10702` (TLS, what devices dial).
The `Verify` section of `README.md` covers the Hermes-surface checks.

## 3. Install an ESP32 satellite (Seeed XVF3800)

The XVF3800 is an ESP32-S3 module with the ReSpeaker 2-mic HAT wired through. A machine with the
board attached by USB runs the installer. `autumn`-class hosts with an ESP-IDF v6.1 install can
build in place; otherwise the prebuilt image below skips the build entirely.

### Set up a board (plug and play)

Plug the board in and run the installer with no answers prepared; the guided setup does the rest:

```sh
scripts/install-esp32.sh
```

It offers to write the firmware, reads the board's own name over USB, and asks the few questions
that are yours alone: the network it joins (and its passphrase file), and how it reaches your
assistant. The one secret you type is the gateway's **setup code** -- five words printed once when
the gateway was installed, something like `correct-horse-battery-staple-ferry` -- and the installer
fetches the board's certificates with it. No folder to find, no files to copy. A brand-new board
has no secret of its own, so the request that fetches its certificate carries your setup code
instead -- the five words only you have. Second and later boards are the same.

The code is never placed on a command line, in a log, or in the output. Put it in
`~/.config/hermes-voice-satellite/enroll-token` (mode 600) and the installer uses it silently, so
it is typed once per machine; the board's fetched certificates are kept in
`~/.config/hermes-voice-satellite/certs`. The installer reaches the gateway's enrolment service at
`http://<gateway-host>:8765`, derived from the endpoint you answer; override the address with
`VOICE_ENROLL_URL` when the gateway runs elsewhere. If the gateway cannot be reached from the
machine doing the flashing, the installer says so and falls back to asking for a certificates
folder by hand (the manual way, below) -- nothing is lost either way. A `--dry-run` never contacts
the gateway.

### From source

```sh
# on the machine with the board attached (it appears as /dev/ttyACM0 via USB Serial/JTAG).
# No ESP-IDF needed: --flash writes the prebuilt bundle. Add --build only to compile from source.
scripts/install-esp32.sh --flash --port /dev/ttyACM0          # flash the prebuilt bundle
scripts/install-esp32.sh --provision --port /dev/ttyACM0 \
    --tls-certs ./registry-tls \
    --device-id kitchen-esp32 \
    --endpoint tls://192.0.2.10:10702 \
    --wifi-ssid MyNet --wifi-psk-file ./psk.txt
```

`--tls-certs` must point at a directory holding `client-ca.crt`, this device's `<id>.crt` and
`<id>.key`, with the Gateway's `device-credentials.json` beside that directory. This is the manual
way -- the automation path, and exactly what the guided flow falls back to when the gateway cannot
be reached. On the gateway host those live in `~/hermes-voice-bridge/state/`; on a separate
flashing machine (the usual case -- the gateway stays on, the board is flashed from a laptop),
copy them there first, e.g.
`scp gateway:~/hermes-voice-bridge/state/registry-tls/{client-ca.crt,<id>.crt,<id>.key} ./registry-tls/`
and `scp gateway:~/hermes-voice-bridge/state/device-credentials.json ./`. The device's `<id>` is
printed by the board itself (`python3 scripts/esp32_provision.py --port /dev/ttyACM0 status`) and is
the identity the gateway enrolls it under (`scripts/issue-registry-tls.py enroll --new <id>`, or
automatically via the setup code above).

The installer runs the provisioning helpers and esptool with the repository's own interpreter
(`.venv/bin/python` when it exists, otherwise `python3`), so both must be importable there -- and
`cryptography` too, which the setup-code enrolment uses for the encrypted answer. The repo
venv is the intended home:
`python3 -m venv .venv && .venv/bin/pip install esptool pyserial cryptography`. Without
it the installer stops up front with that exact command, instead of failing part-way through a flash.

Provisioning is a **set, not a menu**: TLS credential, wake model, endpoint, and Wi-Fi profile are
all required, because a board missing any one is a dead board. An incomplete provision is refused
before a byte is written. Everything secret moves as a file path — the certificate directory, the
model file, the Wi-Fi passphrase file — never through argv.

The four things staged to the board:

1. **TLS credential region** — the client CA, this device's certificate and key, the pinned
   fingerprint of the Gateway's server certificate, and the host:port. Written straight into flash
   by `scripts/provision-device-tls.py` (never through the console, because the console echoes what
   it is sent, and a private key through a line protocol is a private key in a log), and read back
   to prove the write.
2. **Wake model** — the bundled default is `okay_nabu` (`firmware/esp32/models/okay_nabu.tflite`,
   attribution and licence in `firmware/esp32/models/NOTICE.md`). `--model` takes any local model;
   `--download-model` fetches one.
3. **Endpoint** — `tls://host:port` (or `tcp://` for a trusted LAN). This is what the board dials;
   it is staged separately from the credential region on purpose, so the two cannot drift apart.
4. **Wi-Fi profile** — the SSID and the passphrase file. The device reports the profile's presence,
   never its value.

On reboot the board associates, dials the endpoint, registers as its identity, and the registry
accepts it by certificate fingerprint. The wake word is detected **on the device**: the firmware
runs the MicroWakeWord model through TFLite-Micro (the log-mel microfrontend, a streaming inference
window, and a sliding-window cutoff), so the board does not need the Gateway to notice a wake. The
`WAKE_FIRE` console command still forces a single detection, to exercise the chime-and-wake path
without speaking. On-board acoustic behaviour is proven only by actually speaking the wake word in
a quiet room.

### The prebuilt image

Building the firmware needs ESP-IDF v6.1 and a target build; a user who just wants a working board
should not need either. The flash bundle is the reviewed artifact — one image written to `0x0`,
with a manifest that names every component at its offset with size and hash, produced from the
build's own flash map:

```
offset=0x0      bootloader/bootloader.bin       21168   sha256=09935581...
offset=0x8000   partition_table/partition-table.bin 3072 sha256=31d35d02...
offset=0x10000  hermes_esp32_diagnostic.bin    1204432  sha256=0f0c5da7...
```

The manifest carries the bundle's own hash as a `bundle_sha256=` header, so a prebuilt bundle you
have been given can be verified with no build tree:

```sh
.venv/bin/python scripts/bundle-esp32-flash.py --verify flash.bundle.bin --check flash.manifest.txt
# OK bundle flash.bundle.bin sha256=b6354a66...
# bundle check: the prebuilt image matches the reviewed manifest
```

If you compiled the firmware yourself, `--check` re-hashes every component in the build against the
manifest instead:

```sh
.venv/bin/python scripts/bundle-esp32-flash.py --build firmware/esp32/build --check flash.manifest.txt
# OK bootloader/bootloader.bin offset=0x0
# OK partition_table/partition-table.bin offset=0x8000
# OK hermes_esp32_diagnostic.bin offset=0x10000
# manifest check: all files match
```

A reviewed bundle can be flashed without the build tree, because esptool writes the one file
straight to `0x0` (the same invocation `install-esp32.sh --flash` uses):

```sh
python -m esptool --chip esp32s3 --baud 460800 --before default-reset --after hard-reset \
    write_flash --flash_mode dio --flash_size 8MB --flash_freq 80m \
    0x0 flash.bundle.bin
```

**Flash rules.** One file, to `0x0`, DIO flash at 8 MB and 80 MHz (the partition table the app was
built against). No chip erase: the TLS credential region and the wake model live in their own
partitions, and erasing the chip would wipe a provisioned satellite back to a dead board. No
partition-table change: the offsets in the bundle are the build's flash map, and a different map is
a different app.

The wake model is a second, separate write to the `model` partition — that is what
`install-esp32.sh --provision` does, and it is why the model swap never required an app rebuild.

The current reviewed bundle (the one the manifest above describes) is 1,269,968 bytes,
`sha256 b6354a66c6d6d25c23b72ca399b1f3840e31de2fc78299120602a74b81c44103`. The prebuilt bundle and
its manifest ship together as a release asset. `install-esp32.sh --flash` verifies the bundle's own
hash (`--verify`, no build tree) before writing it — that is the gate between "a file named
`flash.bundle.bin`" and "the reviewed image". `install-esp32.sh --build` is the opt-in path that
compiles from source and re-hashes every component instead.

### A Raspberry Pi satellite

The Pi's audio stack is the upstream **wyoming-satellite** project (see
`docs/owning-the-satellite-stack.md`); this repository's `pi/` tree adds the outbound registry
runner and its sibling modules plus the systemd units. `install-pi.sh` copies those modules into
the satellite's `examples/` directory (where the audio units' `ExecStart` and the rendered
`outbound-satellite` unit expect them) and renders the units, so the real sequence is:

```sh
# 1. On the Pi, install the upstream wyoming-satellite per its own docs: the checkout ($VS_BASE)
#    with its venv at $VS_BASE/.venv and script/run. This is a prerequisite the audio units assume.

# 2. Bring the enrolled certificate files onto the Pi -- they live on the gateway host, not the Pi:
scp 192.0.2.10:~/hermes-voice-bridge/state/registry-tls/{client-ca.crt,kitchen-pi.crt,kitchen-pi.key} \
    /etc/voice-satellite/registry-tls/

# 3. Install, from a checkout of THIS repository. --base is the wyoming-satellite root; --tls-certs
#    points at the LOCAL cert directory just copied; --endpoint is the registry link the outbound
#    unit dials.
sudo scripts/install-pi.sh --base /opt/wyoming-satellite --device-name kitchen-pi \
    --endpoint tls://192.0.2.10:10702 \
    --tls-certs /etc/voice-satellite/registry-tls
```

`install-pi.sh` builds the satellite venv, installs the nine `pi/*.py` device modules into
`$BASE/examples/`, renders `satellite.env` (identity, wake word, and the `VOICE_BRIDGE_*` registry
link that `outbound-satellite` reads), installs the coupled audio units **and** `outbound-satellite`
(rendered from its `.in` template: the env file, the venv python, the runner path), and enables them
as one group. Two things it refuses: `--tls-certs` without `--endpoint` (credentials with nowhere to
dial is a dead device), and a `--tls-certs` directory that does not already hold `client-ca.crt`
plus this device's `.crt`/`.key` **on the Pi** -- it verifies their presence before it writes
anything.

The Pi image story is different from the ESP32's: the satellite software is small (the upstream
wyoming-satellite checkout + the `pi/` tree + the units), so there is no large prebuilt artifact to
publish. What is prebuilt for the Pi is the *state*, not the image: the wake model
(`hey_hermes.tflite`), the sounds, and the rendered `satellite.env`. A Pi becomes a satellite in
minutes, which is why the installer is the unit of distribution rather than an SD image.

## 4. After the install

```sh
# gateway host
journalctl --user -u hermes-voice-bridge -f      # watch a device register

# device (ESP32: the provisioning console; Pi: the journal)
python3 scripts/esp32_provision.py --port /dev/ttyACM0 status
journalctl -u wyoming-satellite -u outbound-satellite -n 40
```

A device that registers shows up in the Gateway log with its identity; a device that cannot reach
the Gateway shows a dial failure with the host and port it is trying. Speaking to the device is the
only real test of the wake word, and see *Verify* in `README.md` for the surface checks.

## Where things live

| Thing | Path |
|---|---|
| Gateway venv | `~/hermes-voice-bridge/.venv` |
| Gateway config | `~/hermes-voice-bridge/bridge.env` |
| Registry TLS | `~/hermes-voice-bridge/state/registry-tls/` |
| Credential file | `~/hermes-voice-bridge/state/device-credentials.json` |
| Enrolment setup code | `~/.config/hermes-voice-satellite/enroll-token` (optional; typed once) |
| Board certificates (fetched) | `~/.config/hermes-voice-satellite/certs/` |
| Gateway unit | `~/.config/systemd/user/hermes-voice-bridge.service` |
| ESP32 flash bundle | the reviewed `flash.bundle.bin` + `flash.manifest.txt` |
| ESP32 wake model | staged to the board's `model` partition |
| Pi satellite env | `/etc/voice-satellite/satellite.env` |
| Pi outbound link | `/etc/systemd/system/outbound-satellite.service` |

`SECRETS.md` lists every credential the system needs and where it lives. Read it before deploying
to a network you do not control.
