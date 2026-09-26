# The outbound link: the device dials the bridge

**Where this sits:** stage 3 stream of the voice pipeline. The end-to-end map, and every other stage, is in `docs/architecture.md`.

This is the device half of the connection inversion. The bridge keeps a registry of devices that dial in,
and this is what makes a device dial. It is the reason a satellite works on a network nobody can reach into:
there is no inbound path to arrange, no port forward on either side, and no fixed device address to track.

## What the device has to know, and what it deliberately does not

The device carries exactly two things: **where the bridge is** and **what this device is**. Everything else
that a person might think of as device configuration, the name, the voice, the wake word, the LED behaviour,
is policy and lives on the bridge, so it can be changed without touching the device.

That lives in one file, referenced by the unit as `EnvironmentFile`:

```
VOICE_DEVICE_ID=                  # required: stable identity, not a display name
VOICE_BRIDGE_HOST=                # required: where to dial
VOICE_BRIDGE_PORT=10701           # the registry listener's port
VOICE_DEVICE_NAME=                # optional: what the bridge calls it
VOICE_DEVICE_AREA=                # optional: which room it is in
VOICE_CAPABILITIES=led,speaker,microphone
VOICE_BRIDGE_TLS_CA=              # optional: validate the bridge (see the TLS section below)
VOICE_BRIDGE_TLS_CERT=            # optional: this device's client certificate, for mutual TLS
VOICE_BRIDGE_TLS_KEY=             # optional: its key
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

   The two do share the audio device, by design and not by contention: the relay hands the bridge's
   events to that same satellite service, so it is the satellite doing the listening and the speaking,
   exactly as before, just reached over an outbound socket. The older inbound listener may remain available
   during migration, but the bridge does not race both connection shapes. In registry mode it waits for the
   outbound lease; `VOICE_DIRECT_DIAL_FALLBACK=1` explicitly restores the old dial as a rollback path, after
   the configured lease grace period.

## What it does

The link registers, holds the connection, answers pings, reconnects with capped jitter, and then **relays**.
The device also enables TCP keepalive on the bridge socket: 15 seconds idle followed by three probes five
seconds apart. A powered-off or rebooted bridge therefore becomes a connection error in roughly 30 seconds
instead of leaving a device-side socket falsely `ESTABLISHED` for the kernel's multi-hour default. The normal
reconnect loop then re-resolves the endpoint, reconnects and registers a new generation.

Relaying is the audio: once registered, the runner connects to the satellite service running on the same
device and becomes a transparent pipe between it and the bridge, in both directions.

That is a deliberate choice rather than a missing implementation. The audio path on a device is not just
"socket in, sound out": it is wake-word detection, microphone capture, playback, the chime, the LED states,
and months of tuning against a real room. Writing a second copy of that on the far side of a socket would be
two implementations to keep in step, and this project has already paid once for two code paths disagreeing
about who owns a socket. The device keeps the audio it is already good at, and the outbound link carries it.

So the bridge sees exactly the satellite it has always talked to, with one difference: the device opened the
connection instead of waiting to be dialled. That also means `describe`, `run-satellite`, audio up and down,
`played`, and the LED events all work unchanged, because the thing answering them is the same service as
before. Set `VOICE_RELAY=0` to turn the relay off, which is only useful on a device that has no local
satellite to relay to.

## Verifying it

On the device, `sudo journalctl -u outbound-satellite -n 20` and look for a line like:

```
INFO pi.outbound: registered with the bridge as <device-id> (generation N)
```

On the bridge, the same registration appears, along with the device's self-reported capabilities and a slot
clear when the device disconnects:

```
bridge.registry: device <device-id> registered (<name>), generation N
bridge.loop:     device registered: id=... capabilities=led,speaker,microphone
```

Restart the unit and the bridge should show the old generation cleared and a new one registered, which is
the evidence that a device's own reconnect is not undone by its stale handler.

## What it does not do yet

**Standalone audio.** The relay carries a conversation only when the device also runs its own satellite
service, which is the case on the Pi and is why this works today. A device with no local satellite (the ESP32
firmware in #44, say) has to speak Wyoming itself, which is its own piece of work. A second device being
conversed with at the same time as the first is also not supported: the bridge leases one at a time.

## TLS, and why the listener may be unauthenticated in the meantime

The registration credential model is built on certificate fingerprints, which only mean something once TLS
is terminated at the bridge. Until that exists, a listener on a trusted private network can be run with an
explicit opt-in that accepts self-claimed identities, and it warns loudly on every start when it is. That
opt-in must never be reachable from an untrusted network, and it should be removed the moment a credential
file exists.

Two operational notes that cost real time to find:

- The runner needs an interpreter that can import `wyoming`. On a device built with `pi/script`, that is the
  satellite's virtualenv. The system `python3` cannot import it, and the failure looks like a missing library
  rather than the wrong interpreter.
- Run any shell diagnostics on the device with `bash`. `/dev/tcp/host/port` is a bash feature, and on Debian
  the interpreter for a `#!/bin/sh` script is `dash`, which fails the redirect silently and reports **every**
  port as closed.
