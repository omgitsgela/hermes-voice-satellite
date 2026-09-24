"""Announcement endpoint: house events spoken out loud on the satellite.

Angela's decisions (2026-09-22):

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
from typing import Callable, Optional

from aiohttp import web

from bridge import tts
from bridge.config import (
    ANNOUNCE_CHIME,
    ANNOUNCE_DEDUP_SECONDS,
    ANNOUNCE_HOST,
    ANNOUNCE_LOG,
    ANNOUNCE_MAX_CHARS,
    ANNOUNCE_MAX_QUEUE,
    ANNOUNCE_PORT,
    ANNOUNCE_TOKEN_ENV,
    ANNOUNCE_TOKEN_FILE,
    ANNOUNCE_TURN_MAX_AGE,
    AUDIO_OUT,
    SATELLITE_HOST,
    SATELLITE_PORT,
)
from bridge.satellite import SatelliteClient

_LOGGER = logging.getLogger("bridge.announce")


@dataclass
class Announcement:
    """One queued announcement."""

    id: str
    text: str
    source: str
    key: Optional[str]
    chime: bool
    received_at: float


class Announcer:
    """Owns the announcement HTTP endpoint, its queue, and the satellite connection it speaks on."""

    def __init__(self, *, sat: SatelliteClient, turn_started_at: Callable[[], Optional[float]],
                 follow_up_get: Optional[Callable[[], bool]] = None,
                 follow_up_set: Optional[Callable[[bool], bool]] = None,
                 endpointing_profile_get: Optional[Callable[[], str]] = None,
                 endpointing_profile_set: Optional[Callable[[str], str]] = None,
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

    # -- token ---------------------------------------------------------------

    def _load_token(self) -> str:
        """Env override, else the token file, else generate one.

        The file is the handoff point for the Hubitat driver preference: it is created mode 600
        and never printed, so the value stays out of logs and out of this conversation.
        """
        from_env = os.environ.get(ANNOUNCE_TOKEN_ENV, "").strip()
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

    async def start(self) -> None:
        self._token = self._load_token()
        app = web.Application()
        app.router.add_post("/announce", self._handle_announce)
        app.router.add_get("/announce/health", self._handle_health)
        # The device's own settings, on the port the bridge already listens on. Token-protected
        # like announcements: this changes how the device behaves in the room.
        app.router.add_get("/settings", self._handle_settings_get)
        app.router.add_post("/settings", self._handle_settings_post)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self._worker = asyncio.create_task(self._run_worker(), name="announcer")
        _LOGGER.info("announcement endpoint on http://%s:%d/announce (bearer token required)",
                     self.host, self.port)

    async def stop(self) -> None:
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
        chime = bool(payload.get("chime", True))

        if key and self._is_duplicate(key, text):
            self.dropped += 1
            _LOGGER.info("ignoring a duplicate announcement (key %s from %s)", key, source)
            self._log({"outcome": "duplicate", "source": source, "key": key, "text": text})
            return web.json_response({"ok": True, "queued": False, "duplicate": True})

        item = Announcement(
            id=f"an-{datetime.now().strftime('%H%M%S')}-{secrets.token_hex(2)}",
            text=text, source=source, key=key, chime=chime, received_at=time.monotonic(),
        )
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1
            _LOGGER.warning("announcement queue full; dropping %r from %s", text[:60], source)
            self._log({"outcome": "queue_full", "source": source, "key": key, "text": text})
            return web.json_response({"ok": False, "error": "queue full"}, status=503)

        self.queued += 1
        _LOGGER.info("announcement queued (%s from %s, %d waiting): %s",
                     item.id, source, self._queue.qsize(), text[:80])
        return web.json_response({"ok": True, "queued": True, "id": item.id,
                                  "position": self._queue.qsize()}, status=202)

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
        self._recent[fingerprint] = now
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
                _LOGGER.warning("announcement %s failed: %s: %s", item.id, type(exc).__name__, exc)
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
        await self._sat.play_wav(wav, wait_for_played=False)
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
        if not item.chime:
            return speech
        if not ANNOUNCE_CHIME.exists():
            _LOGGER.warning("chime missing at %s; announcing without it", ANNOUNCE_CHIME)
            return speech
        out = AUDIO_OUT / f"announce-{item.id}-full.wav"
        _concat_wavs([ANNOUNCE_CHIME, speech], out)
        return out

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

    The gap goes BETWEEN clips only, never trailing. Angela asked for a full second of quiet
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
