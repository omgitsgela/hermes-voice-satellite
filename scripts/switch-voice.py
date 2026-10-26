#!/usr/bin/env python3
"""Switch the Piper voice on every surface, in one command.

The bridge and Hermes keep their voices in different places and in different shapes, and getting
either wrong fails quietly:

  * the bridge reads `voices/<name>.onnx` beside the repo and `VOICE_PIPER_VOICE` +
    `VOICE_PIPER_SPEAKER` from `bridge.env`;
  * Hermes reads `~/.hermes/cache/piper-voices/<name>.onnx` and `tts.piper.voice` +
    `tts.piper.speaker_id` from `config.yaml`, and it will NOT download a voice it does not
    recognise — it just fails at synthesis time.

On top of that, a multi-speaker voice is addressed two different ways: the bridge takes a speaker
NAME from the voice's own map, Hermes takes the numeric ID that name resolves to. This tool owns
both mappings so a switch cannot half-apply, and it validates the voice against the satellite's
audio contract (22050 Hz, 16-bit mono, no resampling on the device) before writing anything.

Usage
-----
    # see what is installed and what is live
    scripts/switch-voice.py --list

    # switch both surfaces to a catalog voice
    scripts/switch-voice.py en_US-lessac-medium

    # a multi-speaker voice: the bridge stores "78", Hermes gets the id it maps to
    scripts/switch-voice.py clean100 --speaker 78

    # only one side (e.g. try it in chat before it speaks in the room)
    scripts/switch-voice.py en_US-amy-medium --scope gateway

    # see the plan without touching anything
    scripts/switch-voice.py clean100 --speaker 78 --dry-run

    # undo the last switch
    scripts/switch-voice.py --rollback

Voices come from three places, in this order: an already-downloaded copy in `voices/`, then the
curated external list (`scripts/fetch-voice.py`, hash-pinned), then the Piper catalog
(`piper.download_voices`). `--no-fetch` refuses the last two, which is what you want on a machine
with no network or in a test.

Every switch backs up `bridge.env` and the Hermes `config.yaml` under `state/voice/backups/` and
records what it did in `state/voice/state.json`, so `--rollback` needs no notes and no /tmp.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bridge.voices import VOICE_SAMPLE_RATE, describe_voice, resolve_speaker_id  # noqa: E402

BRIDGE_UNIT = "hermes-voice-bridge.service"
HERMES_EXAMPLE = REPO / "hermes" / "config" / "voice-surface.example.yaml"
ENV_VOICE = "VOICE_PIPER_VOICE"
ENV_SPEAKER = "VOICE_PIPER_SPEAKER"


class Refused(Exception):
    """A switch that must not proceed. Nothing has been written when this is raised."""


def say(msg=""):
    print(msg, flush=True)


def fail(msg):
    print(f"refused: {msg}", file=sys.stderr, flush=True)
    return 1


# --- bridge.env editing ------------------------------------------------------
def env_values(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def set_env(text: str, key: str, value):
    """Set (or remove, when value is None) one KEY=VALUE line, leaving everything else alone.

    Line-level rather than a parse/re-dump on purpose: bridge.env is hand-written and commented,
    and a YAML-style round trip would silently discard those comments.
    """
    lines = text.splitlines()
    out = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            if stripped.split("=", 1)[0].strip() == key:
                if value is not None:
                    out.append(f"{key}={value}")
                replaced = True
                continue
        out.append(line)
    if not replaced and value is not None:
        out.append(f"{key}={value}")
    text = "\n".join(out)
    return text + "\n" if text and not text.endswith("\n") else text


# --- voice acquisition -------------------------------------------------------
def voice_files(repo: Path, name: str):
    return repo / "voices" / f"{name}.onnx", repo / "voices" / f"{name}.onnx.json"


def have_voice(repo: Path, name: str) -> bool:
    model, cfg = voice_files(repo, name)
    return model.is_file() and cfg.is_file()


def ensure_voice(repo: Path, name: str, allow_fetch: bool) -> None:
    """Make `voices/<name>.onnx{,.json}` exist, or explain exactly how to get it."""
    if have_voice(repo, name):
        return
    if not allow_fetch:
        raise Refused(
            f"voice {name!r} is not in {repo / 'voices'}, and --no-fetch was given. Download it "
            f"first: scripts/fetch-voice.py {name} (curated) or "
            f"python -m piper.download_voices --download-dir voices {name} (catalog)")

    voices_dir = repo / "voices"
    voices_dir.mkdir(parents=True, exist_ok=True)
    fetch = REPO / "scripts" / "fetch-voice.py"

    # A curated (non-catalog) voice first: piper.download_voices cannot know those names.
    if fetch.is_file():
        probe = subprocess.run([sys.executable, str(fetch), "--is-external", name],
                               capture_output=True, text=True)
        if probe.returncode == 0:
            say(f"  fetch    {name} (curated external voice, hash-pinned)")
            res = subprocess.run([sys.executable, str(fetch), name, "--download-dir", str(voices_dir)],
                                 capture_output=True, text=True)
            if res.returncode != 0:
                raise Refused(f"scripts/fetch-voice.py failed for {name}:\n{res.stdout}{res.stderr}")
            if have_voice(repo, name):
                return
            raise Refused(f"fetch-voice.py reported success but {name} is still not in {voices_dir}")

    say(f"  fetch    {name} (Piper catalog)")
    res = subprocess.run([sys.executable, "-m", "piper.download_voices",
                          "--download-dir", str(voices_dir), name],
                         capture_output=True, text=True)
    if not have_voice(repo, name):
        raise Refused(
            f"could not obtain voice {name!r}.\n"
            f"  catalog download said: {(res.stdout + res.stderr).strip()[:400]}\n"
            f"  If it is a curated third-party voice, add it to scripts/fetch-voice.py's "
            f"EXTERNAL_VOICES, or drop {name}.onnx and {name}.onnx.json into {voices_dir} by hand.")


# --- state, backups ----------------------------------------------------------
def voice_state_dir(repo: Path) -> Path:
    return repo / "state" / "voice"


def load_state(repo: Path) -> dict:
    path = voice_state_dir(repo) / "state.json"
    if not path.is_file():
        return {"current": None, "previous": None, "history": []}
    try:
        return json.loads(path.read_text())
    except ValueError:
        return {"current": None, "previous": None, "history": []}


def save_state(repo: Path, state: dict) -> None:
    directory = voice_state_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    state["history"] = state.get("history", [])[-50:]
    (directory / "state.json").write_text(json.dumps(state, indent=2) + "\n")


def snapshot(repo: Path, bridge_env_path: Path, hermes_cfg: Path, scope: str) -> dict:
    """What is live right now, from the live files rather than from this tool's memory."""
    voice = speaker = hermes_sid = None
    if bridge_env_path.is_file():
        values = env_values(bridge_env_path.read_text())
        voice = values.get(ENV_VOICE)
        speaker = values.get(ENV_SPEAKER)
    if hermes_cfg.is_file():
        try:
            import yaml
            cfg = yaml.safe_load(hermes_cfg.read_text()) or {}
            piper = ((cfg.get("tts") or {}).get("piper") or {})
            voice = voice or piper.get("voice")
            hermes_sid = piper.get("speaker_id")
        except Exception:
            pass
    return {"voice": voice, "speaker": speaker, "hermes_speaker_id": hermes_sid,
            "scope": scope, "when": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def backup(repo: Path, files, note: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    directory = voice_state_dir(repo) / "backups" / stamp
    n = 1
    while directory.exists():
        n += 1
        directory = voice_state_dir(repo) / "backups" / f"{stamp}-{n}"
    directory.mkdir(parents=True)
    for src in files:
        src = Path(src)
        if src.is_file():
            shutil.copy2(src, directory / src.name)
    (directory / "manifest.json").write_text(json.dumps({"note": note}, indent=2) + "\n")
    return directory


# --- the switch --------------------------------------------------------------
def apply(bridge_env_path: Path, hermes_cfg: Path, cache_dir: Path, repo: Path,
          voice: str, speaker_setting, speaker_id, scope: str) -> None:
    """Write the voice into every file that needs it. Returns nothing; raises Refused."""
    if scope in ("both", "bridge"):
        text = bridge_env_path.read_text() if bridge_env_path.is_file() else ""
        text = set_env(text, ENV_VOICE, voice)
        text = set_env(text, ENV_SPEAKER, speaker_setting)   # None removes the line
        bridge_env_path.write_text(text)

    if scope in ("both", "gateway"):
        cache_dir.mkdir(parents=True, exist_ok=True)
        for src in voice_files(repo, voice):
            shutil.copy2(src, cache_dir / src.name)

        pairs = [f"tts.piper.voice={voice}"]
        args = [sys.executable, str(REPO / "scripts" / "hermes-voice-config.py"),
                "--config", str(hermes_cfg), "--example", str(HERMES_EXAMPLE), *pairs]
        if speaker_id is None:
            args += ["--unset", "tts.piper.speaker_id"]
        else:
            args += [f"tts.piper.speaker_id={speaker_id}"]
        res = subprocess.run(args, capture_output=True, text=True)
        if res.returncode != 0:
            raise Refused(f"could not update {hermes_cfg}:\n{res.stdout}{res.stderr}")
        for line in res.stdout.splitlines():
            if line.strip():
                say(f"  hermes   {line.strip()}")


def restart_bridge() -> str:
    try:
        res = subprocess.run(["systemctl", "--user", "restart", BRIDGE_UNIT],
                             capture_output=True, text=True, timeout=90)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "not restarted (systemctl unavailable)"
    if res.returncode != 0:
        return f"not restarted ({(res.stderr or res.stdout).strip()[:160]})"
    return "restarted"


def verify_bridge(voice: str) -> list:
    """Best-effort: is the running bridge really using this voice? Never fatal."""
    notes = []
    try:
        active = subprocess.run(["systemctl", "--user", "is-active", BRIDGE_UNIT],
                                capture_output=True, text=True, timeout=30).stdout.strip()
        notes.append(f"service {active or 'unknown'}")
        pid = subprocess.run(["systemctl", "--user", "show", BRIDGE_UNIT, "-p", "MainPID", "--value"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        if pid and pid.isdigit():
            environ = Path(f"/proc/{pid}/environ")
            if environ.is_file():
                raw = environ.read_bytes().split(b"\0")
                found = [e.decode(errors="replace") for e in raw if e.startswith(ENV_VOICE.encode())]
                notes.append(f"process env {found[0] if found else 'MISSING'}")
                if found and found[0] != f"{ENV_VOICE}={voice}":
                    notes.append("WARNING: the running process is not on the voice just written")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        notes.append("service state unknown (systemctl unavailable)")
    return notes


def resolve_target(repo: Path, name: str, speaker, allow_fetch: bool):
    """Validate the voice and work out what each surface should be told. Nothing is written."""
    ensure_voice(repo, name, allow_fetch)
    model, cfg_path = voice_files(repo, name)
    if not cfg_path.is_file():
        raise Refused(
            f"{model.name} has no {cfg_path.name} beside it. A Piper voice is the .onnx plus its "
            f"config; without the config there is no speaker map and no sample rate to check.")
    try:
        info = describe_voice(cfg_path)
    except ValueError as exc:
        raise Refused(str(exc)) from None

    num_speakers = info["num_speakers"]
    speaker_setting = None
    speaker_id = None

    if num_speakers > 1:
        if speaker is None:
            names = ", ".join(sorted(info["speaker_id_map"])[:6])
            raise Refused(
                f"{name} has {num_speakers} speakers, so --speaker is required. Without it Piper "
                f"falls to an arbitrary default speaker from the corpus. Pick one with "
                f"scripts/list-piper-speakers.py --voice {name}"
                + (f" (names look like: {names} ...)" if names else ""))
        try:
            speaker_id = resolve_speaker_id(speaker, cfg_path)
        except (ValueError, FileNotFoundError) as exc:
            raise Refused(str(exc)) from None
        speaker_setting = str(speaker)
        say(f"  speaker  {speaker} -> numeric id {speaker_id} "
            f"(bridge stores the name, Hermes the id)")
    elif speaker is not None:
        say(f"  note     {name} has a single speaker; ignoring --speaker {speaker}")

    return info, speaker_setting, speaker_id


def show_list(repo: Path, bridge_env_path: Path, hermes_cfg: Path) -> int:
    live = env_values(bridge_env_path.read_text()).get(ENV_VOICE) if bridge_env_path.is_file() else None
    live_speaker = env_values(bridge_env_path.read_text()).get(ENV_SPEAKER) if bridge_env_path.is_file() else None
    gw = None
    if hermes_cfg.is_file():
        try:
            import yaml
            cfg = yaml.safe_load(hermes_cfg.read_text()) or {}
            gw = ((cfg.get("tts") or {}).get("piper") or {}).get("voice")
        except Exception:
            gw = None

    voices_dir = repo / "voices"
    models = sorted(voices_dir.glob("*.onnx")) if voices_dir.is_dir() else []
    if not models:
        say(f"No voices installed in {voices_dir} yet.")
        say("Add one with: scripts/switch-voice.py <voice-name>   (it will fetch it)")
        return 0

    say(f"{'voice':34s} {'speakers':>8s}  {'Hz':>6s}  live")
    for model in models:
        name = model.stem
        cfg_path = model.with_name(name + ".onnx.json")
        try:
            info = describe_voice(cfg_path)
            speakers, rate = str(info["num_speakers"]), str(info["sample_rate"])
        except (ValueError, FileNotFoundError):
            speakers, rate = "?", "?"
        marks = []
        if name == live:
            marks.append("bridge")
        if name == gw:
            marks.append("gateway")
        say(f"{name:34s} {speakers:>8s}  {rate:>6s}  {', '.join(marks) or '-'}")
    if live and live_speaker:
        say(f"\nbridge speaker setting: {live_speaker}")
    if not live and not gw:
        say("\nNo voice configured on either surface yet.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("voice", nargs="?", help="voice name, e.g. en_US-lessac-medium or clean100")
    parser.add_argument("--speaker", help="speaker NAME from the voice's own map, or a numeric id "
                                          "(multi-speaker voices only)")
    parser.add_argument("--scope", choices=["both", "bridge", "gateway"], default="both",
                        help="which surfaces to change (default both)")
    parser.add_argument("--repo", default=str(REPO), help="the bridge checkout (default: this repo)")
    parser.add_argument("--bridge-env", default=None, help="bridge.env to edit (default <repo>/bridge.env)")
    parser.add_argument("--hermes-config", default=str(Path.home() / ".hermes" / "config.yaml"),
                        help="Hermes config.yaml (default ~/.hermes/config.yaml)")
    parser.add_argument("--hermes-cache", default=None,
                        help="Hermes voice cache (default ~/.hermes/cache/piper-voices)")
    parser.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    parser.add_argument("--rollback", action="store_true", help="revert to the previous voice")
    parser.add_argument("--no-restart", action="store_true", help="do not restart the bridge")
    parser.add_argument("--no-fetch", action="store_true", help="never download a voice")
    parser.add_argument("--no-verify", action="store_true", help="skip post-switch checks")
    parser.add_argument("--list", action="store_true", help="show installed voices and what is live")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    bridge_env_path = Path(args.bridge_env).expanduser() if args.bridge_env else repo / "bridge.env"
    hermes_cfg = Path(args.hermes_config).expanduser()
    cache_dir = Path(args.hermes_cache).expanduser() if args.hermes_cache else \
        Path.home() / ".hermes" / "cache" / "piper-voices"

    if args.list or (not args.voice and not args.rollback):
        return show_list(repo, bridge_env_path, hermes_cfg)

    state = load_state(repo)

    if args.rollback:
        previous = state.get("previous")
        if not previous or not previous.get("voice"):
            return fail("nothing to roll back: no record of a previous voice in "
                        f"{voice_state_dir(repo) / 'state.json'}")
        args.voice = previous["voice"]
        args.speaker = previous.get("speaker")
        say(f"rolling back to {args.voice}" + (f" --speaker {args.speaker}" if args.speaker else ""))

    if not args.voice:
        return fail("no voice given; see --list, or --rollback")

    scope = args.scope
    if scope in ("both", "gateway") and not hermes_cfg.is_file():
        if args.scope == "gateway":
            return fail(f"no Hermes config at {hermes_cfg}; nothing to change there")
        say(f"  note     no Hermes config at {hermes_cfg}: the gateway is skipped, "
            f"changing the bridge only")
        scope = "bridge"

    try:
        info, speaker_setting, speaker_id = resolve_target(repo, args.voice, args.speaker, not args.no_fetch)
    except Refused as exc:
        return fail(str(exc))

    before = snapshot(repo, bridge_env_path, hermes_cfg, scope)
    say(f"  voice    {args.voice} ({info['num_speakers']} speaker"
        f"{'s' if info['num_speakers'] != 1 else ''}, {info['sample_rate']} Hz, "
        f"{info['quality'] or 'unknown'} quality)")

    wants = {
        "voice": args.voice, "speaker": speaker_setting, "hermes_speaker_id": speaker_id,
        "scope": scope, "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    if before["voice"] == args.voice and before.get("speaker") == speaker_setting \
            and before.get("hermes_speaker_id") in (None, speaker_id):
        say("  already  this is the configured voice on the selected surfaces; nothing to do")
        return 0

    if args.dry_run:
        say("  plan     (dry run: nothing written)")
        if scope in ("both", "bridge"):
            say(f"           {bridge_env_path}: {ENV_VOICE}={args.voice}"
                + (f", {ENV_SPEAKER}={speaker_setting}" if speaker_setting else
                   f", {ENV_SPEAKER} removed"))
        if scope in ("both", "gateway"):
            say(f"           {cache_dir}: copy {args.voice}.onnx{{,.json}}")
            say(f"           {hermes_cfg}: tts.piper.voice={args.voice}"
                + (f", speaker_id={speaker_id}" if speaker_id is not None else ", speaker_id removed"))
        return 0

    backup_dir = backup(repo, [bridge_env_path, hermes_cfg], note=f"before switching to {args.voice}")
    say(f"  backup   {backup_dir}")

    try:
        apply(bridge_env_path, hermes_cfg, cache_dir, repo,
              args.voice, speaker_setting, speaker_id, scope)
    except Refused as exc:
        return fail(str(exc))

    state["previous"] = before if before.get("voice") else state.get("previous")
    state["current"] = wants
    state.setdefault("history", []).append(wants)
    save_state(repo, state)

    if scope in ("both", "bridge") and not args.no_restart:
        say(f"  service  {restart_bridge()}")
        if not args.no_verify:
            for note in verify_bridge(args.voice):
                say(f"  verify   {note}")

    say(f"\nDone: {args.voice}" + (f" (speaker {speaker_setting} -> id {speaker_id})"
                                   if speaker_setting else "") + f" on {scope}.")
    if scope in ("both", "gateway"):
        say("The gateway picks the change up without a restart; the next spoken reply uses it.")
    if scope in ("both", "bridge"):
        say("The satellite speaks with it from its next reply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
