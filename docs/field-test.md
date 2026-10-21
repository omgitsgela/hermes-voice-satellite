# Field test: bare board to working satellite

The acceptance runbook. It takes one enrolled identity and one board from nothing to a
satellite you can talk to, in the order the steps actually have to happen, and it names the
one command that proves each step and the one failure you are most likely to hit.

Everything here is a placeholder — `<host>`, `<device-id>`, `<your-net>`, `192.0.2.10`,
`<usb-port>`, `<psk-file>`, `<gateway-dir>` — with real commands around them. Nothing in this
document is a value from a live installation. `192.0.2.10` stands for the gateway address a
device can route to (the `--device-reachable-as` value used at gateway install). Ports:
`10701` is the registry's plaintext listener, `10702` the TLS listener devices dial.

Prerequisites, from `docs/installation.md`:

* the gateway host runs Hermes and `scripts/install-gateway.sh` has completed
  (`systemctl --user status hermes-voice-bridge` is active, and `state/registry-tls/` holds the
  CA + server certificate);
* a machine with the board attached by USB (the board appears as a serial device, typically
  `/dev/ttyACM0` on `<usb-port>`) — often a laptop rather than the gateway host;
* the prebuilt flash bundle: `flash.bundle.bin` + `flash.manifest.txt` beside the checkout
  (default `--bundle-dir` is `../xvf3800-flash`; pass `--bundle-dir <dir>` otherwise).

Every installer has `--dry-run`; use it first if you want to see each command before it runs.

---

## 1. Enroll the device on the gateway (gateway host)

```sh
cd <gateway-dir>
.venv/bin/python scripts/issue-registry-tls.py --state state enroll --new <device-id> \
    --endpoint-host <host> --endpoint-port 10702
```

`<device-id>` is the identity the board will register as; letters, digits, `-` and `_` only.

**Success looks like:**

```
<device-id> enrolled (N device(s) on file)
  fingerprint : sha256:...
  certificate : <gateway-dir>/state/registry-tls/<device-id>.crt
  credentials : <gateway-dir>/state/device-credentials.json
```

followed by the exact `install-esp32.sh --provision` / `install-pi.sh` command for this device.
Sanity check the whole set with
`.venv/bin/python scripts/issue-registry-tls.py --state state verify` →
`verify: N enrolled device(s), CA + server certificate + credentials all consistent`.

**Top failure:** `<device-id>.crt already exists at ...; pass --force to reissue` — the id is
already enrolled. Pick a fresh id, or reissue with `--device <device-id> --force` and
re-provision the board (it pins the certificate). If instead you see `no CA at ...; run init
first`, the gateway's TLS material was never minted: run `scripts/install-gateway.sh` first.

## 2. Bring the material to the flashing machine (if it is not the gateway)

```sh
mkdir -p ./registry-tls
scp <host>:<gateway-dir>/state/registry-tls/{client-ca.crt,<device-id>.crt,<device-id>.key} ./registry-tls/
scp <host>:<gateway-dir>/state/device-credentials.json ./
```

`install-esp32.sh` looks for `device-credentials.json` **beside** the `--tls-certs` directory
(override with `--registry <file>`). Both files are copied because the provisioner cross-checks
the certificate against the bridge's credential entry before writing anything.

**Top failure:** provisioning dies with `no registry file at ...` — the credentials copy is not
beside the certs directory; re-copy it or pass `--registry`.

## 3. Flash the board (flashing machine)

```sh
scripts/install-esp32.sh --flash --port <usb-port>
```

No ESP-IDF needed: this writes the prebuilt bundle (bootloader + partition table + app) to
`0x0`. It is the only step with teeth, so it is never implied — `--flash` is required.

**Success looks like:** `OK bundle flash.bundle.bin sha256=...`, `bundle check: the prebuilt
image matches the reviewed manifest`, esptool writing and verifying (`Hash of data verified.`),
then `install-esp32: done`. Sanity: `python3 scripts/esp32_provision.py --port <usb-port>
status` answers `OK UNCONFIGURED` — a board that answers at all is flashed and alive.

**Top failure:** `no bundle at ... (download the prebuilt bundle, or pass --build to compile
from source)` — the bundle directory is empty or wrong (pass `--bundle-dir <dir>` holding both
`flash.bundle.bin` and `flash.manifest.txt`). `bundle disagrees with the reviewed manifest` —
the file is not the reviewed image; re-download it. esptool `Failed to connect to ESP32-S3` —
wrong `<usb-port>`, or the board is not accepting a download (hold BOOT while re-plugging).

## 4. Provision it (flashing machine)

```sh
scripts/install-esp32.sh --provision --port <usb-port> \
    --tls-certs ./registry-tls --device-id <device-id> \
    --endpoint tls://<host>:10702 \
    --wifi-ssid <your-net> --wifi-psk-file <psk-file>
```

Provisioning is a **set, not a menu**: TLS credential, wake model, endpoint and Wi-Fi profile
are all required and an incomplete set is refused before a byte is written. The wake model
default to the bundled `okay_nabu` (`firmware/esp32/models/`, licence note in its
`NOTICE.md`); `--model <file>` or `--download-model <url>` stage a different one.
`<psk-file>` holds exactly the Wi-Fi passphrase (plus one trailing newline) — the passphrase
moves as a file path, never through argv.

**Success looks like:** `certificate matches the bridge registry entry for <device-id>`,
`PROVISIONED <device-id> at 0x...: ... bytes`, `readback byte-identical to the region image`,
`using the bundled default wake model`, and finally `provisioned: TLS credential, wake model,
endpoint, Wi-Fi. On reboot the board associates, dials tls://<host>:10702 and registers as
<device-id>.` Sanity: `python3 scripts/esp32_provision.py --port <usb-port> list-profiles`
shows `<your-net>`.

**Top failure:** `no <device-id>.crt in ./registry-tls (is the device enrolled?)` — the cert
set from step 2 is not there. `... does not map sha256:... to <device-id>` — the certificate on
disk is not the one the bridge has on file for this id (stale copy, or the device was
re-enrolled after copying): re-copy `registry-tls/` + `device-credentials.json`, or re-enroll.
`<psk-file> is neither an 8-63 character passphrase nor a 64-hex raw key` — the passphrase file
holds the wrong bytes.

### Pi satellite instead (steps 3–4)

On the Pi, after copying the same cert set to `/etc/voice-satellite/registry-tls/` (see
`docs/installation.md`):

```sh
sudo scripts/install-pi.sh --base <device-base> --device-name <device-id> \
    --endpoint tls://<host>:10702 \
    --tls-certs /etc/voice-satellite/registry-tls
```

(`<device-base>` is the upstream wyoming-satellite root on the Pi, e.g. `/opt/wyoming-satellite`.)

**Success:** the coupled audio units and `outbound-satellite` are enabled and active
(`systemctl status outbound-satellite wyoming-satellite`). **Top failure:** `--tls-certs`
without `--endpoint` (credentials with nowhere to dial), or a certs directory that does not
already hold `client-ca.crt` + `<device-id>.crt`/`.key` **on the Pi**.

## 5. Confirm it registers (gateway host)

Power-cycle the board (or run `python3 scripts/esp32_provision.py --port <usb-port> connect`
to join now), then run the one command that checks the whole chain, read-only:

```sh
cd <gateway-dir>
.venv/bin/python scripts/verify-satellite.py --device-id <device-id>
```

It proves four links against live state — the registry listener is really listening and
accepting, the TLS set is consistent (the issuer's own `verify`), the device is in
`state/device-credentials.json`, and the bridge's own state/log show the device registered —
and exits non-zero if any link fails.

**Success looks like:**

```
verify-satellite: device <device-id>, state <gateway-dir>/state, registry <host>:10702 (TLS)
PASS  listener    ss: LISTEN on 0.0.0.0:10702; connect <host>:10702: accepted
PASS  tls         issuer verify: verify: 1 enrolled device(s), CA + server certificate + credentials all consistent
PASS  credentials <device-id> in device-credentials.json, certificate fingerprint matches
PASS  registered  fleet-config: registration identity recorded (...); log: ... device registered: id=<device-id> ...
verify-satellite: PASS (4/4 links) -- <device-id> is registered and ready
```

**Top failure:** `FAIL registered ... no live evidence that <device-id> has registered` — the
board never reached the bridge. Watch `journalctl --user -u hermes-voice-bridge -f` while
power-cycling the board: `refused a registration from <addr>: no recognised credential` or
`unknown credential sha256:... from <addr>` means the board presents a certificate the bridge
does not know (re-enroll, then re-provision). A dial failure naming the host and port (ESP32:
`GATEWAY_STATUS` on the provisioning console; Pi: `journalctl -u outbound-satellite`) means a
wrong `--endpoint` or no route to `<host>`. A `FAIL listener` with `Connection refused` means
nothing is listening on the port: check `systemctl --user status hermes-voice-bridge` and the
port in `bridge.env`.

## 6. Speak the wake word (the acceptance test)

Stand at normal speaking distance and say the wake word — **"Okay Nabu"** on the ESP32
(say it clearly: its 0.97 cutoff is the selectivity gate) — **"Hey Hermes"** on the Pi —
then ask one short question.

**Success looks like:** the awake chime plays immediately, and a spoken answer comes back
within a few seconds. On the Pi that is the whole test: the acoustic wake word is live.

**Status on the ESP32:** the on-board scorer now runs real inference. The firmware loads the
provisioned model and scores microphone audio through TFLite-Micro (the log-mel microfrontend plus
a streaming inference window), so the wake word fires from the microphone without the bridge. The
console's `WAKE_STATUS` reports `scorer=tflm` when the real scorer is live and `model=real` when a
validated model is loaded; if either reads as the stub/placeholder, re-run the provision step. The
`WAKE_FIRE` console command still forces a single detection (one line on `<usb-port>` at 115200
baud; see `firmware/esp32/README.md`), which is the right tool for exercising the chime → wake →
answer path without speaking, or when the room is too noisy to test acoustically.

**Top failure:** the chime plays but no answer — that is the mic/endpointing path, not the wake
detector; follow the turn in `journalctl --user -u hermes-voice-bridge -f`. No chime at all —
the detector did not fire: ESP32, `WAKE_STATUS` on the console (re-run the provision step if no
model is loaded); Pi, `journalctl -u wyoming-openwakeword -u wake-shim` and check the model
file exists. Too many false wakes in a busy room — say the phrase less clearly at first
(the bundled `okay_nabu` model ships `probability_cutoff 0.97`, already near the top of its
range, so the cutoff is barely a lever: `firmware/esp32/models/NOTICE.md`), and check the
room and the microphone before reaching for settings.

---

## What "done" means

- [ ] `issue-registry-tls.py --state state verify` is clean on the gateway
- [ ] the board flashed with `bundle check: the prebuilt image matches the reviewed manifest`
- [ ] provisioning ended `provisioned: TLS credential, wake model, endpoint, Wi-Fi`
- [ ] `verify-satellite.py --device-id <device-id>` reports `PASS (4/4 links)`
- [ ] speaking the wake word (or, to force one detection, `WAKE_FIRE` on the console) produces a chime and a spoken answer
