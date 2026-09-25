# The Wyoming protocol: what we rely on, and what we could

Reviewed 2026-09-23 against the protocol repository **`github.com/OHF-Voice/wyoming`** (note the org:
this project lived under `rhasspy` when we adopted it). Our Python dependency is `wyoming` 1.5.4, pinned
in the satellite's venv. This is the transport for everything we do: the bridge talks to the satellite,
and the satellite talks to the wake engine and the LED service, all in this protocol.

The protocol is a peer-to-peer TCP protocol, effectively JSONL plus PCM audio: a JSON header line, then
optional additional data, then optional binary payload.

## The security model, which is part of our design

Straight from the repository, and worth quoting because it settles a recurring question:

> Wyoming has no authentication or encryption, by design. It is meant for a trusted network, and should
> not be exposed directly to the internet. The same applies to the optional HTTP servers
> (`wyoming.http.*`), which are unauthenticated gateways that run on the Flask development server.

Three consequences for us:

1. **The satellite listening on the LAN is by design, not an oversight.** Port 10700 and SSH are the two
   LAN listeners on the device, and the protocol expects exactly that. Read the README's exposure claim
   in that light: our control paths are loopback, and the protocol port is the intended exception
   (issue #39).
2. **Never expose a Wyoming port to the internet**, and we do not.
3. **We do not use the `wyoming.http.*` gateways**, which are unauthenticated and run on a development
   server. If anyone is tempted, the answer is no.

## Who dials whom, and why that is changing

The protocol is symmetric in its events but not in its connections: one side listens, the other dials. Until
now **the device has been the server**: `wyoming-satellite` listens on `tcp://0.0.0.0:10700`, the bridge
dials it, and the device is inert until a client sends `run-satellite`. The device's only client role is
toward its wake engine, where `--wake-uri` points at the local wake service.

**Decided 2026-09-24 (issue #61): the direction inverts.** Satellites open the connection to the bridge, the
bridge runs a Wyoming server, and it keeps a registry of the devices connected to it. Reasons: the end user
then manages one set of configuration, on the device; nothing has to reach the device, so there is no port
forwarding to arrange on either side; and it is the shape a fleet needs, because identity arrives with the
connection.

| Relationship | Server | Client | Notes |
|---|---|---|---|
| Bridge ↔ satellite | **bridge** (after the change) | **device** | today this is the other way round: the device listens and the bridge dials |
| Satellite ↔ wake engine | wake service | device | unchanged, and stays on the device's loopback through the wake shim |
| Satellite ↔ LED service | LED service | satellite | unchanged, loopback only |

The event vocabulary does not change, and the direction each event travels does not change either. What
changes is which side accepts the socket, plus a **registration step that the protocol does not define**:

- **The protocol has no authentication, no encryption and no notion of a device identity.** `info` carries
  `satellite.name` and `satellite.area`, and both are metadata a client can claim, so neither can be an
  identity. Identity therefore has to come from the transport (mutual TLS, with the credential fingerprint
  as the registry entry) or from an authenticated tunnel, never from a field in an event.
- **`run-satellite` is still required after every new connection.** The device is inert until it arrives,
  which is what makes an unregistered socket harmless: it can connect and say nothing useful.
- **One reader per socket.** Playback, capture, control events and the reply path all share the one
  connection the device opened, and exactly one coroutine may read it, because competing readers swallow
  each other's events.

The full design, prerequisites and acceptance criteria live in issue #61.

### Our registration extension

The protocol has no registration, so we add three events. They are an extension, and a client that predates
them simply never registers, which is why the listener drops a connection that says nothing.

| Event | Direction | Carries |
|---|---|---|
| `hermes.register` | device → bridge | `device_id`, `name`, `area`, `firmware_version`, `protocol_version`, `capabilities`, `boot_id` |
| `hermes.register.ok` | bridge → device | the accepted `device_id`, `protocol_version`, `name`, `area` |
| `hermes.register.refused` | bridge → device | a human-readable `reason` |

The order on a new connection:

1. The bridge sends the standard **`describe`**. The device answers **`info`**, which is where the
   protocol's own metadata arrives (`satellite.name`, `satellite.area`, `active_wake_words`).
2. The device sends **`hermes.register`** with its identity claim.
3. The bridge checks that claim against whatever the transport proved. It answers
   **`hermes.register.ok`** or **`hermes.register.refused`**.
4. **`run-satellite`** is sent only once the bridge is actually ready to consume events. Registration
   alone does not arm a device, and `describe`/`info` never did: the device is inert until it is told to
   run.

Rules that are part of the contract:

- **`info` is metadata, not identity.** A client can claim any name in it, so the identity comes from the
  credential and the registration payload only has to *agree* with it. A payload whose `device_id`
  disagrees with the authenticated identity is refused.
- **An unknown credential is a refusal, not a trusted device.** A listener configured with a credential
  lookup never falls back to believing a device that presented none.
- **Registration is time-bound** (ten seconds by default). A socket that connects and claims nothing is
  closed.
- **A version mismatch is refused with a reason** naming the version the bridge speaks, so an old device is
  told what is wrong rather than connecting into a half-working state.
- **Duplicates follow a configured policy**, `reject` or `replace`. With `replace` the newer connection
  wins and the older socket is closed; with `reject` the newcomer is refused. Either way, exactly one
  connection per device is ever writable.
- **A refusal is always sent before the socket closes.** Silence is what makes a device retry forever
  against what looks like a network fault.
- **Removal is generation-safe.** Every connection carries a monotonic generation and cleanup only removes a
  slot if the generation still matches, so a device that reconnects is not removed by its own old handler.

Implemented in `bridge/registry.py`, pinned by `scripts/test_registry.py` (twelve offline checks over real
sockets), and **not yet carrying voice traffic**: the running bridge still dials the device, and the worker
that leases a registered device is migration phase 4.

## Events we already rely on

| Event | Direction | Where we use it |
|---|---|---|
| `describe` / `info` | both | the bridge asks the satellite who it is at startup |
| `audio-start`, `audio-chunk`, `audio-stop` | both | capture and playback, 16 kHz mono in, 22050 mono out |
| `detect` / `detection` | both | wake word, via the shim |
| `transcribe` / `transcript` | both | speech to text, sent to the local STT |
| `synthesize-start`, `synthesize-chunk`, `synthesize-stop`, `synthesize-stopped` | both | streaming speech out, sentence by sentence |
| `played` | satellite to bridge | what `wait_for_played` waits for |
| `run-satellite`, `pause-satellite` | bridge to satellite | arming and pausing the satellite |

**Our LED channel is not a first-class protocol type.** The bridge publishes `led-busy`, `led-idle` and
`led-followup` as custom event types, and the satellite forwards events it does not recognise to the LED
service over its event URI. That works and it is loopback-only, but it leans on the satellite's
forward-unrecognised behaviour rather than on the protocol. The spec has a formal **`user-event`** type
with a `name` field, which is the safer long-term shape if we ever move to another satellite
implementation. Worth a look before a migration, not urgent while we run our own build.

## Events we could use and do not

- **Timers.** The protocol defines `timer-started`, `timer-updated`, `timer-cancelled` and
  `timer-finished`, carrying an `id`, `is_active` and `total_seconds`, and they flow **server to
  satellite**. We are the server, so the bridge can drive the satellite's own timer rather than
  implementing timing itself, which hands us the tone, the repeat and the event hooks. This is the
  protocol-level confirmation of the correction already noted on issue #5.
- **`satellite.area`** in the `info` response: the name of the area a satellite sits in, as a standard
  field. A protocol-native home for the room context wanted by #31 and the room-addressed replies in #33,
  rather than something we invent.
- **`satellite.active_wake_words` and `max_active_wake_words`**: the protocol can advertise which wake
  words are live and how many can run at once. Relevant to #4: multiple wake words is an expected state
  rather than an abuse of the interface.
- **`satellite.supports_trigger`**: whether the satellite supports remotely-triggered pipelines. Worth
  understanding before we build more on our wake shim, since it may be the supported way to do what the
  shim does.
- **`select-program`**: choose which program handles a connection when a service offers several, with the
  guarantee that a server predating it drops the event harmlessly. Relevant wherever we run more than one
  program of a type.
- **`transcribe.vad_sensitivity`**: how quickly the end of a spoken command is detected, available when
  the ASR program does not require external VAD. Our endpointing is local to the bridge, so this may not
  apply; worth checking rather than dismissing, given the noisy-room waits.

## Tracking

`OHF-Voice/wyoming` is watched by the same monthly cron job as the successor application
(`watch-successor-voice-repos`), because the transport matters more to us than the application built on
it. It reports only when something in the repository actually changes.
