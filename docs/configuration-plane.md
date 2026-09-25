# The configuration plane

**Where this sits:** stage across the device of the voice pipeline. The end-to-end map, and every other stage, is in `docs/architecture.md`.

Interface contract for the voice bridge's settings and fleet-configuration surface. Written for the
system on the other side of it: the Hermes gateway, a script, a cron job, or an operator's browser.

Verified against the implementation on branch `work/voice-fleet-config`. Every field, bound and status
code below is read from the code, not from an intention: `bridge/config_plane.py` (state and validation),
`bridge/announce.py` (HTTP), `bridge/registry.py` (wire events), `pi/config_agent.py` (device side).

## 1. What this is

One authenticated HTTP endpoint on the bridge host, plus a control pair of Wyoming events over the
connection a device already holds.

- **Host side** owns the truth. Desired state is durable, versioned and validated here.
- **Device side** applies what it can, then reports what it actually has.
- **Drift is computed, never assumed.** A device is `converged` only when its reported generation equals
  the desired generation, its reported values match, and it reports `healthy`.

The plane is deliberately **not** a remote shell. There is no command field, no path field, and no
generic "run this" operation. Every write is a named setting with a server-side validator, because the
account that would act on a Pi-side change has passwordless root.

## 2. Transport and authentication

| | |
|---|---|
| Base URL | `http://<bridge-host>:8765` (loopback default; LAN by configuration) |
| Authentication | `Authorization: Bearer <token>` on every route except `/configure` and `/announce/health` |
| Content type | `application/json` for all writes |
| Token source | `state/announce-token.txt` (mode `0600`), overridable with `VOICE_ANNOUNCE_TOKEN` |

The token is never returned by any endpoint, never logged, and never placed in a URL. A client reads it
from the state file or from the environment; it is not discoverable over the wire.

`/configure` serves a static page containing **no** credential: the operator types the token into the
page and it is held in that browser tab's `sessionStorage` only.

Unauthenticated requests to a protected route return `401` with `{"ok": false, "error": "unauthorized"}`.
The attempt is logged with the source address, without the presented value.

## 3. Setting schema

`GET /config/schema` returns this. Bounds are enforced on both sides; the device revalidates even though
the host already did, because a host is not a trust boundary for a device.

| Setting | Type | Bounds | Default | Applies by |
|---|---|---|---|---|
| `wake_word` | string | `^[a-z0-9][a-z0-9_-]{0,63}$` | `hey_hermes` | wake service restart |
| `wake_threshold` | number | 0.05 – 0.99 | `0.35` | wake service restart |
| `mic_gain` | integer | 0 – 100 | `78` | immediate |
| `playback_volume` | integer | 0 – 100 | `55` | immediate |
| `led_brightness` | integer | 0 – 100 | `40` | LED service restart |
| `locale` | string | enum: `en-US` | `en-US` | next turn |
| `update_channel` | string | enum: `stable`, `beta` | `stable` | next update check |

`apply` is part of the contract, not a hint: it tells a client whether a change is audible immediately or
requires a service bounce. A client that batches settings should group by `apply` so one restart covers
every restart-class change.

`playback_volume` is the setting with a real device-side implementation today, and it carries one extra
guarantee the schema cannot express: **it can never be silent.** The bridge clamps it to an audible floor
and the device clamps it independently, so a `0` written here lands as the floor rather than as a deaf
device. Writing `0` is not an error; it is recorded and reported, and the applied value comes back higher
than the requested one, with the reason in `last_error`. See `docs/volume.md`. The other four of
`wake_word`, `wake_threshold`, `mic_gain` and `led_brightness` are staged only, and the device reports
`degraded` rather than pretending otherwise.

**`playback_volume` is on a USER scale of 1-100, not a raw ALSA percentage.** 1 is the quietest this
hardware can be and 100 is full volume, so the same number means the same thing on a device with a
different measured floor. On the current device a floor of raw 50 means user 50% is raw 75 - halfway up
what the hardware can actually do. Raw ALSA numbers would make this API meaningless across a fleet.

It also carries a **`round_trip_tolerance` of 1**, visible in `GET /config/schema`, because the
user→raw→user round trip passes through an integer ALSA percentage and shifts by one for 49 of the 100
values. Without it those settings would report permanent drift and never converge, which is worse than
useless: it teaches whoever reads the fleet view to ignore `drift` altogether. Convergence means
"the same instruction arrived back", not "the integers are identical".

Rejected inputs, all `400`: unknown setting name, wrong JSON type (a string where an integer is required,
and note that `true` is rejected for a numeric field rather than coerced to `1`), out of range, value
outside an enum, and a `wake_word` containing anything but lowercase letters, digits, `_` and `-`.

## 4. State model

### Device record

```json
{
  "device_id": "kitchen-1",
  "metadata": {"name": "Kitchen", "area": "kitchen", "firmware_version": "1.2.3",
               "capabilities": ["speaker", "led"]},
  "generation": 4,
  "desired": {"wake_word": "hey_hermes", "mic_gain": 78},
  "reported_generation": 4,
  "reported": {"wake_word": "hey_hermes", "mic_gain": 78},
  "health": "healthy",
  "last_error": null,
  "desired_updated_at": "2026-09-25T14:02:11+00:00",
  "reported_at": "2026-09-25T14:02:12+00:00",
  "drift": {},
  "converged": true,
  "connected": true
}
```

`metadata` is whatever the device said at registration. It is **not** secret and is never used for
authentication: identity comes from the transport credential, not from this payload.

`drift` names every desired setting whose reported value differs, as
`{"<setting>": {"desired": ..., "reported": ...}}`. A setting the device has never reported appears with
`"reported": null`, which is drift, not agreement.

`connected` is present only on reads that were given live registry information. It reflects the registry,
not the reported state: a device can be connected and unconverged, or converged and offline.

### Monotonic generations

Every desired-state write increments `generation` by exactly one. Generation `0` means "never written";
enrolment seeds generation `1` with the full default profile. Generations never go backwards and are never
reused, so a device can discard a stale or replayed write by comparison alone.

### Atomic, private persistence

State is written to `state/fleet-config.json` through a same-directory temp file, `fsync`, then
`os.replace`, with mode `0600` applied to the temp file before the rename. A crash mid-write leaves the
previous file intact. Reading an unreadable or version-mismatched file is a startup error, not a silent
reset: a corrupt config file must not look like a device with no settings.

## 5. Endpoints

### `GET /config/schema`

`200` → `{"ok": true, "schema_version": 1, "device_settings": {...}, "persistence": "..."}`

### `GET /config/devices`

`200` → `{"ok": true, "count": N, "devices": [<device record>, ...]}`, sorted by `device_id`.

### `GET /config/devices/{device_id}`

`200` → `{"ok": true, "device": <device record>}`
`400` → invalid `device_id` · `404` → unknown device

### `PUT /config/devices/{device_id}/desired`

Partial writes: `settings` is a patch merged over existing desired state, so setting one field does not
clear the others.

```json
{"expected_generation": 3, "settings": {"mic_gain": 82}}
```

`202` → `{"ok": true, "accepted": true, "dispatched": true, "device": <device record>}`

`accepted` means **durable**; `dispatched` means the write also reached a live connection. They are
separate on purpose. A disconnected device still records the new generation and picks it up on
reconnect, so a client that wants convergence today must check `dispatched`, and one that only wants the
intent recorded must not.

- `409` → `expected_generation` did not match the current generation. Re-read and retry. The new value is
  **not** applied; this is the optimistic-concurrency guard, and honouring it is what stops two writers
  silently overwriting each other.
- `400` → missing/non-integer/negative `expected_generation`, unknown setting, bad type, out of range.

### `POST /config/devices/{device_id}/reported`

The device's own report. Also carries health, so an apply failure is observable rather than inferred.

```json
{"generation": 4, "actual": {"mic_gain": 82}, "health": "healthy", "error": null}
```

`200` → `{"ok": true, "device": <device record>}`

`health` is one of `healthy`, `degraded`, `error`, `unknown`. `error` is `null` or a string of at most 500
characters. A device that has accepted a generation but applied nothing reports `health: "degraded"` with
an explanatory `error` and an empty `actual` — honest reporting is required by the contract, and a
`degraded` device is not `converged`.

## 6. Wire events

Carried on the same Wyoming connection the device registers on. Both are handled **out of band**: they do
not enter the audio ingress queue, and a `hermes.config.*` event never reaches the local satellite relay.

| Event | Direction | Data |
|---|---|---|
| `hermes.config.desired` | host → device | `{schema_version, device_id, generation, settings}` |
| `hermes.config.reported` | device → host | `{generation, actual, health, error}` |

The device applies a desired event only when `generation` is strictly greater than the generation it has
already confirmed; an equal or lower generation is a no-op that returns the current report. This is what
makes redelivery safe, and it is why a reconnect can simply be re-sent the current desired state.

If the device's `device_id` disagrees with the desired event's, the event is refused with
`health: "error"` and nothing is applied: a device must not accept another device's settings.

A malformed report is logged and dropped by the host. It must not break the voice transport, because the
same socket is carrying the microphone.

## 7. Device-side application

`pi/config_agent.py` owns validation, the generation guard, and atomic persistence of
`desired`/`actual` in the device's own state file (mode `0600`, same temp-file-and-rename discipline).

Hardware application is an **injected adapter**, not something this module guesses at. The adapter
receives the validated settings and returns `(actual, health, error)`.

With no adapter installed the default is to stage and tell the truth:

```json
{"generation": 1, "actual": {}, "health": "degraded",
 "error": "desired state is staged; no hardware adapter is installed"}
```

That is deliberate. A device that cannot apply a setting must not claim it did. It also means the
contract can be exercised end to end before any hardware-specific code exists, and that a Pi-side adapter
is a contained, individually testable unit.

## 8. Existing surfaces on the same port

| Route | Auth | Purpose |
|---|---|---|
| `POST /announce` | bearer | Speak text on the satellite |
| `GET /announce/health` | none | Liveness only: no payload, no text |
| `GET /settings` | bearer | Read `follow_up` and `endpointing_profile` |
| `POST /settings` | bearer | Set exactly one of those two |
| `GET /timers` | bearer | List active timers |
| `POST /timers` | bearer | `{"action": "set"\|"list"\|"cancel", ...}` |
| `GET /configure` | none | Static configuration page (no credential embedded) |

`/settings` and `/config/*` are intentionally separate. `/settings` is the **single-device, live-room**
surface: it changes how the device behaves in the room right now, and it is read back immediately.
`/config/*` is the **declarative fleet** surface: it records intent durably and converges over time. A
client that wants "turn follow-up on for this conversation" wants `/settings`; a client that wants "this
device should run this wake word from now on" wants `/config`.

### Timers

```json
{"action": "set", "duration": "10m", "label": "pasta"}
{"action": "list"}
{"action": "cancel", "label": "pasta"}
{"action": "cancel", "all": true}
```

`duration` accepts `30s`, `10m`, `1h30m`, `1 h 30 m`. Exactly one of `seconds` or `duration` — passing
both is a `400`. Timers are bridge-owned because they need sub-minute relative deadlines, and they are
persisted atomically so a bridge restart does not silently cancel one. On expiry the timer enters the
announcement queue with a distinct sound and a dedup key, so one timer produces one announcement.

## 9. Interfacing from the Hermes gateway

Two plugin tools already drive these surfaces over the gateway.

**`hermes_timers`** (`hermes/plugins/hermes_timers/`) — `set`, `list`, `cancel` on the timer surface, plus
`alarms`, which lists cron-backed alarms whose name begins with `alarm:`. Those alarms fire through
`scripts/alarm_fire.py`, which calls the authenticated announcement endpoint with the `alarm` sound. So
relative timers live in the bridge and absolute wall-clock alarms live in Hermes cron, with one honest
list per kind rather than one API pretending to do both.

**`voice_satellite`** (`hermes/plugins/voice_satellite/`) — two families of action, split by intent:

| Action | Surface | Use for |
|---|---|---|
| `follow_up_on` / `follow_up_off` / `follow_up_status` | `/settings` | how this conversation behaves right now |
| `endpointing_pause_tolerant` / `endpointing_standard` / `endpointing_status` | `/settings` | waiting longer while someone speaks |
| `fleet_status` | `/config/devices` | every device and its convergence state |
| `config_status` | `/config/devices/{id}` | one device: desired, reported, drift |
| `config_set` | `/config/devices/{id}/desired` | durable settings from now on |

The split matters. `/settings` is read back immediately and changes the live room; `/config/*` records
intent durably and converges. An assistant asked to "keep listening for a reply" wants the first
family; asked to "make the kitchen device use a different wake word from now on", the second.

`config_set` reads the device first and sends back the generation it saw, so the optimistic-concurrency
guard is honoured rather than bypassed: it cannot clobber a change it did not observe. It validates
setting names locally so a typo produces a useful message instead of an opaque `400`, and it reports
`dispatched` separately from `accepted`, because those genuinely differ when a device is away.

Both read the bearer token from the bridge state directory or `VOICE_ANNOUNCE_TOKEN`, and both **read the
value back** after writing, so the reply the assistant speaks is the bridge's state rather than an
assumption. A client integrating the same way should do the same: `202` means recorded, and only a
follow-up read means applied.

## 10. What is not here

Named honestly so a client does not build against a shape that does not exist:

- **No push notifications.** Convergence is discovered by reading. There is no websocket or callback.
- **No multi-host federation.** One bridge owns one fleet's config file.
- **No secret storage.** The schema is for non-secret device policy. Credentials are not settings.
- **No arbitrary execution and no path settings.** By design; see section 1.
- **TLS is not terminated here.** The endpoint is LAN-scoped, matching the announcement endpoint's
  existing trust model. Exposing it beyond the LAN is a separate decision with its own work.
