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
    connection problems and exits, which is the behaviour this replaces (Angela, 2026-09-23).
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
# The bridge owns "thinking", not the satellite, and that is deliberate. The satellite cannot
# tell its own filler ("One moment.", played when a tool call would otherwise leave a stretch of
# silence) from the real answer: both arrive as AudioStart, so a hook on the satellite ended the
# pulse about a second after it started and the lights stayed dark through the whole research
# step. Publishing the phase from here is exact, and the satellite forwards unknown events
# straight to its LED service, so no extra listener or LAN exposure is needed.
# Ask the Pi to open the microphone (via the wake shim) so a follow-up window has audio at all. A
# wake-word satellite never streams without a wake detection: see the hermes-voice-bridge skill.
WAKE_NOW = "wake-now"

LED_BUSY = "led-busy"
LED_IDLE = "led-idle"

from bridge.config import (CAPTURE_ACTIVATION_SECONDS, GATEWAY_BASE_URL, GATEWAY_ENV_FILE,
                           GATEWAY_API_KEY_VAR, SATELLITE_HOST, SATELLITE_PORT,
                           VOICE_INSTRUCTIONS, VOICE_ROUTE_ALIAS, VOICE_SESSION_ID)
from bridge.endpointing import Endpointer, EndpointResult
from bridge.gateway import VoiceGateway, VoiceGatewayError, read_api_key
from bridge.satellite import SatelliteClient

_LOGGER = logging.getLogger("bridge.loop")

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
        self._ep_kwargs = endpointer or {}
        self._log_events = log_events

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
        self.self_trigger_count = 0
        self.turns: list[TurnStats] = []

        # Announcements must never talk over a conversation. `turn_started_at` marks the active
        # part of a turn (an utterance has been captured, through the answer finishing), and the
        # announcer waits for it to clear, for up to ANNOUNCE_TURN_MAX_AGE so that a turn which
        # dies mid-flight cannot wedge announcements forever.
        self.turn_started_at: Optional[float] = None

        from bridge.announce import Announcer

        # Shares the loop's satellite connection: see bridge/announce.py for why it does not open
        # its own.
        self.announcer = Announcer(
            sat=self.sat,
            turn_started_at=lambda: self.turn_started_at,
            # The settings endpoint publishes the follow-up switch. It reads the attribute rather
            # than a copy, so a change made by voice or by the assistant is visible immediately.
            follow_up_get=lambda: self.follow_up,
            follow_up_set=self.set_follow_up,
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

        The in-process check (`_maybe_rotate_on_idle`) only sees turns this process handled. A bridge
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

    # -- follow-up mode ------------------------------------------------------

    def _load_follow_up(self) -> bool:
        """Read the persisted follow-up flag. Off unless the state file says on."""
        from bridge.config import FOLLOW_UP_STATE_FILE

        try:
            return FOLLOW_UP_STATE_FILE.read_text().strip().lower() in ("on", "1", "true", "yes")
        except FileNotFoundError:
            return False
        except OSError as exc:  # noqa: BLE001
            _LOGGER.warning("could not read %s: %s", FOLLOW_UP_STATE_FILE, exc)
            return False

    def set_follow_up(self, on: bool) -> bool:
        """Turn follow-up mode on or off and persist it. Returns the new state."""
        from bridge.config import FOLLOW_UP_STATE_FILE

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
        from bridge.config import (FOLLOW_UP_OFF_PHRASES, FOLLOW_UP_ON_PHRASES,
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

    async def transcribe(self, audio: bytes) -> str:
        """Local transcription, off the event loop so the satellite stays serviced."""
        model = self._model()

        def _run() -> str:
            samples = np.frombuffer(audio, dtype="<i2").astype("f4") / 32768.0
            segments, _info = model.transcribe(samples, language="en", beam_size=1,
                                               vad_filter=False)
            return " ".join(seg.text.strip() for seg in segments).strip()

        return await asyncio.to_thread(_run)

    # -- event helpers ------------------------------------------------------

    async def _next(self, timeout: float):
        return await self.sat.next_event(timeout)

    async def wait_for_wake(self, timeout: Optional[float] = None) -> str:
        """Block until the satellite reports its wake word. Returns the word name.

        A timeout here is NOT a connection failure. An hour with nobody speaking is the normal
        overnight state, and treating it as fatal is exactly what made the bridge exit, restart,
        replay its connect tone, reload whisper and Piper, and go deaf for nine seconds, once an
        hour all night (Angela, 2026-09-23). `run()` catches IdleWakeRefresh and refreshes the
        socket in place instead.
        """
        from bridge.config import WAKE_WAIT_SECONDS

        if timeout is None:
            timeout = WAKE_WAIT_SECONDS
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise IdleWakeRefresh(f"no wake word in {timeout:.0f}s")
            event = await self._next(min(remaining, 30.0))
            if event is None:
                if self.sat.eof:
                    raise ConnectionError("satellite connection closed")
                continue
            if Detection.is_type(event.type):
                name = Detection.from_event(event).name
                if self._speaking:
                    # No echo cancellation, so our own answer can trigger the wake word.
                    self.self_trigger_count += 1
                    _LOGGER.warning("wake word %r during playback: treating as a "
                                    "self-trigger, ignoring", name)
                    continue
                _LOGGER.info("wake word: %s", name)
                return name

    async def wait_for_played(self, timeout: float = 180.0) -> bool:
        """Wait for the satellite's `played`, absorbing stray detections meanwhile."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            event = await self._next(remaining)
            if event is None:
                if self.sat.eof:
                    return False
                continue
            if Played.is_type(event.type):
                return True
            if Detection.is_type(event.type):
                self.self_trigger_count += 1
                _LOGGER.warning("wake word during playback: treating as a self-trigger")

    # -- interruption while a task is running (Angela, 2026-09-23) ----------

    async def _handle_interruption(self, transcript: str) -> str:
        """Decide what a mid-task utterance means, and act on it.

        Returns "ignored", "stopped", "steered", "steer_failed" or "control".

        Called only while a run is in flight, from the interruption watcher. It touches very little
        of this object on purpose, so scripts/test_interrupt.py can drive it with stubs instead of
        a satellite, a gateway and a microphone.
        """
        from bridge.config import INTERRUPT_ENABLED, INTERRUPT_MIN_CHARS

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
            await self.say("Stopped.")
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

    async def _watch_for_interruption(self, state: "InterruptState") -> None:
        """Watch the satellite for a wake word while a task is running, and interrupt it.

        A wake-word satellite only streams audio after a detection, so a detection arriving here
        means someone deliberately woke the device mid-task. What follows decides the outcome.
        """
        from bridge.config import INTERRUPT_CAPTURE_SECONDS

        while not state.done:
            event = await self._next(0.5)
            if event is None:
                if self.sat.eof:
                    return
                continue
            if not Detection.is_type(event.type):
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
            # (Angela hit exactly this on 2026-09-23, minutes after this feature shipped).
            # CancelledError is deliberately NOT caught here: swallowing it is how a task that should
            # have died keeps running.
            try:
                result = await asyncio.wait_for(self.capture_utterance(name),
                                                timeout=INTERRUPT_CAPTURE_SECONDS)
            except asyncio.TimeoutError:
                _LOGGER.info("interruption: no speech after the wake (%.0fs); "
                             "carrying on with the running task", INTERRUPT_CAPTURE_SECONDS)
                await self._end_capture()
                continue
            if state.done:
                return
            if result is None:
                await self._end_capture()
                continue
            transcript = await self.transcribe(result.audio)
            if not transcript:
                _LOGGER.info("interruption: nothing intelligible after the wake; ignoring")
                await self._end_capture()
                continue
            _LOGGER.info("interruption heard: %s", transcript)
            outcome = await self._handle_interruption(transcript)
            # The capture is over whichever way it went, so hand the microphone back. Leaving the
            # satellite streaming is issue #12: a streaming satellite ignores its own wake word, so
            # the device looks deaf until the next hourly link refresh. Angela hit exactly that on
            # 2026-09-23 by interrupting during an answer.
            await self._end_capture()
            if outcome == "stopped":
                state.stop_playback = True
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
        """Feed mic audio to the endpointer until the utterance is complete."""
        self._ep.reset()
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
                result = self._ep.process(chunk.audio)
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
        mic as a few dozen milliseconds of "speech" at the very start of the stream, and Michael
        waits for the tone to finish before he speaks, so the 700 ms silence timer used to close
        the window in his face about a second after the wake: he said the wake word, the device
        chimed, went quiet, and he had to start over. The noise is discarded and the mic stays
        open, bounded by CAPTURE_ACTIVATION_SECONDS so a wake with nobody there cannot hold it.

        Returns (endpoint, note). A None endpoint means the activation ended with no utterance,
        and the note says why.
        """
        deadline = time.monotonic() + CAPTURE_ACTIVATION_SECONDS
        while True:
            endpoint = await self.capture_utterance(wake_word)
            if endpoint is None:
                return None, "capture returned nothing"
            if endpoint.ended_by == "no_speech":
                # Silence for the whole window is an empty room, not a blip: close the
                # activation rather than listening again.
                return None, "nothing said after the wake word"
            if not endpoint.rejected:
                return endpoint, ""
            _LOGGER.info("ignoring a %0.2fs blip and still listening", endpoint.speech_seconds)
            if time.monotonic() >= deadline:
                return None, (f"only blips in {CAPTURE_ACTIVATION_SECONDS:.0f}s "
                              f"after the wake word")

    async def _reinforce_wake_now(self) -> None:
        """Ask a second time for the microphone, for a window that did not get one."""
        from bridge.config import FOLLOW_UP_WAKE_RETRY_SECONDS

        try:
            await asyncio.sleep(FOLLOW_UP_WAKE_RETRY_SECONDS)
            await self.publish_led(WAKE_NOW)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a missed retry must never cost the window
            _LOGGER.debug("could not repeat the wake request: %s", exc)

    async def capture_follow_up(self) -> tuple[Optional[EndpointResult], str]:
        """Listen for a reply with no wake word, bounded by FOLLOW_UP_SECONDS.

        Angela's follow-up mode. The device has just answered, so the person is already talking to
        it and should not have to wake it again to continue. One utterance and this hands over to
        the normal turn; silence and it closes.

        The active-turn flag goes up for the whole window. An announcement arriving here would be
        picked up by the open mic and answered by the device, which is a feedback loop rather than
        a feature.
        """
        from bridge.config import FOLLOW_UP_LED, FOLLOW_UP_SECONDS

        self.turn_started_at = time.monotonic()
        # Ask for the microphone first. Without this the window is silent by construction: the
        # satellite only streams after a wake detection, and this window deliberately has none.
        await self.publish_led(WAKE_NOW)
        # Ask again shortly, in case the satellite did not act on it. Free when the first request
        # worked: an already-streaming satellite ignores the request before any chime or light.
        retry = asyncio.create_task(self._reinforce_wake_now(), name="wake-now-retry")
        # Show that it is listening: without this the answer ends, the lights go dark and the
        # device looks switched off, which is exactly how Angela read it on 2026-09-22 ("after you
        # give an answer you're just shutting off") while the window was open behind the darkness.
        await self.publish_led(FOLLOW_UP_LED)
        # Stay deaf while the device's own wake chime and its room echo are in the room, or the window
        # answers its own boop (Angela, 2026-09-22). See FOLLOW_UP_QUIET_SECONDS. The window's own
        # timing starts after this, so she still gets the full listening time.
        from bridge.config import FOLLOW_UP_QUIET_SECONDS

        await asyncio.sleep(FOLLOW_UP_QUIET_SECONDS)
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
                # silent follow-up window leaves the device apparently stuck: Angela saw exactly that
                # on 2026-09-22.
                await self._rearm_wake_word()

    async def follow_up_chain(self, already: int = 0, max_turns: Optional[int] = None) -> int:
        """Keep listening after an answer for as long as follow-up mode is on.

        Each answer opens a short window, so a conversation continues without the wake word.
        Bounded by FOLLOW_UP_MAX_TURNS, because a chain that never ends is a bug rather than a
        conversation, and it stops the moment a spoken command turns the mode off.
        """
        from bridge.config import FOLLOW_UP_MAX_TURNS, FOLLOW_UP_SETTLE_SECONDS

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
            self.turns.append(stats)
            return stats
        _LOGGER.info("heard (%0.1fs speech, stt %0.2fs): %s", endpoint.speech_seconds,
                     stats.stt_seconds, transcript)

        # Bridge-local spoken controls first: instant, and they work even when the gateway is
        # unreachable. Deliberately before the session reset, so a control never reaches the model.
        local_reply = self._local_command(transcript)
        if local_reply:
            stats.note = "local control"
            _LOGGER.info("local control: %s", local_reply)
            await self.say(local_reply)
            self.turns.append(stats)
            return stats

        # On-demand session reset, before anything is sent to the gateway.
        if any(p in normalise(transcript) for p in NEW_CONVERSATION_PHRASES):
            self.rotate_session("spoken new-conversation")
            stats.note = "session rotated on request"
            await self.say("Starting a new conversation.")
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
        spoken_any = False

        async def fill_silence(_event) -> None:
            """A tool call costs seconds of silence; say something so it does not feel dead.

            Only before the first real sentence, and only once: if the model has already begun
            answering, a lead-in would talk over it.
            """
            nonlocal spoken_any
            if not spoken_any and stats.first_delta_seconds == 0.0:
                _LOGGER.info("tool call before any speech: filling the silence")
                await stream.say("One moment.")
                spoken_any = True

        # Watch for an interruption for as long as the task is running. The satellite keeps
        # listening for its wake word throughout, so a detection arriving here means someone
        # deliberately woke the device mid-task (Angela, 2026-09-23).
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
                buffer = SentenceBuffer()
                async for delta in self.gateway.ask(transcript,
                                                    on_tool_started=fill_silence):
                    if stats.first_delta_seconds == 0.0:
                        stats.first_delta_seconds = time.perf_counter() - t_gateway
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
            if state.stop_playback or "cancel" in str(exc).lower():
                # We asked for this. Do not apologise for a stop the person requested.
                stats.note = "interrupted: run cancelled"
                _LOGGER.info("run cancelled at the person's request")
            else:
                stats.note = f"gateway error: {exc}"
                _LOGGER.error("gateway failed: %s", exc)
                await self.say("I can't reach my brain right now.")
        finally:
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
        # without this, a bridge that was down for hours resumes an old session untouched.
        self.rotate_if_stale()

        # Connect before warming the models. With keepalive surfacing a dead satellite in about
        # 30 s, systemd will restart this process repeatedly while the Pi is off; if the models
        # were warmed first, every one of those retries would reload whisper and Piper, which is
        # seconds of CPU each time for nothing.
        # The gateway client's aiohttp session must be opened explicitly; nothing else does
        # it, and every call (including health) fails without it.
        await self.gateway.open()
        await self.sat.connect()
        info = await self.sat.describe()
        if info is not None:
            name = getattr(getattr(info, "satellite", None), "name", "?")
            _LOGGER.info("satellite: %s", name)
        _LOGGER.info("gateway health: %s", await self.gateway.health())
        # Tell the satellite we are ready; it arms its wake word and stops being inert.
        await self.sat.run()

        # Pre-warm the slow parts, off the event loop, so the first real turn is not noticeably
        # slower than the rest (Whisper model load is seconds, Piper's ~1 s).
        from bridge.tts import load_voice

        await asyncio.to_thread(self._model)
        await asyncio.to_thread(load_voice)
        _LOGGER.info("stt and tts models warm")

        # Announcements speak on this same connection, serialised with answers by the playback
        # lock, so the two can never talk over each other.
        await self.announcer.start()

        # The satellite cannot tell anyone it is connected, so the host does it.
        await self.say_startup_tone()

    async def say_startup_tone(self) -> None:
        """Play the "connected" tone, so the device can report the host link out loud.

        The satellite has no power light and no connection light, and Angela asked to be able to
        tell "powered on" from "actually connected". Best effort on purpose: a missing or
        unplayable tone must never stop the loop from starting.

        Once per boot, not once per connection. Angela, 2026-09-23: the hourly link refresh used to
        restart this process, so the tone played all night long. A restart of this service, or a
        reconnection, is now silent; a real reboot of this host still says hello.
        """
        from bridge.config import CONNECTED_TONE, CONNECTED_TONE_BOOT_MARKER

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
        """Refresh the satellite socket in place after an idle wait, without exiting.

        The satellite goes inert on a new connection until it is told the host is ready, so `run()`
        must be re-sent or the device would sit there awake but deaf. That re-send is the whole
        difference between a working refresh and a silent dead device.
        """
        _LOGGER.info("refreshing the satellite link after an idle wait")
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
                # deaf for seconds, once an hour overnight (Angela, 2026-09-23).
                _LOGGER.info("idle: %s", exc)
                try:
                    await self._refresh_link()
                except Exception as refresh_exc:  # noqa: BLE001 - keep waiting, do not exit
                    _LOGGER.error("link refresh failed: %s", refresh_exc)
                    await asyncio.sleep(5.0)
                continue
            except (ConnectionError, TimeoutError) as exc:
                _LOGGER.error("connection problem: %s", exc)
                raise
            except Exception as exc:  # noqa: BLE001 - one bad turn must not end the loop
                _LOGGER.exception("turn failed: %s", exc)
                await asyncio.sleep(1.0)
        _LOGGER.info("stopping after %d turn(s)", turns)

    async def close(self) -> None:
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
