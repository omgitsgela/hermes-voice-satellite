# The outbound link: the device dials the Gateway

**Where this sits:** stage 3 stream of the voice pipeline. The end-to-end map, and every other stage, is in `docs/architecture.md`.

This is the device half of the connection inversion. The Gateway keeps a registry of devices that dial in,
and this is what makes a device dial. It is the reason a satellite works on a network nobody can reach into:
there is no inbound path to arrange, no port forward on either side, and no fixed device address to track.

## What the device has to know, and what it deliberately does not

The device carries exactly two things: **where the Gateway is** and **what this device is**. Everything else
that a person might think of as device configuration, the name, the voice, the wake word, the LED behaviour,
is policy and lives on the Gateway, so it can be changed without touching the device.

That lives in one file, referenced by the unit as `EnvironmentFile`:

```
VOICE_DEVICE_ID=                  # required: stable identity, not a display name
VOICE_GATEWAY_HOST=                # required: where to dial
VOICE_GATEWAY_PORT=10701           # the registry listener's port
VOICE_DEVICE_NAME=                # optional: what the Gateway calls it
VOICE_DEVICE_AREA=                # optional: which room it is in
VOICE_CAPABILITIES=led,speaker,microphone
VOICE_GATEWAY_TLS_CA=              # optional: validate the Gateway (see the TLS section below)
VOICE_GATEWAY_TLS_CERT=            # optional: this device's client certificate, for mutual TLS
VOICE_GATEWAY_TLS_KEY=             # optional: its key
```

The file is the only place real values live. It is never committed, and it is created with placeholders by
the card-writing script so that a rebuild has something to read and a human has something to edit.

## Installing it on a device

1. Put the runner and the environment file where the rest of the device-side code lives. On a card this is
   `scripts/deploy-to-card.sh <mountpoint>`, which also writes the unit and enables it.
2. On a running device, write `/etc/systemd/system/outbound-satellite.service` from
   `pi/systemd/outbound-satellite.service.in` with the substitutions its header lists, then:

```
sudo systemctl daemon-reload
sudo systemctl enable --now outbound-satellite
```

3. The unit prefers the older satellite service without requiring it: ordered `After=` it so the relay
   usually finds its peer listening, but not `Require=`, because `_serve_local()` retries until the
   satellite appears and a hard dependency would turn that ordinary boot race into a failed unit.

   The two do share the audio device, by design and not by contention: the relay hands the Gateway's
   events to that same satellite service, so it is the satellite doing the listening and the speaking,
   exactly as before, just reached over an outbound socket. The older inbound listener may remain available
   during migration, but the Gateway does not race both connection shapes. In registry mode it waits for the
   outbound lease; `VOICE_DIRECT_DIAL_FALLBACK=1` explicitly restores the old dial as a rollback path, after
   the configured lease grace period.

## What it does

The link registers, holds the connection, answers pings, reconnects with capped jitter, and then **relays**.
The device also enables TCP keepalive on the Gateway socket: 15 seconds idle followed by three probes five
seconds apart. A powered-off or rebooted gateway therefore becomes a connection error in roughly 30 seconds
instead of leaving a device-side socket falsely `ESTABLISHED` for the kernel's multi-hour default. The normal
reconnect loop then re-resolves the endpoint, reconnects and registers a new generation.

**Keepalive runs in both directions, and it is the same numbers on both ends.** The device's outbound socket
covers a Gateway that has gone away. The Gateway's **accepted** registry socket covers the opposite case, which
is the more common one for a roaming satellite: the device loses power or its route, closes nothing, and the
gateway goes on holding a registration and a lease for something that is not there. An idle link has no timeout
of its own and the protocol offers no heartbeat — the satellite does not answer application-level pings — so
without keepalive the Gateway cannot tell at all.

Measured 2026-09-26: the satellite was power-cycled and the accepted socket stayed `ESTABLISHED` for the whole
outage, cleaned up only because the returning device displaced it. Had it stayed off, the stale state would
have persisted indefinitely and anything trying to use the device would have blocked on a dead socket. The
gateway's dialled client and its accepted registry sockets now share one implementation in `gateway/net.py`, so
the two ends cannot drift apart; the device repeats the same values from `pi/outbound_satellite.py`.

Relaying is the audio: once registered, the runner connects to the satellite service running on the same
device and becomes a transparent pipe between it and the Gateway, in both directions.

That is a deliberate choice rather than a missing implementation. The audio path on a device is not just
"socket in, sound out": it is wake-word detection, microphone capture, playback, the chime, the LED states,
and months of tuning against a real room. Writing a second copy of that on the far side of a socket would be
two implementations to keep in step, and this project has already paid once for two code paths disagreeing
about who owns a socket. The device keeps the audio it is already good at, and the outbound link carries it.

So the Gateway sees exactly the satellite it has always talked to, with one difference: the device opened the
connection instead of waiting to be dialled. That also means `describe`, `run-satellite`, audio up and down,
`played`, and the LED events all work unchanged, because the thing answering them is the same service as
before. Set `VOICE_RELAY=0` to turn the relay off, which is only useful on a device that has no local
satellite to relay to.

## Verifying it

On the device, `sudo journalctl -u outbound-satellite -n 20` and look for a line like:

```
INFO pi.outbound: registered with the Gateway as <device-id> (generation N)
```

On the Gateway, the same registration appears, along with the device's self-reported capabilities and a slot
clear when the device disconnects:

```
bridge.registry: device <device-id> registered (<name>), generation N
bridge.loop:     device registered: id=... capabilities=led,speaker,microphone
```

Restart the unit and the Gateway should show the old generation cleared and a new one registered, which is
the evidence that a device's own reconnect is not undone by its stale handler.

## What it does not do yet

**Standalone audio.** The relay carries a conversation only when the device also runs its own satellite
service, which is the case on the Pi and is why this works today. A device with no local satellite (the ESP32
firmware in #44, say) has to speak Wyoming itself, which is its own piece of work.

**Fleet conversations (#11/#32).** The Gateway now leases, arms and supervises *every* registered device at
once (issue #111 landed: a registration gets its own lease, a `run-satellite` arm, and a reader, so a second
device is wake-responsive instead of permanently deaf). What is still one-at-a-time is the *conversation*:
wakes from any armed device queue FIFO and one device owns each turn, announcements play on every armed
device except the one mid-conversation, and playback serializes per device. Single-responder arbitration when
several devices hear the *same* utterance is wired but level-based only once devices report a wake level
(`wake_level` in the detection context); until then the first arrival wins. Fully parallel conversations per
room remain open in #11.

## TLS

TLS terminates **inside the Gateway**, with no proxy in front, and the registration credential is the
fingerprint of the device's client certificate. Both halves of that sentence need each other: a fingerprint
means nothing without a handshake that actually verified a certificate, and a verified certificate means
nothing if the listener then believes the device ID in a JSON payload.

### Turning it on

Put three files in place and restart the Gateway. There is no second switch: **a deployed certificate is the
opt-in**, because "the certificates are installed" and "the traffic is encrypted" have to be the same
statement. With neither file present the listener is a plain socket, which remains a first-class choice on a
network the household controls rather than a degraded mode.

| File | Default | What it is |
|---|---|---|
| server certificate | `state/registry-tls/server.crt` | The Gateway's own certificate. Its subject alternative names must cover the address devices dial. |
| its private key | `state/registry-tls/server.key` | Keep it readable only by the Gateway's user, or point `VOICE_REGISTRY_TLS_KEY` at somewhere root-owned. |
| client CA | `state/registry-tls/client-ca.crt` | The CA that signs device certificates. Without it the listener cannot check a fingerprint, so it refuses to start. |

Each path is overridable with `VOICE_REGISTRY_TLS_CERT`, `VOICE_REGISTRY_TLS_KEY` and
`VOICE_REGISTRY_TLS_CLIENT_CA`.

Two failures are deliberately loud rather than clever:

- **Half a pair stops the Gateway.** A certificate with no key, or a key with no certificate, raises at
  startup. Falling back to a plain listener would mean someone deploys a certificate, sees the Gateway come
  up, and believes the traffic is encrypted when it is not.
- **A certificate with no client CA stops the Gateway**, because mutual TLS without the CA is not mutual
  TLS: every device certificate would fail verification and no device could ever register.

### Generating the certificates

One CA signing the Gateway and the devices is the simple case and the one these commands produce. The
`client-ca.crt` is the same file as `ca.crt`; keeping the name separate is what lets an installation that
wants a separate device CA do so without changing the code.

```sh
cd ~/hermes-voice-satellite
mkdir -p state/registry-tls && chmod 700 state/registry-tls
cd state/registry-tls
DAYS=3650

# The CA. Keep ca.key out of the repository and off any backup that is not encrypted.
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days $DAYS -subj "/CN=voice-bridge-ca"

# The Gateway's own certificate. The subject alternative names are what the device checks, so they have to
# cover the address the device dials: a hostname, an address, or both.
HOSTNAME="$(hostname)"
ADDRESS="$(hostname -I | awk '{print $1}')"
openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=$HOSTNAME"
printf "subjectAltName=DNS:%s,IP:%s\nextendedKeyUsage=serverAuth\n" "$HOSTNAME" "$ADDRESS" > server.ext
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days $DAYS -extfile server.ext
cp ca.crt client-ca.crt
rm -f server.csr server.ext
chmod 600 server.key ca.key
```

Then a certificate per device, and the credential entry that admits it:

```sh
cd ~/hermes-voice-satellite/state/registry-tls
DEVICE_ID=kitchen-1
printf "basicConstraints=CA:FALSE\nextendedKeyUsage=clientAuth\n" > client.ext
openssl req -newkey rsa:2048 -nodes -keyout "$DEVICE_ID.key" -out "$DEVICE_ID.csr" -subj "/CN=$DEVICE_ID"
openssl x509 -req -in "$DEVICE_ID.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -out "$DEVICE_ID.crt" -days 3650 -extfile client.ext
rm -f "$DEVICE_ID.csr" client.ext

# The credential is the SHA-256 of the certificate's DER form, which is what the listener computes.
FINGERPRINT="sha256:$(openssl x509 -in "$DEVICE_ID.crt" -outform DER | sha256sum | awk '{print $1}')"
python3 - "$DEVICE_ID" "$FINGERPRINT" <<'PY'
import json, pathlib, sys
device_id, fingerprint = sys.argv[1], sys.argv[2]
path = pathlib.Path("device-credentials.json")
mapping = json.loads(path.read_text()) if path.exists() else {}
mapping[fingerprint] = device_id
path.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n")
print(f"{fingerprint} -> {device_id}")
PY
```

`state/registry-tls/` is not committed. The device's key leaves this machine once, out of band, and the
device keeps it: the CA's private key never leaves at all.

### What each end does

- **Bridge.** `load_server_tls_context()` builds one context for the listener: TLS 1.2 as the floor,
  `CERT_REQUIRED`, the client CA loaded for verification. 1.2 is the floor because the ESP32 satellites
  speak mbedTLS; a floor one end cannot meet is a connection that fails in the field instead of a decision
  taken here. The listener is built by a subclass of the pinned wyoming `AsyncTcpServer`, because that
  class creates its socket without an `ssl` argument; only the two methods that create the server are
  overridden, so the handler lifecycle stays the library's.
- **Device.** `VOICE_GATEWAY_TLS_CA` is the CA to validate the Gateway against, `VOICE_GATEWAY_TLS_CERT` and
  `VOICE_GATEWAY_TLS_KEY` are its own certificate and key. A device that presents a certificate is
  registering as an identity; a device without one can only talk to an unauthenticated listener, which is
  exactly the plain local case.

### What it does not do yet

- **Enrolment is manual.** The pairing window, the rate limit and the first-boot confirmation in #61 are
  not built: a device certificate is generated here and copied to the device by hand.
- **Revocation is the removal of a credential entry**, which refuses new connections. Closing a live
  connection and telling the device it was revoked (so it stops retrying and shows an indicator) is still
  to come.
- **No rotation story.** Certificates are long-lived and replaced by hand.
- **Plain TCP keeps its place.** On a network the household controls, no certificate is a reasonable
  choice, and the listener says so loudly on every start when it runs unauthenticated with the trusted
  network opt-in. What must never happen is a listener on a reachable address with no credential file and
  no TLS, believing whatever a device claims: that is what `VOICE_REGISTRY_ALLOW_UNAUTHENTICATED` is for,
  and why it is off by default.

Two operational notes that cost real time to find:

- The runner needs an interpreter that can import `wyoming`. On a device built with `pi/script`, that is the
  satellite's virtualenv. The system `python3` cannot import it, and the failure looks like a missing library
  rather than the wrong interpreter.
- Run any shell diagnostics on the device with `bash`. `/dev/tcp/host/port` is a bash feature, and on Debian
  the interpreter for a `#!/bin/sh` script is `dash`, which fails the redirect silently and reports **every**
  port as closed.
