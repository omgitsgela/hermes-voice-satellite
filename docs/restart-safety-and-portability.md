# Restart safety and portability: current state and proposal

Written after an inventory on 2026-09-23. This document is a proposal. Nothing described under
"Proposed" has been implemented beyond what is already merged in this repository (the patch set, the
checker, the dependency pin and the documentation).

## What already protects the system

Verified live, not assumed:

| Protection | Evidence |
|---|---|
| Everything comes back after a host reboot with nobody logged in | `loginctl show-user angela -p Linger` is `yes`, and both `hermes-gateway` and `hermes-voice-bridge` are `enabled` |
| The bridge survives a gateway restart instead of being dragged down with it | The unit deliberately has no `Requires=hermes-gateway`, only `After=`/`Wants=`, and says so in a comment, because the bridge retries by itself |
| The bridge itself always comes back | `Restart=always`, `RestartSec=5` |
| A crash or a lost satellite cannot leave the system wedged | The main loop re-raises connection errors, so the process exits and systemd re-dials; `start()` reconnects on the next run |
| Voice capabilities are not inside the Hermes checkout | Plugins live in `~/.hermes/plugins/`, config in `config.yaml`, procedures in `~/.hermes/skills/`; an update cannot revert them |
| The bridge cannot drift into Hermes internals | It talks only to `POST /v1/runs` plus SSE on loopback, reads one key from `.env`, and imports nothing from Hermes |
| The voice persona is ours, not Hermes' | `VOICE_INSTRUCTIONS` lives in `bridge/config.py` and is passed per run as `instructions` |

## Failure model: what happens today

| Event | Today | Wanted |
|---|---|---|
| Bridge process dies | systemd restarts it in 5 s; a turn in flight is lost | same, and say so in the room if a turn was lost |
| Gateway restarted (planned) | In-flight turn errors, bridge exits, reconnects; the device is deaf for a few seconds | same. Optionally retry the interrupted turn once |
| `hermes update` runs | **Five core patches are stashed and the surface silently degrades** (see below) | Patches detected, re-applied and verified automatically |
| Host reboots | Everything returns unattended | same (verified) |
| Pi reboots or loses power | Services are enabled and come back; no RTC, so nothing time-based is trusted | same. The gold SD image stays the recovery path |
| Pi SD card dies | Restore from the gold image, then re-copy `pi/` files | Keep a dated image and a checklist in this repo |
| API key or announce token rotated | The bridge keeps the old value until restarted | Re-read per run, or document the restart in the runbook |
| Wake word or route changed | Correct, but needs a restart of the right component | Already documented; keep it that way |

The update row is the real exposure. One file is now an uncommitted edit in `~/.hermes/hermes-agent`,
with no pre-update backup configured (`updates.pre_update_backup: false`), so the updater's autostash is
the only copy of them:

| Patch | Voice-critical? |
|---|---|
| `toolsets.py` — the four names in `_HERMES_CORE_TOOLS` plus `skills_read` | Yes: without it those tools defer behind a `tool_search` bridge the surface does not carry, so they vanish |
| `hermes_cli/tools_config.py` — `hubitat`, `google`, `voice_satellite` set default-off | Yes: without default-off, plugin toolsets switch on for every bundled platform |
| `gateway/platforms/api_server.py` — drop stale provider `extra_body` when a route switches provider | Yes: the voice route runs a different provider from the global default, which is exactly the case that silently fell back to a paid lane |
| `model_tools.py` — no bridge when the deferrable catalogue is empty | No: costs tokens and a detour, still works |
| `cron/jobs.py` — store `requested_provider` | No: unrelated to voice |

## Proposed, in the order I would do it

**A. Make patch state visible and recoverable (highest value).**
Keep the patch set here (done), run `hermes/patches/check-patches.sh` after every update (done, read-only).
Next: an `apply-patches.sh` that re-applies only what is missing, refuses to write when a patch neither
applies nor reverses (upstream restructured the file), compiles each touched file, and prints what it
did. Then a small daily cron job that runs the checker and, when something is missing, **announces it
through the house speaker** (`scripts/announce.py`, source `cron`). That turns a silent degradation into
a sound in the room, which is what the announcement channel is for. Finally, propose patch 0004
upstream so the carried set shrinks; it is a general bug, not a voice feature.

**B. Move machine-specific values into one config file.**
`SATELLITE_HOST`, the Hubitat base URL, the announcement port, the NAS paths and the token paths are
hardcoded in `bridge/config.py`. One `bridge/config/local.yaml` (git-ignored, with a committed
`local.example.yaml`) turns "deploy on another host" from a code edit into a config edit.

**C. Idempotent installers, one per side.**
`setup/hermes-side.sh`: copy plugins, enable them, apply config keys, run the patch checker, restart the
gateway, run the verification block from the README. `setup/device-side.sh`: copy `pi/` files, install
and enable the units in the right order, install the wake model, copy `sounds/`. Both should be safe to
run twice, and both should refuse rather than guess.

**D. Pin the environment.**
`requirements.txt` is committed now. Add a `bootstrap.sh` that builds `.venv` from it, and record the
Piper voice's source URL so `voices/` can stay out of git (61 MB) while still being reproducible.

**E. Write the voice-surface contract down as a test.**
The exact toolsets, the expected tool list and the "no `tool_search` bridge" rule should live in one
command that can be run after any Hermes upgrade. The README's verification block is that check today;
promote it to `setup/verify-surface.sh` so an upgrade can be validated in one line.

**F. Document extension, and admit the limits.**
Add `docs/EXTENDING.md` covering: adding a second satellite (currently **not** supported: one
`SATELLITE_HOST`, one session file), adding a second room by running a second bridge on another port,
adding a new tool (plugin, then the `_HERMES_CORE_TOOLS` line, then `_DEFAULT_OFF_TOOLSETS`, then the
platform toolset list), and the security posture that a new deployment must preserve (loopback API
server, token-authenticated announcements, no LAN exposure of the agent surface).

## What this proposal does not do

It does not commit secrets, does not change any running service, and does not touch the Hermes checkout.
Everything on the system is exactly as it was found; this repository is additive.
