"""Announcement endpoint: house events spoken out loud on the satellite.

Decisions (2026-09-22):

  * a dedicated LAN port, never behind Caddy and never reachable from outside;
  * any LAN device may call it, authenticated with a bearer token;
  * an announcement waits until any conversation in progress has finished speaking;
  * every announcement opens with a wake tone;
  * a failed delivery is dropped, with no fallback path.

The announcer speaks on the CONVERSATION LOOP's connection rather than opening its own.
Measured, not assumed: a second connection never reached the satellite's snd service at all
(aplay never started), because the satellite only arms playback for a client that has sent
`RunSatellite`, and sending that on a second connection risks the satellite delivering wake-word
detections to the wrong client. Sharing the loop's connection avoids both problems. The cost is
that the announcer must not wait for the satellite's `Played` event (the loop's capture can
swallow it), so playback is fire-and-forget and the clip's playout is timed locally instead. All
speech is serialised by `SatelliteClient.PLAYBACK_LOCK`: the satellite has one aplay, so two
streams would interleave into it.

Wire protocol:

    POST /announce
      Authorization: Bearer <token>
      {"text": "Freezer temperature is 40 degrees", "source": "hubitat", "key": "freezer-40"}
    -> 202 {"ok": true, "queued": true, "id": "an-...", "position": 1}

    401 unauthorised
    400 text missing, empty, or longer than ANNOUNCE_MAX_CHARS
    503 queue full (the text is dropped rather than spoken late)

The response means "queued", not "spoken": delivery is best-effort by design, and every outcome
is recorded in ANNOUNCE_LOG so that "did it announce?" always has an answer.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

from aiohttp import web

from bridge import tts
from bridge.config import (
    ALARM_VOLUME_HOLD_MARGIN,
    ALARM_VOLUME_HOLD_MAX,
    ANNOUNCE_DEDUP_SECONDS,
    ANNOUNCE_HOST,
    ANNOUNCE_LOG,
    ANNOUNCE_MAX_CHARS,
    ANNOUNCE_MAX_QUEUE,
    ANNOUNCE_PORT,
    ANNOUNCE_TOKEN_ENV,
    _cfg,
    ANNOUNCE_TOKEN_FILE,
    ANNOUNCE_SOUNDS,
    ANNOUNCE_TURN_MAX_AGE,
    AUDIO_OUT,
    FLEET_RESTART_DISPATCH_INTERVAL_SECONDS,
    FLEET_DISPATCH_TIMEOUT_SECONDS,
)
from bridge.satellite import SatelliteClient
from bridge.enroll import EnrollmentService
from bridge.timers import DEFAULT_STATE_PATH, TimerLimitError, TimerManager, parse_duration
from bridge.config_plane import (
    ConfigConflict, ConfigPlane, ConfigValidationError, setting_requires_restart,
)

_LOGGER = logging.getLogger("bridge.announce")


@dataclass
class Announcement:
    """One queued announcement."""

    id: str
    text: str
    source: str
    key: Optional[str]
    sound: str
    received_at: float


class Announcer:
    """Owns the announcement HTTP endpoint, its queue, and the satellite connection it speaks on."""

    def __init__(self, *, sat: SatelliteClient, turn_started_at: Callable[[], Optional[float]],
                 follow_up_get: Optional[Callable[[], bool]] = None,
                 follow_up_set: Optional[Callable[[bool], bool]] = None,
                 endpointing_profile_get: Optional[Callable[[], str]] = None,
                 endpointing_profile_set: Optional[Callable[[str], str]] = None,
                 timer_state_path: Path = DEFAULT_STATE_PATH,
                 config_plane: Optional[ConfigPlane] = None,
                 connected_device_ids: Optional[Callable[[], Iterable[str]]] = None,
                 config_dispatch: Optional[
                     Callable[[str, dict[str, Any]], Awaitable[bool] | bool]
                 ] = None,
                 volume_get: Optional[Callable[[], int]] = None,
                 volume_set: Optional[Callable[[int], Any]] = None,
                 enrollment: Optional[EnrollmentService] = None,
                 host: str = ANNOUNCE_HOST, port: int = ANNOUNCE_PORT) -> None:
        self._sat = sat          # the conversation loop's connection; see the module docstring
        self._turn_started_at = turn_started_at
        # Follow-up mode is the loop's state, not ours: we only publish the switch. Both callbacks
        # are optional so an Announcer built without a loop still works.
        self._follow_up_get = follow_up_get
        self._follow_up_set = follow_up_set
        self._endpointing_profile_get = endpointing_profile_get
        self._endpointing_profile_set = endpointing_profile_set
        self.host = host
        self.port = port
        self.token_path = ANNOUNCE_TOKEN_FILE
        self._queue: asyncio.Queue[Announcement] = asyncio.Queue(maxsize=ANNOUNCE_MAX_QUEUE)
        self._runner: Optional[web.AppRunner] = None
        self._worker: Optional[asyncio.Task] = None
        self._token = ""
        self._recent: dict[str, float] = {}
        self.queued = 0
        self.spoken = 0
        self.dropped = 0
        self.last_spoken_at: Optional[float] = None
        self.timers = TimerManager(state_path=timer_state_path, on_expire=self._timer_expired)
        self.config_plane = config_plane
        self._connected_device_ids = connected_device_ids or (lambda: ())
        self._config_dispatch = config_dispatch
        # Alarm volume override (issue #10, 2026-09-25): an alarm may
        # raise a room that is too quiet to hear it. Upward only. Both optional, so an
        # Announcer built without a volume path still announces.
        self._volume_get: Optional[Callable[[], int]] = volume_get
        self._volume_set: Optional[Callable[[int], Any]] = volume_set
        # Plug-and-play satellite enrolment (2026-10-08) shares this LAN port with announcements:
        # same server, same trusted-network story. Its token is loaded lazily at start().
        self.enrollment = enrollment if enrollment is not None else EnrollmentService()
        # Pacing for fleet writes that touch a restart-class setting (wake word, threshold):
        # one device per interval, so the rooms go quiet one at a time instead of all at once.
        # The write itself is durable; pacing only shapes *when* each connected device is told.
        # The lock serializes the wait/dispatch/deadline-update cycle: without it, two
        # concurrent fleet writes would each sleep until the same deadline and wake up
        # together, bouncing two rooms at once.
        self._restart_dispatch_at = 0.0
        self._fleet_tasks: set = set()
        self._fleet_pace_lock = asyncio.Lock()

    # -- token ---------------------------------------------------------------

    def _load_token(self) -> str:
        """Env override, else the token file, else generate one.

        The file is the handoff point for the Hubitat driver preference: it is created mode 600
        and never printed, so the value stays out of logs and out of this conversation.
        """
        from_env = _cfg(ANNOUNCE_TOKEN_ENV, "").strip()
        if from_env:
            return from_env
        try:
            existing = ANNOUNCE_TOKEN_FILE.read_text().strip()
        except FileNotFoundError:
            existing = ""
        except OSError as exc:  # noqa: BLE001
            _LOGGER.warning("could not read %s: %s", ANNOUNCE_TOKEN_FILE, exc)
            existing = ""
        if existing:
            return existing
        token = secrets.token_urlsafe(32)
        ANNOUNCE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        ANNOUNCE_TOKEN_FILE.write_text(token + "\n")
        ANNOUNCE_TOKEN_FILE.chmod(0o600)
        _LOGGER.info("generated an announcement token at %s (mode 600)", ANNOUNCE_TOKEN_FILE)
        return token

    # -- lifecycle -----------------------------------------------------------

    def use_connection(self, sat) -> None:
        """Point announcements at the conversation's current connection.

        The announcer speaks on the loop's connection rather than opening its own (see the module
        docstring). That reference is taken at construction, so when the loop swaps the satellite underneath
        it, a lease replacing a dialled client, announcements have to be told. Without this they keep talking
        into a socket that has been replaced.
        """
        self._sat = sat

    async def start(self) -> None:
        self._token = self._load_token()
        app = web.Application()
        app.router.add_post("/announce", self._handle_announce)
        app.router.add_get("/announce/health", self._handle_health)
        # The device's own settings, on the port the bridge already listens on. Token-protected
        # like announcements: this changes how the device behaves in the room.
        app.router.add_get("/settings", self._handle_settings_get)
        app.router.add_post("/settings", self._handle_settings_post)
        app.router.add_get("/timers", self._handle_timers_get)
        app.router.add_post("/timers", self._handle_timers_post)
        app.router.add_get("/configure", self._handle_config_page)
        app.router.add_get("/config/schema", self._handle_config_schema)
        app.router.add_get("/config/devices", self._handle_config_devices)
        app.router.add_get("/config/devices/{device_id}", self._handle_config_device)
        app.router.add_put("/config/devices/{device_id}/desired", self._handle_config_desired)
        app.router.add_put("/config/fleet/desired", self._handle_config_fleet)
        app.router.add_post("/config/devices/{device_id}/reported", self._handle_config_reported)
        # Satellite enrolment, beside /announce on this same LAN port (bridge/enroll.py).
        self.enrollment.register(app)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self._worker = asyncio.create_task(self._run_worker(), name="announcer")
        await self.timers.start()
        _LOGGER.info("announcement endpoint on http://%s:%d/announce (bearer token required)",
                     self.host, self.port)

    async def stop(self) -> None:
        # Pending fleet restart-class dispatches are sleeping tasks that would otherwise
        # survive shutdown and keep bouncing device services after the bridge has stopped.
        # This cleanup is unconditional: it must run even if an earlier shutdown step raises,
        # because a task that outlives stop() is a service being restarted after the fact.
        try:
            await self.timers.stop()
            if self._worker is not None:
                self._worker.cancel()
                try:
                    await self._worker
                except asyncio.CancelledError:
                    pass
                self._worker = None
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = None
        finally:
            if self._fleet_tasks:
                for task in list(self._fleet_tasks):
                    task.cancel()
                await asyncio.gather(*self._fleet_tasks, return_exceptions=True)
                self._fleet_tasks.clear()
        # The satellite connection belongs to the loop, which closes it during its own shutdown.

    # -- http ----------------------------------------------------------------

    def _authorised(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        presented = header[len("Bearer "):].strip()
        return bool(presented) and hmac.compare_digest(presented, self._token)

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Unauthenticated liveness only: no payload, no text, nothing worth leaking."""
        return web.json_response({"ok": True, "queued": self._queue.qsize(),
                                  "spoken": self.spoken, "dropped": self.dropped})

    async def _handle_settings_get(self, request: web.Request) -> web.Response:
        """Read the device's settings. Bearer token required, like announcements."""
        if not self._authorised(request):
            _LOGGER.warning("rejected an unauthorised settings read from %s", request.remote)
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        if self._follow_up_get is None or self._endpointing_profile_get is None:
            return web.json_response({"ok": False, "error": "not wired to the loop"}, status=501)
        return web.json_response({
            "ok": True,
            "follow_up": bool(self._follow_up_get()),
            "endpointing_profile": self._endpointing_profile_get(),
        })

    async def _handle_settings_post(self, request: web.Request) -> web.Response:
        """Change exactly one validated device setting."""
        if not self._authorised(request):
            _LOGGER.warning("rejected an unauthorised settings change from %s", request.remote)
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        if self._follow_up_set is None or self._endpointing_profile_set is None:
            return web.json_response({"ok": False, "error": "not wired to the loop"}, status=501)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - a bad body is a 400, not a 500
            return web.json_response({"ok": False, "error": "body must be JSON"}, status=400)
        if not isinstance(body, dict) or len(body) != 1:
            return web.json_response(
                {"ok": False,
                 "error": "expected exactly one of follow_up or endpointing_profile"}, status=400)

        if "follow_up" in body:
            wanted = body["follow_up"]
            if not isinstance(wanted, bool):
                return web.json_response(
                    {"ok": False, "error": "follow_up must be true or false"}, status=400)
            result = bool(self._follow_up_set(wanted))
            _LOGGER.info("follow-up mode set to %s over the settings endpoint", result)
            return web.json_response({"ok": True, "follow_up": result})

        if "endpointing_profile" in body:
            wanted = body["endpointing_profile"]
            if not isinstance(wanted, str):
                return web.json_response(
                    {"ok": False, "error": "endpointing_profile must be a string"}, status=400)
            try:
                result = self._endpointing_profile_set(wanted)
            except ValueError as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=400)
            except OSError as exc:
                _LOGGER.error("could not persist endpointing profile: %s", exc)
                return web.json_response(
                    {"ok": False, "error": "could not persist endpointing profile"}, status=500)
            _LOGGER.info("endpointing profile set to %s over the settings endpoint", result)
            return web.json_response({"ok": True, "endpointing_profile": result})

        return web.json_response(
            {"ok": False, "error": "unknown setting; expected follow_up or endpointing_profile"},
            status=400)

    async def _handle_announce(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            self.dropped += 1
            _LOGGER.warning("rejected an unauthorised announcement from %s", request.remote)
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - any malformed body is a 400, never a 500
            return web.json_response({"ok": False, "error": "body must be JSON"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"},
                                     status=400)

        text = self._clean(str(payload.get("text") or ""))
        if not text:
            return web.json_response({"ok": False, "error": "text is required"}, status=400)
        if len(text) > ANNOUNCE_MAX_CHARS:
            self.dropped += 1
            _LOGGER.warning("refused a %d char announcement (limit %d)", len(text),
                            ANNOUNCE_MAX_CHARS)
            return web.json_response(
                {"ok": False,
                 "error": f"text is {len(text)} chars, the limit is {ANNOUNCE_MAX_CHARS}"},
                status=400)

        source = self._clean(str(payload.get("source") or "unknown"))[:40] or "unknown"
        key = self._clean(str(payload.get("key") or ""))[:80] or None
        requested_sound = payload.get("sound")
        if requested_sound is None:
            sound = "chime" if bool(payload.get("chime", True)) else "none"
        elif not isinstance(requested_sound, str) or requested_sound not in ANNOUNCE_SOUNDS:
            return web.json_response(
                {"ok": False, "error": f"sound must be one of {list(ANNOUNCE_SOUNDS)}"}, status=400)
        else:
            sound = requested_sound

        if key and self._is_duplicate(key, text):
            self.dropped += 1
            _LOGGER.info("ignoring a duplicate announcement (key %s from %s)", key, source)
            self._log({"outcome": "duplicate", "source": source, "key": key, "text": text})
            return web.json_response({"ok": True, "queued": False, "duplicate": True})

        try:
            item = self._enqueue(text=text, source=source, key=key, sound=sound)
            if key:
                # Only accepted messages may suppress retries after queue pressure.
                self._recent[f"{key}|{text}"] = time.monotonic()
        except asyncio.QueueFull:
            self.dropped += 1
            _LOGGER.warning("announcement queue full; dropping %r from %s", text[:60], source)
            self._log({"outcome": "queue_full", "source": source, "key": key, "text": text})
            return web.json_response({"ok": False, "error": "queue full"}, status=503)

        _LOGGER.info("announcement queued (%s from %s, %d waiting): %s",
                     item.id, source, self._queue.qsize(), text[:80])
        return web.json_response({"ok": True, "queued": True, "id": item.id,
                                  "position": self._queue.qsize()}, status=202)

    def _enqueue(self, *, text: str, source: str, key: Optional[str], sound: str) -> Announcement:
        item = Announcement(
            id=f"an-{datetime.now().strftime('%H%M%S')}-{secrets.token_hex(2)}",
            text=text, source=source, key=key, sound=sound, received_at=time.monotonic(),
        )
        self._queue.put_nowait(item)
        self.queued += 1
        return item

    async def _timer_expired(self, timer: dict) -> None:
        label = self._clean(str(timer.get("label") or ""))
        text = f"Your {label} timer is done." if label else "Your timer is done."
        try:
            self._enqueue(text=text, source="timer", key=f"timer:{timer['id']}", sound="timer")
        except asyncio.QueueFull:
            self.dropped += 1
            _LOGGER.error("timer %s expired but the announcement queue was full", timer.get("id"))
            self._log({"outcome": "queue_full", "source": "timer", "key": timer.get("id"),
                       "text": text})

    async def _handle_timers_get(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        timers = self.timers.list_timers()
        return web.json_response({"ok": True, "count": len(timers), "timers": timers})

    async def _handle_timers_post(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "body must be JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)
        action = body.get("action")
        try:
            if action == "list":
                timers = self.timers.list_timers()
                return web.json_response({"ok": True, "count": len(timers), "timers": timers})
            if action == "set":
                if "seconds" in body and "duration" in body:
                    raise ValueError("provide seconds or duration, not both")
                seconds = (int(body["seconds"]) if "seconds" in body
                           else parse_duration(str(body.get("duration") or "")))
                timer = self.timers.set_timer(seconds, str(body.get("label") or ""))
                return web.json_response({"ok": True, "timer": timer}, status=201)
            if action == "cancel":
                cancelled = self.timers.cancel_timer(
                    timer_id=str(body.get("id") or "") or None,
                    label=str(body.get("label") or "") or None,
                    all_timers=body.get("all") is True,
                )
                return web.json_response({"ok": True, "cancelled": len(cancelled),
                                          "timers": cancelled})
        except (ValueError, TypeError, TimerLimitError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response(
            {"ok": False, "error": "unknown action; expected set, list, or cancel"}, status=400)

    def _config_ready(self) -> Optional[web.Response]:
        if self.config_plane is None:
            return web.json_response(
                {"ok": False, "error": "configuration plane is not enabled"}, status=501)
        return None

    async def _handle_config_page(self, _request: web.Request) -> web.Response:
        """Serve a credential-free client; the operator supplies the bearer token locally."""
        page = Path(__file__).resolve().parent / "static" / "settings.html"
        try:
            content = page.read_text()
        except OSError:
            return web.Response(text="configuration page is not installed", status=503)
        return web.Response(text=content, content_type="text/html")

    async def _handle_config_schema(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        unavailable = self._config_ready()
        if unavailable is not None:
            return unavailable
        return web.json_response({"ok": True, **self.config_plane.schema()})

    async def _handle_config_devices(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        unavailable = self._config_ready()
        if unavailable is not None:
            return unavailable
        devices = self.config_plane.list_devices(connected_ids=self._connected_device_ids())
        return web.json_response({"ok": True, "count": len(devices), "devices": devices})

    async def _handle_config_device(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        unavailable = self._config_ready()
        if unavailable is not None:
            return unavailable
        device_id = request.match_info.get("device_id", "")
        try:
            device = self.config_plane.get_device(
                device_id, connected=device_id in set(self._connected_device_ids()))
        except ConfigValidationError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except KeyError:
            return web.json_response({"ok": False, "error": "device not found"}, status=404)
        return web.json_response({"ok": True, "device": device})

    async def _handle_config_desired(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        unavailable = self._config_ready()
        if unavailable is not None:
            return unavailable
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "body must be JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)
        expected = body.get("expected_generation")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            return web.json_response(
                {"ok": False, "error": "expected_generation must be a non-negative integer"},
                status=400)
        device_id = request.match_info.get("device_id", "")
        try:
            device = self.config_plane.set_desired(
                device_id, body.get("settings"), expected_generation=expected)
        except ConfigConflict as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=409)
        except ConfigValidationError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        dispatched = False
        if self._config_dispatch is not None and device_id in set(self._connected_device_ids()):
            try:
                result = self._config_dispatch(device_id, device)
                dispatched = bool(await result) if asyncio.iscoroutine(result) else bool(result)
            except Exception as exc:  # noqa: BLE001 - desired state remains durable for reconnect
                _LOGGER.warning("could not dispatch desired state to %s: %s", device_id, exc)
        return web.json_response(
            {"ok": True, "accepted": True, "dispatched": dispatched, "device": device},
            status=202,
        )

    async def _handle_config_fleet(self, request: web.Request) -> web.Response:
        """Write one desired-state patch to the whole fleet (or a named subset), durably.

        This is the "change it on all satellites" endpoint. The patch is applied to every
        targeted device in a single atomic save, so the fleet never ends up half-written.
        Dispatch to connected devices happens afterwards and is best-effort: a device that is
        away still carries the new generation and converges when it reconnects, which is the
        declarative guarantee the plane is built around.
        """
        if not self._authorised(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        unavailable = self._config_ready()
        if unavailable is not None:
            return unavailable
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "body must be JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)

        raw_ids = body.get("device_ids")
        device_ids: Optional[list] = None
        if raw_ids is not None:
            if not isinstance(raw_ids, list) or not all(
                isinstance(i, str) for i in raw_ids
            ):
                return web.json_response(
                    {"ok": False, "error": "device_ids must be a list of strings"},
                    status=400,
                )
            device_ids = list(raw_ids)

        settings = body.get("settings")
        if not isinstance(settings, dict):
            return web.json_response(
                {"ok": False, "error": "settings must be a JSON object"}, status=400,
            )
        if self.config_plane is None:
            return web.json_response(
                {"ok": False, "error": "configuration plane is not enabled"}, status=501,
            )

        try:
            records = self.config_plane.set_desired_fleet(settings, device_ids=device_ids)
        except ConfigValidationError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        connected = set(self._connected_device_ids())
        # One uniform patch goes to every targeted device. If it touches a setting that bounces a
        # device service (wake word, threshold), then every device in this write is a restart-class
        # application, so the whole write is paced: rooms go quiet one at a time, not all at once.
        restart_class = any(setting_requires_restart(name) for name in settings)
        results = []
        pending_restart: list = []
        for record in records:
            device_id = record["device_id"]
            dispatched = False
            live = device_id in connected and self._config_dispatch is not None
            if live and not restart_class:
                # Immediate-class change to a connected device: tell it now and report it.
                try:
                    result = self._config_dispatch(device_id, record)
                    dispatched = bool(await result) if asyncio.iscoroutine(result) else bool(result)
                except Exception as exc:  # noqa: BLE001 - desired state remains durable for reconnect
                    _LOGGER.warning("could not dispatch fleet desired state to %s: %s", device_id, exc)
            if restart_class and live:
                # A restart-class change bounces this device's audio stack. Do not fire the whole
                # fleet at once: queue it for the paced dispatcher, one device per interval.
                pending_restart.append(record)
            results.append({**record, "dispatched": dispatched})

        if pending_restart and self._config_dispatch is not None:
            task = asyncio.create_task(
                self._dispatch_restart_class(pending_restart), name="fleet-restart-dispatch"
            )
            self._fleet_tasks.add(task)
            task.add_done_callback(self._fleet_tasks.discard)

        return web.json_response({
            "ok": True,
            "accepted": True,
            "count": len(results),
            "restart_pacing": bool(pending_restart),
            "devices": results,
        }, status=202)

    async def _do_dispatch(self, device_id: str, record: dict) -> bool:
        """Run one fleet dispatch and return its success.

        Supports both sync and async dispatch callbacks. Kept separate so the caller can
        bound it with a timeout: the dispatch writes through the device's connection, and a
        connection that stopped consuming output would otherwise block on its own write lock
        for an unbounded time.
        """
        result = self._config_dispatch(device_id, record) if self._config_dispatch else None
        if result is None:
            return False
        if asyncio.iscoroutine(result):
            return bool(await result)
        return bool(result)

    async def _dispatch_restart_class(self, records: list) -> None:
        """Pace the delivery of a restart-class fleet write: one device per interval.

        The write is already durable, so this only controls *when* each connected device is
        told to bounce. A device that is away is skipped here (it converges on reconnect); a
        dispatch that fails or times out is logged, because the device still holds the
        generation and will take it on its next registration, which is the plane's normal
        recovery path.

        Each device **reserves its slot under the lock**, then sleeps and dispatches
        **outside** it. The reservation is fast and never awaits external work, so one device
        that hangs cannot hold the pacing slot and stall every other room: concurrent fleet
        writes still get distinct, ordered deadlines (no two rooms bounce at the same
        instant), but a slow device's dispatch is bounded by `FLEET_DISPATCH_TIMEOUT_SECONDS`
        and runs in parallel with the next device's scheduled slot rather than ahead of it.
        """
        interval = max(0.0, FLEET_RESTART_DISPATCH_INTERVAL_SECONDS)
        for record in records:
            device_id = record["device_id"]
            if self._config_dispatch is None:
                return
            if device_id not in set(self._connected_device_ids()):
                _LOGGER.info(
                    "fleet restart: %s is not connected; it will take the change on reconnect",
                    device_id)
                continue
            # Reserve this device's slot under the lock: distinct deadlines for concurrent
            # writes, no external await while holding it.
            async with self._fleet_pace_lock:
                now = time.monotonic()
                slot = self._restart_dispatch_at
                if slot <= now:
                    my_slot = now
                    self._restart_dispatch_at = now + interval
                else:
                    my_slot = slot
                    self._restart_dispatch_at = slot + interval
            # Sleep and dispatch OUTSIDE the lock, bounded so a hung device cannot block the
            # fleet's pacing indefinitely.
            wait = my_slot - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                ok = await asyncio.wait_for(
                    self._do_dispatch(device_id, record),
                    timeout=FLEET_DISPATCH_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "fleet restart: dispatch to %s timed out; durable for reconnect", device_id)
                continue
            except Exception as exc:  # noqa: BLE001 - one device failing must not stop the rest
                _LOGGER.warning("fleet restart: dispatch to %s failed: %s", device_id, exc)
                continue
            if not ok:
                _LOGGER.warning("fleet restart: could not dispatch %s; durable for reconnect",
                                device_id)

    async def _handle_config_reported(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        unavailable = self._config_ready()
        if unavailable is not None:
            return unavailable
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "body must be JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)
        try:
            device = self.config_plane.report(
                request.match_info.get("device_id", ""),
                generation=body.get("generation"),
                actual=body.get("actual"),
                health=body.get("health"),
                error=body.get("error"),
            )
        except ConfigValidationError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "device": device})

    @staticmethod
    def _clean(text: str) -> str:
        """Collapse whitespace and drop unprintable characters.

        The text is spoken verbatim, so a control character is a caller bug, not content.
        """
        return " ".join("".join(ch for ch in text if ch.isprintable()).split())

    def _is_duplicate(self, key: str, text: str) -> bool:
        """True when the same key and text were seen inside the dedup window."""
        now = time.monotonic()
        for fingerprint, seen in list(self._recent.items()):
            if now - seen > ANNOUNCE_DEDUP_SECONDS:
                del self._recent[fingerprint]
        fingerprint = f"{key}|{text}"
        if fingerprint in self._recent:
            self._recent[fingerprint] = now
            return True
        return False

    # -- speaking ------------------------------------------------------------

    def _turn_is_active(self) -> bool:
        """True while a conversation is being understood or spoken.

        The maximum age exists so that a turn which dies mid-flight cannot wedge announcements
        forever: past it, the flag is treated as stale and the announcement proceeds.
        """
        started = self._turn_started_at()
        if started is None:
            return False
        return (time.monotonic() - started) < ANNOUNCE_TURN_MAX_AGE

    async def _wait_for_quiet(self, item: Announcement) -> float:
        started = time.monotonic()
        if self._turn_is_active():
            _LOGGER.info("announcement %s is waiting: a conversation is in progress", item.id)
        while self._turn_is_active():
            await asyncio.sleep(0.25)
            if time.monotonic() - started > ANNOUNCE_TURN_MAX_AGE + 5.0:
                _LOGGER.warning("gave up waiting for the conversation to finish")
                break
        return time.monotonic() - started

    async def _run_worker(self) -> None:
        """Speak announcements one at a time, in arrival order."""
        while True:
            item = await self._queue.get()
            try:
                await self._announce(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a failed announcement is dropped
                self.dropped += 1
                # Log the traceback, not just the exception. A bare AssertionError with an empty
                # message was undiagnosable from the log line alone -- the announcement failed and
                # there was nothing to act on. exc_info costs nothing here and only fires on the
                # failure path.
                _LOGGER.warning("announcement %s failed: %s: %s", item.id, type(exc).__name__, exc,
                                exc_info=exc)
                self._log({"id": item.id, "outcome": "failed", "source": item.source,
                           "key": item.key, "text": item.text,
                           "error": f"{type(exc).__name__}: {exc}"})
            finally:
                self._queue.task_done()

    async def _announce(self, item: Announcement) -> None:
        waited = await self._wait_for_quiet(item)
        wav = await asyncio.to_thread(self._build_wav, item)
        # Fire and forget: play_wav returns once the clip is sent AND its playout has been timed
        # locally, holding the playback lock for the audible duration. Waiting for the satellite's
        # Played event is exactly what a caller on another component's connection must not do.
        #
        # An alarm may raise the room first (issue #10): an alarm nobody can hear is not an
        # alarm. The raise is upward-only, and it is held for the clip's own length because
        # play_wav returns when the audio has been SENT, not when it finishes.
        restore_to = await self._raise_for_alarm(item)
        try:
            await self._sat.play_wav(wav, wait_for_played=False)
            if restore_to is not None:
                await asyncio.sleep(self._volume_hold_seconds(wav))
        finally:
            if restore_to is not None:
                try:
                    result = self._volume_set(restore_to)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:  # noqa: BLE001 - never let tidying up break an announcement
                    _LOGGER.warning("could not restore the volume to %d: %s", restore_to, exc)
        self.spoken += 1
        self.last_spoken_at = time.time()
        self._prune_audio()
        _LOGGER.info("announcement %s spoken (%s, waited %.1fs)", item.id, item.source, waited)
        self._log({"id": item.id, "outcome": "spoken", "source": item.source, "key": item.key,
                   "text": item.text, "waited_seconds": round(waited, 2)})

    def _build_wav(self, item: Announcement) -> Path:
        """Wake tone and speech as one clip, with a beat between them and one playback session."""
        speech = AUDIO_OUT / f"announce-{item.id}.wav"
        tts.synthesize_to_wav(item.text, speech)
        sound_path = ANNOUNCE_SOUNDS.get(item.sound)
        if sound_path is None:
            return speech
        if not sound_path.exists():
            _LOGGER.warning("%s sound missing at %s; announcing without it", item.sound, sound_path)
            return speech
        out = AUDIO_OUT / f"announce-{item.id}-full.wav"
        _concat_wavs([sound_path, speech], out)
        return out

    async def _raise_for_alarm(self, item: Announcement) -> Optional[int]:
        """Raise the room for an alarm, returning the level to restore, or None.

        The decision, 2026-09-25: an alarm may override upward to a minimum alarm
        level so it is always audible. It is deliberately upward-only, because lowering a
        level the owner chose would make the override a surprise rather than a safeguard.

        Returns None when nothing needs restoring, which is the common case: a room already
        loud enough, a non-alarm sound, or no volume path wired at all.
        """
        if item.sound != "alarm":
            return None
        if self._volume_get is None or self._volume_set is None:
            _LOGGER.debug("no volume path wired; the alarm plays at the current level")
            return None

        from bridge.volume import alarm_percent

        try:
            current = int(self._volume_get())
        except Exception as exc:  # noqa: BLE001 - a bad read must not stop the alarm
            _LOGGER.warning("could not read the volume for the alarm override: %s", exc)
            return None

        target = alarm_percent(current)
        if target == current:
            return None
        try:
            result = self._volume_set(target)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - the alarm still has to play
            _LOGGER.warning("could not raise the volume for the alarm: %s", exc)
            return None
        if result is False:
            _LOGGER.warning("the volume raise for the alarm was refused; playing at %d%%", current)
            return None
        _LOGGER.info("alarm raised the volume from %d%% to %d%%", current, target)
        return current

    @staticmethod
    def _volume_hold_seconds(wav: Path) -> float:
        """How long to keep a raised level, from the clip's own length.

        play_wav returns once the audio has been SENT, not once it has finished, so
        restoring immediately would cut the alarm back down before anyone heard it and the
        override would be cosmetic. Bounded so a very long clip cannot pin the volume.
        """
        try:
            with wave.open(str(wav), "rb") as handle:
                rate = handle.getframerate() or 1
                seconds = handle.getnframes() / rate
        except (OSError, wave.Error) as exc:
            _LOGGER.warning("could not measure %s for the alarm hold: %s", wav, exc)
            seconds = 5.0
        return min(seconds + ALARM_VOLUME_HOLD_MARGIN, ALARM_VOLUME_HOLD_MAX)

    @staticmethod
    def _prune_audio(keep: int = 40) -> None:
        """Keep only the most recent announcement clips.

        Each announcement writes a WAV plus the chime-prefixed copy. They are worth keeping for
        "did it really say that?", but the host disk is ~77% full, so they must not grow without
        bound.
        """
        try:
            clips = sorted(AUDIO_OUT.glob("announce-*.wav"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for stale in clips[:-keep]:
            try:
                stale.unlink()
            except OSError:
                pass

    def _log(self, record: dict) -> None:
        """Append one JSON line per outcome, so announcements have an audit trail."""
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), **record}
        try:
            ANNOUNCE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with ANNOUNCE_LOG.open("a") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:  # noqa: BLE001
            _LOGGER.warning("could not append to %s: %s", ANNOUNCE_LOG, exc)


def _concat_wavs(paths: list[Path], out: Path, *, gap_ms: int = 1000) -> None:
    """Concatenate WAVs that already share the satellite's playback format.

    The gap goes BETWEEN clips only, never trailing. A full second of quiet was asked for
    between the wake tone and the announcement (2026-09-22); a trailing second would only extend
    how long the playback lock is held.

    Raises rather than silently resampling: a format mismatch here means the tone was copied
    wrongly, and guessing would hide it until someone heard a chipmunk announcement.
    """
    frames: list[bytes] = []
    rate = width = channels = None
    for index, path in enumerate(paths):
        with wave.open(str(path), "rb") as wf:
            fmt = (wf.getframerate(), wf.getsampwidth(), wf.getnchannels())
            if rate is None:
                rate, width, channels = fmt
            elif fmt != (rate, width, channels):
                raise ValueError(f"{path.name} is {fmt}; expected {(rate, width, channels)}")
            if index:
                frames.append(b"\x00" * int(rate * width * channels * gap_ms / 1000))
            frames.append(wf.readframes(wf.getnframes()))
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(b"".join(frames))
