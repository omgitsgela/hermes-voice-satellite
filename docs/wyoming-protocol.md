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
