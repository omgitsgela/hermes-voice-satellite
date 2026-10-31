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
from dataclasses import dataclass, field
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
    #: Where the published preview lives. Small, and played before anything large is downloaded.
    sample_url: str = ""
    size_mb: int = 60
    licence: str = ""
    default: bool = False

    @property
    def sample_is_local(self) -> bool:
        return self.sample_url.startswith("file://")


VOICES: tuple[Voice, ...] = (
    Voice(
        id="en_US-hfc_female-medium", name="HFC, female", default=True, size_mb=60,
        sample_url=("https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_female/"
                    "medium/samples/speaker_0.mp3"),
        licence="see the piper-voices model card",
    ),
    Voice(
        id="en_US-hfc_male-medium", name="HFC, male", size_mb=60,
        sample_url=("https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_male/"
                    "medium/samples/speaker_0.mp3"),
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


def voices() -> tuple[Voice, ...]:
    return VOICES


def default_voice() -> Voice:
    return next(v for v in VOICES if v.default)


def wake_words(fmt: str | None = None, query: str = "") -> tuple[WakeWord, ...]:
    """Entries a device of this format can run, optionally filtered by a search string.

    Ordered with our own phrase first, then published measurements from most to least sensitive,
    then the rest. The order is a starting point for a person, not a verdict.
    """
    if fmt is not None and fmt not in FORMATS:
        raise CatalogueError(f"unknown wake-word format {fmt!r}; expected one of {', '.join(FORMATS)}")
    needle = query.strip().lower()
    found = [
        entry for entry in WAKE_WORDS
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
    for entry in WAKE_WORDS:
        if entry.id == model_id and entry.format == fmt:
            return entry
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
) -> Path:
    """Fetch a model or voice to `destination`, verifying it when a hash is known.

    A file that fails the check is deleted rather than left in place: a half-verified model that is
    flashed into a device is worse than no model at all.
    """
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
