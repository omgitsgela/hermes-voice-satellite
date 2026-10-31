#!/usr/bin/env python3
"""External Piper voices: the audited fetch path for voices the Piper catalog does not know.

`piper.download_voices` only accepts `<language>-<name>-<quality>` names published in the
HuggingFace voices release. Voices hosted elsewhere ("clean100" from brycebeattie.com) must be
fetched by a path that pins the exact bytes and enforces the audio contract, or an installation
silently downloads whatever that URL happens to serve on the day.

This pins scripts/fetch-voice.py:

1. Curated entries carry url + sha256 per file; the downloader REFUSES a hash mismatch and
   leaves no partial file in voices/.
2. The voice json must declare the 22,050 Hz contract; a wrong-rate voice is rejected, not installed.
3. Re-running is idempotent: existing files are hash-checked, not refetched.
4. --is-external is what the installer branches on (catalog voice -> piper downloader,
   curated voice -> this script).

    .venv/bin/python scripts/test_fetch_voice.py
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "fetch-voice.py"
PY = sys.executable

failures: list[str] = []
checks = 0


def check(label: str, condition) -> None:
    global checks
    checks += 1
    if not condition:
        failures.append(label)


def load_module():
    spec = importlib.util.spec_from_file_location("fetch_voice", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(SCRIPT), *args], capture_output=True, text=True, timeout=300)


check("scripts/fetch-voice.py exists", SCRIPT.is_file())
if not SCRIPT.is_file():
    print(f"fetch-voice: {checks - len(failures)}/{checks} passed")
    for f in failures:
        print(f"  FAIL: {f}")
    raise SystemExit(1)

mod = load_module()

# --- 1. the curated catalog is complete and pinned ---------------------------------
ext = mod.EXTERNAL_VOICES
check("clean100 is a curated external voice", "clean100" in ext)
for name, entry in ext.items():
    for fname, spec in entry["files"].items():
        check(f"{name}/{fname} has a url", str(spec.get("url", "")).startswith("https://"))
        sha = str(spec.get("sha256", ""))
        check(f"{name}/{fname} has a 64-hex sha256 pin",
              len(sha) == 64 and all(c in "0123456789abcdef" for c in sha))
    check(f"{name} names its license terms", bool(entry.get("license")))

c100 = ext.get("clean100", {}).get("files", {})
check("clean100 installs under the names satellite_gateway/config.py builds",
      "clean100.onnx" in c100 and "clean100.onnx.json" in c100)

# --- 2/3. behaviour: happy path, hash refusal, idempotency, rate refusal ----------
with tempfile.TemporaryDirectory() as raw_td:
    td = Path(raw_td)
    good_onnx = td / "good.onnx"
    good_onnx.write_bytes(b"model-bytes")
    good_json = td / "good.onnx.json"
    good_json.write_text(json.dumps({"audio": {"sample_rate": 22050, "quality": "test"},
                                     "num_speakers": 2, "speaker_id_map": {"7": 0}}))

    def fake_catalog(onnx_sha=None, rate=22050):
        jbytes = json.dumps({"audio": {"sample_rate": rate, "quality": "test"},
                             "num_speakers": 2, "speaker_id_map": {"7": 0}}).encode()
        jpath = td / "served.onnx.json"
        jpath.write_bytes(jbytes)
        return {
            "fakevoice": {
                "license": "test",
                "files": {
                    "fakevoice.onnx": {"url": good_onnx.resolve().as_uri(),
                                       "sha256": onnx_sha or hashlib.sha256(good_onnx.read_bytes()).hexdigest()},
                    "fakevoice.onnx.json": {"url": jpath.resolve().as_uri(),
                                            "sha256": hashlib.sha256(jbytes).hexdigest()},
                },
            }
        }

    def run_with(catalog, *args):
        """Drive the real script with a patched catalog (local file:// URLs)."""
        code = (f"import importlib.util, sys;"
                f"spec=importlib.util.spec_from_file_location('fv', {str(SCRIPT)!r});"
                f"m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
                f"m.EXTERNAL_VOICES={catalog!r};"
                f"sys.argv=['fetch-voice.py'] + {list(args)!r};"
                f"raise SystemExit(m.main())")
        return subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=300)

    voices = td / "voices"

    # happy path installs both files under the curated names
    r = run_with(fake_catalog(), "fakevoice", "--download-dir", str(voices))
    check("fetch installs the voice files", r.returncode == 0
          and (voices / "fakevoice.onnx").is_file() and (voices / "fakevoice.onnx.json").is_file())

    # idempotency: a second run validates without refetching
    r2 = run_with(fake_catalog(), "fakevoice", "--download-dir", str(voices))
    check("re-running validates instead of refetching", r2.returncode == 0
          and "already installed" in r2.stdout)

    # hash mismatch: refused, and NOTHING is left installed
    tampered = td / "tampered"
    tampered.mkdir()
    bad = tampered / "bad.onnx"
    bad.write_bytes(b"model-bytes-tampered")
    bad_catalog = fake_catalog(onnx_sha=hashlib.sha256(b"model-bytes").hexdigest())
    bad_catalog["fakevoice"]["files"]["fakevoice.onnx"]["url"] = bad.resolve().as_uri()
    voices2 = td / "voices2"
    r3 = run_with(bad_catalog, "fakevoice", "--download-dir", str(voices2))
    check("a sha256 mismatch is refused", r3.returncode != 0 and "sha256" in (r3.stderr + r3.stdout).lower())
    check("a refused download leaves no installed voice",
          not (voices2 / "fakevoice.onnx").exists() and not list(voices2.glob("*.part")))

    # wrong sample rate: the 22,050 Hz contract is enforced before install
    voices3 = td / "voices3"
    r4 = run_with(fake_catalog(rate=16000), "fakevoice", "--download-dir", str(voices3))
    check("a non-22050 Hz voice is rejected", r4.returncode != 0
          and "22050" in (r4.stderr + r4.stdout))
    check("a rejected voice leaves no installed voice", not (voices3 / "fakevoice.onnx").exists())

    # unknown name fails loudly (this is the installer's guard against typos)
    r5 = run_with(fake_catalog(), "not-curated", "--download-dir", str(voices))
    check("an unknown name fails with guidance", r5.returncode != 0
          and "piper.download_voices" in r5.stderr)

# --- 4. the installer branches on --is-external ------------------------------------
r = run_script("--is-external", "clean100")
check("--is-external says yes for clean100", r.returncode == 0)
r = run_script("--is-external", "en_US-lessac-medium")
check("--is-external says no for a catalog voice", r.returncode != 0)

installer = (ROOT / "scripts" / "install-gateway.sh").read_text()
check("install-gateway.sh branches on fetch-voice for curated voices", "fetch-voice" in installer)

print(f"fetch-voice: {checks - len(failures)}/{checks} passed")
for f in failures:
    print(f"  FAIL: {f}")
raise SystemExit(1 if failures else 0)
