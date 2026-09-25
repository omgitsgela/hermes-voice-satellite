# A configuration server for the satellite

Feasibility research, 2026-09-23. **Status: the recommended design has since been built.**

This document is kept as the record of *why* the recommendation was what it was. For the interface
itself — endpoints, schema, status codes, wire events, and how the gateway drives it — read
`docs/configuration-plane.md`. Do not treat the "decisions parked" section below as current: the
decisions were made, and they went the recommended way.

What was actually built, against this document's recommendation:

- a settings and fleet surface on the **host**, on the port the bridge already listens on (`/configure`,
  `/config/*`), not a new LAN listener on the Pi;
- an **allowlist of named settings with server-side validators**, never a shell and never a free-form
  command field;
- changes applied to the Pi through the **control channel the bridge already owns** (`hermes.config.*`
  over the registered connection), so the Pi gains no new listening port;
- the same bearer-token authentication as announcements.

Still open against this document's other recommendations: narrowing the Pi's `NOPASSWD: ALL`, and the
device-side hardware adapter that turns staged settings into actual `amixer`/LED/service changes.

Her request, verbatim:

> another feature to add later is an HTTP server on the satellite that allows configuration of
> things on it, if we have enough capacity to make it happen. research the feasibility, and make a
> new feature documented in the repo

## Verdict

**Technically feasible. But the satellite is the wrong place to put it, and the request can be met
better without it.**

Build the configuration page on the host, next to the bridge's existing authenticated HTTP server,
and apply changes to the Pi through the control channel the bridge already uses. The user gets what
she actually asked for, which is a web page on the LAN that configures the device from any device
in the house, with none of the risk below and no new load on the Pi.

Everything in this document is measured, not estimated. Measurements taken 2026-09-23.

## What the two machines actually have

| | the host | the satellite |
|---|---|---|
| CPU | workstation class | 4 x Cortex-A53, load average 0.69 |
| RAM | 17.9 GB, **13.5 GB available** | 416 MB total, 244 MB used, **171 MB available** |
| Disk | 98 GB, 76 GB used (82%) | 28 GB, 3.4 GB used (13%) |
| Thermals | n/a | 59.6 C, `get_throttled` 0x0 (healthy, no undervoltage) |
| Python | Hermes venv | 3.11.2 |
| HTTP libraries | aiohttp, everything | **no aiohttp, no flask**; stdlib `http.server` only |
| Services | gateway, bridge | `wyoming-satellite`, `wyoming-openwakeword`, `wake-shim`, `2mic_leds` |
| Largest process | Hermes gateway | 74.6 MB (openwakeword) |

### What the Pi exposes to the network today

```
LAN:      22 (ssh), 10700 (wyoming satellite)
loopback: 10400 (shim), 10401 (openwakeword), 10402 (shim control), 10500
```

That split is deliberate and worth protecting: **the device adds nothing to the LAN except SSH and
the one port the bridge dials into.** Every internal control path is loopback.

**That port is going away too** (issue #61): when the device dials the bridge instead of being dialled, it
needs no listener at all, which is what makes a device behind somebody else's NAT - or on a network you do
not control - a supported shape rather than a compromise. The qualification that matters, and it is in the
issue rather than here: an outbound connection still needs a **reachable destination**, so the bridge has to
be on the same LAN, on a private overlay, behind an authenticated tunnel, or listening with TLS on a public
endpoint. Everything below about a management interface on the host still holds; only the direction of the
voice connection changes.

### The fact that decides this

```
The satellite user may run the following commands on the satellite:
    (ALL : ALL) ALL
    (ALL) NOPASSWD: ALL
```

The account that would own any web server on this device has **passwordless, unrestricted root**.

## Why not on the Pi

1. **A LAN HTTP server plus blanket passwordless sudo is remote root.** Not a theoretical concern:
   the server runs as a user who can run anything as root without a password, and the settings it
   would write are consumed by root-owned systemd services. One parsing bug, one unvalidated
   field, one leaked token, and the result is full control of a device inside the house network.
   The project has kept the Pi's LAN surface at two ports on purpose; this would be the first thing
   to break that.
2. **The headroom is real but thin, and audio is the thing that must not regress.** 171 MB is
   available, and a small stdlib server costs maybe 20 to 35 MB of it, which is roughly a fifth of
   what is left. The device already shows occasional playback underruns, with Wi-Fi as the prime
   suspect. Adding a second workload that competes for the same CPU and the same Wi-Fi is a
   plausible way to make the one symptom that annoys the user most.
3. **It does not need to be there.** Almost every setting she would want to change is applied by
   restarting a service on the Pi. Something else on the LAN can do that. The bridge already runs
   an authenticated HTTP server on a machine with 13.5 GB spare, and there is already a proven,
   loopback-only path from the bridge to the Pi: the bridge sends an event over the satellite
   connection it owns, the satellite forwards unrecognised events to the LED service, and that
   service is the only thing on the Pi that talks to the other local services.

## The recommended design

A settings page served by the bridge on the host, using the same aiohttp app that already serves
`/announce` and `/settings`.

- **Front end:** one static page, no framework, no build step. Works in a phone browser.
- **Auth:** the same bearer-token pattern as `/announce`, issued to the browser once and stored
  locally, or a single shared password with a session cookie. LAN trust, exactly like the
  announcement endpoint, which is already accepted for this design. **Not internet-facing, and no TLS, because
  a self-signed certificate means installing a CA on every device that visits, which is exactly the
  onboarding friction she does not want.**
- **Writes are an allowlist, never a shell.** Each configurable item is a named operation with a
  server-side validator. There is no generic "run this" path, and no free-form command field,
  because the thing on the other end has passwordless root.
- **Reuse the validators that already exist.** The wake-word switch script already checks the
  TFLite header, refuses blind edits, pre-flights the command line and verifies the relay chain
  afterwards. A web form should call the same code, not a second implementation of the same rules.
- **Respect the restart order, and verify the device came back.** This is not optional. Restarting
  `wake-shim` bounces the satellite because the satellite `Requires=` it, and a satellite that has
  been stopped does not come back on its own. `wyoming-satellite` also `Requires=` the LED service,
  and a past pulse demo left the whole device dead this way. So: change the file, restart in the
  documented order, then confirm the services are active and the wake word is armed, and roll the
  file back if it did not come up.
- **Audit line per change:** what changed, when, from which address, and whether the device came
  back. Log only this, never the token.

### What should actually be configurable

| Setting | Applies by | Service to bounce | Risk of getting it wrong |
|---|---|---|---|
| Wake word | model file + unit flag | wake service, then shim, then satellite | silent fallback to the old word |
| Wake threshold | systemd drop-in | wake service | more false wakes, or a deaf device |
| Mic preamp gain | `amixer` | none | clipping, or a device that hears badly |
| TTS voice | bridge config | bridge only | wrong voice, instantly audible |
| LED colours and brightness | CLI arguments | LED service (which bounces the satellite) | device looks dead, or stays lit |
| Follow-up default | a state file | none | the mode is on when it should not be |
| STT model | bridge config | bridge only | slower answers, more RAM on the host |

The first two are the ones worth building, because the wake word and its threshold are the settings
that actually get tuned, and `scripts/switch-wake-word.py` already does most of the work correctly.

## If it has to live on the Pi

Documented for completeness, since it is her call and the request was explicit.

- Bind to **loopback only** and accept that reaching it then requires an SSH tunnel, which
  contradicts the no-onboarding preference and adds friction to every use.
- Or bind to the LAN and accept remote root as the standing risk, with the mitigations below.
- **Mitigation worth doing regardless of which way this goes: replace `NOPASSWD: ALL` with a named
  allowlist** covering the specific commands the satellite actually needs. That is a genuine
  hardening win on its own, independent of any web feature, and it is the thing that makes option
  two survivable.
- Use the stdlib `http.server` rather than installing aiohttp, since nothing HTTP is present on the
  device and every megabyte there is load-bearing.

## The cheap experiment that would answer the capacity question with data

Before building anything, run a read-only status endpoint on the Pi for 24 to 48 hours and compare
it against the baseline in this document:

- its own RSS over time, against the 171 MB available now
- `vcgencmd get_throttled` at the end, against `0x0` now
- temperature, against 59.6 C now
- **and the bridge log's underrun count over the same window**, because audio is the thing at risk

If the numbers barely move and the underruns do not increase, the capacity objection mostly
disappears and only the root-access objection remains, which is solvable by hardening sudo. If the
underruns climb, that settles it.

## Decisions parked

Not to be raised again until she asks, by her instruction on 2026-09-23:

1. Configuration page on the host (recommended, no new Pi surface) or on the Pi as originally
   imagined?
2. If it goes on the Pi, is LAN exposure acceptable in exchange for convenience, with sudo
   hardened first?
3. Should we do the 24 to 48 hour capacity experiment before deciding at all? (Recommended, since
   it is cheap and turns an opinion into a measurement.)
4. Should the Pi's `NOPASSWD: ALL` be narrowed regardless of this feature? (Recommend yes,
   independent of everything else here.)

## Files, when built

New: `bridge/config_ui.py` (routes and validators), `bridge/static/settings.html`,
`scripts/test_config_ui.py` (validators, allowlist, rollback logic), and a line in `README.md`.

Changed: `bridge/announce.py` (mount the app), `pi/2mic_service.py` (a settings-apply event
handler, reusing the existing forward-event path), and whatever sudoers allowlist the hardening
produces.

Unchanged on purpose: nothing in the Hermes checkout. This feature adds no patch.

## Security notes to carry forward

- The announce token, and any new token, lives in a mode-600 state file and never appears in a log,
  a note, or a chat message.
- The settings page is LAN-only and must never be exposed to the internet. There is no TLS story
  and no CA onboarding, by design.
- Anything that can reach the page is, by construction, able to restart services on the Pi, because
  that is what applying a setting means. Keep the allowlist short for that reason.
- The Pi's gold-card image and restore procedure are the safety net if a settings change renders
  the device unbootable.
