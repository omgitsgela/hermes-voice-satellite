"""Bridge configuration for this voice satellite.

The satellite is a Wyoming *server*: we dial out to it. All audio processing
(VAD, STT, TTS) happens here on the bridge host; the Pi only does mic capture,
wake word, and playback.

That direction is being inverted (issue #61): a device may instead dial *us*, and we keep a registry of
who is connected. The listener lives under "the registry listener" below and is off by default, because
the voice loop still dials the satellite.

Installation-specific values (the satellite's address, the gateway's port, the session id, ...) are
never literals here. They come from the environment first and from `bridge.env` (at the project
root, git-ignored, see `bridge.env.example`) second, so a clone on another machine is configured by
copying the example file rather than by editing source. The host's systemd unit points at the same
file, so one file is the whole host-side configuration. Same pattern on the device side:
`pi/systemd/satellite.env`. See issue #45.
"""

import os
from pathlib import Path

# --- local configuration file -------------------------------------------------
# The one place this installation's values live. `KEY=VALUE` lines, `#` comments, optional quotes.
# Absent is fine: every value falls back to its default below. A real environment variable always
# wins over the file, so the file holds the durable values and the environment holds experiments.
LOCAL_ENV_FILE = Path(__file__).resolve().parent.parent / "bridge.env"

_LOCAL_ENV_CACHE: dict | None = None


def _parse_env(text: str) -> dict[str, str]:
    """Read systemd EnvironmentFile assignments, without shell expansion.

    Quotes only have special meaning at the start of a value. Hashes inside values
    are literal; backslash continuation and quoted multiline values are supported.
    """
    values = {}
    lines = iter(text.splitlines(keepends=True))
    for line in lines:
        line = line.lstrip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.isidentifier() or not key.isascii():
            continue
        value = value.lstrip(" \t\r")
        quote = value[0] if value and value[0] in "'\"" else None
        if quote:
            value = value[1:]
        out = []
        i = 0
        while i < len(value):
            char = value[i]
            if quote and char == quote:
                break
            if char == "\n":
                if not quote:
                    break
                out.append(char)
                value += next(lines, "")
            elif char == "\\" and quote != "'" and i + 1 < len(value):
                following = value[i + 1]
                if following == "\n":
                    value += next(lines, "")
                elif quote == '"' and following not in '\\"$`':
                    out.extend((char, following))
                else:
                    out.append(following)
                i += 1
            else:
                out.append(char)
            i += 1
        values[key] = "".join(out) if quote else "".join(out).rstrip(" \t\r")
    return values


def _local_env() -> dict:
    """Read bridge.env once per process; missing/unreadable files use defaults."""
    global _LOCAL_ENV_CACHE
    if _LOCAL_ENV_CACHE is None:
        try:
            _LOCAL_ENV_CACHE = _parse_env(LOCAL_ENV_FILE.read_text())
        except OSError:
            _LOCAL_ENV_CACHE = {}
    return _LOCAL_ENV_CACHE


def _cfg(name: str, default: str = "") -> str:
    """Environment > bridge.env > default, including explicitly empty values."""
    if name in os.environ:
        return os.environ[name]
    return _local_env().get(name, default)


# --- the satellite ---------------------------------------------------------
# Where the bridge dials. This was a hardcoded LAN address, which is why the bridge once spent
# hours dialling a satellite that had gone away and why moving the device broke it silently
# (issue #45). Default is loopback on purpose: a clone with no configuration fails visibly and
# locally instead of quietly hunting a foreign address.
SATELLITE_HOST = _cfg("VOICE_SATELLITE_HOST", "127.0.0.1")
SATELLITE_PORT = int(_cfg("VOICE_SATELLITE_PORT", "10700"))

# --- the registry listener (issue #61) -------------------------------------
# Lets a device dial in and register. OFF by default, because the loop falls back to dialling the
# satellite at its configured address when no device has dialled in.
#
# On is not handshake-only any more. A registered device is leaseable by the voice loop, carries real
# conversations over the leased connection, and receives declarative desired state from the
# configuration plane (bridge/config_plane.py) with its reported actual state flowing back.
#
# What is still missing before this can replace the dial-in shape: listener-side authentication that
# is not the trusted-network override, and device-side cutover. See issue #61.
REGISTRY_ENABLED = _cfg("VOICE_REGISTRY_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
REGISTRY_HOST = _cfg("VOICE_REGISTRY_HOST", "127.0.0.1")
REGISTRY_PORT = int(_cfg("VOICE_REGISTRY_PORT", "10701"))
# reject = refuse a second connection from a device that is already connected; replace = close the old one
# and keep the new. Either way exactly one connection per device is ever writable.
REGISTRY_DUPLICATE_POLICY = _cfg("VOICE_REGISTRY_DUPLICATE_POLICY", "replace")
# fingerprint -> device_id. Absent means the listener believes what a device claims, which is only
# acceptable on a trusted network: never expose this listener to the internet without this file.
REGISTRY_CREDENTIALS_FILE = Path(__file__).resolve().parent.parent / "state" / "device-credentials.json"
# Running the listener with no authentication has to be asked for explicitly. Without this flag, a missing
# or unreadable credential file stops the listener instead of quietly accepting self-claimed identities
# (review finding, issue #62). Trusted networks only.
REGISTRY_ALLOW_UNAUTHENTICATED = _cfg("VOICE_REGISTRY_ALLOW_UNAUTHENTICATED", "").strip().lower() in (
    "1", "true", "yes", "on")
# How often the bridge retries the direct dial to the satellite while it is staying up without one. With
# the registry enabled, a failed connect no longer exits: the listeners have to outlive the absence of
# every device, because a device that is away can only get in by dialling us (issue #61). The dial retry
# exists so a device that comes home is picked up exactly as before.
REGISTRY_WAIT_RETRY_SECONDS = float(_cfg("VOICE_REGISTRY_WAIT_RETRY_SECONDS", "30"))
# How long to wait for a device to dial in before falling back to dialling the configured satellite. Both
# shapes are supported during the migration, and a device that dials in is preferred (it is the newer shape,
# and two clients competing for one device is what the one-client rule exists to prevent). Waiting a moment
# first stops the bridge from grabbing a device that is about to dial us itself.
REGISTRY_LEASE_WAIT_SECONDS = float(_cfg("VOICE_REGISTRY_LEASE_WAIT_SECONDS", "30"))
# The direct dial is a migration/rollback path, not something that should race the device-initiated link.
# Registry deployments therefore wait only for a device by default. An installation that still has an
# old-shape device can opt in explicitly; even then the lease grace above is honoured before the dial.
DIRECT_DIAL_FALLBACK = _cfg("VOICE_DIRECT_DIAL_FALLBACK", "").strip().lower() in (
    "1", "true", "yes", "on")

# Taken from the satellite's own ExecStart flags, so these are its real formats:
#   --mic-command 'arecord -D plughw:CARD=seeed2micvoicec,DEV=0 -r 16000 -c 1 -f S16_LE -t raw'
#   --snd-command 'aplay  -D plughw:CARD=seeed2micvoicec,DEV=0 -r 22050 -c 1 -f S16_LE -t raw'
MIC_FORMAT = (16000, 2, 1)   # rate Hz, width bytes, channels  -> what the mic sends us
SND_FORMAT = (22050, 2, 1)   # what aplay expects from us

# --- local paths -----------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
FLEET_CONFIG_STATE_FILE = PROJECT_DIR / "state" / "fleet-config.json"
VOICES_DIR = PROJECT_DIR / "voices"
# The Piper voice. A pick made for this installation (2026-09-22, also the platform voice), so it
# is a value a different deployment would want to change.
PIPER_VOICE_NAME = _cfg("VOICE_PIPER_VOICE", "en_US-hfc_male-medium")
VOICE_MODEL = VOICES_DIR / f"{PIPER_VOICE_NAME}.onnx"
VOICE_CONFIG = VOICES_DIR / f"{PIPER_VOICE_NAME}.onnx.json"
AUDIO_OUT = PROJECT_DIR / "audio"

# --- the Hermes gateway's API server (loopback only) -----------------------
GATEWAY_BASE_URL = _cfg("VOICE_GATEWAY_URL", "http://127.0.0.1:8642")      # loopback; never exposed to the LAN
GATEWAY_ENV_FILE = Path.home() / ".hermes" / ".env"
GATEWAY_API_KEY_VAR = "API_SERVER_KEY"
# Set to a `gateway.api_server.model_routes` alias in ~/.hermes/config.yaml to pin the voice
# surface to a specific model. Empty = do not pin: the voice surface runs on the gateway's normal
# default (and its fallback chain), same as any other conversation.
#
# Pinned 2026-09-25 to `voice-fast` (deepseek / deepseek-flash), because the
# local qwen38 route was too slow to iterate against. This is a CROSS-PROVIDER route: the profile
# default is llamacpp-qwen38 and this one is DeepSeek, which is the exact condition that once
# required the api_server `extra_body` re-scope patch (see hermes/patches/README.md). That patch is
# no longer carried, so if a routed run starts failing with a provider error, suspect the profile's
# provider-specific `extra_body` leaking onto DeepSeek before suspecting DeepSeek itself.
VOICE_ROUTE_ALIAS = _cfg("VOICE_ROUTE_ALIAS", "voice-fast")
# Stable id so the assistant has continuity across bridge restarts. Carries the device's name, so it
# is a deployment value rather than a literal.
VOICE_SESSION_ID = _cfg("VOICE_SESSION_ID", "voice-satellite")

# How long a wake keeps the mic open while the bridge waits for a real utterance. A blip (the
# satellite's wake chime leaking into the mic, a cough, a lip smack) no longer ends the
# activation, so this is the only bound on an activation where nobody actually speaks. Chosen
# 2026-09-22 after it was reported that the activation was closing before a person could speak.
CAPTURE_ACTIVATION_SECONDS = 30.0

# How long the person gets to produce real speech after a wake before the activation closes with
# nothing. A *blip* must not extend this. Measured on the device, 2026-09-24: the room's fan produces
# a short speech-like blip every few seconds, each blip ended the endpointer's own no-speech window
# and restarted it, and activations therefore ran the full CAPTURE_ACTIVATION_SECONDS cap instead of
# closing after a silent 8 seconds. Blips are noise; only real speech counts against this clock.
# Same idea as the endpointer's own no-speech window, but held at the activation level where a blip's
# restart cannot reach it. `capture_follow_up` already works this way, with a wall-clock deadline a
# blip cannot move; this is the wake path getting the same treatment.
WAKE_NO_SPEECH_SECONDS = 8.0
# How much of the opening of a wake activation to ignore, so the device's own awake chime
# and its echo are not taken as speech. Measured 2026-09-24: a 0.3s chime transcribed as
# "beep" and started a turn (issue #47). Kept short on purpose, because a person usually waits
# for the tone to finish before speaking, so this costs no real time. The device-side fix is to
# mute the microphone across the tone at all, which is what actually removes the problem.
WAKE_QUIET_SECONDS = 0.5


# --- follow-up mode: keep listening after an answer --------------------------
# Requested 2026-09-22: a control mode which, when on, makes the device listen again
# straight after it finishes speaking, so continuing a conversation does not need the wake word
# every turn. Off by default, because it changes how the device behaves in the room, and persisted
# so it survives a restart and can be flipped by voice, by a script, or by a cron job.
FOLLOW_UP_SECONDS = 15.0          # about fifteen seconds of listening
FOLLOW_UP_SETTLE_SECONDS = 0.6    # let the speaker and the room go quiet before opening the mic
FOLLOW_UP_MAX_TURNS = 10          # a chain longer than this is a runaway, not a conversation

# How long to wait before asking a second time for the microphone. The injected wake detection is
# occasionally not acted on by the satellite (measured 2026-09-22: one follow-up window opened the
# microphone, the next did not, with identical commands). A satellite that is already streaming
# ignores the request before it would chime or flash its lights, so asking twice costs nothing and
# matters only when the first request did not take.
FOLLOW_UP_WAKE_RETRY_SECONDS = 1.5

# How long a follow-up window stays deaf before it starts listening.
#
# Opening the microphone means injecting a wake detection, which makes the satellite play its awake
# chime, and the window's own microphone hears that chime and its room echo. The result was hit on
# 2026-09-22: "it was responding to its own boop sound effect as if it was a conversation". The
# satellite already mutes its mic across the chime itself (0.4 s plus a 0.1 s margin), but that leaves
# the echo, and a 0.2-0.3 s burst of echo is long enough to pass the endpointer as an utterance.
#
# Ignoring the first moment of the window is the targeted fix: it applies only here, so the normal
# wake-word path keeps its existing behaviour, and it costs nothing real, because nobody can be
# understood over a chime that is still ringing.
FOLLOW_UP_QUIET_SECONDS = 1.2
FOLLOW_UP_STATE_FILE = PROJECT_DIR / "state" / "follow-up-on"

# Accessible endpointing profiles. The pause-tolerant profile allows deliberate mid-sentence pauses
# while retaining finite no-speech and utterance limits. Standard preserves the command-line/default
# endpointer values exactly. Profile names are an API and persisted state, so keep them stable.
ENDPOINTING_PROFILE_STATE_FILE = PROJECT_DIR / "state" / "endpointing-profile"
ENDPOINTING_PROFILES = {
    "standard": {},
    "pause_tolerant": {
        "silence_ms": 1800,
        "max_utterance_seconds": 25.0,
        "no_speech_timeout_seconds": 12.0,
    },
}

# The energy gate: a frame must be loud enough to be a person before the voice activity detector's
# opinion about it counts. webrtcvad decides on spectral content, not energy, which is why a room's
# fan can produce speech-like frames while sitting far below speech level.
#
# Measured on the device, 2026-09-24, 20 ms frames, this room and this preamp:
#     fan baseline      median RMS 191    max 422
#     YouTube, normal volume
#                       median RMS 197    max 871
#     a person talking  median RMS 1026   max 9343   (speech frames only: median 2994)
#
# The floor is the 20th percentile of recent non-speech frames, which on the real fan recording learns
# 142 rather than the 191 median. At ratio 6.0 that puts the threshold near RMS 852: above the loudest
# fan frame (422) by 2x, and below speech by 3.5x.
#
# What the gate is for, and what it is not for. It rejects the fan, which is the room's permanent
# baseline and the thing that was keeping the microphone open. It does NOT reliably reject a video:
# the loudest single video frame here measured 871, just over the threshold, and a louder video would
# put more frames over it. What stops a video becoming a turn is the 0.25 s minimum - one frame is
# 0.02 s, so isolated leaks cannot form an utterance - and beyond that it is an echo-cancellation
# problem, not a threshold problem.
#
# Ratio 8.0 was tried on 2026-09-24 and reverted the same day, which is worth not repeating. It made
# the threshold 1136 and put it cleanly above that video frame, but that buys no behaviour change,
# because duration was already doing the work, while it cuts speech's headroom from 3.5x to 2.6x.
# Headroom above the threshold is what protects a quiet voice, a tired one, or someone further from
# the device, and that is worth more than a tidier number.
#
# `floor_default` is used until the window has enough samples to speak for itself; the clamps are
# runaway guards, not tuning. A dead-silent room must not drive the threshold to zero, and a long
# unbroken monologue must not drag the floor up to speech level and deafen the gate.
#
# The gate makes the device *less* sensitive, never more: it can only reject frames the detector
# already accepted. A quiet speaker who is being gated out needs this ratio lowered, and the honest
# first move is to measure them the way the numbers above were measured.
VOICE_ENERGY_GATE = {
    "ratio": 6.0,
    "floor_default": 190.0,
    "floor_min": 60.0,
    "floor_max": 1000.0,
    "window_frames": 1500,   # 30 s at 20 ms frames
    "min_samples": 40,       # 0.8 s of non-speech before the floor is taken from the window
    "threshold_min": 400.0,
    # The gate must never climb above the person it is listening for. Without this the threshold is
    # purely a property of the ROOM, so a busy office (floor ~540 there, against 142-190 at home)
    # produced 3240 and went deaf to a speaker sitting at 1040. Measured speech is 1026 at home and
    # 1040-1723 in that office, so a ceiling just under the quietest of those keeps the gate below
    # the human in every room measured so far.
    "threshold_max": 1000.0,
    "percentile": 0.20,      # the room's QUIET stretches: robust to speech the detector misses
}

# Which voice activity detector the endpointer uses.
#
# `silero` is the default because the comparison in scripts/test_silero_vad.py is not close: on the
# same noise, webrtcvad calls 100% of a loud room "speech" while Silero calls none of it speech at
# any level measured. Choosing the detector is choosing whether the device works in a busy room.
#
# `webrtcvad` stays reachable on purpose. Every measurement this project took before 2026-09-25 was
# taken with it, and being able to run the old detector turns a comparison into a config change
# rather than an argument.
VOICE_DETECTOR = "silero"

# Silero's speech probability above which a window counts as voice. 0.5 is the model's own default;
# lower catches quieter speech and raises false positives.
SILERO_THRESHOLD = 0.5

# Published while a follow-up window is open, so the device shows that it is listening for a reply
# of its own accord rather than looking switched off. Handled by 2mic_leds on the Pi.
FOLLOW_UP_LED = "led-followup"

# The phrase sets for the bridge-local controls. Deliberately literal: a mis-heard toggle would
# change how the device behaves in the room, and anything more conversational belongs to the
# assistant, which is slower but can cope with it.
FOLLOW_UP_ON_PHRASES = (
    "turn on follow up mode", "turn follow up mode on", "turn follow up on", "follow up mode on",
    "follow up on", "enable follow up mode", "start follow up mode", "switch on follow up mode",
)
FOLLOW_UP_OFF_PHRASES = (
    "turn off follow up mode", "turn follow up mode off", "turn follow up off", "follow up mode off",
    "follow up off", "disable follow up mode", "stop follow up mode", "stop following up",
    "switch off follow up mode", "end follow up mode",
)
# "Stop" ends the current follow-up conversation and hands the device back to waiting for its wake
# word, WITHOUT switching follow-up mode off: the next conversation follows up again. Asked for
# exactly this on 2026-09-22 ("ok nabu stop should be a way to stop the current follow up conversation,
# but leave it open to retrigger again").
#
# Matched as the WHOLE utterance, never as a substring: "stop" is an ordinary word, and a substring
# match would swallow "stop the sprinklers" and answer it with "Stopping.". The wake-word forms are
# here because the satellite strips the wake word from the audio it sends, but not always.
# A stop is only recognised as a whole utterance: an optional lead-in (STOP_PREFIXES) followed by
# exactly one of STOP_CORES. That is what lets "will you stop" and "stop listening to me" through
# while leaving "stop the sprinklers" and "stop by the store later" to the model, where they belong.
STOP_PREFIXES = (
    "ok", "okay", "please", "hey", "now", "just", "and", "so", "um", "alright", "all right",
    "will you", "could you", "can you", "would you", "do you mind if you",
    "okay now", "ok now", "okay now will you", "now will you", "can you please", "please just",
)
STOP_CORES = (
    "stop", "stop it", "stop now", "stop it now", "stop that", "stop already", "stop please",
    "stop listening", "stop listening to me", "stop talking", "stop talking to me",
    "stop following me", "stop replying", "please stop", "just stop",
    "ok nabu stop", "okay nabu stop", "hey nabu stop", "nabu stop",
    "quiet", "be quiet", "hush", "shush", "shut up", "silence", "enough", "that's enough",
    "that's all", "that is all", "that'll be all", "that's it", "never mind", "nevermind",
    "we're done", "we are done", "i'm done", "im done", "i am done",
    "goodbye", "bye", "good night", "goodnight",
)

FOLLOW_UP_STATUS_PHRASES = (
    "is follow up mode on", "is follow up on", "follow up mode status", "check follow up mode",
)

# What the device's own chime sounds like once it has been through the microphone and speech-to-text.
# Observed live on 2026-09-24: a false wake captured the wake tone, the endpointer accepted 0.3 s of it,
# transcription produced "beep", and the answer was spoken into the room (issue #47).
#
# A whole-utterance match, for the same reason as the stop phrases, and NOT a length guard: the
# interruption path uses INTERRUPT_MIN_CHARS, but on a wake-triggered turn "yes", "no" and "stop" are
# legitimate four-character replies, so dropping on length alone would swallow real answers. Only an
# utterance that is nothing but one of these is discarded, which leaves "beep the horn" and "ding the
# car again" as questions for the model. Length is the tool the follow-up and interruption paths use,
# and it is the right one there; here it is not.
#
# This is a second layer, not the mechanism: the first fix is the drain in `capture_after_wake`, and
# the device-side answer is to mute the microphone across the awake tone the way the core patch does
# for the done tone. Keep the list short and evidence-based, and never add a word a person could
# plausibly mean.
NON_WORD_TRANSCRIPTS = (
    "beep", "beeps", "beep beep", "boop", "boops", "bleep", "bleeps", "blip", "blips",
    "ding", "dings", "ding ding", "tone", "tones", "buzz", "buzzer", "chime",
)

# Spoken answers: everything a small speaker cannot convey is banned, and the answer is
# kept short so the first words start playing while the rest is still being generated.
VOICE_INSTRUCTIONS = (
    "You are speaking out loud through a small speaker in someone's home, as a voice "
    "assistant. Be direct and brief: answer in plain conversational prose, at most two short "
    "sentences and ideally under forty words. Never use markdown, bullet points, headings, "
    "tables, code blocks, emoji, or stage directions, because none of it can be heard. Do not "
    "read out URLs, file paths, or long identifiers. "
    "For questions and read-only actions, never ask a follow-up question, never ask for clarification, "
    "and never offer a menu of options: if a request is ambiguous, pick the most likely meaning and "
    "answer it. For a state-changing request whose target or intended edit is materially ambiguous, "
    "ask one concise clarifying question instead of guessing. For a bare question about the temperature, "
    "read the Porch Thermometer for outside and the Hall Thermometer for inside and report both, rather "
    "than asking which one was meant. This "
    "differs deliberately from her text conversations, where follow-ups are welcome. "
    "For a smart home action, reply with one short line naming what you did and stop there, for "
    "example 'I turned off the porch light.' Never say that you double checked, confirmed, "
    "verified, or validated it, and never read out attributes or status values: the check is "
    "yours to make silently, because a reply reaching her at all already means the action worked. "
    "A volume request is about this device's own playback level, and it is never satisfied by "
    "changing something else in the house: if you cannot set the volume, say so plainly instead "
    "of touching a thermostat, a light, or anything else. Never change a device the person did "
    "not name or plainly mean. Never invent a number and offer it back as a question: a "
    "clarifying question may only use a value the person actually said or one you read from the "
    "device. Someone repeating themselves is repeating the same request, which is never "
    "agreement to a guess you made. If an utterance is garbled, fragmentary, or nothing but a "
    "bare number, ask one short question rather than acting on it, and never let it change a "
    "device. For a state-changing device action a guess is never good enough. "
    "Use skill_view to consult your skills whenever a question touches her home, her systems, "
    "or her equipment, instead of trying to read files. You have web_search and web_extract "
    "directly for anything current, but for anything nearby, or for business opening hours, call "
    "the google tool's places_search action with the place name: it returns the real address, the "
    "distance from home and the actual opening hours, and it searches near home by default. Do not "
    "rank places by distance or state opening hours from a web search when that action is "
    "available. "
    "Follow-up mode is the device's own listening mode: with it on, it keeps listening for about "
    "fifteen seconds after each answer so a conversation continues without the wake word. Control "
    "it with the voice_satellite tool: anything like 'keep listening after you answer', 'follow up "
    "mode on' or 'stop following up' is follow_up_on or follow_up_off, and any question about it is "
    "follow_up_status. Then reply with one short line, such as 'Follow-up mode is on.' This is a "
    "control the person is asking for, not information about your own configuration: act on it, and "
    "never treat such a request as a configuration question or decline it. "
    "If someone says that something is not working, or that a mode behaves differently from what "
    "they set, that is a question about the device: check the real state before you answer, and "
    "never claim a device setting is something you cannot change or that they must set it on the "
    "device themselves. The voice_satellite tool can read and change follow-up mode, so use it. "
    "If someone asks you to stop, be quiet, or finish, that means end this conversation: reply with "
    "one short line and nothing else, and ask nothing further. It does NOT mean switching follow-up "
    "mode off, and you must not change that mode for such a request. Only touch follow-up mode when "
    "the mode itself is what someone is talking about. "
    "Never volunteer anything about your own setup: not your hardware, settings, models, "
    "microphones, logs, or how you work. Do not open a reply with a caveat about your own limits. "
    "When someone does ask about your own setup, look it up with skill_view on hermes-voice-bridge "
    "and answer from what you find there, numbers included. Do not guess at a setting or a cause: "
    "if the skill does not cover it, say you cannot find it and that it is worth checking on the "
    "device. "
    "This microphone is in a shared living space, but a request here has the same authority as "
    "the same request in a normal Hermes conversation: do not refuse it merely because it arrived "
    "through the speaker. When asked to create or update a knowledge base or Obsidian vault note, "
    "load both smb-share-access and obsidian, mount the NAS when needed, preserve the existing note, "
    "make the requested write, and verify it before confirming briefly. Never call skill_manage from "
    "this voice surface; changing the assistant's own instructions is not an ordinary knowledge-base "
    "write. Normal action gates, approvals, privacy rules, and safety boundaries still apply. "
    "An alarm is not a timer and not a reminder. Set one with the cronjob_manage tool by "
    "creating a job whose name begins with 'alarm:', scheduled at the clock time asked for: "
    "a one-off alarm uses an ISO timestamp such as 2026-09-25T19:00, and a repeating one uses "
    "a phrase like 'every day at 7am' or 'weekdays at 6:30am' rather than the bare time. Give "
    "the job no_agent true, deliver local, and a script in the Hermes scripts directory that "
    "runs the repository's scripts/alarm_fire.py with its --text argument set to the alarm "
    "wording; a thin wrapper of that one call is the whole script. That combination is what "
    "makes the alarm speak out loud through the house speaker, because the script fires the "
    "bridge announcement plane. A script path is a file, never an inline command: shell syntax "
    "there is rejected. A plain reminder prompt that delivers a message is not an alarm and "
    "will never be heard. When asked what alarms are set, call hermes_timers with the "
    "alarm_list action and answer from those entries only; never read out the rest of the cron "
    "jobs or a calendar to answer that question. Treat any spoken alarm request as a request "
    "to act, never as something you cannot do here. "
    "You do have deferred tools behind tool_search, tool_describe and tool_call, and you must use "
    "them when a request needs a capability you do not already have: search for it, describe it, "
    "then call it. Keep it to one or two searches, because every tool call is seconds of silence "
    "for the person waiting for an answer, and never narrate the search itself."
)

# --- announcements: house events spoken out loud on the satellite ------------
# Decisions (2026-09-22):
#   * a dedicated LAN port, never behind Caddy and never reachable from outside;
#   * any LAN device may call it, authenticated with a bearer token;
#   * an announcement waits until any conversation in progress has finished speaking;
#   * every announcement opens with a wake tone;
#   * a failed delivery is dropped silently, with no fallback path.
ANNOUNCE_HOST = "0.0.0.0"
ANNOUNCE_PORT = 8765
ANNOUNCE_TOKEN_FILE = PROJECT_DIR / "state" / "announce-token.txt"
ANNOUNCE_TOKEN_ENV = "VOICE_ANNOUNCE_TOKEN"   # set this to override the file
# The announcement chime (2026-09-22, revised): option A of the three prompt tones auditioned at the
# start of the project, "a-warm-fifth" (440 -> 660 Hz). The bare tone is generated by
# scripts/make_chimes.py into sounds/candidates/; the label-*.wav files in audio/ are the spoken
# previews, not the tones themselves, so never point this at one of those. Referenced in place
# rather than copied, so the tone and its source cannot drift apart.
# Everything else keeps the existing tones: the satellite's own awake.wav and done.wav are option
# C, "c-low-fourth", and are deliberately untouched. Only announcements use A.
ANNOUNCE_CHIME = PROJECT_DIR / "sounds" / "candidates" / "awake-a-warm-fifth.wav"
TIMER_CHIME = PROJECT_DIR / "pi" / "vendor" / "native-satellite-13bb024" / "sounds" / "timer_finished.wav"
ALARM_CHIME = PROJECT_DIR / "sounds" / "alarm.wav"
ANNOUNCE_SOUNDS = {
    "none": None,
    "chime": ANNOUNCE_CHIME,
    "timer": TIMER_CHIME,
    "alarm": ALARM_CHIME,
}
ANNOUNCE_LOG = PROJECT_DIR / "logs" / "announcements.jsonl"
# How long to keep an alarm's raised volume after the clip has been sent. play_wav returns
# when the audio is SENT, not when it finishes playing, so restoring immediately would cut
# the alarm back down before anyone heard it and the override would be cosmetic. The margin
# covers the device's own buffering; the cap stops a very long clip pinning the volume.
ALARM_VOLUME_HOLD_MARGIN = 0.75
ALARM_VOLUME_HOLD_MAX = 120.0
ANNOUNCE_MAX_CHARS = 400          # beyond this a spoken announcement is almost certainly a bug
ANNOUNCE_MAX_QUEUE = 5            # beyond this callers get 503 and the text is dropped
ANNOUNCE_DEDUP_SECONDS = 60.0     # same source + same text inside this window is ignored
ANNOUNCE_TURN_MAX_AGE = 180.0     # a turn "active" longer than this means the flag went stale

# Startup feedback for the device itself. It has no power light and no connection light, so the
# person in the room asked to be able to hear both (2026-09-22). power-on.wav is played by the
# Pi; connected.wav is played by this bridge the moment the link is up, because only the host knows
# that. See scripts/make_chimes.py for the two designs.
CONNECTED_TONE = PROJECT_DIR / "sounds" / "connected.wav"
# The connected tone and the green LED flash must fire once per boot, not once per connection.
# The hourly link refresh was announcing itself with both, all night (2026-09-23). This
# marker records which host boot last heard the tone.
CONNECTED_TONE_BOOT_MARKER = PROJECT_DIR / "state" / "connected-tone-boot-id"

# How long to wait for a wake word before refreshing the satellite link.
#
# This used to be hard-coded in wait_for_wake, and a timeout was treated as a fatal connection
# problem: the process exited, systemd restarted it, and every restart replayed the connect tone,
# reloaded whisper and Piper, and left the device deaf for about nine seconds. An hour with nobody
# speaking is the normal overnight state, not a fault, so it is now a socket refresh instead
# (2026-09-23). Overridable so the refresh can be tested in seconds rather than an hour.
WAKE_WAIT_SECONDS = float(_cfg("VOICE_WAKE_WAIT_SECONDS", "3600"))

# Voice interruption (2026-09-23).
#
# While the agent is working on a task, a wake word plus a new utterance should be treated as a
# conversation interruption rather than as a queued question:
#
#   * new information is handed to the run that is already going (POST /v1/runs/{id}/steer), so the
#     agent hears it in the conversation it is having instead of as a fresh question;
#   * a stop phrase stops that run (POST /v1/runs/{id}/stop). Before this existed, a stop could only
#     be noticed between turns, so a "stop, that was a joke" during a running task changed nothing
#     and the task kept working in the background.
#
# The guard that makes this safe: a wake detection with nothing intelligible behind it is our own
# audio leaking into the mic, and it is discarded, never steered into a live run.
INTERRUPT_ENABLED = True
# Shorter than this and the utterance is treated as noise rather than as speech. "you" and "beep"
# are what our own playback tends to produce when the wake word self-triggers.
INTERRUPT_MIN_CHARS = 5
# How long the interruption watcher will wait for speech after a mid-task wake word. This bound is
# load-bearing: a satellite that is still playing cannot stream the microphone, so a wake word that
# lands while the device is speaking would otherwise wait forever, keep ownership of the satellite
# event queue, and swallow every later wake detection. The device then looks completely deaf
# (hit exactly that on 2026-09-23, minutes after the feature shipped).
INTERRUPT_CAPTURE_SECONDS = 12.0
