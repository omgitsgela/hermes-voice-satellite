"""The voice loop: wake word -> endpointing -> local STT -> gateway -> streaming TTS.

One turn, in order, and why each step is where it is:

1. Wait for the satellite's `detection` event (its local wake word fired).
2. Feed mic `audio-chunk`s into the endpointer until it returns an utterance.
3. Send `transcript` to the satellite, which is the protocol's stop signal: it stops
   streaming, plays the done tone, and returns to wake-word listening. Doing this BEFORE
   any network work is what frees the mic promptly.
4. Transcribe locally (faster-whisper `small.en`, verified against ground truth in
   Phase 2), off the event loop so the satellite connection keeps being serviced.
5. Handle "new conversation" and the hourly idle rotation, both of which rotate the
   session id rather than editing anything.
6. Stream the answer from the gateway and synthesize it sentence by sentence, so the first
   words are audible while the rest is still being generated.

Self-trigger guard: while an answer is playing, the satellite is back in wake-word
listening mode and its own speaker output can be mistaken for the wake word (there is no
acoustic echo cancellation, and no refractory period is configured). A `detection` that
arrives while we are speaking is therefore treated as a suspected self-trigger: the stream
is aborted and the audio discarded, rather than starting an unintended new turn.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# The current conversation id, remembered across restarts. Without this, a restart falls back to the
# base id, which may be the very session carrying a stale, self-reinforcing claim (see the project
# note's "session inertia" section): the assistant would start insisting it cannot reach the house
# again, no matter how many capabilities were added since.
STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "voice-session.txt"


class IdleWakeRefresh(Exception):
    """No wake word for the configured window. Not a failure, just time to refresh the link.

    Deliberately NOT a subclass of TimeoutError or ConnectionError: the loop treats those as real
    connection problems and exits, which is the behaviour this replaces (2026-09-23).
    """


def _boot_id() -> str:
    """The host's boot id, used to tell a reboot from a restart."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""

import numpy as np
from wyoming.asr import Transcript
from wyoming.audio import AudioChunk
from wyoming.event import Event
from wyoming.snd import Played
from wyoming.wake import Detection

# LED states the satellite knows how to display, published by this loop.
#
# The Gateway owns "thinking", not the satellite, and that is deliberate. The satellite cannot
# tell its own filler ("One moment.", played when a tool call would otherwise leave a stretch of
# silence) from the real answer: both arrive as AudioStart, so a hook on the satellite ended the
# pulse about a second after it started and the lights stayed dark through the whole research
# step. Publishing the phase from here is exact, and the satellite forwards unknown events
# straight to its LED service, so no extra listener or LAN exposure is needed.
# Ask the Pi to open the microphone (via the wake shim) so a follow-up window has audio at all. A
# wake-word satellite never streams without a wake detection: see the hermes-voice-satellite skill.
WAKE_NOW = "wake-now"

LED_BUSY = "led-busy"
LED_IDLE = "led-idle"

from satellite_gateway.config import (CAPTURE_ACTIVATION_SECONDS, GATEWAY_BASE_URL, GATEWAY_ENV_FILE,
                           GATEWAY_API_KEY_VAR, SATELLITE_HOST, SATELLITE_PORT,
                           VOICE_INSTRUCTIONS, VOICE_ROUTE_ALIAS, VOICE_SESSION_ID,
                           WAKE_CLAIM_WINDOW_S)
from satellite_gateway.endpointing import Endpointer, EndpointResult
from satellite_gateway.gateway import VoiceGateway, VoiceGatewayError, read_api_key
from satellite_gateway.satellite import SatelliteClient

_LOGGER = logging.getLogger("satellite_gateway.loop")

# How long after our own playback a wake word may still be dismissed as our own echo. Beyond
# this a wake is a genuine one no matter what any flag says.
SELF_TRIGGER_WINDOW_S = 3.0

# How long the assistant may think out loud before it says anything at all before we say
# "One moment." on its behalf. The reference satellite fills a slow tool call the same way, and
# a slow first TOKEN deserves it just as much: silence after "Hey Hermes" reads as failure.
SILENCE_FILLER_SECONDS = 4.0

# Phrases that rotate the session on demand, checked on the normalised transcript.
NEW_CONVERSATION_PHRASES = (
    "new conversation", "start a new conversation", "start over", "forget that",
    "forget this conversation", "reset the conversation", "clear the conversation",
    "fresh conversation", "clean slate",
)

_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def normalise(text: str) -> str:
    # Punctuation becomes a space, so a comma leaves a double space: "Okay, stop." normalising to
    # "okay  stop" silently broke every multi-word phrase match until 2026-09-22. Collapse runs of
    # whitespace, or a phrase list can never be trusted.
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


class SentenceBuffer:
    """Chunk a token stream into speakable fragments.

    Two triggers: a sentence end, or `max_chars` reached. The length trigger matters because
    one long sentence would otherwise delay the first audible word for the whole sentence,
    which is exactly the latency the streaming design exists to avoid.
    """

    def __init__(self, max_chars: int = 140) -> None:
        self.max_chars = max_chars
        self._buffer = ""

    def push(self, delta: str) -> list[str]:
        self._buffer += delta
        out: list[str] = []
        while True:
            match = _SENTENCE_END.search(self._buffer)
            if match:
                piece = self._buffer[: match.end()].strip()
                self._buffer = self._buffer[match.end():]
                if piece:
                    out.append(piece)
                continue
            if len(self._buffer) >= self.max_chars:
                cut = self._buffer.rfind(" ", 0, self.max_chars)
                cut = cut if cut > 0 else self.max_chars
                piece = self._buffer[:cut].strip()
                self._buffer = self._buffer[cut:]
                if piece:
                    out.append(piece)
                continue
            break
        return out

    def flush(self) -> str:
        piece, self._buffer = self._buffer.strip(), ""
        return piece


@dataclass
class TurnStats:
    wake_word: str = ""
    transcript: str = ""
    reply: str = ""
    stt_seconds: float = 0.0
    first_delta_seconds: float = 0.0
    first_audio_seconds: float = 0.0
    total_seconds: float = 0.0
    # Split out on 2026-09-22 because the old single total was actively misleading: it included the
    # idle wait for the wake word, so a turn that sat for six minutes read as a turn that took six
    # minutes to answer. `listen` is the gap between the wake word and a completed utterance, which is
    # the number that shows an endpointer waiting on a noisy room.
    idle_seconds: float = 0.0
    listen_seconds: float = 0.0
    processing_seconds: float = 0.0
    audio_seconds: float = 0.0
    session_id: str = ""
    note: str = ""


class VoiceLoop:
    def __init__(self, *, satellite_host: str = SATELLITE_HOST,
                 satellite_port: int = SATELLITE_PORT,
                 gateway_base_url: str = GATEWAY_BASE_URL,
                 api_key: Optional[str] = None,
                 stt_model: str = "small.en", stt_threads: int = 4,
                 idle_reset_minutes: float = 60.0,
                 endpointer: Optional[dict] = None,
                 log_events: bool = False) -> None:
        self.satellite_host = satellite_host
        self.satellite_port = satellite_port
        self.stt_model = stt_model
        self.stt_threads = stt_threads
        self.idle_reset_minutes = idle_reset_minutes
        self._base_ep_kwargs = dict(endpointer or {})
        self.endpointing_profile = self._load_endpointing_profile()
        self._ep_kwargs = self._profiled_endpointing_kwargs(self.endpointing_profile)
        self._log_events = log_events
        # The device we are serving, when one dialled in and was leased (issue #61). None means `self.sat` is
        # the client that dials the satellite at its configured address.
        self.leased = None
        # Fleet leases (issue #111): EVERY registered device gets a lease, an arm and a
        # supervisor reader. `self.sat` is whichever lease currently owns the conversation;
        # the single lease that used to be the whole shape is what left a second registered
        # device permanently deaf (measured 2026-10-23: generations 1..97, never armed).
        self.leases: dict = {}
        self._supervisor_tasks: dict = {}
        # Which device owns the one global turn (decided 2026-10-23: one household
        # conversation, wakes queue FIFO). None means nobody owns it and supervisors read freely.
        self.turn_owner: Optional[str] = None
        # Self-trigger suppression is keyed PER DEVICE: device X's own answer can re-wake
        # device X, and today's global flag would wrongly suppress device Y's independent wake.
        self._speaking_at_device: dict = {}
        # Wakes from every armed device queue here, FIFO, and exactly one is claimed per turn.
        from satellite_gateway.wake_router import WakeRouter
        self.wake_router = WakeRouter(suppress_check=self._is_self_trigger,
                                      claim_window_s=WAKE_CLAIM_WINDOW_S)
        self.registry = None
        self.registry_server = None

        self._state_file = STATE_FILE
        # Follow-up mode: when on, the device listens again straight after it answers, so a
        # conversation does not need the wake word every turn. Persisted, so it survives a restart
        # and a script or a cron job can flip it as well as a voice command.
        self.follow_up = self._load_follow_up()
        # Set by a spoken "stop": the follow-up chain ends, but follow-up mode itself is untouched.
        self.stop_requested = False
        self.sat = SatelliteClient(satellite_host, satellite_port, log_events=log_events)
        self.gateway = VoiceGateway(
            base_url=gateway_base_url,
            api_key=api_key or read_api_key(GATEWAY_ENV_FILE, GATEWAY_API_KEY_VAR),
            # Resumed from disk when available, so continuity survives a restart AND a restart
            # cannot silently drop back into a session carrying a stale claim.
            session_id=self._load_session_id(),
            model=VOICE_ROUTE_ALIAS,
            instructions=VOICE_INSTRUCTIONS,
        )
        self._ep = Endpointer(**self._ep_kwargs)
        self._whisper = None
        self._last_turn_at: Optional[float] = None
        self._speaking = False
        self._speaking_at = 0.0   # monotonic time of the last playback activity
        # A wake detection that arrived mid-task and could not be used. It is the
        # person's only way to be heard, so it is kept for the next turn rather
        # than discarded -- see wait_for_wake.
        self._held_wake: Optional[str] = None
        self.self_trigger_count = 0
        self.turns: list[TurnStats] = []

        # Announcements must never talk over a conversation. `turn_started_at` marks the active
        # part of a turn (an utterance has been captured, through the answer finishing), and the
        # announcer waits for it to clear, for up to ANNOUNCE_TURN_MAX_AGE so that a turn which
        # dies mid-flight cannot wedge announcements forever.
        self.turn_started_at: Optional[float] = None

        from satellite_gateway.announce import Announcer
        from satellite_gateway.config import FLEET_CONFIG_STATE_FILE
        from satellite_gateway.config_plane import ConfigPlane

        self.config_plane = ConfigPlane(FLEET_CONFIG_STATE_FILE)
        # The device the configuration plane addresses. Empty until a device registers, at
        # which point `_adopt_device` sets it to the id that device actually used.
        #
        # This used to be VOICE_SESSION_ID, which is a SESSION id and not a device id. The
        # consequence was silent and total: a spoken volume command wrote a configuration
        # record for a device called "voice-satellite" that does not exist, the dispatch then
        # found no connection under that name, and the reply told the user it could not
        # reach the device - while every piece involved looked correctly wired.
        self.device_id = ""
        self.volume = 0
        self._load_volume(self)

        # Shares the loop's satellite connection: see satellite_gateway/announce.py for why it does not open
        # its own.
        self.announcer = Announcer(
            sat=self.sat,
            turn_started_at=lambda: self.turn_started_at,
            # Fleet (issue #111): whose conversation an announcement must not talk over.
            turn_owner=lambda: self.turn_owner,
            # The settings endpoint publishes the follow-up switch. It reads the attribute rather
            # than a copy, so a change made by voice or by the assistant is visible immediately.
            follow_up_get=lambda: self.follow_up,
            follow_up_set=self.set_follow_up,
            # A confirmed "yes" to the assistant's own "shall I start a new conversation?" needs
            # somewhere to go: the fixed-phrase matcher can only fire on an exact phrase, and the
            # model had no tool, so it had to refuse a request it had just proposed.
            conversation_reset=self.rotate_session,
            endpointing_profile_get=lambda: self.endpointing_profile,
            endpointing_profile_set=self.set_endpointing_profile,
            config_plane=self.config_plane,
            connected_device_ids=self._connected_device_ids,
            config_dispatch=self._dispatch_device_config,
            # The alarm volume override needs to read the level it is raising from and
            # write it back, so it goes through the same recorded path as a spoken change.
            volume_get=lambda: self.volume,
            volume_set=self._set_volume,
            # An alarm fans out to EVERY armed device, so the override has to be per device. The
            # single pair above addresses whichever device the loop happens to be talking to.
            volume_get_for=self._volume_for_device,
            volume_set_for=self._set_volume_for_device,
            # Fleet (issue #111): announcements speak on every armed lease, each over its own
            # registered connection, never a socket of the announcer's own.
            satellites=lambda: list(self.leases.values()),
        )

    # -- session handling ---------------------------------------------------

    def _new_session_id(self) -> str:
        """A session id that changes when we rotate, so drift cannot accumulate forever."""
        return f"{VOICE_SESSION_ID}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    def rotate_session(self, reason: str) -> str:
        """Start a fresh conversation, and remember it.

        Persisting matters: without it a restart falls back to the base id, which may be exactly the
        session whose history carries a stale, self-reinforcing claim. Measured on 2026-09-22: the
        assistant insisted it could not reach the house long after the tool existed, and invented
        fresh excuses for the limitation each time.
        """
        self.gateway.session_id = self._new_session_id()
        self._save_state()
        _LOGGER.info("session rotated (%s) -> %s", reason, self.gateway.session_id)
        return self.gateway.session_id

    def rotate_if_stale(self) -> bool:
        """Rotate when the gap since the last turn exceeds the idle window, ACROSS restarts.

        The in-process check (`_maybe_rotate_on_idle`) only sees turns this process handled. A Gateway
        that was down for hours — power cut, service stop, update — would otherwise resume an old
        conversation with no rotation at all, however long the silence lasted. That gap is the
        difference between "resets after an hour" being true and being nearly true, and it is exactly
        the case where a stale self-belief would survive.
        """
        if self.idle_reset_minutes <= 0:
            return False
        epoch = self._load_last_turn_epoch()
        if epoch is None:
            return False
        idle_minutes = (time.time() - epoch) / 60.0
        if idle_minutes >= self.idle_reset_minutes:
            self.rotate_session(
                f"idle {idle_minutes:.0f} min since the last turn (carried across a restart)")
            return True
        _LOGGER.info("resumed conversation is %.0f min old; idle window is %.0f min",
                     idle_minutes, self.idle_reset_minutes)
        return False

    def _state_lines(self) -> list[str]:
        """Non-empty lines of the state file: [session id, last-turn epoch]."""
        try:
            return [line.strip() for line in self._state_file.read_text().splitlines()
                    if line.strip()]
        except FileNotFoundError:
            return []
        except OSError as exc:  # noqa: BLE001
            _LOGGER.warning("could not read %s: %s", self._state_file, exc)
            return []

    def _load_session_id(self) -> str:
        """The last conversation id we used, or the stable base id on a first run."""
        saved = self._state_lines()
        if saved:
            _LOGGER.info("resuming conversation %s", saved[0])
            return saved[0]
        return VOICE_SESSION_ID

    def _load_last_turn_epoch(self) -> Optional[float]:
        """Wall-clock epoch of the last completed turn, or None when never recorded."""
        lines = self._state_lines()
        if len(lines) < 2:
            return None
        try:
            return float(lines[1])
        except ValueError:
            return None

    def _save_state(self) -> None:
        """Persist the conversation id and the current time as the last-turn stamp.

        "Now" is correct in both callers: right after a turn completes, and at a rotation.
        """
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(f"{self.gateway.session_id}\n{time.time():.0f}\n")
        except OSError as exc:  # noqa: BLE001
            _LOGGER.warning("could not persist the conversation state: %s", exc)

    # -- endpointing profile -------------------------------------------------

    def _load_endpointing_profile(self) -> str:
        """Read the persisted profile, failing safely to the standard timing."""
        from satellite_gateway.config import ENDPOINTING_PROFILES, ENDPOINTING_PROFILE_STATE_FILE

        try:
            saved = ENDPOINTING_PROFILE_STATE_FILE.read_text().strip()
        except FileNotFoundError:
            return "standard"
        except OSError as exc:  # noqa: BLE001
            _LOGGER.warning("could not read %s: %s", ENDPOINTING_PROFILE_STATE_FILE, exc)
            return "standard"
        if saved not in ENDPOINTING_PROFILES:
            _LOGGER.warning("ignoring unknown persisted endpointing profile %r", saved)
            return "standard"
        return saved

    def _profiled_endpointing_kwargs(self, profile: str) -> dict:
        """Overlay one validated profile onto the installation's base endpointer settings."""
        from satellite_gateway.config import ENDPOINTING_PROFILES

        if profile not in ENDPOINTING_PROFILES:
            raise ValueError(f"unknown endpointing profile: {profile}")
        result = dict(self._base_ep_kwargs)
        result.update(ENDPOINTING_PROFILES[profile])
        return result

    def set_endpointing_profile(self, profile: str) -> str:
        """Select and persist a bounded endpointing profile, then apply it to all capture paths."""
        from satellite_gateway.config import ENDPOINTING_PROFILE_STATE_FILE

        kwargs = self._profiled_endpointing_kwargs(profile)
        replacement = Endpointer(**kwargs)
        ENDPOINTING_PROFILE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        pending = ENDPOINTING_PROFILE_STATE_FILE.with_suffix(".tmp")
        try:
            pending.write_text(profile + "\n")
            pending.replace(ENDPOINTING_PROFILE_STATE_FILE)
        except OSError as exc:
            pending.unlink(missing_ok=True)
            raise OSError(f"could not persist endpointing profile: {exc}") from exc
        self.endpointing_profile = profile
        self._ep_kwargs = kwargs
        self._ep = replacement
        _LOGGER.info("endpointing profile set to %s", profile)
        return profile

    # -- follow-up mode ------------------------------------------------------

    def _load_follow_up(self) -> bool:
        """Read the persisted follow-up flag. Off unless the state file says on."""
        from satellite_gateway.config import FOLLOW_UP_STATE_FILE

        try:
            return FOLLOW_UP_STATE_FILE.read_text().strip().lower() in ("on", "1", "true", "yes")
        except FileNotFoundError:
            return False
        except OSError as exc:  # noqa: BLE001
            _LOGGER.warning("could not read %s: %s", FOLLOW_UP_STATE_FILE, exc)
            return False

    def set_follow_up(self, on: bool) -> bool:
        """Turn follow-up mode on or off and persist it. Returns the new state."""
        from satellite_gateway.config import FOLLOW_UP_STATE_FILE

        self.follow_up = bool(on)
        try:
            FOLLOW_UP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            FOLLOW_UP_STATE_FILE.write_text("on\n" if self.follow_up else "off\n")
        except OSError as exc:  # noqa: BLE001
            _LOGGER.warning("could not persist the follow-up flag: %s", exc)
        _LOGGER.info("follow-up mode %s", "on" if self.follow_up else "off")
        return self.follow_up

    async def _rearm_wake_word(self) -> None:
        """Put the satellite back to wake-word detection: close the mic, lights dark, armed again.

        RunSatellite rather than Transcript, because Transcript also plays the done tone and a window
        that heard nothing, or a conversation that was just told to stop, should stay silent.
        """
        try:
            await self.sat.run()
        except Exception as exc:  # noqa: BLE001 - never let tidying up break the loop
            _LOGGER.debug("could not re-arm the wake word: %s", exc)

    def _local_command(self, transcript: str) -> str:
        """Bridge-local spoken controls, answered without a model call.

        Handled here rather than by the assistant so they are instant and keep working when the
        gateway is unreachable. Returns the line to speak, or "" when this is not a control.
        """
        from satellite_gateway.config import (FOLLOW_UP_OFF_PHRASES, FOLLOW_UP_ON_PHRASES,
                                   FOLLOW_UP_STATUS_PHRASES, STOP_CORES, STOP_PREFIXES)

        heard = normalise(transcript)
        # Whole-utterance match only, never a substring: "stop" is an ordinary word and a substring
        # match would answer "stop the sprinklers" with "Stopping." instead of letting the model deal
        # with it. An optional lead-in plus exactly one core is what a person actually says ("okay
        # stop", "will you stop", "stop listening to me"), and it keeps the sprinklers safe.
        # Both sides normalised, for the same reason as before: the lists are written the way a
        # person writes them ("that's all"), while the transcript arrives folded ("that s all").
        cores = {normalise(c) for c in STOP_CORES}
        prefixes = {normalise(p) for p in STOP_PREFIXES}
        words = heard.split()
        stop_heard = False
        for take in range(min(4, len(words)), -1, -1):
            lead = " ".join(words[:take])
            if lead and lead not in prefixes:
                continue
            if " ".join(words[take:]) in cores:
                stop_heard = True
                break
        if stop_heard:
            self.stop_requested = True
            _LOGGER.info("spoken stop: ending this follow-up conversation, mode stays %s",
                         "on" if self.follow_up else "off")
            return "Stopping."
        # Status FIRST. "is follow up mode on" ends with "follow up mode on", so checking the
        # on-phrases first made the question turn the mode on. A question must never change state.
        if any(p in heard for p in FOLLOW_UP_STATUS_PHRASES):
            return f"Follow-up mode is {'on' if self.follow_up else 'off'}."
        if any(p in heard for p in FOLLOW_UP_OFF_PHRASES):
            self.set_follow_up(False)
            return "Follow-up mode is off."
        if any(p in heard for p in FOLLOW_UP_ON_PHRASES):
            self.set_follow_up(True)
            return ("Follow-up mode is on. I will keep listening for about fifteen seconds after "
                    "each answer.")
        return ""

    # -- volume (issue #10) --------------------------------------------------

    def _load_volume(self, target) -> int:
        """Adopt the recorded playback level, clamped, so a restart resumes it.

        The value is on the USER scale (1..100), where 1 is the quietest this hardware can
        be, so it is clamped on the USER scale. Using the raw clamp() here was a real bug:
        it treated a user value as a raw ALSA one and raised anything under the raw floor
        (50 here) up to it, so a stored 30 came back as 50 and the bottom half of the
        scale was unreachable from a restart.
        """
        from satellite_gateway.volume import USER_MIN, clamp_user

        level = USER_MIN
        if not self.device_id:
            # No device has registered yet, so there is nobody whose level to resume. The
            # quietest level is the right placeholder: quiet, but audible.
            target.volume = level
            return level
        try:
            record = self.config_plane.get_device(self.device_id)
        except KeyError:
            # The normal first-run case: no device has been enrolled yet, so there is no
            # stored level. Logged at info, not as a warning, because a warning on every
            # fresh install trains people to ignore warnings.
            _LOGGER.info("no stored volume yet; starting at the quietest level (%d%%)", level)
            record = None
        except Exception as exc:  # noqa: BLE001 - a config problem must not stop the boot
            _LOGGER.warning("could not read the stored volume (%s); using the quietest", exc)
            record = None
        if record is not None:
            stored = (record.get("desired") or {}).get("playback_volume")
            if isinstance(stored, int) and not isinstance(stored, bool):
                level = clamp_user(stored)
            elif stored is not None:
                _LOGGER.warning("ignoring an unusable stored playback_volume %r", stored)
        target.volume = level
        return level

    def _adopt_device(self, device_id: str) -> None:
        """Follow the device we are actually serving, by the id it registered with.

        Called on registration and on lease. Without this the loop addresses a device that
        does not exist, which breaks configured volume in a way that looks like a network
        problem rather than a naming one.
        """
        device_id = str(device_id or "").strip()
        if not device_id or device_id == self.device_id:
            return
        previous = self.device_id or "(none)"
        self.device_id = device_id
        level = self._load_volume(self)
        _LOGGER.info("now addressing device %s (was %s); volume %d%%", device_id, previous, level)

    async def _set_volume(self, level: int) -> bool:
        """Record a level durably and tell the device. True only if both happened.

        Order matters. The desired state is written first, so a device that is briefly
        unreachable still converges when it reconnects, and the tracked value only moves
        once the write is recorded. If the device cannot be told, the caller says so
        rather than claiming a change nobody applied.

        The level is on the USER scale, so it is clamped on that scale. Clamping it with
        the raw clamp() would push every quiet level up to the raw floor and make the
        bottom of the user's scale unreachable - the bug this comment exists to prevent.
        """
        from satellite_gateway.volume import clamp_user

        level = clamp_user(level)
        try:
            self.config_plane.set_desired(
                self.device_id, {"playback_volume": level}, expected_generation=None
            )
        except Exception as exc:  # noqa: BLE001 - a failed record must surface, not be swallowed
            _LOGGER.error("could not record playback_volume %d: %s", level, exc)
            return False

        self.volume = level
        try:
            delivered = await self._dispatch_device_config(
                self.device_id, self.config_plane.get_device(self.device_id)
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("could not deliver playback_volume %d to the device: %s", level, exc)
            delivered = False
        if not delivered:
            _LOGGER.warning(
                "playback_volume %d is recorded but the device has not been told; it will "
                "converge on reconnect", level,
            )
        return delivered

    def _volume_for_device(self, device_id: Optional[str]) -> int:
        """The level recorded for ONE device, for the alarm override.

        Falls back to the loop's own level when no device is named, so a single-satellite
        deployment answers exactly as before. A device with no record yet answers with the loop's
        level rather than raising: an alarm must not be blocked by a missing config record.
        """
        if not device_id:
            return int(self.volume)
        try:
            record = self.config_plane.get_device(device_id)
        except Exception:  # noqa: BLE001 - a missing record must not stop an alarm
            return int(self.volume)
        stored = (record.get("desired") or {}).get("playback_volume")
        if isinstance(stored, int) and not isinstance(stored, bool):
            return stored
        return int(self.volume)

    async def _set_volume_for_device(self, level: int, device_id: Optional[str]) -> bool:
        """Record and deliver a level for ONE device (the alarm override), fleet-safe.

        Same order as `_set_volume`: record first so an offline device converges on reconnect, then
        tell it. Only the loop's own tracked level moves when the device is the one the loop is
        addressing; another device's level is a config-plane fact, not the loop's.
        """
        from satellite_gateway.volume import clamp_user

        if not device_id:
            return await self._set_volume(level)

        level = clamp_user(level)
        try:
            self.config_plane.set_desired(
                device_id, {"playback_volume": level}, expected_generation=None
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("could not record playback_volume %d for %s: %s", level, device_id, exc)
            return False

        if device_id == self.device_id:
            self.volume = level
        try:
            delivered = await self._dispatch_device_config(
                device_id, self.config_plane.get_device(device_id)
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("could not deliver playback_volume %d to %s: %s", level, device_id, exc)
            delivered = False
        if not delivered:
            _LOGGER.warning("playback_volume %d for %s is recorded but not delivered; it will "
                            "converge on reconnect", level, device_id)
        return delivered

    async def _volume_command(self, transcript: str) -> str:
        """A spoken volume request, answered without a model call.

        Returns the line to speak, or "" when this is not a volume request. Set and step
        both change the device; a query never does.
        """
        from satellite_gateway.volume import parse_volume_command, resolve

        intent = parse_volume_command(transcript)
        if intent is None:
            return ""
        level, speech = resolve(intent, self.volume)
        if intent.kind == "query":
            return speech
        delivered = await self._set_volume(level)
        if not delivered:
            # Do not speak a success that did not happen. The level is recorded, so the
            # sentence says what is true: asked for, not yet applied.
            return f"{speech} I could not reach the device yet."
        return speech

    def _maybe_rotate_on_idle(self) -> bool:
        if self._last_turn_at is None or self.idle_reset_minutes <= 0:
            return False
        idle_minutes = (time.monotonic() - self._last_turn_at) / 60.0
        if idle_minutes >= self.idle_reset_minutes:
            self.rotate_session(f"idle {idle_minutes:.0f} min")
            return True
        return False

    # -- STT ----------------------------------------------------------------

    def _model(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel
            _LOGGER.info("loading faster-whisper %s (int8, %d threads)",
                         self.stt_model, self.stt_threads)
            self._whisper = WhisperModel(self.stt_model, device="cpu",
                                         compute_type="int8",
                                         cpu_threads=self.stt_threads)
        return self._whisper

    def _maybe_dump_utterance(self, audio: bytes) -> None:
        """Write the utterance to a WAV when VOICE_DUMP_UTTERANCES is set.

        A transcript that is missing its first word is indistinguishable from a clip that never
        contained it, and the two have completely different fixes -- one is a capture/onset problem,
        the other is the speech recogniser. Guessing between them costs a round trip with the person
        holding still each time, so when this is on the evidence is kept on disk instead.
        """
        import os
        import wave

        if not os.environ.get("VOICE_DUMP_UTTERANCES"):
            return
        try:
            from satellite_gateway.config import AUDIO_OUT

            AUDIO_OUT.mkdir(parents=True, exist_ok=True)
            path = AUDIO_OUT / f"utterance-{int(time.time() * 1000)}.wav"
            with wave.open(str(path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(16000)
                out.writeframes(audio)
            _LOGGER.info("dumped utterance: %s (%.2fs)", path, len(audio) / 32000)
        except Exception as exc:  # noqa: BLE001 - a debug dump must never break a turn
            _LOGGER.warning("could not dump the utterance: %s", exc)

    async def transcribe(self, audio: bytes) -> str:
        """Local transcription, off the event loop so the satellite stays serviced."""
        self._maybe_dump_utterance(audio)
        model = self._model()

        def _run() -> str:
            samples = np.frombuffer(audio, dtype="<i2").astype("f4") / 32768.0
            segments, _info = model.transcribe(samples, language="en", beam_size=1,
                                               vad_filter=False)
            return " ".join(seg.text.strip() for seg in segments).strip()

        return await asyncio.to_thread(_run)

    # -- event helpers ------------------------------------------------------

    def _replay_buffer(self):
        """Events the watcher saw but does not own, kept for whoever is waiting for them.

        Created lazily rather than in __init__ so this stays a local change. Bounded, because a
        buffer that grows without limit on a long task is its own bug.
        """
        buf = getattr(self, "_replay", None)
        if buf is None:
            from collections import deque

            buf = deque(maxlen=32)
            self._replay = buf
        return buf

    async def _next(self, timeout: float):
        # Draining the buffer first is what lets two tasks share one satellite queue: the
        # interruption watcher and the turn both pull from the same socket, and the watcher used to
        # drop anything it did not recognise, including the `played` event the turn was waiting for
        # (issue #13).
        buf = self._replay_buffer()
        if buf:
            return buf.popleft()
        return await self.sat.next_event(timeout)

    def _ensure_fleet(self) -> dict:
        """Fleet state, created lazily so bare test loops (`VoiceLoop.__new__`) keep working.

        Several tests build a VoiceLoop without __init__ (the config-plane and volume suites),
        so every fleet touchpoint goes through here rather than assuming __init__ ran.
        Idempotent attribute by attribute: a test that pre-seeds some state is respected.
        """
        if not hasattr(self, "leases"):
            self.leases = {}
        if not hasattr(self, "_supervisor_tasks"):
            self._supervisor_tasks = {}
        if not hasattr(self, "turn_owner"):
            self.turn_owner = None
        if not hasattr(self, "_speaking_at_device"):
            self._speaking_at_device = {}
        if not hasattr(self, "wake_router"):
            from satellite_gateway.config import WAKE_CLAIM_WINDOW_S
            from satellite_gateway.wake_router import WakeRouter
            self.wake_router = WakeRouter(suppress_check=self._is_self_trigger,
                                          claim_window_s=WAKE_CLAIM_WINDOW_S)
        return self.leases

    async def wait_for_wake(self, timeout: Optional[float] = None) -> str:
        """Block until *any* armed device reports its wake word. Returns the word name.

        A timeout here is NOT a connection failure. An hour with nobody speaking is the normal
        overnight state, and treating it as fatal is exactly what made the Gateway exit, restart,
        replay its connect tone, reload whisper and Piper, and go deaf for nine seconds, once an
        hour all night (2026-09-23). `run()` catches IdleWakeRefresh and refreshes the
        socket in place instead.

        Fleet shape (issue #111): every lease has its own supervisor reader, and every wake it
        hears is submitted to the WakeRouter FIFO. This method claims one record and hands the
        conversation over to that device; the winner keeps its already-open listen stream
        (decided 2026-10-23), so claiming sends the device nothing at all. With the registry off
        the dialled-satellite queue path is unchanged.
        """
        from satellite_gateway.config import WAKE_WAIT_SECONDS

        # A fresh wake begins a fresh claim: whatever owned the previous turn releases its
        # socket now, so every supervisor resumes reading its device (issue #111). Follow-up
        # chains never pass through here, so the chain's device stays owner throughout.
        self._release_turn_owner()
        leases = self._ensure_fleet()

        # A wake held from an earlier, unusable moment comes before anything on the
        # queue: the person already asked, and they have been waiting since.
        if self._held_wake is not None:
            held, self._held_wake = self._held_wake, None
            held_device = getattr(self, "_held_wake_device", None)
            self._held_wake_device = None
            _LOGGER.info("wake word: %s (held from a detection that arrived mid-task)", held)
            self._release_turn_owner()
            if held_device and held_device in self._ensure_fleet():
                return await self._claim_record(held_device, held)
            return held

        if timeout is None:
            timeout = WAKE_WAIT_SECONDS
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise IdleWakeRefresh(f"no wake word in {timeout:.0f}s")

            # Fleet: the supervisors own the device sockets, so wakes arrive through the router
            # and never through this loop's own read of a lease queue.
            if self.leases:
                record = await self.wake_router.claim(timeout=min(remaining, 5.0))
                if record is None:
                    continue
                if record.device_id not in self.leases:
                    # The lease died between the wake and the claim; the wake cannot be answered.
                    # Retire the tentative claim or the next iteration would re-claim the same
                    # dead record forever.
                    _LOGGER.warning("wake from %s has no live lease; discarding it",
                                    record.device_id)
                    self.wake_router.retire()
                    continue
                return await self._claim_record(record.device_id, record.name)

            # Registry off: the long-standing dialled-satellite queue path, unchanged. The 30s
            # slice is deliberate: SatelliteClient.next_event cancels its queue read at each
            # slice boundary, and a read cancelled after an item was handed to it loses that
            # item, so the dialled path keeps the original long slice.
            event = await self._next(min(remaining, 30.0))
            if event is None:
                if self.sat.eof:
                    raise ConnectionError("satellite connection closed")
                continue
            if Detection.is_type(event.type):
                detection = Detection.from_event(event)
                device_id = getattr(self.sat, "device_id", None) or self.device_id
                if self._is_self_trigger(device_id):
                    continue
                _LOGGER.info("wake word: %s", detection.name)
                return detection.name

    def _is_self_trigger(self, device_id: Optional[str]) -> bool:
        """Whether a wake on THIS device is its own answer echoing back.

        No acoustic echo cancellation, so a satellite's own speaker can trigger its own wake
        word. But the suppression must be TIME-BOUNDED and PER DEVICE (issue #111): _speaking is
        cleared only at the end of a turn, so if it ever latches true the device goes permanently
        deaf -- wakes rejected as self-triggers forever, no turn starts, nothing ever clears the
        flag. That deadlock was observed. And with a fleet, device X's answer must NOT swallow
        device Y's independent wake: suppression keys on the *speaking* device, not on any device
        that happens to hear a sound. The device also independently suppresses its own chime
        before reporting a detection, so this is a second line of defence, not the primary one,
        and it must never be able to suppress indefinitely.
        """
        if not self._speaking:
            return False
        key = device_id or ""
        speaking_at = self._ensure_fleet() and self._speaking_at_device
        # A device is only suspected of hearing its own answer when *that device* (or, on the
        # dialled path with no per-device stamps at all, the loop's global stamp) spoke inside
        # the window. A device absent from a non-empty per-device map simply has not spoken:
        # device X's answer must never suppress device Y's independent wake (issue #111).
        if key in speaking_at:
            last_spoke = speaking_at[key]
        elif not speaking_at:
            last_spoke = self._speaking_at
        else:
            last_spoke = 0.0
        recently_spoke = (time.monotonic() - last_spoke) < SELF_TRIGGER_WINDOW_S
        if recently_spoke:
            self.self_trigger_count += 1
            _LOGGER.warning("wake word on %s within %.0fs of its own playback: treating as a "
                            "self-trigger, ignoring", key or "the satellite", SELF_TRIGGER_WINDOW_S)
            return True
        # The flag is latched but this device's last playback is long over. Recover rather than
        # stay deaf, and say so loudly: a latch means a turn never completed.
        _LOGGER.warning("wake word on %s arrived with _speaking still set %.0fs after the last "
                        "playback (a turn never completed); accepting it and clearing the flag",
                        key or "the satellite", time.monotonic() - self._speaking_at)
        self._speaking = False
        return False

    def _speaking_for_device(self, device_id: Optional[str], when: float) -> None:
        """Record that this device spoke at `when`, for per-device echo suppression."""
        self._ensure_fleet()
        self._speaking_at_device[device_id or ""] = when

    async def _claim_record(self, device_id: str, name: str) -> str:
        """Hand the conversation to the device whose wake was claimed. Its stream stays open.

        Best-audio arbitration (issue #32 rule, decided 2026-10-23): with the claim window
        open, wakes from OTHER devices that arrived for the same utterance are gathered, and
        the winner is the device that heard it *best* — highest `wake_level` in the detection
        context, tie-broken by wake score, tie-broken by arrival order. The winner keeps the
        stream it already opened under the activation word it heard; every unfavourable device
        gets its listen stream cancelled (Task A5) and a silent re-arm. With the window closed
        (Milestone A default) the router gathers nothing, so first arrival wins and nobody is
        cancelled. Detections without a level (old firmware, the rollback shape) compare equal,
        so arbitration degrades to FIFO without crashing — arrival order decides, exactly as
        before levels existed.
        """
        leases = self._ensure_fleet()
        if device_id not in leases:
            # The lease died between the wake and the claim: retire the record and fall back
            # to the loop's connection, the pre-fleet shape.
            self.wake_router.retire()
            return name
        winner, losers = await self.wake_router.arbitrate()
        winner_device = winner.device_id if winner is not None else device_id
        winner_name = winner.name if winner is not None else name
        if winner is not None and winner.device_id != device_id:
            _LOGGER.info("arbitration: %s out-heard %s on the same utterance (%s)",
                         winner_device, device_id, self.wake_router.evidence(winner))
        if losers:
            _LOGGER.info("arbitration: releasing %d unfavourable device(s): %s",
                         len(losers),
                         "; ".join(f"{c.device_id} {self.wake_router.evidence(c)}" for c in losers))
        self._use_satellite(leases[winner_device], leased=True)
        self.turn_owner = winner_device
        # A7 (issue #111): config/volume addressing follows the device actually being
        # talked to, not whichever registered most recently.
        self._adopt_device(winner_device)
        for loser in losers:
            await self._release_losing_stream(loser.device_id, f"out-heard by {winner_device}")
        _LOGGER.info("arbitration: %s claims the turn (listen stream kept open)", winner_device)
        self.wake_router.retire()
        return winner_name

    def _release_turn_owner(self) -> None:
        """End of a turn (or a turn that never happened): nobody owns the socket, so every
        supervisor resumes reading its device."""
        self._ensure_fleet()
        self.turn_owner = None

    async def wait_for_played(self, timeout: float = 20.0) -> bool:
        """Wait for the satellite's `played`, absorbing stray detections meanwhile.

        The bound is short on purpose. `played` is the device saying its speaker
        finished, and firmware that does not emit it would otherwise park the whole
        conversation here -- for the full timeout -- after every single reply."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            # Recheck replay promptly if the watcher receives Played while this
            # consumer is already blocked on the satellite queue.
            event = await self._next(min(remaining, 0.5))
            if event is None:
                if self.sat.eof:
                    return False
                continue
            if Played.is_type(event.type):
                return True
            if Detection.is_type(event.type):
                # A wake here is NOT self-trigger suppression material. This wait
                # is for the device's `played`, which some firmware never sends --
                # so the wait ran to its full timeout while every wake inside it
                # was dismissed as an echo. Measured: one working conversation,
                # then the next two attempts silenced with "treating as a
                # self-trigger". The person is awake and asking; answer them.
                name = Detection.from_event(event).name
                _LOGGER.warning("wake word %r arrived while waiting for playback to "
                                "finish; the reply is done as far as the person is "
                                "concerned -- holding it for the next turn", name)
                self._hold_wake(name)
                return True

    # -- interruption while a task is running (2026-09-23) --------------

    async def _handle_interruption(self, transcript: str) -> str:
        """Decide what a mid-task utterance means, and act on it.

        Returns "ignored", "stopped", "steered", "steer_failed" or "control".

        Called only while a run is in flight, from the interruption watcher. It touches very little
        of this object on purpose, so scripts/test_interrupt.py can drive it with stubs instead of
        a satellite, a gateway and a microphone.
        """
        from satellite_gateway.config import INTERRUPT_ENABLED, INTERRUPT_MIN_CHARS

        if not getattr(self, "interrupt_enabled", INTERRUPT_ENABLED):
            return "ignored"

        text = (transcript or "").strip()
        if not text:
            return "ignored"

        # Order matters here, and the test caught why. A stop is checked FIRST and with no length
        # guard: "stop" is four characters long and is the single most important word in this whole
        # feature. The length guard exists for the steer path only, where our own playback leaking
        # into the mic produces things like "you" or "beep".
        #
        # The control-phrase matcher is reused rather than reimplemented: it sets stop_requested only
        # for a whole-utterance stop, which is what keeps "stop the sprinklers" out of this path.
        self.stop_requested = False
        reply = self._local_command(text)
        if self.stop_requested:
            self.stop_requested = False
            stopped = await self.gateway.stop()
            _LOGGER.info("interruption: stop heard mid-task; gateway stop accepted=%s", stopped)
            # The reply is spoken by the CALLER, after it has set stop_playback. Speaking it here
            # would block on the playback lock held by the very answer being interrupted: up to the
            # 30 second acquire timeout, with the answer still playing throughout, because the flag
            # that stops it had not been set yet (issue #15).
            return "stopped"
        if reply:
            _LOGGER.info("interruption: control phrase mid-task, answering locally")
            await self.say(reply)
            return "control"

        if len(text) < INTERRUPT_MIN_CHARS:
            _LOGGER.info("interruption: %r is too short to be speech; ignoring", text)
            return "ignored"

        accepted = await self.gateway.steer(text)
        if accepted:
            _LOGGER.info("interruption: new information steered into the running task")
            return "steered"
        _LOGGER.warning("interruption: run would not accept the text, so it was dropped")
        return "steer_failed"

    def _hold_wake(self, name: str, device_id: Optional[str] = None) -> None:
        """Keep a wake detection that arrived at a moment we could not use it.

        A wake word is the person's only way to be heard. The interruption watcher
        owns the satellite queue while a task runs, so a wake it cannot act on used
        to be consumed and discarded: the tone sounded in the room and then nothing
        at all happened. Hold it instead, and wait_for_wake hands it to the next
        turn. Only one is kept -- a later wake replaces it, which is the more
        recent request. It is pinned to the device that heard it (issue #111), so
        the next turn runs on the speaker the person actually walked up to."""
        self._held_wake = name
        self._held_wake_device = device_id or getattr(self.sat, "device_id", None) or self.device_id
        _LOGGER.info("wake word %r arrived mid-task and could not be used; "
                     "keeping it for the next turn", name)

    async def _watch_for_interruption(self, state: "InterruptState") -> None:
        """Watch the satellite for a wake word while a task is running, and interrupt it.

        A wake-word satellite only streams audio after a detection, so a detection arriving here
        means someone deliberately woke the device mid-task. What follows decides the outcome.
        """
        from satellite_gateway.config import INTERRUPT_CAPTURE_SECONDS

        while not state.done:
            # Replay belongs to the turn, never to the watcher that populated it.
            event = await self.sat.next_event(0.5)
            if event is None:
                if self.sat.eof:
                    return
                continue
            if not Detection.is_type(event.type):
                # Hand it back rather than dropping it. While a task is running the turn is usually
                # waiting for `played` from this same queue, and discarding it here made the answer
                # look as though playback had failed, for the full 180 second timeout (issue #13).
                # Wake detections are deliberately NOT buffered: the watcher owns those.
                if Played.is_type(event.type):
                    self._replay_buffer().append(event)
                continue
            name = Detection.from_event(event).name
            _LOGGER.info("interruption: wake word %r while a task is running", name)
            if state.done:
                return
            # Bounded, and this bound is not optional. `capture_utterance` waits for audio with no
            # deadline of its own, and a satellite that is still PLAYING cannot stream the
            # microphone, so a wake word that lands during the answer leaves it waiting forever.
            # Worse, the waiting watcher stays the active consumer of the satellite queue and
            # silently eats every later wake detection, which makes the device look completely deaf
            # (hit exactly this on 2026-09-23, minutes after this feature shipped).
            # CancelledError is deliberately NOT caught here: swallowing it is how a task that should
            # have died keeps running.
            try:
                result = await asyncio.wait_for(self.capture_utterance(name),
                                                timeout=INTERRUPT_CAPTURE_SECONDS)
            except asyncio.TimeoutError:
                _LOGGER.info("interruption: no speech after the wake (%.0fs); "
                             "carrying on with the running task", INTERRUPT_CAPTURE_SECONDS)
                await self._end_capture()
                self._hold_wake(name)
                continue
            if state.done:
                self._hold_wake(name)
                return
            if result is None:
                await self._end_capture()
                self._hold_wake(name)
                continue
            transcript = await self.transcribe(result.audio)
            if not transcript:
                _LOGGER.info("interruption: nothing intelligible after the wake; ignoring")
                await self._end_capture()
                self._hold_wake(name)
                continue
            _LOGGER.info("interruption heard: %s", transcript)
            outcome = await self._handle_interruption(transcript)
            # The capture is over whichever way it went, so hand the microphone back. Leaving the
            # satellite streaming is issue #12: a streaming satellite ignores its own wake word, so
            # the device looks deaf until the next hourly link refresh. Hit exactly that on
            # 2026-09-23 by interrupting during an answer.
            await self._end_capture()
            if outcome == "stopped":
                # Set the flag FIRST, then speak. The answer's stream holds the playback lock until
                # it notices this flag, so a reply spoken before it waits out the whole 30 second
                # acquire timeout while the audio it is meant to interrupt keeps playing (issue #15).
                state.stop_playback = True
                await self.say("Stopped.")
                return

    async def _end_capture(self) -> None:
        """Hand the microphone back to wake-word listening after a capture we did not finish.

        A satellite that has been woken is streaming, and a streaming satellite ignores its own wake
        word. The normal turn closes the microphone by sending an empty transcript, which the protocol
        treats as the end of the utterance; the interruption path bypasses that flow entirely, so it
        has to do it here. `run-satellite` goes too, because a satellite mid-stream may otherwise
        ignore the first message. Best effort: failing to tidy up must never break the running task.
        """
        try:
            await self.sat.send(Transcript(text="").event())
            await self.sat.run()
        except Exception as exc:  # noqa: BLE001 - never let tidying up break the turn
            _LOGGER.warning("could not re-arm the satellite after an interruption: %s", exc)

    async def capture_utterance(self, wake_word: str) -> Optional[EndpointResult]:
        # Counts the microphone frames that actually arrive. "The device is
        # streaming but the Gateway hears nothing" is only diagnosable when the two
        # ends are counted separately; the device prints the matching `capacct`.
        audio_frames_seen = 0
        """Feed mic audio to the endpointer until the utterance is complete.

        Raises the announcement gate on entry. While the microphone is open the room is, by
        definition, in the middle of being listened to, and an announcement spoken now would be
        captured and transcribed as though it were the person's own words. The flag is raised only
        if it is not already set, so an interruption during a running task keeps counting from the
        turn's own start rather than resetting the stale-age safety net (issue #19).
        """
        if getattr(self, "turn_started_at", None) is None:
            self.turn_started_at = time.monotonic()
        # Bind the endpointer for the whole utterance. Resolving `self._ep` per chunk instead would
        # let a profile change arriving mid-sentence swap the object underneath us: the new one
        # starts empty, so the audio already buffered in the old one is dropped and the utterance
        # restarts. Taking a local reference means a change lands on the next capture, which is
        # what docs/endpointing-profiles.md promises.
        endpoint = self._ep
        endpoint.begin_capture()
        announce = getattr(self, "_on_listening", None)
        if announce:
            announce()
        while True:
            event = await self._next(30.0)
            if event is None:
                if self.sat.eof:
                    raise ConnectionError("satellite connection closed")
                continue
            if AudioChunk.is_type(event.type):
                chunk = AudioChunk.from_event(event)
                audio_frames_seen += 1
                if audio_frames_seen % 50 == 0:
                    # Runs every ~0.5 s of real audio. If these stop while the
                    # device reports `capacct: sent …`, the loss is after the wire;
                    # if the device reports drops, it never left the board.
                    _LOGGER.info("capture: %d microphone frames received from %s",
                                 audio_frames_seen, getattr(self.sat, "device_id", "satellite"))
                result = endpoint.process(chunk.audio)
                if result is not None:
                    return result
            elif Detection.is_type(event.type):
                _LOGGER.debug("extra wake detection while capturing; ignoring")

    # -- the turn -----------------------------------------------------------

    async def publish_led(self, event_type: str) -> None:
        """Tell the satellite's LED service which state to show for this turn.

        Best effort on purpose: a missing indicator must never cost a turn.
        """
        try:
            await self.sat.send(Event(type=event_type))
        except Exception as exc:  # noqa: BLE001 - an indicator is never worth an error
            _LOGGER.debug("could not publish %s: %s", event_type, exc)

    async def say(self, text: str) -> None:
        """Speak a short phrase (an error or an announcement) through the satellite."""
        try:
            async with self.sat.tts_stream() as stream:
                await stream.say(text)
            await self.wait_for_played(timeout=30.0)
        except Exception as exc:  # noqa: BLE001 - never let talking kill the loop
            _LOGGER.warning("could not speak %r: %s", text, exc)

    async def capture_after_wake(self, wake_word: str) -> tuple[Optional[EndpointResult], str]:
        """Capture the utterance, ignoring anything that is not one.

        A rejected blip must NOT end the activation. The satellite's wake chime leaks into the
        mic as a few dozen milliseconds of "speech" at the very start of the stream, and a person
        waits for the tone to finish before speaking, so the 700 ms silence timer used to close
        the window about a second after the wake: they said the wake word, the device
        chimed, went quiet, and they had to start over. The noise is discarded and the mic stays
        open, bounded by CAPTURE_ACTIVATION_SECONDS so a wake with nobody there cannot hold it.

        Returns (endpoint, note). A None endpoint means the activation ended with no utterance,
        and the note says why.
        """
        # The awake chime reaches the microphone as ordinary audio, and its echo is long enough for
        # the endpointer to accept: measured 2026-09-24, a 0.3s chime transcribed as "beep" and
        # started a turn. Ignore the opening moment and drop what is already queued, the way the
        # follow-up window does. The device-side mute across the tone is the real fix; this is the
        # Gateway's own share of it (issue #47).
        from satellite_gateway.config import WAKE_NO_SPEECH_SECONDS, WAKE_QUIET_SECONDS

        await asyncio.sleep(WAKE_QUIET_SECONDS)
        # Bounded by the chime's own length, NOT "everything queued": the queue can hold seconds of
        # the person's speech behind the chime, and draining all of it loses the first word.
        dropped = await self._discard_queued_events(WAKE_QUIET_SECONDS)
        if dropped:
            _LOGGER.info("wake: discarded %d event(s) of our own chime before listening", dropped)
        # Two clocks, deliberately. `no_speech_deadline` is how long the person has to produce real
        # speech, and a blip must not move it: measured 2026-09-24, this room's fan produces a blip
        # every few seconds, each one ended the endpointer's own no-speech window and restarted it,
        # and activations ran the whole CAPTURE_ACTIVATION_SECONDS cap instead of closing after a
        # silent 8 seconds. `deadline` stays as the outer wall-clock bound on everything else.
        # `capture_follow_up` already bounds itself this way, with a deadline a blip cannot extend.
        # A new activation starts with a clean no-speech budget. It is cleared HERE, once per
        # activation, rather than on every capture attempt: an attempt that ends as a blip must not
        # buy the room more time, and an activation that ended with a full budget must not close the
        # next one on its first frame (issue #47).
        self._ep.begin_activation()
        no_speech_deadline = time.monotonic() + WAKE_NO_SPEECH_SECONDS
        deadline = time.monotonic() + CAPTURE_ACTIVATION_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, (f"only blips in {CAPTURE_ACTIVATION_SECONDS:.0f}s "
                              f"after the wake word")
            try:
                endpoint = await asyncio.wait_for(self.capture_utterance(wake_word),
                                                  timeout=remaining)
            except asyncio.TimeoutError:
                # The cap is a wall-clock bound, not a count of blips. capture_utterance waits up to
                # 30s for events of its own and only returns when the endpointer finishes, so
                # without this a wake that produced an unending stream of noise held the microphone
                # far past the advertised window, and the deadline below was never reached because
                # the loop never came round again (issue #36).
                return None, (f"nothing said in {CAPTURE_ACTIVATION_SECONDS:.0f}s "
                              f"after the wake word")
            if endpoint is None:
                return None, "capture returned nothing"
            if endpoint.ended_by == "no_speech":
                # Silence for the whole window is an empty room, not a blip: close the
                # activation rather than listening again. (The endpointing line above
                # already reports how many bytes arrived; that count is the one to
                # compare against the device's `capacct`.)
                return None, "nothing said after the wake word"
            if not endpoint.rejected:
                return endpoint, ""
            _LOGGER.info("ignoring a %0.2fs blip and still listening", endpoint.speech_seconds)
            if time.monotonic() >= no_speech_deadline:
                # Nothing but noise since the wake. Close rather than sit here for the full cap: the
                # person either spoke or they did not, and holding the microphone open on a fan is
                # what made every empty activation run 30 seconds.
                return None, (f"no words in {WAKE_NO_SPEECH_SECONDS:.0f}s after the wake word, "
                              f"only blips")
            if time.monotonic() >= deadline:
                return None, (f"only blips in {CAPTURE_ACTIVATION_SECONDS:.0f}s "
                              f"after the wake word")

    async def _reinforce_wake_now(self) -> None:
        """Ask a second time for the microphone, for a window that did not get one."""
        from satellite_gateway.config import FOLLOW_UP_WAKE_RETRY_SECONDS

        try:
            await asyncio.sleep(FOLLOW_UP_WAKE_RETRY_SECONDS)
            await self.publish_led(WAKE_NOW)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a missed retry must never cost the window
            _LOGGER.debug("could not repeat the wake request: %s", exc)

    async def _discard_queued_events(self, window_seconds: Optional[float] = None) -> int:
        """Throw away events already waiting for us, without waiting for any of them.

        Used after the follow-up quiet interval and after a wake. A satellite that is already
        streaming keeps sending audio, so the device's own chime arrives as ordinary chunks.
        Sleeping through the interval only *delays* reading them, and the endpointer then treats its
        own chime as speech: it can close the window on a word nobody said (issue #40).

        `window_seconds` bounds what is discarded BY AUDIO DURATION. Without it, everything queued
        is thrown away -- which is what the wake path used to do, and it is not safe there: the queue
        holds however much the device sent while the Gateway was sleeping, and that can be far more
        than the chime. Measured 2026-10-30 on the ESP32 satellite (which has no device-side mic
        mute to hide behind): `discarded 93 event(s)`, and "new conversation" arrived as
        "conversation" -- the first word was queued behind the chime and dumped with it. The phrase
        matcher needs the whole phrase, so the reset never fired and the model answered instead.

        With a window, only the chime's worth of audio is dropped and everything still waiting is
        put back at the FRONT of the queue, so the person's first word survives to the endpointer.
        """
        dropped = 0
        if window_seconds is None:
            buf = self._replay_buffer()
            while buf:
                buf.popleft()
                dropped += 1
            while True:
                event = await self._next(0)
                if event is None:
                    break
                dropped += 1
            return dropped

        discarded_seconds = 0.0
        while discarded_seconds < window_seconds:
            event = await self._next(0)
            if event is None:
                break
            dropped += 1
            if AudioChunk.is_type(event.type):
                chunk = AudioChunk.from_event(event)
                bytes_per_second = max(1, chunk.rate * chunk.width * chunk.channels)
                discarded_seconds += len(chunk.audio) / bytes_per_second

        # Anything still queued is the person, not the chime: hand it back to the front of the
        # queue in its original order so the capture sees it.
        kept = []
        while True:
            event = await self._next(0)
            if event is None:
                break
            kept.append(event)
        buf = self._replay_buffer()
        for event in reversed(kept):
            buf.appendleft(event)
        return dropped

    async def capture_follow_up(self) -> tuple[Optional[EndpointResult], str]:
        """Listen for a reply with no wake word, bounded by FOLLOW_UP_SECONDS.

        Follow-up mode. The device has just answered, so the person is already talking to
        it and should not have to wake it again to continue. One utterance and this hands over to
        the normal turn; silence and it closes.

        The active-turn flag goes up for the whole window. An announcement arriving here would be
        picked up by the open mic and answered by the device, which is a feedback loop rather than
        a feature.
        """
        from satellite_gateway.config import FOLLOW_UP_LED, FOLLOW_UP_SECONDS

        self.turn_started_at = time.monotonic()
        # Ask for the microphone first. Without this the window is silent by construction: the
        # satellite only streams after a wake detection, and this window deliberately has none.
        await self.publish_led(WAKE_NOW)
        # Ask again shortly, in case the satellite did not act on it. Free when the first request
        # worked: an already-streaming satellite ignores the request before any chime or light.
        retry = asyncio.create_task(self._reinforce_wake_now(), name="wake-now-retry")
        # Show that it is listening: without this the answer ends, the lights go dark and the
        # device looks switched off, which is exactly how it read on 2026-09-22 ("after you
        # give an answer you're just shutting off") while the window was open behind the darkness.
        await self.publish_led(FOLLOW_UP_LED)
        # Stay deaf while the device's own wake chime and its room echo are in the room, or the window
        # answers its own boop (2026-09-22). See FOLLOW_UP_QUIET_SECONDS. The window's own
        # timing starts after this, so the person still gets the full listening time.
        from satellite_gateway.config import FOLLOW_UP_QUIET_SECONDS

        await asyncio.sleep(FOLLOW_UP_QUIET_SECONDS)
        # The chime has finished, but its samples are still queued. Sleep only delayed reading them,
        # and the endpointer counted them as speech, so the window could close on a word nobody said
        # (issue #40). Discard them now, then listen to what is actually the room.
        stale = await self._discard_queued_events()
        if stale:
            _LOGGER.info("follow-up: discarded %d stale event(s) left by our own playback", stale)
        # This window is its own activation: clean budget, however the previous one ended.
        self._ep.begin_activation()
        deadline = time.monotonic() + FOLLOW_UP_SECONDS
        handover = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, f"nothing said in {FOLLOW_UP_SECONDS:.0f}s of follow-up"
                try:
                    endpoint = await asyncio.wait_for(self.capture_utterance("(follow-up)"),
                                                      timeout=remaining)
                except asyncio.TimeoutError:
                    return None, f"nothing said in {FOLLOW_UP_SECONDS:.0f}s of follow-up"
                if endpoint is None:
                    return None, "capture returned nothing"
                if endpoint.ended_by == "no_speech":
                    return None, "nothing said during follow-up"
                if endpoint.rejected:
                    # A blip is noise, not a reply: the same rule as after a wake word. It must not
                    # close the window, or the device swallows the turn the person was about to
                    # take. The window's deadline still bounds it.
                    _LOGGER.info("ignoring a %0.2fs blip during follow-up and still listening",
                                 endpoint.speech_seconds)
                    continue
                handover = True
                return endpoint, ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken window must not end the loop
            _LOGGER.warning("follow-up capture failed: %s", exc)
            return None, f"follow-up capture failed: {exc}"
        finally:
            retry.cancel()
            # The lights go dark either way; the turn publishes its own state a moment later, so a
            # hand-over is only briefly dark.
            await self.publish_led(LED_IDLE)
            # The announcement gate is only released when the window closes with nothing to answer.
            # On a hand-over the turn is about to start and re-arms it, and clearing it here would
            # briefly reopen the gate while that turn is beginning.
            if not handover:
                self.turn_started_at = None
                # Put the satellite back to wake-word detection. A streaming satellite is not
                # listening for its wake word and its lights stay yellow, so without this a single
                # silent follow-up window leaves the device apparently stuck: it looked exactly that
                # on 2026-09-22.
                await self._rearm_wake_word()

    async def follow_up_chain(self, already: int = 0, max_turns: Optional[int] = None) -> int:
        """Keep listening after an answer for as long as follow-up mode is on.

        Each answer opens a short window, so a conversation continues without the wake word.
        Bounded by FOLLOW_UP_MAX_TURNS, because a chain that never ends is a bug rather than a
        conversation, and it stops the moment a spoken command turns the mode off.
        """
        from satellite_gateway.config import FOLLOW_UP_MAX_TURNS, FOLLOW_UP_SETTLE_SECONDS

        extra = 0
        while self.follow_up and extra < FOLLOW_UP_MAX_TURNS:
            if self.stop_requested:
                # A spoken stop ends the conversation and nothing else: follow-up mode is left as it
                # was, so the next time she talks to it, it follows up again.
                self.stop_requested = False
                _LOGGER.info("stop requested: ending the follow-up conversation")
                await self._rearm_wake_word()
                break
            if max_turns is not None and already + extra >= max_turns:
                break
            # Let the speaker and the room go quiet: the answer has only just finished playing and
            # its tail must not be captured as the next question.
            await asyncio.sleep(FOLLOW_UP_SETTLE_SECONDS)
            if not self.follow_up:
                break
            _LOGGER.info("follow-up mode: listening without the wake word")
            stats = await self.one_turn(follow_up=True)
            extra += 1
            if stats is None or not stats.transcript:
                break
        return extra

    async def one_turn(self, follow_up: bool = False) -> Optional[TurnStats]:
        """Run a single wake -> answer cycle. Returns None if nothing worth answering.

        With `follow_up`, the utterance comes from the follow-up window instead of a wake word:
        the device has just spoken, so someone continuing the conversation should not have to wake
        it again.
        """
        started = time.perf_counter()
        stats = TurnStats(session_id=self.gateway.session_id)

        if follow_up:
            stats.wake_word = "follow-up"
            listen_started = time.perf_counter()
            endpoint, note = await self.capture_follow_up()
            stats.listen_seconds = time.perf_counter() - listen_started
        else:
            wake_word = await self.wait_for_wake()
            stats.idle_seconds = time.perf_counter() - started
            stats.wake_word = wake_word
            listen_started = time.perf_counter()
            endpoint, note = await self.capture_after_wake(wake_word)
            stats.listen_seconds = time.perf_counter() - listen_started
        if endpoint is None:
            if note:
                stats.note = note
                _LOGGER.info("%s", note)
                self.turns.append(stats)
            # A wake with nothing said, or a plain false wake, still leaves the satellite streaming,
            # and a streaming satellite ignores its own wake word: the device goes deaf silently
            # until the link refreshes. Re-arm it, and use the silent re-arm rather than the empty
            # transcript because nobody spoke and the device should stay quiet (issue #12).
            await self._rearm_wake_word()
            # Nobody spoke, so this window is not a conversation: release the gate that
            # capture_utterance raised, or announcements would wait out ANNOUNCE_TURN_MAX_AGE on a
            # false wake (issue #19).
            self.turn_started_at = None
            return stats
        # Everything from here on is the part that answers the question.
        processing_started = time.perf_counter()
        # Stop the mic first: the protocol's transcript event ends the utterance and puts
        # the satellite straight back into wake-word listening.
        await self.sat.send(Transcript(text="").event())

        t_stt = time.perf_counter()
        transcript = await self.transcribe(endpoint.audio)
        stats.stt_seconds = time.perf_counter() - t_stt
        stats.transcript = transcript
        if not transcript:
            stats.note = "empty transcript"
            await self.say("I didn't catch that.")
            self.turn_started_at = None
            self.turns.append(stats)
            return stats
        _LOGGER.info("heard (%0.1fs speech, stt %0.2fs): %s", endpoint.speech_seconds,
                     stats.stt_seconds, transcript)

        # The last line of defence against the device answering its own chime (issue #47). The drain in
        # `capture_after_wake` removes the queued chime audio and the endpointer's blip filter rejects
        # the rest, but on 2026-09-24 0.3 s got past both, transcribed as "beep", and a full turn went
        # to the gateway and was spoken into the room. Dropping it here means the chime can no longer
        # become words even when the earlier two layers miss.
        #
        # Silent on purpose: the incident's harm was that it replied out loud. A whole-utterance match,
        # so "beep the horn" is still a question.
        from satellite_gateway.config import NON_WORD_TRANSCRIPTS

        if normalise(transcript) in {normalise(w) for w in NON_WORD_TRANSCRIPTS}:
            stats.note = "non-word transcript"
            _LOGGER.info("%r is a non-word, not speech; staying silent", transcript)
            # Nobody is being answered, so release the gate capture_utterance raised, or an
            # announcement arriving now waits out ANNOUNCE_TURN_MAX_AGE on the device's own chime.
            self.turn_started_at = None
            self.turns.append(stats)
            return stats

        # Bridge-local spoken controls first: instant, and they work even when the gateway is
        # unreachable. Deliberately before the session reset, so a control never reaches the model.
        local_reply = self._local_command(transcript)
        if local_reply:
            stats.note = "local control"
            _LOGGER.info("local control: %s", local_reply)
            await self.say(local_reply)
            self.turn_started_at = None
            self.turns.append(stats)
            return stats

        # Volume next, and separately, because unlike the controls above it talks to the device
        # before it can answer. Still before the session reset, so the request never reaches the
        # model: a volume command the assistant rephrases into a question is a worse experience
        # than one answered in the room instantly.
        volume_reply = await self._volume_command(transcript)
        if volume_reply:
            stats.note = "volume control"
            _LOGGER.info("volume control: %s", volume_reply)
            await self.say(volume_reply)
            self.turn_started_at = None
            self.turns.append(stats)
            return stats

        # On-demand session reset, before anything is sent to the gateway.
        if any(p in normalise(transcript) for p in NEW_CONVERSATION_PHRASES):
            self.rotate_session("spoken new-conversation")
            stats.note = "session rotated on request"
            await self.say("Starting a new conversation.")
            self.turn_started_at = None
            self.turns.append(stats)
            return stats

        self._maybe_rotate_on_idle()

        # From here until the answer has finished playing, this turn is active, and an
        # announcement arriving now would talk over the conversation, so it waits (see
        # Announcer). Deliberately placed after every early return above, so a rejected blip or
        # an empty transcript can never leave the flag set for the full maximum age.
        self.turn_started_at = time.monotonic()
        # The lights stay violet from here until the answer actually starts playing, so the
        # filler and a slow tool call both read as "still working" rather than as "finished".
        await self.publish_led(LED_BUSY)

        t_gateway = time.perf_counter()
        self._speaking = True
        self._speaking_at = time.monotonic()
        # Per-device echo suppression (issue #111): stamp the device that is about to speak,
        # not "whichever device exists". Device X's answer must only suppress device X's wake.
        self._speaking_for_device(getattr(self.sat, "device_id", None)
                                  or getattr(self, "device_id", ""),
                                  self._speaking_at)
        spoken_any = False

        async def fill_silence(_event=None) -> None:
            """Silence after "Hey Hermes" reads as failure; say something.

            Covers BOTH of the ways a turn goes quiet before it starts: a tool call,
            and a model that simply takes a while to produce its first token. The
            reference satellite fills a slow tool call the same way; a slow first
            token is the same experience from the room and deserves the same answer.

            Only before the first real sentence, and only once: if the model has
            already begun answering, a lead-in would talk over it.
            """
            nonlocal spoken_any
            if not spoken_any and stats.first_delta_seconds == 0.0:
                _LOGGER.info("nothing spoken yet after %.0fs: filling the silence",
                             SILENCE_FILLER_SECONDS)
                await stream.say("One moment.")
                spoken_any = True

        # The clock-based half of that: a turn that is merely slow to start used to
        # get nothing, because nothing fired without a tool call.
        async def _fill_if_still_silent() -> None:
            await asyncio.sleep(SILENCE_FILLER_SECONDS)
            await fill_silence()

        filler_guard = asyncio.create_task(_fill_if_still_silent(),
                                           name="silence-filler")

        # Watch for an interruption for as long as the task is running. The satellite keeps
        # listening for its wake word throughout, so a detection arriving here means someone
        # deliberately woke the device mid-task (2026-09-23).
        class _Interrupt:
            """Mutable state shared with the interruption watcher."""

            def __init__(self) -> None:
                self.done = False
                self.stop_playback = False

        state = _Interrupt()
        watcher = asyncio.create_task(self._watch_for_interruption(state),
                                      name="interruption-watcher")
        try:
            async with self.sat.tts_stream() as stream:
                _LOGGER.info("turn: audio-start sent; awaiting first reply text")
                buffer = SentenceBuffer()
                async for delta in self.gateway.ask(transcript,
                                                    on_tool_started=fill_silence):
                    if stats.first_delta_seconds == 0.0:
                        stats.first_delta_seconds = time.perf_counter() - t_gateway
                        filler_guard.cancel()   # the model is answering: let it
                    if state.stop_playback:
                        break
                    for piece in buffer.push(delta):
                        stats.reply += piece + " "
                        if stats.first_audio_seconds == 0.0:
                            stats.first_audio_seconds = time.perf_counter() - t_gateway
                            # The answer is starting: this, not an AudioStart event, is the
                            # moment the working pulse should stop.
                            await self.publish_led(LED_IDLE)
                        await stream.say(piece)
                if not state.stop_playback:
                    tail = buffer.flush()
                    if tail:
                        stats.reply += tail
                        if stats.first_audio_seconds == 0.0:
                            stats.first_audio_seconds = time.perf_counter() - t_gateway
                            await self.publish_led(LED_IDLE)
                        await stream.say(tail)
                stats.audio_seconds = stream.seconds_sent
            if state.stop_playback:
                # Closing the speech stream is what cuts the satellite's playback buffer. The run
                # itself was already cancelled server-side by the stop call.
                stats.note = "interrupted: stopped mid-task"
                _LOGGER.info("task stopped on request; abandoning the rest of the answer")
            else:
                await self.wait_for_played()
        except VoiceGatewayError as exc:
            _LOGGER.error("turn: VoiceGatewayError while producing the reply: %s", exc)
            if state.stop_playback or "cancel" in str(exc).lower():
                # We asked for this. Do not apologise for a stop the person requested.
                stats.note = "interrupted: run cancelled"
                _LOGGER.info("run cancelled at the person's request")
            else:
                stats.note = f"gateway error: {exc}"
                _LOGGER.error("gateway failed: %s", exc)
                await self.say("I can't reach my brain right now.")
        except Exception:
            _LOGGER.exception("turn: reply failed (device=%s generation=%s)",
                              getattr(self.sat, "device_id", "direct"),
                              getattr(self.sat, "generation", None))
            raise
        finally:
            filler_guard.cancel()
            state.done = True
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
            # A wake detection left in the queue would be read by the next wait_for_wake as a fresh
            # wake and start a phantom turn. Drain here, where we know any detection was ours.
            drained = 0
            while True:
                leftover = await self._next(0.05)
                if leftover is None:
                    break
                if Detection.is_type(leftover.type):
                    drained += 1
            if drained:
                _LOGGER.info("drained %d leftover wake detection(s) after the turn", drained)
            self._speaking = False
            # Safety net for the paths that never speak (gateway error, empty reply): a turn
            # must not leave the working pulse running until its cap.
            await self.publish_led(LED_IDLE)

        stats.total_seconds = time.perf_counter() - started
        stats.processing_seconds = time.perf_counter() - processing_started
        self._last_turn_at = time.monotonic()
        self.turn_started_at = None     # the turn is over: announcements may speak again
        # Stamp the state file so the idle window is measured from the real last turn even if this
        # process exits before the next one starts (see rotate_if_stale).
        self._save_state()
        self.turns.append(stats)
        _LOGGER.info("turn: process %.2fs (idle %.1fs, listen %.1fs, stt %.2fs, first delta %.2fs, "
                     "first audio %.2fs; whole %.1fs), %d chars spoken over %.1fs of audio",
                     stats.processing_seconds, stats.idle_seconds, stats.listen_seconds,
                     stats.stt_seconds, stats.first_delta_seconds, stats.first_audio_seconds,
                     stats.total_seconds, len(stats.reply.strip()), stats.audio_seconds)
        _LOGGER.info("  heard: %s", stats.transcript)
        _LOGGER.info("  said : %s", stats.reply.strip())
        return stats

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        # A conversation carried over from a previous process is still subject to the idle window:
        # without this, a Gateway that was down for hours resumes an old session untouched.
        self.rotate_if_stale()

        # Connect before warming the models. With keepalive surfacing a dead satellite in about
        # 30 s, systemd will restart this process repeatedly while the Pi is off; if the models
        # were warmed first, every one of those retries would reload whisper and Piper, which is
        # seconds of CPU each time for nothing.
        # The gateway client's aiohttp session must be opened explicitly; nothing else does
        # it, and every call (including health) fails without it.
        await self.gateway.open()

        # Listeners come up BEFORE the satellite (phase 4 of #61). Announcements and the registry must be
        # available whether or not any device is reachable, because a device that is away can only be
        # reached by dialling IN. Both used to sit behind the connect below, which is why the whole
        # Gateway went down with the satellite and nothing was listening on 8765 or 10701 while it was
        # away: a device could not have registered even if it tried.
        from satellite_gateway.config import (DIRECT_DIAL_FALLBACK, REGISTRY_ALLOW_UNAUTHENTICATED,
                                   REGISTRY_CREDENTIALS_FILE, REGISTRY_DUPLICATE_POLICY, REGISTRY_ENABLED,
                                   REGISTRY_HOST, REGISTRY_LEGACY_PLAINTEXT, REGISTRY_PORT,
                                   REGISTRY_TLS_CERT_FILE, REGISTRY_TLS_CLIENT_CA_FILE,
                                   REGISTRY_TLS_KEY_FILE, REGISTRY_TLS_PORT)

        self.registry = None
        self.registry_server = None
        self.legacy_registry_server = None
        if REGISTRY_ENABLED:
            try:
                from satellite_gateway.registry import (CredentialConfigError, RegistryServer, SatelliteRegistry,
                                             TlsConfigError, load_credential_lookup,
                                             load_server_tls_context)

                # A credential file that is present but unusable stops the listener rather than silently
                # downgrading it to believing whatever a device claims (review finding, issue #62).
                lookup = load_credential_lookup(
                    REGISTRY_CREDENTIALS_FILE, allow_unauthenticated=REGISTRY_ALLOW_UNAUTHENTICATED
                )
                if lookup is None:
                    _LOGGER.warning(
                        "registry listener is running WITHOUT authentication because "
                        "VOICE_REGISTRY_ALLOW_UNAUTHENTICATED is set and there is no credential file at "
                        "%s: any peer that can reach it may claim any device identity. Trusted networks "
                        "only.", REGISTRY_CREDENTIALS_FILE,
                    )
                # TLS whenever a certificate is deployed, a plain local socket when none is. A
                # half-configured pair raises here rather than falling back to plaintext: the one
                # outcome that must be impossible is certificates installed and traffic in the clear.
                ssl_context = load_server_tls_context(
                    REGISTRY_TLS_CERT_FILE, REGISTRY_TLS_KEY_FILE, REGISTRY_TLS_CLIENT_CA_FILE
                )
                self.registry = SatelliteRegistry(duplicate_policy=REGISTRY_DUPLICATE_POLICY)

                if ssl_context is not None:
                    # Every device that holds a credential dials here, and the certificate is the identity.
                    self.registry_server = RegistryServer(
                        self.registry, REGISTRY_HOST, REGISTRY_TLS_PORT,
                        credential_lookup=lookup, ssl_context=ssl_context,
                        on_registered=self._on_device_registered,
                        on_control_event=self._on_device_control_event,
                    )
                    await self.registry_server.start()
                    _LOGGER.info(
                        "registry listening on %s:%s with TLS (peer certificates required; registered "
                        "devices are leaseable and receive desired state)",
                        REGISTRY_HOST, REGISTRY_TLS_PORT,
                    )
                else:
                    _LOGGER.info(
                        "registry listener has no certificate at %s: plain local socket, for a network "
                        "the household controls", REGISTRY_TLS_CERT_FILE,
                    )

                # The plain listener runs when there is no certificate at all, which is the local case, and
                # also when a device that cannot present one yet still has to register. Over plain TCP
                # there is nothing to authenticate, so it believes self-claimed identities and warns.
                self.legacy_registry_server = None
                plain_also_runs = ssl_context is None or REGISTRY_LEGACY_PLAINTEXT
                if plain_also_runs:
                    legacy_lookup = lookup if ssl_context is None else None
                    self.legacy_registry_server = RegistryServer(
                        self.registry, REGISTRY_HOST, REGISTRY_PORT,
                        credential_lookup=legacy_lookup,
                        on_registered=self._on_device_registered,
                        on_control_event=self._on_device_control_event,
                    )
                    await self.legacy_registry_server.start()
                    if legacy_lookup is None and ssl_context is not None:
                        _LOGGER.warning(
                            "registry is ALSO listening on %s:%s in plain text for devices that have not "
                            "migrated: any peer that can reach that port may claim any device identity. "
                            "Turn VOICE_REGISTRY_LEGACY_PLAINTEXT off once every device speaks TLS.",
                            REGISTRY_HOST, REGISTRY_PORT,
                        )
                    else:
                        _LOGGER.info(
                            "registry listening on %s:%s without TLS", REGISTRY_HOST, REGISTRY_PORT
                        )
            except (CredentialConfigError, TlsConfigError) as exc:
                _LOGGER.error("registry listener NOT started: %s", exc)
                self.registry_server = None
                if not DIRECT_DIAL_FALLBACK:
                    raise
            except Exception as exc:  # noqa: BLE001 - direct fallback may preserve the voice path
                _LOGGER.error("could not start the registry listener: %s", exc)
                self.registry_server = None
                if not DIRECT_DIAL_FALLBACK:
                    # Without a listener or the explicitly enabled direct fallback, acquisition could only
                    # sleep forever. Exit so systemd can retry after the interface/listener becomes ready.
                    raise

        # Announcements bind their HTTP endpoint here and hold this loop's connection by reference; they
        # do not need the satellite to be connected, so they no longer wait for it.
        await self.announcer.start()

        # Now obtain something to talk to. With the registry off this is exactly as it always was: dial the
        # satellite, and let a failure exit for systemd to restart. With it on, a device that dialled in is
        # preferred and a failure to acquire is waited out rather than fatal, because a device that is away
        # can only reach us by dialling in and the listeners have to outlive its absence.
        if REGISTRY_ENABLED:
            await self._acquire_a_satellite()
        else:
            await self.sat.connect()
        # Claim the conversation socket while the handshake and first arm run, so the fleet
        # supervisors pause on it and cannot steal the describe reply into their parked
        # buffers (issue #111: exactly one consumer per socket at a time).
        self.turn_owner = getattr(self.sat, "device_id", None)
        info = await self.sat.describe()
        if info is not None:
            name = getattr(getattr(info, "satellite", None), "name", "?")
            _LOGGER.info("satellite: %s", name)
        health = await self._wait_for_gateway_ready()
        _LOGGER.info("gateway health: %s", health)
        # Tell the satellite we are ready; it arms its wake word and stops being inert.
        await self.sat.run()
        self.turn_owner = None

        # Pre-warm the slow parts, off the event loop, so the first real turn is not noticeably
        # slower than the rest (Whisper model load is seconds, Piper's ~1 s).
        from satellite_gateway.tts import load_voice

        await asyncio.to_thread(self._model)
        await asyncio.to_thread(load_voice)
        _LOGGER.info("stt and tts models warm")

        # The satellite cannot tell anyone it is connected, so the host does it.
        await self.say_startup_tone()

    async def _wait_for_gateway_ready(self) -> dict:
        """Keep the Gateway alive while the Gateway API finishes an ordinary boot.

        systemd's ``After=`` orders process starts; it does not mean the API listener is ready. The registry
        and announcement listeners are already up when this runs, so exiting here would tear down the very
        endpoint the satellite needs in order to recover. Do not arm the microphone until health succeeds.
        """
        delay = 1.0
        while True:
            try:
                return await self.gateway.health()
            except VoiceGatewayError as exc:
                _LOGGER.warning("gateway is not ready yet (%s); retrying in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, 10.0)

    def _use_satellite(self, satellite, *, leased: bool) -> None:
        """Point the loop, and everything that speaks on its connection, at this satellite.

        The announcer holds the conversation's connection by reference (see satellite_gateway/announce.py), so swapping
        the satellite under the loop has to reach it too, or announcements keep talking into a socket that a
        lease has replaced.
        """
        self.sat = satellite
        self.leased = satellite if leased else None
        announcer = getattr(self, "announcer", None)
        if announcer is not None:
            announcer.use_connection(satellite)

    def _first_ready_device(self):
        """The first device that has finished registering, if any."""
        if self.registry is None:
            return None
        ready = self.registry.connections(ready_only=True)
        if not ready:
            return None
        return ready[0]

    # -- fleet leases (issue #111) ------------------------------------------------
    #
    # Every registered device gets a lease, an arm and a supervisor reader. The old shape
    # leased exactly one device and left the rest registered-but-deaf: the firmware only
    # scores wake words under RUN_REQUESTED, and an unleased device is never sent
    # run-satellite. Measured 2026-10-23: one device held the lease since Oct 21 while the
    # other re-registered 97 times and never armed.

    def _ensure_lease(self, connection):
        """Create (or generation-replace) the lease for one registered connection.

        Only real `SatelliteConnection`s get leases: the config-plane tests drive the
        registration hook with fakes, and a supervisor must never try to supervise one.
        Generation-safe the same way the registry is: a stale connection object must never
        replace its own successor's lease.
        """
        from satellite_gateway.registry import SatelliteConnection

        leases = getattr(self, "leases", None)
        if leases is None or not isinstance(connection, SatelliteConnection):
            # Bare test loops (VoiceLoop.__new__) and fake registry slots have no fleet to
            # supervise; the registration hook still does its config-plane work.
            return None
        existing = leases.get(connection.device_id)
        if existing is not None:
            if existing.generation == connection.generation:
                return existing
            _LOGGER.info(
                "device %s re-registered (generation %s -> %s): replacing its lease",
                connection.device_id, existing.generation, connection.generation,
            )
            if existing is not self.sat:
                asyncio.ensure_future(existing.close())
            self.leases.pop(connection.device_id, None)
        from satellite_gateway.lease import LeasedSatellite

        lease = LeasedSatellite(connection, log_events=self._log_events)
        leases[connection.device_id] = lease
        task = self._supervisor_tasks.get(connection.device_id)
        if task is None or task.done():
            self._supervisor_tasks[connection.device_id] = asyncio.create_task(
                self._supervise_device(connection.device_id),
                name=f"lease-supervisor-{connection.device_id}",
            )
        return lease

    def _drop_lease(self, device_id: str, why: str) -> None:
        """Forget a lease whose link died. The registry's slot cleanup owns the socket.

        Cancels the supervisor too: a poller must never outlive its lease, or a generation
        replacement would end up with two pollers on one device (issue #111's two-readers
        failure class). The supervisor re-arms itself when `_ensure_lease` creates the new one.
        """
        lease = self.leases.pop(device_id, None)
        if lease is None:
            return
        _LOGGER.info("dropping lease on %s (%s)", device_id, why)
        lease._cancel_pending()
        task = self._supervisor_tasks.get(device_id)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if device_id == self.turn_owner:
            self.turn_owner = None

    async def _arm_all(self) -> None:
        """Send `run-satellite` to every lease: registration arms a device, but the
        wake gates (RUN_REQUESTED) only stay armed while the host keeps saying it is
        ready, so every arm point here is what keeps a device genuinely wake-responsive.
        """
        for device_id, lease in list(self.leases.items()):
            if lease.connection.closed:
                self._drop_lease(device_id, "link closed")
                continue
            try:
                await lease.run()
            except Exception as exc:  # noqa: BLE001 - one deaf device must not stop the others
                _LOGGER.warning("could not arm %s: %s", device_id, exc)

    async def _supervise_device(self, device_id: str) -> None:
        """One reader per leased socket, for the whole life of the lease.

        The rule that keeps the historic two-readers-on-one-socket failure class (issues
        #13/#46) shut: this coroutine only ever calls `get_nowait`, and only while the lease
        does NOT own the conversation. A live blocking read (`_pending_get`) belongs to the
        turn, and an arriving event is handed to a waiting getter before it can land in the
        queue where a poll would see it, so the two consumers cannot take the same event or
        cross order.

        What the supervisor does with each event:
          * detection  -> the WakeRouter (queued FIFO; per-device self-trigger suppression)
          * audio-start/stop -> track the lease's streaming state
          * everything else  -> the lease's bounded parked buffer, drained first by whoever
            claims the lease next (this is what keeps the winner's post-wake chunks alive)
        A supervisor that dies is logged with the device id, never silent (inversion
        review P2).
        """
        slice_s = 0.05
        logged_streaming = False
        try:
            while True:
                lease = self.leases.get(device_id)
                if lease is None:
                    return
                if lease.eof:
                    self._drop_lease(device_id, "link closed")
                    return
                if device_id == self.turn_owner:
                    # The turn machinery owns this socket right now. Do not touch it.
                    await asyncio.sleep(slice_s)
                    continue
                event = lease.poll_event()
                if event is None:
                    await asyncio.sleep(slice_s)
                    continue
                if Detection.is_type(event.type):
                    detection = Detection.from_event(event)
                    self.wake_router.submit(device_id, detection.name, detection)
                    continue
                if event.type == "audio-start":
                    lease.streaming = True
                    if not logged_streaming:
                        _LOGGER.info(
                            "%s is streaming while another device owns the conversation; "
                            "parking until arbitration releases it", device_id)
                        logged_streaming = True
                elif event.type == "audio-stop":
                    lease.streaming = False
                    logged_streaming = False
                lease.parked.append(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead supervisor must be loud, not silent
            _LOGGER.error("lease supervisor for %s died: %s", device_id, exc)
            self._drop_lease(device_id, "supervisor died")
        finally:
            # Pop only our own slot: a generation replacement may already have installed its
            # successor's supervisor, and popping blindly would orphan it.
            if self._supervisor_tasks.get(device_id) is asyncio.current_task():
                self._supervisor_tasks.pop(device_id, None)

    async def _release_losing_stream(self, device_id: str, reason: str) -> None:
        """Cancel the listen stream on an unfavourable device and return it to listening.

        `pause-satellite` closes the mic stream on both platforms (the ESP32 session's stop-
        streaming path; the Pi satellite's RunPause), then a *silent* run-satellite re-arms
        its wake word with no done tone -- the documented invariant for the losing-arbitration
        path. The device is wake-responsive again within one round trip.
        """
        lease = self.leases.get(device_id)
        if lease is None:
            return
        _LOGGER.info("arbitration: releasing %s (%s)", device_id, reason)
        lease.parked.clear()
        lease.streaming = False
        try:
            await lease.pause()
            await lease.run()
        except Exception as exc:  # noqa: BLE001 - the device will re-register if the link is gone
            _LOGGER.warning("could not release %s cleanly: %s", device_id, exc)
            self._drop_lease(device_id, "release failed")

    async def _acquire_a_satellite(self) -> None:
        """Hold at least one satellite: leases arrive via registration; the dial is a fallback.

        Fleet shape (issue #111): registration already leased and armed whatever dialled in
        (`_on_device_registered`). This method's remaining job is to keep the loop alive until
        at least one lease exists, and to honour the direct-dial migration/rollback option
        after its grace period. When leases already exist (a re-acquire after a partial loss),
        it arms the whole fleet rather than waiting.
        """
        from satellite_gateway.config import (DIRECT_DIAL_FALLBACK, REGISTRY_LEASE_WAIT_SECONDS,
                                   REGISTRY_WAIT_RETRY_SECONDS, SATELLITE_HOST, SATELLITE_PORT)

        waited = 0.0
        while True:
            if self._ensure_fleet():
                await self._arm_all()
                self._use_satellite(next(iter(self.leases.values())), leased=True)
                return

            if DIRECT_DIAL_FALLBACK and waited >= REGISTRY_LEASE_WAIT_SECONDS:
                waited = 0.0
                try:
                    await self.sat.connect()
                except (OSError, ConnectionError) as exc:
                    _LOGGER.debug(
                        "no device has dialled in, and fallback %s:%s did not answer either: %s",
                        SATELLITE_HOST, SATELLITE_PORT, exc,
                    )
                else:
                    _LOGGER.info(
                        "dialled fallback satellite at %s:%s after the lease grace expired",
                        SATELLITE_HOST, SATELLITE_PORT,
                    )
                    return

            if DIRECT_DIAL_FALLBACK:
                _LOGGER.info(
                    "no registered satellite yet; waiting for a lease before fallback dial to %s:%s",
                    SATELLITE_HOST, SATELLITE_PORT,
                )
            else:
                _LOGGER.info("no registered satellite yet; direct-dial fallback is disabled")
            await asyncio.sleep(REGISTRY_WAIT_RETRY_SECONDS)
            waited += REGISTRY_WAIT_RETRY_SECONDS

    async def _reacquire_after_loss(self, exc: Exception) -> None:
        """Replace a dead satellite without leaving the process.

        In registry mode a lost link is not a reason to exit. A device that dialled in owns its socket and
        returns when it reconnects, and exiting would take the listeners down with it, which is the defect
        that made this whole shape necessary. Nothing here reloads Whisper or Piper: only the link is replaced.

        Fleet shape (issue #111): a lost lease is dropped individually; the other leases keep their arms
        and supervisors untouched. Only when there is no fleet at all does the dialled fallback path run.
        """
        from satellite_gateway.config import SATELLITE_HOST, SATELLITE_PORT
        from satellite_gateway.satellite import SatelliteClient

        lost_device = getattr(self.sat, "device_id", None)
        if self._ensure_fleet():
            _LOGGER.warning("lost the lease on %s (%s); the rest of the fleet stays armed",
                            lost_device or "a device that dialled in", exc)
            if lost_device:
                self._drop_lease(lost_device, "link loss")
            try:
                await self.sat.close()
            except Exception:  # noqa: BLE001 - a dead link is already dead
                pass
            await self._acquire_a_satellite()
            _LOGGER.info(
                "re-acquired a device that dialled in; listening again (session %s), %d lease(s) armed",
                self.gateway.session_id, len(self.leases),
            )
            return

        _LOGGER.warning("lost the dialled satellite (%s); re-acquiring rather than exiting", exc)
        try:
            await self.sat.close()
        except Exception:  # noqa: BLE001 - a dead link is already dead
            pass
        self._use_satellite(
            SatelliteClient(SATELLITE_HOST, SATELLITE_PORT, log_events=self._log_events), leased=False
        )
        await self._acquire_a_satellite()
        # Re-arm: the device only reports wake words once the host has said it is ready.
        await self.sat.run()
        _LOGGER.info(
            "re-acquired %s; listening again (session %s)",
            "a device that dialled in" if self.leased else "the dialled satellite",
            self.gateway.session_id,
        )

    def _connected_device_ids(self):
        if self.registry is None:
            return ()
        return tuple(self.registry.connected_devices())

    async def _dispatch_device_config(self, device_id: str, record: dict) -> bool:
        """Send one durable desired-state generation over a live registered connection."""
        if self.registry is None:
            return False
        slot = self.registry.get(device_id)
        connection = getattr(slot, "connection", None) if slot is not None else None
        if connection is None:
            return False
        from satellite_gateway.registry import CONFIG_DESIRED_EVENT

        return await connection.send(Event(type=CONFIG_DESIRED_EVENT, data={
            "schema_version": 1,
            "device_id": device_id,
            "generation": record["generation"],
            "settings": record["desired"],
        }))

    async def _on_device_control_event(self, device_id: str, event: Event) -> bool:
        """Persist a device's reported configuration without putting it in the audio queue."""
        from satellite_gateway.registry import CONFIG_REPORTED_EVENT

        if event.type != CONFIG_REPORTED_EVENT:
            return False
        data = dict(event.data or {})
        # The device states the rate its own audio path runs at. Cache it on the connection so
        # out_format() can send that rate with no conversion on a device that cannot afford one,
        # and no change at all for a device that did not report.
        try:
            rate = (data.get("actual") or {}).get("snd_rate")
            if isinstance(rate, int) and rate > 0:
                for sat in self.leases.values():
                    if getattr(sat, "device_id", None) == device_id:
                        sat.snd_rate = rate
        except Exception:  # noqa: BLE001 - a malformed report must not break the config path
            pass
        self.config_plane.report(
            device_id,
            generation=data.get("generation"),
            actual=data.get("actual"),
            health=data.get("health"),
            error=data.get("error"),
        )
        return True

    async def _on_device_registered(self, slot) -> None:
        """A device completed the handshake: lease it, arm it, and supervise its socket.

        Fleet rule (issue #111): registration is not just "available to be leased later" --
        every registered device is leased immediately, armed with run-satellite so its wake
        gates open, and given one supervisor reader for the life of the connection. The lease
        hook used to only log what arrived while a single lease elsewhere stayed pinned.
        """
        identity = getattr(slot, "identity", None)
        # Address this device from now on, UNLESS a conversation is in progress on another
        # device (issue #111, A7): volume and desired-state addressing follow the device the
        # person is actually talking to, not whichever registered most recently. The config
        # dispatch below still uses slot.device_id directly, so the new device still gets its
        # desired state immediately regardless of who is being addressed.
        self._ensure_fleet()
        if self.turn_owner is None or self.turn_owner == slot.device_id:
            self._adopt_device(slot.device_id)
        else:
            _LOGGER.info("device %s registered during a conversation on %s; staying addressed to %s",
                         slot.device_id, self.turn_owner, self.turn_owner)
        _LOGGER.info(
            "device registered: id=%s name=%s area=%s version=%s protocol=%s capabilities=%s",
            slot.device_id,
            getattr(identity, "name", "") or "-",
            getattr(identity, "area", "") or "-",
            getattr(identity, "firmware_version", "") or "-",
            getattr(identity, "protocol_version", "?"),
            ",".join(getattr(identity, "capabilities", ()) or ()) or "-",
        )
        metadata = identity.as_dict() if identity is not None else {}
        record = self.config_plane.ensure_device(slot.device_id, metadata=metadata)
        if not await self._dispatch_device_config(slot.device_id, record):
            _LOGGER.warning("device %s registered but desired state could not be dispatched", slot.device_id)
        lease = self._ensure_lease(getattr(slot, "connection", None))
        if lease is not None:
            try:
                await lease.run()
                _LOGGER.info("device %s leased and armed (generation %s); wake word live",
                             lease.device_id, lease.generation)
            except Exception as exc:  # noqa: BLE001 - a failed arm must not lose the registration
                _LOGGER.warning("device %s registered but could not be armed: %s", slot.device_id, exc)

    async def say_startup_tone(self) -> None:
        """Play the "connected" tone, so the device can report the host link out loud.

        The satellite has no power light and no connection light, and the person in the room asked
        to be able to tell "powered on" from "actually connected". Best effort on purpose: a missing or
        unplayable tone must never stop the loop from starting.

        Once per boot, not once per connection. 2026-09-23: the hourly link refresh used to
        restart this process, so the tone played all night long. A restart of this service, or a
        reconnection, is now silent; a real reboot of this host still says hello.
        """
        from satellite_gateway.config import CONNECTED_TONE, CONNECTED_TONE_BOOT_MARKER

        boot = _boot_id()
        if boot and CONNECTED_TONE_BOOT_MARKER.is_file():
            try:
                if CONNECTED_TONE_BOOT_MARKER.read_text().strip() == boot:
                    _LOGGER.info("connected tone already played this boot; staying silent")
                    return
            except OSError as exc:
                _LOGGER.warning("could not read %s: %s", CONNECTED_TONE_BOOT_MARKER, exc)

        if not CONNECTED_TONE.is_file():
            _LOGGER.warning("connected tone missing at %s", CONNECTED_TONE)
            return
        try:
            await self.sat.play_wav(CONNECTED_TONE)
        except Exception as exc:  # noqa: BLE001 - never let a chime block startup
            _LOGGER.warning("could not play the connected tone: %s", exc)
            return
        if boot:
            try:
                CONNECTED_TONE_BOOT_MARKER.parent.mkdir(parents=True, exist_ok=True)
                CONNECTED_TONE_BOOT_MARKER.write_text(boot + "\n")
            except OSError as exc:
                _LOGGER.warning("could not record the boot id: %s", exc)

    async def _refresh_link(self) -> None:
        """Refresh satellite links in place after an idle wait, without exiting.

        The satellite goes inert on a new connection until it is told the host is ready, so `run()`
        must be re-sent or the device would sit there awake but deaf. That re-send is the whole
        difference between a working refresh and a silent dead device.

        Fleet shape (issue #111): the hourly refresh touches every lease, not just whichever
        device happened to hold the single old lease. It stays silent: the once-per-boot marker
        for the connected tone is untouched here, and re-arming is a `run-satellite`, not a chime.
        """
        _LOGGER.info("refreshing satellite links after an idle wait")
        if self._ensure_fleet():
            await self._arm_all()
            _LOGGER.info("satellite links refreshed; wake word re-armed on %d device(s)",
                         len(self.leases))
            return
        await self.sat.reconnect()
        await self.sat.run()
        _LOGGER.info("satellite link refreshed; wake word re-armed")

    async def run(self, max_turns: Optional[int] = None) -> None:
        await self.start()
        _LOGGER.info("listening (session %s)", self.gateway.session_id)
        turns = 0
        while max_turns is None or turns < max_turns:
            try:
                await self.one_turn()
                turns += 1
                # Follow-up mode: an answer can open a listening window of its own.
                turns += await self.follow_up_chain(already=turns, max_turns=max_turns)
            except asyncio.CancelledError:
                raise
            except IdleWakeRefresh as exc:
                # Not a fault: nobody spoke for the configured window. Refresh in place instead of
                # exiting. Exiting is what replayed the connect tone, reloaded the models and went
                # deaf for seconds, once an hour overnight (2026-09-23).
                _LOGGER.info("idle: %s", exc)
                try:
                    await self._refresh_link()
                except Exception as refresh_exc:  # noqa: BLE001 - keep waiting, do not exit
                    _LOGGER.error("link refresh failed: %s", refresh_exc)
                    await asyncio.sleep(5.0)
                continue
            except (ConnectionError, TimeoutError) as exc:
                _LOGGER.error("connection problem: %s", exc)
                from satellite_gateway.config import REGISTRY_ENABLED
                if not REGISTRY_ENABLED:
                    # The long-standing shape: exit and let systemd start a fresh process, with the connect
                    # tone and the model reload that go with it. Unchanged for the dialled path.
                    raise
                # A device that dialled in owns its socket, so a lost link is not ours to re-establish:
                # wait for it to come back, keeping the listeners and the warm models.
                await self._reacquire_after_loss(exc)
                continue
            except Exception as exc:  # noqa: BLE001 - one bad turn must not end the loop
                _LOGGER.exception("turn failed: %s", exc)
                await asyncio.sleep(1.0)
        _LOGGER.info("stopping after %d turn(s)", turns)

    async def close(self) -> None:
        # Supervisors first: they poll the leases, so they must stop before the sockets do,
        # or they log spurious "supervisor died" errors on the way out (issue #111).
        self._ensure_fleet()
        for task in list(self._supervisor_tasks.values()):
            task.cancel()
        if self._supervisor_tasks:
            await asyncio.gather(*self._supervisor_tasks.values(), return_exceptions=True)
        self._supervisor_tasks.clear()
        self.leases.clear()
        for label, server in (("registry", getattr(self, "registry_server", None)),
                              ("plain registry", getattr(self, "legacy_registry_server", None))):
            if server is None:
                continue
            try:
                await server.stop()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                _LOGGER.warning("could not stop the %s listener: %s", label, exc)
        await self.announcer.stop()
        await self.gateway.close()
        await self.sat.close()


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Hermes voice loop")
    parser.add_argument("--turns", type=int, default=None, help="stop after N turns")
    # 20 rather than the old 60: the voice session's context only ever grows, and the point of the
    # idle rotation is to stop carrying a long history into a fresh conversation. Short enough to
    # matter, long enough that a conversation with a gap in it keeps its thread.
    parser.add_argument("--idle-reset-minutes", type=float, default=20.0)
    parser.add_argument("--silence-ms", type=int, default=700)
    parser.add_argument("--log-events", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    loop = VoiceLoop(idle_reset_minutes=args.idle_reset_minutes,
                     endpointer={"silence_ms": args.silence_ms},
                     log_events=args.log_events)
    try:
        await loop.run(max_turns=args.turns)
    finally:
        await loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
