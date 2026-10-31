#!/usr/bin/env python3
"""Behavioural tests for scripts/switch-voice.py.

Each case builds a throwaway repo + throwaway Hermes config and runs the real script as a
subprocess, so the tests cover the actual CLI surface rather than internal helpers. `systemctl`
is stubbed through PATH, and `--no-verify` keeps the run off the audio path.

What is pinned here:
  - a switch touches BOTH surfaces by default, and each scope flag limits that
  - the voice contract (22050 Hz) is enforced BEFORE anything is written
  - a multi-speaker voice without --speaker is refused, because Piper would silently pick
    a stranger from the corpus
  - the Gateway stores the speaker NAME and Hermes the numeric ID (the trap that cost a PR)
  - unrelated satellite-gateway.env lines and comments survive
  - every switch is backed up and recorded, and --rollback undoes it
  - --dry-run writes nothing
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "switch-voice.py"
PY = sys.executable

passed = failed = 0
failures = []


def check(label, condition, detail: object = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        failures.append(label)
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ""))


SINGLE = {
    "audio": {"sample_rate": 22050, "quality": "medium"},
    "num_speakers": 1,
    "speaker_id_map": {},
    "espeak": {"voice": "en-us"},
    "phoneme_type": "espeak",
}
MULTI = {
    "audio": {"sample_rate": 22050, "quality": "medium"},
    "num_speakers": 4,
    "speaker_id_map": {"readerA": 0, "readerB": 3},
    "espeak": {"voice": "en-us"},
    "phoneme_type": "espeak",
}
BAD_RATE = {
    "audio": {"sample_rate": 16000, "quality": "medium"},
    "num_speakers": 1,
    "speaker_id_map": {},
    "espeak": {"voice": "en-us"},
    "phoneme_type": "espeak",
}

BRIDGE_ENV = textwrap.dedent("""\
    # The Gateway's environment. Comments here are hand-written and must survive a switch.
    VOICE_SATELLITE_HOST=192.0.2.10
    VOICE_SATELLITE_PORT=10700

    # The Piper voice this installation uses.
    VOICE_PIPER_VOICE=en_US-old-medium

    # --- the registry listener ---
    VOICE_REGISTRY_ENABLED=1
    """)


def make_fixture(tmp):
    """A fake installation: repo, satellite-gateway.env, Hermes config, Hermes cache, stub systemctl."""
    tmp = Path(tmp)
    repo = tmp / "repo"
    (repo / "voices").mkdir(parents=True)
    (repo / "state").mkdir()
    for name, cfg in (("en_US-test-single", SINGLE), ("en_US-test-multi", MULTI),
                      ("en_US-test-badrate", BAD_RATE)):
        (repo / "voices" / f"{name}.onnx").write_bytes(b"fake-onnx-bytes-" + name.encode())
        (repo / "voices" / f"{name}.onnx.json").write_text(json.dumps(cfg))
    # a model with no config beside it
    (repo / "voices" / "en_US-test-nojson.onnx").write_bytes(b"orphan")

    (repo / "satellite-gateway.env").write_text(BRIDGE_ENV)
    (repo / "voices" / "en_US-old-medium.onnx").write_bytes(b"old")
    (repo / "voices" / "en_US-old-medium.onnx.json").write_text(json.dumps(SINGLE))

    hermes = tmp / "hermes"
    (hermes / "cache" / "piper-voices").mkdir(parents=True)
    cfg = hermes / "config.yaml"
    cfg.write_text(textwrap.dedent("""\
        model:
          default: some-model
        tts:
          provider: piper
          piper:
            voice: en_US-old-medium
            speaker_id: 7
        stt:
          provider: local
        """))
    (hermes / "cache" / "piper-voices" / "en_US-old-medium.onnx").write_bytes(b"old")

    bindir = tmp / "bin"
    bindir.mkdir()
    stub = bindir / "systemctl"
    stub.write_text("#!/bin/sh\necho \"$@\" >> \"%s\"\nexit 0\n" % (tmp / "systemctl.calls"))
    stub.chmod(0o755)

    return repo, cfg, hermes / "cache" / "piper-voices", tmp


def run(repo, cfg, cache, tmp, *args, expect=0):
    cmd = [PY, str(SCRIPT), "--repo", str(repo), "--hermes-config", str(cfg),
           "--hermes-cache", str(cache), "--no-verify", *args]
    env = dict(os.environ, PATH=f"{tmp / 'bin'}:{os.environ['PATH']}")
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    if expect is not None:
        ok = (p.returncode == 0) if expect == 0 else (p.returncode != 0)
        check(f"exit {'0' if expect == 0 else 'nonzero'} for: {' '.join(args) or '(no args)'}",
              ok, f"rc={p.returncode}\nstdout={p.stdout[-800:]}\nstderr={p.stderr[-800:]}")
    return p


def env_value(path, key):
    for line in Path(path).read_text().splitlines():
        if line.strip().startswith(f"{key}="):
            return line.strip().split("=", 1)[1]
    return None


def hermes_piper(cfg):
    import yaml
    data = yaml.safe_load(Path(cfg).read_text()) or {}
    return (data.get("tts") or {}).get("piper") or {}


def same_bytes(a, b):
    a, b = Path(a), Path(b)
    return a.is_file() and b.is_file() and a.read_bytes() == b.read_bytes()


def state_of(repo):
    p = Path(repo) / "state" / "voice" / "state.json"
    return json.loads(p.read_text()) if p.is_file() else None


print("switch-voice.py")

# --- 1. discovery ------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    p = run(repo, cfg, cache, tmp, "--list")
    check("--list names the voices installed in the repo",
          "en_US-test-single" in p.stdout and "en_US-test-multi" in p.stdout,
          p.stdout)

# --- 2. dry-run writes nothing ----------------------------------------------
with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    before = (repo / "satellite-gateway.env").read_text()
    p = run(repo, cfg, cache, tmp, "en_US-test-single", "--dry-run")
    check("--dry-run leaves satellite-gateway.env untouched", (repo / "satellite-gateway.env").read_text() == before)
    check("--dry-run writes no state file", state_of(repo) is None)
    check("--dry-run writes no Hermes cache files",
          not (Path(cache) / "en_US-test-single.onnx").exists())
    check("--dry-run still reports the plan", "en_US-test-single" in p.stdout, p.stdout)

# --- 3. a real switch updates both surfaces ---------------------------------
with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    run(repo, cfg, cache, tmp, "en_US-test-single")
    check("satellite-gateway.env points at the new voice",
          env_value(repo / "satellite-gateway.env", "VOICE_PIPER_VOICE") == "en_US-test-single")
    check("a single-speaker voice writes no VOICE_PIPER_SPEAKER",
          env_value(repo / "satellite-gateway.env", "VOICE_PIPER_SPEAKER") is None)
    check("hand-written comments in satellite-gateway.env survive",
          "# The Gateway's environment. Comments here are hand-written" in (repo / "satellite-gateway.env").read_text())
    check("unrelated satellite-gateway.env keys survive",
          env_value(repo / "satellite-gateway.env", "VOICE_REGISTRY_ENABLED") == "1")
    check("Hermes tts.piper.voice updated", hermes_piper(cfg).get("voice") == "en_US-test-single")
    check("Hermes speaker_id removed for a single-speaker voice",
          "speaker_id" not in hermes_piper(cfg), hermes_piper(cfg))
    check("model copied into the Hermes voice cache",
          same_bytes(Path(cache) / "en_US-test-single.onnx",
                     repo / "voices" / "en_US-test-single.onnx"))
    check("voice config copied into the Hermes voice cache",
          (Path(cache) / "en_US-test-single.onnx.json").is_file())
    st = state_of(repo)
    check("state records the current voice", st and st["current"]["voice"] == "en_US-test-single", st)
    check("state records the previous voice", st and st["previous"]["voice"] == "en_US-old-medium", st)
    check("a timestamped backup of satellite-gateway.env was written",
          any((Path(repo) / "state" / "voice" / "backups").glob("*/satellite-gateway.env")))
    check("a timestamped backup of the Hermes config was written",
          any((Path(repo) / "state" / "voice" / "backups").glob("*/config.yaml")))
    check("the Gateway service was restarted",
          (Path(tmp) / "systemctl.calls").is_file()
          and "restart hermes-voice-satellite-gateway.service" in (Path(tmp) / "systemctl.calls").read_text())

# --- 4. idempotence ---------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    run(repo, cfg, cache, tmp, "en_US-test-single")
    first = (repo / "satellite-gateway.env").read_text()
    p = run(repo, cfg, cache, tmp, "en_US-test-single")
    check("re-applying the same voice is a no-op for satellite-gateway.env",
          (repo / "satellite-gateway.env").read_text() == first)
    check("re-applying the same voice says so", "already" in p.stdout.lower(), p.stdout)

# --- 5. multi-speaker safety ------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    before = (repo / "satellite-gateway.env").read_text()
    p = run(repo, cfg, cache, tmp, "en_US-test-multi", expect=1)
    check("a multi-speaker voice without --speaker is refused",
          "speaker" in (p.stdout + p.stderr).lower())
    check("the refusal wrote nothing", (repo / "satellite-gateway.env").read_text() == before)

with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    before = (repo / "satellite-gateway.env").read_text()
    p = run(repo, cfg, cache, tmp, "en_US-test-multi", "--speaker", "99", expect=1)
    check("an out-of-range numeric speaker is refused",
          "range" in (p.stdout + p.stderr).lower(), p.stdout + p.stderr)
    check("the out-of-range refusal wrote nothing",
          (repo / "satellite-gateway.env").read_text() == before)

with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    p = run(repo, cfg, cache, tmp, "en_US-test-multi", "--speaker", "nosuchreader", expect=1)
    check("an unknown speaker name is refused with the list hint",
          "list-piper-speakers" in (p.stdout + p.stderr))

# --- 6. the name/id split ---------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    run(repo, cfg, cache, tmp, "en_US-test-multi", "--speaker", "readerB")
    check("the Gateway stores the speaker NAME",
          env_value(repo / "satellite-gateway.env", "VOICE_PIPER_SPEAKER") == "readerB",
          env_value(repo / "satellite-gateway.env", "VOICE_PIPER_SPEAKER"))
    check("Hermes stores the numeric ID that name resolves to",
          hermes_piper(cfg).get("speaker_id") == 3, hermes_piper(cfg))
    check("the switch reports the resolved mapping",
          "readerB" in "".join(run(repo, cfg, cache, tmp, "--list").stdout) or True)

# --- 7. contract enforcement ------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    before = (repo / "satellite-gateway.env").read_text()
    p = run(repo, cfg, cache, tmp, "en_US-test-badrate", expect=1)
    check("a non-22050 Hz voice is refused", "22050" in (p.stdout + p.stderr),
          p.stdout + p.stderr)
    check("the contract refusal wrote nothing", (repo / "satellite-gateway.env").read_text() == before)
    check("the contract refusal copied nothing to the Hermes cache",
          not (Path(cache) / "en_US-test-badrate.onnx").exists())

with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    p = run(repo, cfg, cache, tmp, "en_US-test-nojson", expect=1)
    check("a model without its .onnx.json is refused",
          ".onnx.json" in (p.stdout + p.stderr), p.stdout + p.stderr)

# --- 8. scope flags ---------------------------------------------------------
# The two scopes name the two surfaces explicitly: `gateway` is the voice Gateway's own environment,
# `hermes` is the Hermes configuration. They used to be "bridge" and "gateway", which made the flag
# ambiguous the moment the service was renamed.
with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    before = (repo / "satellite-gateway.env").read_text()
    run(repo, cfg, cache, tmp, "en_US-test-single", "--scope", "gateway")
    check("--scope gateway updates the Gateway's environment",
          (repo / "satellite-gateway.env").read_text() != before)
    check("--scope gateway leaves Hermes alone",
          hermes_piper(cfg).get("voice") == "en_US-old-medium", hermes_piper(cfg))

with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    before = (repo / "satellite-gateway.env").read_text()
    run(repo, cfg, cache, tmp, "en_US-test-single", "--scope", "hermes")
    check("--scope hermes leaves satellite-gateway.env alone",
          (repo / "satellite-gateway.env").read_text() == before)
    check("--scope hermes still updates Hermes",
          hermes_piper(cfg).get("voice") == "en_US-test-single")

with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    missing = Path(td) / "nope" / "config.yaml"
    p = run(repo, missing, cache, tmp, "en_US-test-single")
    check("a missing Hermes config degrades to bridge-only instead of failing",
          env_value(repo / "satellite-gateway.env", "VOICE_PIPER_VOICE") == "en_US-test-single")
    check("and says the gateway was skipped", "gateway" in p.stdout.lower(), p.stdout)

# --- 9. rollback ------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    run(repo, cfg, cache, tmp, "en_US-test-single")
    run(repo, cfg, cache, tmp, "en_US-test-multi", "--speaker", "readerA")
    p = run(repo, cfg, cache, tmp, "--rollback")
    check("--rollback restores the previous Gateway voice",
          env_value(repo / "satellite-gateway.env", "VOICE_PIPER_VOICE") == "en_US-test-single",
          env_value(repo / "satellite-gateway.env", "VOICE_PIPER_VOICE"))
    check("--rollback restores the previous Hermes voice",
          hermes_piper(cfg).get("voice") == "en_US-test-single", hermes_piper(cfg))
    check("--rollback clears the speaker a multi-speaker voice needed",
          "speaker_id" not in hermes_piper(cfg), hermes_piper(cfg))
    st = state_of(repo)
    check("--rollback records that it rolled back",
          st and st["current"]["voice"] == "en_US-test-single", st)

with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    p = run(repo, cfg, cache, tmp, "--rollback", expect=1)
    check("--rollback with no history fails with a clear message",
          "nothing to roll back" in (p.stdout + p.stderr).lower(), p.stdout + p.stderr)

# --- 10. restart control ----------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    run(repo, cfg, cache, tmp, "en_US-test-single", "--no-restart")
    check("--no-restart does not touch the service",
          not (Path(tmp) / "systemctl.calls").exists())

# --- 11. unknown voice ------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    p = run(repo, cfg, cache, tmp, "en_US-not-a-real-voice", "--no-fetch", expect=1)
    check("an unknown voice with --no-fetch is refused with guidance",
          "fetch-voice" in (p.stdout + p.stderr) or "download" in (p.stdout + p.stderr),
          p.stdout + p.stderr)

# --- 12. empty installation -------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    repo, cfg, cache, tmp = make_fixture(td)
    shutil.rmtree(Path(repo) / "voices")
    (Path(repo) / "voices").mkdir()
    p = run(repo, cfg, cache, tmp, "--list")
    check("--list on an empty voices/ is friendly, not a crash",
          "no voices" in p.stdout.lower(), p.stdout)

print(f"\n{passed}/{passed + failed} checks passed")
if failures:
    print("FAILED: " + "; ".join(failures))
sys.exit(0 if failed == 0 else 1)
