"""What a person can choose: wake words and voices, with the numbers that describe them.

Three rules shape this module.

**The two wake-word formats are not interchangeable.** A Pi runs openWakeWord ONNX, an ESP32 runs
microWakeWord TFLite. A catalogue entry states its format, and a device is only offered entries it
can actually run. A phrase that exists in one format may not exist in the other.

**The metrics are shown, never summarised into a score.** Recall and false activations per hour are
measured by whoever trained the model, on their audio, in their conditions. A single "quality" number
would imply a precision nobody has, so the interface presents both figures with the caveat beside
them and lets a person decide.

**A download is verified or it is not trusted.** Every entry may carry a SHA-256; when it does, the
bytes are hashed and a mismatch is refused. A model flashed into a device is code-adjacent data, and
the installer is a supply chain for whoever uses it.

The snapshot below is checked in so the catalogue works with no network at all. It is deliberately
small: the free sources only, plus our own phrase. Refreshing from the live API is an enhancement,
never a requirement.
"""
from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

#: The formats, named as the firmware names them.
ONNX = "openwakeword_onnx"
TFLITE = "microwakeword_tflite"
FORMATS = (ONNX, TFLITE)


class CatalogueError(Exception):
    """A refusal worth showing a person: an unknown model, a bad download, a wrong format."""


@dataclass(frozen=True)
class WakeWord:
    id: str
    word: str
    format: str
    source_url: str
    #: Our own phrase, which we can ship and support, as opposed to a third party's.
    first_party: bool = False
    #: Measured by the trainer, on their audio. Absent rather than guessed.
    recall: float | None = None
    fa_per_hour: float | None = None
    sha256: str | None = None
    licence: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.format not in FORMATS:
            raise ValueError(f"{self.id}: unknown format {self.format!r}")

    @property
    def metrics_line(self) -> str:
        if self.recall is None or self.fa_per_hour is None:
            return "no published measurements"
        return (f"{self.recall:.0f}% recall, {self.fa_per_hour:.2f} false activations an hour, "
                f"measured by the trainer on their audio")


#: The checked-in snapshot. Free sources only.
WAKE_WORDS: tuple[WakeWord, ...] = (
    WakeWord(
        id="hey_hermes",
        word="hey hermes",
        format=TFLITE,
        source_url="firmware/esp32/models/hey_hermes.tflite",
        first_party=True,
        licence="shipped with this project",
        note="The default. Ours, so it can be supported.",
    ),
    WakeWord(
        id="hey_hermes",
        word="hey hermes",
        format=ONNX,
        source_url="provisioned by the satellite installer",
        first_party=True,
        licence="shipped with this project",
        note="The default for a Pi, provisioned by its installer.",
    ),
    WakeWord(
        id="okay_nabu",
        word="okay nabu",
        format=TFLITE,
        source_url=("https://github.com/esphome/micro-wake-word-models/raw/main/models/okay_nabu.tflite"),
        licence="Apache 2.0 (weights included)",
        note="Bundled in the firmware as a second option.",
    ),
    WakeWord(
        id="alexa",
        word="alexa",
        format=TFLITE,
        source_url="https://github.com/esphome/micro-wake-word-models/raw/main/models/alexa.tflite",
        licence="Apache 2.0 (weights included)",
        note="A third party's trademark: listed, not recommended.",
    ),
    WakeWord(
        id="hey_jarvis",
        word="hey jarvis",
        format=TFLITE,
        source_url=("https://github.com/esphome/micro-wake-word-models/raw/main/models/hey_jarvis.tflite"),
        licence="Apache 2.0 (weights included)",
        note="A third party's character: listed, not recommended.",
    ),
    WakeWord(
        id="alexa",
        word="alexa",
        format=ONNX,
        source_url=("https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/alexa_v0.1.onnx"),
        licence="Apache 2.0",
        note="A third party's trademark: listed, not recommended.",
    ),
    WakeWord(
        id="hey_jarvis",
        word="hey jarvis",
        format=ONNX,
        source_url=("https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
                    "hey_jarvis_v0.1.onnx"),
        licence="Apache 2.0",
        note="A third party's character: listed, not recommended.",
    ),
    WakeWord(
        id="hey_mycroft",
        word="hey mycroft",
        format=ONNX,
        source_url=("https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
                    "hey_mycroft_v0.1.onnx"),
        licence="Apache 2.0",
    ),
    WakeWord(
        id="hey_rhasspy",
        word="hey rhasspy",
        format=ONNX,
        source_url=("https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
                    "hey_rhasspy_v0.1.onnx"),
        licence="Apache 2.0",
    ),
)

#: The public API that serves measurements for the community library. Anonymous reads work for
#: search and detail; downloading a model from it needs an account, which is why the free GitHub
#: sources above are the download path.
OPENWAKEWORD_API = "https://openwakeword.com/api"

#: The caveat that must appear beside any measurements.
METRIC_CAVEAT = (
    "Recall is how often it heard the word in the trainer's test clips; false activations an hour is "
    "how often it fired when nobody said it. Both were measured on their audio, not your room, your "
    "voice or your microphone, so treat them as a rough filter for what to try, not a prediction."
)


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    language: str = "en_US"
    #: What the interface plays. A bundled clip for the default voices, so the choice is audible with
    #: no network; a published remote sample for the rest.
    sample_url: str = ""
    #: The published clip, always, so a bundled one can be replaced or fall back.
    remote_sample_url: str = ""
    size_mb: int = 60
    licence: str = ""
    default: bool = False
    files: dict = field(default_factory=dict)

    @property
    def sample_is_local(self) -> bool:
        return self.sample_url.startswith("/samples/") or self.sample_url.startswith("file://")


HFC_FEMALE_SAMPLE = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_female/medium/samples/"
    "speaker_0.mp3"
)
HFC_MALE_SAMPLE = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_male/medium/samples/"
    "speaker_0.mp3"
)

VOICES: tuple[Voice, ...] = (
    Voice(
        id="en_US-hfc_female-medium", name="HFC, female", default=True, size_mb=60,
        # Bundled: the default choice must be audible offline, and 87 KB is affordable.
        sample_url="/samples/en_US-hfc_female-medium.mp3",
        remote_sample_url=HFC_FEMALE_SAMPLE,
        licence="see the piper-voices model card",
    ),
    Voice(
        id="en_US-hfc_male-medium", name="HFC, male", size_mb=60,
        sample_url="/samples/en_US-hfc_male-medium.mp3",
        remote_sample_url=HFC_MALE_SAMPLE,
        licence="see the piper-voices model card",
    ),
    Voice(
        id="en_US-amy-medium", name="Amy", size_mb=60,
        sample_url=("https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/"
                    "medium/samples/speaker_0.mp3"),
        licence="see the piper-voices model card",
    ),
    Voice(
        id="en_US-lessac-medium", name="Lessac", size_mb=60,
        sample_url=("https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/"
                    "medium/samples/speaker_0.mp3"),
        licence="see the piper-voices model card",
    ),
    Voice(
        id="en_US-libritts-high", name="LibriTTS (many speakers)", size_mb=130,
        sample_url=("https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts/high/"
                    "samples/speaker_0.mp3"),
        licence="see the piper-voices model card",
        ),
)


def sample_path(voice: Voice) -> str:
    """The URL the interface should play: the bundled clip when there is one on disk."""
    if voice.sample_url.startswith("/samples/"):
        bundled = Path(__file__).resolve().parent / "web" / voice.sample_url.lstrip("/")
        if bundled.exists():
            return voice.sample_url
        return voice.remote_sample_url or voice.sample_url
    return voice.sample_url


_LIVE_VOICES: tuple[Voice, ...] = ()
PIPER_INDEX = "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json"
PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"


def _get_json(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read())


def refresh_voices(*, get_json=None) -> tuple[Voice, ...]:
    """Read the published library; a failed refresh never removes the offline snapshot."""
    global _LIVE_VOICES
    try:
        index = (get_json or _get_json)(PIPER_INDEX)
        entries = {v.id: v for v in VOICES}
        for key, item in index.items():
            if not isinstance(item, dict) or not item.get("files"):
                continue
            if not all(not path.startswith("/") and ".." not in path.split("/")
                       for path in item["files"]):
                continue
            old = entries.get(key)
            language = (item.get("language") or {}).get("code", key.split("-")[0])
            model = next((path for path in item["files"] if path.endswith(".onnx")), "")
            if not model:
                continue
            entries[key] = Voice(
                id=key, name=old.name if old else item.get("name", key), language=language,
                sample_url=old.sample_url if old else PIPER_BASE + model.rsplit("/", 1)[0] + "/samples/speaker_0.mp3",
                remote_sample_url=old.remote_sample_url if old else "",
                default=old.default if old else False,
                size_mb=round(sum(f.get("size_bytes", 0) for f in item["files"].values()) / 1_000_000),
                licence="see the piper-voices model card", files=item["files"],
            )
        _LIVE_VOICES = tuple(entries.values())
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return voices()


def voices() -> tuple[Voice, ...]:
    return _LIVE_VOICES or VOICES


def default_voice() -> Voice:
    return next(v for v in VOICES if v.default)


_LIVE_WAKE: tuple[WakeWord, ...] = ()


def refresh_wake_words(*, get_json=None) -> tuple[WakeWord, ...]:
    """Free microWakeWord and openWakeWord sources, never account-only downloads."""
    global _LIVE_WAKE
    fetch = get_json or _get_json
    entries = {(w.format, w.id): w for w in WAKE_WORDS}
    try:
        tree = fetch("https://api.github.com/repos/esphome/micro-wake-word-models/git/trees/main?recursive=1")
        for item in tree.get("tree", []):
            path = item.get("path", "")
            if item.get("type") != "blob" or not path.startswith("models/") or not path.endswith(".tflite"):
                continue
            name = Path(path).stem
            if (TFLITE, name) not in entries:
                entries[TFLITE, name] = WakeWord(name, name.replace("_", " "), TFLITE,
                    "https://github.com/esphome/micro-wake-word-models/raw/main/" + path,
                    licence="see upstream model card", note="Free source; download requires a trusted SHA-256.")
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        release = fetch("https://api.github.com/repos/dscripka/openWakeWord/releases/tags/v0.5.1")
        for item in release.get("assets", []):
            name = item.get("name", "")
            if not name.endswith(".onnx") or name.startswith(("embedding_", "melspectrogram", "silero_")):
                continue
            key = name.rsplit("_v", 1)[0] if "_v" in name else Path(name).stem
            digest = item.get("digest") or ""
            if (ONNX, key) not in entries:
                entries[ONNX, key] = WakeWord(key, key.replace("_", " "), ONNX,
                    item["browser_download_url"], sha256=digest.removeprefix("sha256:") if digest else None,
                    licence="see upstream model card")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    _LIVE_WAKE = tuple(entries.values())
    return wake_words()


def wake_words(fmt: str | None = None, query: str = "") -> tuple[WakeWord, ...]:
    """Entries a device of this format can run, optionally filtered by a search string.

    Ordered with our own phrase first, then published measurements from most to least sensitive,
    then the rest. The order is a starting point for a person, not a verdict.
    """
    if fmt is not None and fmt not in FORMATS:
        raise CatalogueError(f"unknown wake-word format {fmt!r}; expected one of {', '.join(FORMATS)}")
    needle = query.strip().lower()
    found = [
        entry for entry in (_LIVE_WAKE or WAKE_WORDS)
        if (fmt is None or entry.format == fmt) and (not needle or needle in entry.word.lower())
    ]
    return tuple(sorted(
        found,
        key=lambda e: (
            not e.first_party,
            -(e.recall or 0.0),
            e.fa_per_hour if e.fa_per_hour is not None else 99.0,
        ),
    ))


def find(model_id: str, fmt: str) -> WakeWord:
    for entry in (_LIVE_WAKE or WAKE_WORDS):
        if entry.id == model_id and entry.format == fmt:
            try:
                trusted = json.loads(Path(__file__).with_name("wake-checksums.json").read_text())
            except (OSError, ValueError):
                trusted = {}
            return replace(entry, sha256=entry.sha256 or trusted.get(entry.source_url))
    raise CatalogueError(f"no {fmt} model called {model_id!r} in the catalogue")


# -- downloads --------------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(
    url: str,
    destination: Path,
    *,
    expect_sha256: str | None = None,
    opener: Callable[[str], bytes] | None = None,
    timeout: float = 60.0,
    require_hash: bool = False,
) -> Path:
    """Fetch a model or voice to `destination`, verifying it when a hash is known.

    A file that fails the check is deleted rather than left in place: a half-verified model that is
    flashed into a device is worse than no model at all.
    """
    if require_hash and (not expect_sha256 or len(expect_sha256) != 64):
        raise CatalogueError("No published SHA-256 for this download; refused.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    if opener is None:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = response.read()
    else:
        payload = opener(url)
    if not payload:
        raise CatalogueError(f"{url} returned nothing")
    temporary.write_bytes(payload)
    if expect_sha256:
        actual = sha256_of(temporary)
        if actual.lower() != expect_sha256.lower():
            temporary.unlink(missing_ok=True)
            raise CatalogueError(
                f"{url} did not match its recorded hash, so it was discarded rather than used"
            )
    temporary.replace(destination)
    return destination


def voice_trust_snapshot() -> dict:
    return json.loads(Path(__file__).with_name("piper-checksums.json").read_text())


def download_voice_files(voice_id: str, directory: Path) -> dict:
    """Install any voice in the SHA-256 trust snapshot, alongside all existing voices.

    Library discovery is live. New files absent from this trust snapshot are refused rather than
    silently downgraded to the upstream index's MD5. Refresh the snapshot with the maintainer script.
    """
    import re
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", voice_id):
        raise CatalogueError("Invalid voice identifier")
    trust = voice_trust_snapshot()
    try:
        locale, rest = voice_id.split("-", 1)
        name, quality = rest.rsplit("-", 1)
    except ValueError:
        raise CatalogueError("A Piper voice ID must include locale, name and quality.") from None
    stem = f"{locale.split('_')[0]}/{locale}/{name}/{quality}/{voice_id}.onnx"
    paths = [stem, stem + ".json"]
    if not all(trust["sha256"].get(path) for path in paths):
        raise CatalogueError("This voice has no trusted SHA-256 snapshot yet; download refused.")
    downloaded = []
    for path in paths:
        target = directory / Path(path).name
        expected = trust["sha256"][path]
        if target.is_file() and sha256_of(target) == expected:
            continue
        url = f"https://huggingface.co/rhasspy/piper-voices/resolve/{trust['revision']}/{path}"
        download(url, target, expect_sha256=expected, require_hash=True)
        downloaded.append(str(target))
    return {"ok": True, "voice": voice_id, "downloaded": downloaded, "sha256_verified": True,
            "revision": trust["revision"]}


# -- live measurements ------------------------------------------------------------------------


def refresh_measurements(
    query: str,
    *,
    get_json: Callable[[str], dict] | None = None,
    limit: int = 10,
) -> list[dict]:
    """Look up community models for a phrase, using the API's public search and detail endpoints.

    Best effort by design: no network, no answer, or an unexpected shape all return an empty list,
    because the checked-in snapshot is what the installer actually depends on.
    """
    def default_get(url: str) -> dict:
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read())

    fetch = get_json or default_get
    try:
        listing = fetch(f"{OPENWAKEWORD_API}/models/search?q={urllib.parse.quote(query)}")
    except Exception:  # noqa: BLE001 - the catalogue works without this
        return []
    results = []
    for hit in (listing.get("results") or [])[:limit]:
        model_id = hit.get("id")
        if model_id is None:
            continue
        try:
            detail = fetch(f"{OPENWAKEWORD_API}/models/{model_id}")
        except Exception:  # noqa: BLE001
            continue
        benchmarks = detail.get("benchmarks") or {}
        results.append({
            "id": model_id,
            "word": detail.get("word") or hit.get("word") or "",
            "downloads": detail.get("download_count") or hit.get("download_count") or 0,
            "recall": benchmarks.get("recall_pct"),
            "fa_per_hour": benchmarks.get("fa_per_hour"),
            "tflite": bool(detail.get("has_tflite")),
        })
    return results
