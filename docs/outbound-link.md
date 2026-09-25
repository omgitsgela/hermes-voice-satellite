# The outbound link: the device dials the bridge

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

3. The unit deliberately does not order itself against the older satellite service. That service still
   serves the dial-in path while the bridge supports both shapes, and the runner carries no audio yet, so
   they coexist without contending for the microphone.

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

**Audio.** This link registers, holds the connection, answers pings and reconnects with capped jitter, and
nothing else. Sending microphone audio and playing speech over it, and the bridge side of running a
conversation on a registered device, are the remaining work. Until then a device that dials in is tracked
but silent, and the older dial-in path is still the one that carries a conversation.

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
