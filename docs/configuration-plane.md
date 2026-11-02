# The configuration plane

**Where this sits:** stage across the device of the voice pipeline. The end-to-end map, and every other stage, is in `docs/architecture.md`.

Interface contract for the voice gateway's settings and fleet-configuration surface. Written for the
system on the other side of it: the Hermes gateway, a script, a cron job, or an operator's browser.

Verified against the implementation on `main`. Every field, bound and status code below is read from
the code, not from an intention: `gateway/config_plane.py` (state and validation), `gateway/announce.py`
(HTTP), `gateway/registry.py` (wire events), `pi/config_agent.py` (device side), `pi/device_apply.py`
(hardware adapter), `pi/mic_gain_control.py` (input level).

## Workbench management and per-device voices

The installer's **Manage satellites over the network** route works from another computer without
copying the announcement bearer token there. Its GUI and console share the same `Session` API.

1. Connect to the pairing service's TLS port (normally 10704).
2. Derive the identity code from the peer certificate and compare it to the saved record.
3. Only after confirmation, POST `/manage` with pairing words and an allowlisted operation:
   `{"words":"<pairing words>","path":"/config/devices","method":"GET","payload":{}}`.
4. The pairing service authenticates/rate-limits the words and forwards the operation to its local
   configuration plane using the host-only bearer token. No device certificate is issued.

The workbench rechecks certificate identity on every request, including downloads and updates.
Pairing words are not logged or put in URLs. Start over clears the authenticated target in memory.
The proxy allows schema/device/library reads, single-device desired writes, and explicit voice
downloads only; arbitrary routes, reports, announcements and fleet-wide writes are not proxyable.
The host-owned `VOICE_CONFIG_LOCAL_URL` sets the loopback config-plane address when it differs from
`http://127.0.0.1:8765`. It is never accepted from a remote request. Existing bearer HTTP clients
continue to work; the HTTP token is not disclosed through pairing.

### Voice library HTTP API

- `GET /config/voices`: authenticated live Piper library plus installed models, including custom
  models present as `.onnx`/`.onnx.json` pairs. Returns `voices` and `installed` IDs. Offline it falls
  back to the checked-in snapshot; installed voices are still shown.
- `POST /config/voices`: authenticated `{"voice":"<model ID>","consent":true}` installs a model/config
  pair alongside existing voices. Both files must match the pinned SHA-256 trust snapshot; missing
  hashes or mismatches are refused. No default/per-device selection changes as a side effect.
- `PUT /config/devices/<id>/desired`: `settings.voice` assigns an installed voice; `default` removes
  the override. An uninstalled voice is refused before changing desired state. Optimistic generation
  checks still apply, and the installer reads the exact target back after accepting a write.

`voice` is a **Satellite Gateway-owned** setting, not device firmware configuration. It is excluded
from `hermes.config.desired.settings` sent to device agents. Public readback includes the accepted
host selection separately in `gateway_reported`; `reported` remains the device’s own actual values.
Physical device values and health still require their own report
and matching generation. Existing profiles without a `voice` field retain the gateway fallback.
The selected voice is resolved per destination at synthesis time; Piper model caches are keyed by
model/config paths, never a mutable global choice. Streaming conversation speech and per-device
announcements use the same provider interface. WAV speech from voices at other native sample rates
is converted to the announcement tone format before concatenation; streamed speech uses the existing
per-device converter. Only Piper is implemented; paid providers remain
outside this contract.

### Capability and convergence display

Enrolled certificates with no configuration record are listed as unknown, waiting for their first
device report; no profile or editable capability is guessed for them. Listing does not mutate state.
The installer intersects controls with the device profile's remote flags. It shows name, health,
connected/offline state, remote-capable settings and cable/card-only settings. Existing firmware
metadata identifies first-party profiles; an explicit `metadata.profile_id` identifies other
registered profiles. Unknown profiles have no editable remote controls.

Playback/microphone volume, LED brightness and wake threshold are remote-capable in first-party
profiles. Pi wake-word selection is remote; the ESP32 profile requires a cable for that selection.
In all cases a newly selected wake word must already have its model on the device. Wi-Fi, dial
addresses, TLS credentials and wake-model installation remain cable/card operations. Writes to
those fields are refused by the installer's backend before any config-plane write.

A successful HTTP write means **accepted**, not applied. The installer displays desired versus
reported values, health and pending/converged status, and has an explicit readback refresh. An offline
device stays pending; a device error is shown. Browser coverage uses fixture acknowledgements, while
`scripts/test_installer_fleet_network.py` exercises the actual TLS proxy and HTTP handlers locally.
Neither test configures a deployed service or proves a real satellite applied a setting.

## 1. What this is

One authenticated HTTP endpoint on the Satellite Gateway host, plus a control pair of Wyoming events over the
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
| `voice` | string | safe Piper model ID or `default` | `default` | Satellite Gateway, next synthesis |
| `wake_word` | string | `^[a-z0-9][a-z0-9_-]{0,63}$` | `okay_nabu` | wake service restart |
| `wake_threshold` | number | 0.05 – 0.99 | `0.35` | wake service restart |
| `mic_volume` | integer | 0 – 100 | `78` | immediate |
| `playback_volume` | integer | 0 – 100 | `55` | immediate |
| `led_brightness` | integer | 1 – 31 | `31` | coupled audio group restart |

Two keys that used to be here are gone: `locale` and `update_channel`. Neither had a consumer on any
device -- speech is transcribed and synthesised on the Satellite Gateway, so a satellite has no use
for a language, and `update_channel` presupposed an updater that does not exist, since firmware is
written by cable. A schema that asks for settings nothing can honour marks every device `degraded`
for ever, which teaches everyone to ignore the word.

`mic_volume` is named for the intent, not the mechanism. How a device realises "how loudly this
satellite hears you" is its own business: an ALSA capture control, a codec gain path, or a fixed
level it reports honestly because the hardware has no control.

`apply` is part of the contract, not a hint: it tells a client whether a change is audible immediately or
requires a service bounce. A client that batches settings should group by `apply` so one restart covers
every restart-class change.

`playback_volume` is the setting with the strongest device-side guarantee, and it carries one extra
guarantee the schema cannot express: **it can never be silent.** The Satellite Gateway clamps it to an audible floor
and the device clamps it independently, so a `0` written here lands as the floor rather than as a deaf
device. Writing `0` is not an error; it is recorded and reported, and the applied value comes back higher
than the requested one, with the reason in `last_error`. See `docs/volume.md`.

The other hardware settings are applied by the device's adapter (`pi/device_apply.py`) rather than staged:
`mic_volume` (the input level: an ALSA capture control on the Pi, the codec's gain path on the ESP32) and `led_brightness` / `wake_threshold` / `wake_word` (durable env-file
writes plus a coupled service restart). Two honest caveats are built into the adapter and reported rather
than hidden:

- **`mic_gain` may be a no-op on some cards.** If the card's capture is an `arecord` command line with no
  amixer input control, there is nothing to set. The value is still recorded (so it takes effect if a
  control is later configured via `VOICE_MIC_GAIN_CONTROL`), but the report is `degraded` and names the
  reason — never a `healthy` for a gain the device did not take.
- **`led_brightness` is on the LED driver's 1-31 scale, not a percentage.** The APA102 global-brightness
  register is 5 bits and the service refuses anything outside 1-31. The strip's power rail is a GPIO, so
  there is no `0`/"off" level in this setting; 1 is its dimmest.

### What `wake_threshold` does and does not do on a microWakeWord device

On the ESP32 satellite the wake detector runs a microWakeWord model whose scorer gates FIRST: it returns
a score only when its sliding-window mean crosses the model's own `probability_cutoff` (0.97 in the
bundled `okay_nabu` manifest), and returns `0.0` at every other frame. The fleet's `wake_threshold` is a
SECOND gate applied to that score.

The consequence is worth stating plainly, because it makes the setting look more powerful than it is:

- a value **below** the model cutoff (0.15, 0.22, 0.35 …) accepts every crossing the model already
  produced, so it is acoustically identical to the default — it neither helps nor hurts;
- a value **above** it can reject a marginal crossing, i.e. it can only make the device LESS sensitive.

So raising sensitivity is not something this setting can do. Real sensitivity control means driving the
scorer's `probability_cutoff`, which is a scorer parameter rather than a core one, and lowering it trades
false wakes for reach — a decision that needs real-world testing, not a config change.

On the Pi satellite the same value is handed to the wake service, which applies it to its own detector.

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
  "desired": {"wake_word": "hey_hermes", "mic_volume": 78},
  "reported_generation": 4,
  "reported": {"wake_word": "hey_hermes", "mic_volume": 78},
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
enrolment seeds generation `1` with the full default profile. The seeded `wake_word` is platform-aware:
an ESP32 (the image reports `firmware_version` `esp32-…`, computed from the running ELF) is seeded
`okay_nabu`, the model its firmware bundles; every other device is a Pi, seeded `hey_hermes`, the
model its installer provisions — so a fresh device matches the model already on the box instead of
reporting a permanent missing-model degradation. The device id is deliberately not the signal: a Pi's
is an operator-chosen name. Move any device to any other word with `set_desired` once the model is
deployed. Generations never go backwards and are never
reused, so a device can discard a stale or replayed write by comparison alone.

### Atomic, private persistence

State is written to `state/fleet-config.json` through a same-directory temp file, `fsync`, then
`os.replace`, with mode `0600` applied to the temp file before the rename. A crash mid-write leaves the
previous file intact. Reading an unreadable or version-mismatched file is a startup error, not a silent
reset: a corrupt config file must not look like a device with no settings.

**Legacy out-of-range values are repaired on load, not served.** A device enrolled under an older schema
can carry a `led_brightness` outside the 1-31 scale (the old page wrote it on a 0-100 percentage scale).
Such a value would poison every delivery for that device, including an unrelated volume change, because
validation runs on the merged profile. The plane therefore migrates any out-of-range `led_brightness` to
`31` — the LED driver's hardware default — when it loads the file, persists the repaired file before the
plane is used, and bumps that device's generation so the corrected profile is delivered. In-range values
are left untouched.

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
{"expected_generation": 3, "settings": {"mic_volume": 82}}
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

### `PUT /config/fleet/desired`

The "change it on every satellite" write. It merges `settings` into the desired state of **every**
registered device — or into a named subset when `device_ids` is given — in a single atomic save, bumping
each targeted device's generation by exactly one.

```json
{"settings": {"led_brightness": 20}}
{"settings": {"playback_volume": 40}, "device_ids": ["kitchen-1", "hall-1"]}
```

`202` → `{"ok": true, "accepted": true, "count": N, "restart_pacing": <bool>,
"devices": [<device record>, ...]}` — one record per targeted device, each carrying its new
`generation`, `desired` state and a `dispatched` flag. `restart_pacing` is `true` when the patch
touched a restart-class setting, so the per-device bounces are being queued rather than fired.

An empty `settings`, an empty `device_ids`, or any unknown target is a `400`; the write is atomic, so a
rejected fleet write changes nothing.

A fleet write is then dispatched like a per-device write: if every targeted device is connected the new
generation reaches it immediately. If the patch touches any **restart-class** setting
(`wake_word`, `wake_threshold`, `led_brightness`) the dispatch is **paced**: the Satellite Gateway spaces out the
per-device restart-class restarts by `VOICE_FLEET_RESTART_DISPATCH_INTERVAL` seconds (default 8) rather
than bouncing every room's audio stack in the same instant. Immediate-class settings are dispatched
without delay.

Each device **reserves its slot under a lock** (a fast section that never awaits external work), then
sleeps and dispatches **outside** the lock. This means two concurrent fleet writes still get distinct,
ordered deadlines and cannot bounce two rooms at the same instant, while a single device whose
connection stopped consuming output cannot hold the fleet's pacing slot: every dispatch is bounded by
`VOICE_FLEET_DISPATCH_TIMEOUT` (default 30 s), and a timeout is logged rather than fatal, because the
write is durable and the device takes the generation on its next registration. Pending paced dispatches
are cancelled and awaited by `Announcer.stop()`, in a `finally` that runs even when an earlier shutdown
step raises, so a shutdown cannot leave a restart firing after the Satellite Gateway is down.

The configuration page (`GET /configure`, served from `gateway/static/settings.html`) sends **only the
settings the operator selected** on a fleet apply: each fleet field is checkbox-gated, and an
unselected field is omitted from the request entirely. This is what makes a fleet apply a *change*
rather than a *clobber* — a setting the operator did not touch keeps each device's own value, which
may legitimately differ across the fleet. The page's convergence poll refreshes each device card in
place (status, generation, reported-state notes only) and rebuilds the card list only when the device
population changed; a card the operator is editing is left untouched by polling, and a save is issued
against the generation the card currently shows, so a poll cannot turn an unsaved edit into a stale
write.

### `POST /config/devices/{device_id}/reported`

The device's own report. Also carries health, so an apply failure is observable rather than inferred.

```json
{"generation": 4, "actual": {"mic_volume": 82}, "health": "healthy", "error": null}
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

The device applies newer desired generations and never applies older ones. For the same generation,
healthy state is a no-op, including after restart. If the last result was `error`, or was not healthy
and some desired values remain unapplied (including staged settings with no adapter), redelivery
retries the adapter **once per event**, without a background retry loop. Retry settings must exactly
match the stored desired settings; changing them requires a new generation. A fully applied but
degraded result is not retried just for an unrelated diagnostic.

The accepted generation, desired values, actual values and health are persisted together. Thus a
restart preserves both the retry eligibility of failed/staged settings and the idempotence of healthy
ones; installing a new adapter can reconcile staged settings on the next redelivery. Adapters must
safely support reapplying desired values, including values already applied by a partially successful
attempt. A callback exception does not advance the accepted generation.

If the device's `device_id` disagrees with the desired event's, the event is refused with
`health: "error"` and nothing is applied: a device must not accept another device's settings.

A malformed report is logged and dropped by the host. It must not break the voice transport, because the
same socket is carrying the microphone.

## 7. Device-side application

`pi/config_agent.py` owns validation, the generation guard, and atomic persistence of
`desired`/`actual` in the device's own state file (mode `0600`, same temp-file-and-rename discipline).

Hardware application is an **injected adapter**, not something this module guesses at. The adapter
receives the validated settings and returns `(actual, health, error)`.

The adapter shipped with the fleet configuration work (`pi/device_apply.py`) applies the settings in two
tiers:

- **Immediate** — `playback_volume` (ALSA output via `volume_control`) and `mic_gain` (ALSA input via
  `mic_gain_control`). Written straight to the hardware and read back; no service is touched.
- **Restart-class** — `led_brightness`, `wake_threshold`, `wake_word`. Written durably to the device's
  env file and applied by restarting the coupled audio group. **Every** restart-class setting restarts
  the whole group: the LED service is part of it (`wyoming-satellite` `Requires=` and `After=` it), so
  an "LED-only" restart is not an independent operation. The group is restarted as one, and it
  deliberately excludes `outbound-satellite` — the process running the adapter — so the device can
  bounce its own audio stack and still hold the connection that carries the report back. For
  `wake_word` the model file must be present first: a word whose `.tflite` is missing is refused up
  front (reported `degraded` and named, never written into the active config), and a *failed* model
  lookup (a transient I/O error) is reported the same way as any other application failure.

A restart-class value is reported in `actual` only **after its service restart succeeds**. The env
write itself is read back to prove it landed, and that readback is a fallible step: a write error or a
readback error is reported `error` for that setting, the value is left out of `actual`, and it is marked
stale so a redelivery re-attempts the write+restart instead of trusting the env file. The same rule
covers a decode failure (an undecodable env file, which `read_text` raises as a `ValueError` rather than
an `OSError`): a failed env read never proves a value is in force, and a failed env write or readback is
a reported application failure. If the env
write lands but the restart fails, the value is reported `error`, left out of `actual`, and remembered:
the agent persists which settings were not applied, and a *recreated* adapter (an outbound-runner
restart, not an audio-stack restart) is seeded with that set so it re-attempts the restart instead of
reading the env file and reporting the staged value as in force. A `converged` flag must never be a
lie.

The **honesty rule** is the whole point. A device that cannot apply a setting must not claim it did. A
setting with no path on this hardware (e.g. `mic_gain` when the card has no input control) is reported
`degraded` and named; a path that exists but fails is reported `error`; only a value the hardware
actually took is reported in `actual` with `healthy`. The default with **no adapter installed** remains
to stage and tell the truth:

```json
{"generation": 1, "actual": {}, "health": "degraded",
 "error": "desired state is staged; no hardware adapter is installed"}
```

That is deliberate. It means the contract can be exercised end to end before any hardware-specific code
exists, and that a Pi-side adapter is a contained, individually testable unit.

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
| `GET /config/schema` | bearer | Setting names, bounds, defaults, `apply` class |
| `GET /config/devices` | bearer | Every device and its convergence state |
| `GET /config/devices/{id}` | bearer | One device's desired, reported, drift |
| `PUT /config/devices/{id}/desired` | bearer | Durable per-device desired state |
| `PUT /config/fleet/desired` | bearer | Durable fleet-wide desired state |
| `POST /config/devices/{id}/reported` | bearer | A device's own report |

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
persisted atomically so a Gateway restart does not silently cancel one. On expiry the timer enters the
announcement queue with a distinct sound and a dedup key, so one timer produces one announcement.

## 9. Interfacing from the Hermes gateway

Two plugin tools already drive these surfaces over the gateway.

**`hermes_timers`** (`hermes/plugins/hermes_timers/`) — `set`, `list`, `cancel` on the timer surface, plus
`alarms`, which lists cron-backed alarms whose name begins with `alarm:`. Those alarms fire through
`scripts/alarm_fire.py`, which calls the authenticated announcement endpoint with the `alarm` sound. So
relative timers live in the Satellite Gateway and absolute wall-clock alarms live in Hermes cron, with one honest
list per kind rather than one API pretending to do both.

**`voice_satellite`** (`hermes/plugins/voice_satellite/`) — two families of action, split by intent:

| Action | Surface | Use for |
|---|---|---|
| `follow_up_on` / `follow_up_off` / `follow_up_status` | `/settings` | how this conversation behaves right now |
| `endpointing_pause_tolerant` / `endpointing_standard` / `endpointing_status` | `/settings` | waiting longer while someone speaks |
| `fleet_status` | `/config/devices` | every device and its convergence state |
| `config_status` | `/config/devices/{id}` | one device: desired, reported, drift |
| `config_set` | `/config/devices/{id}/desired` | durable settings for one device |
| `fleet_config_set` | `/config/fleet/desired` | durable settings for every device (or a subset) |

The split matters. `/settings` is read back immediately and changes the live room; `/config/*` records
intent durably and converges. An assistant asked to "keep listening for a reply" wants the first
family; asked to "make the kitchen device use a different wake word from now on", the second.

`config_set` reads the device first and sends back the generation it saw, so the optimistic-concurrency
guard is honoured rather than bypassed: it cannot clobber a change it did not observe. It validates
setting names locally so a typo produces a useful message instead of an opaque `400`, and it reports
`dispatched` separately from `accepted`, because those genuinely differ when a device is away.

Both read the bearer token from the Satellite Gateway state directory or `VOICE_ANNOUNCE_TOKEN`, and both **read the
value back** after writing, so the reply the assistant speaks is the Satellite Gateway's state rather than an
assumption. A client integrating the same way should do the same: `202` means recorded, and only a
follow-up read means applied.

## 10. What is not here

Named honestly so a client does not build against a shape that does not exist:

- **No push notifications.** Convergence is discovered by reading. There is no websocket or callback.
- **No multi-host federation.** One gateway owns one fleet's config file.
- **No secret storage.** The schema is for non-secret device policy. Credentials are not settings.
- **No arbitrary execution and no path settings.** By design; see section 1.
- **TLS is not terminated here.** The endpoint is LAN-scoped, matching the announcement endpoint's
  existing trust model. Exposing it beyond the LAN is a separate decision with its own work.
