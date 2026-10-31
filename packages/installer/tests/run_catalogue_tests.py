"""Offline checks for the catalogues: what a device is offered, and what a download is trusted for.

    python packages/installer/tests/run_catalogue_tests.py
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages" / "installer" / "src"))

from hermes_installer import catalogue  # noqa: E402
from hermes_installer.catalogue import CatalogueError  # noqa: E402


class WakeWords(unittest.TestCase):
    def test_a_device_is_only_offered_models_it_can_run(self):
        for entry in catalogue.wake_words(catalogue.TFLITE):
            self.assertEqual(entry.format, catalogue.TFLITE)
        for entry in catalogue.wake_words(catalogue.ONNX):
            self.assertEqual(entry.format, catalogue.ONNX)
        # The two formats are not interchangeable, so the sets must not be identical.
        self.assertNotEqual({e.id for e in catalogue.wake_words(catalogue.TFLITE)},
                            {e.id for e in catalogue.wake_words(catalogue.ONNX)})

    def test_the_default_phrase_comes_first_in_both_formats(self):
        for fmt in (catalogue.TFLITE, catalogue.ONNX):
            first = catalogue.wake_words(fmt)[0]
            self.assertEqual(first.word, "hey hermes", fmt)
            self.assertTrue(first.first_party)

    def test_search_narrows_by_phrase(self):
        found = catalogue.wake_words(catalogue.ONNX, query="jarvis")
        self.assertTrue(found)
        self.assertTrue(all("jarvis" in entry.word for entry in found))

    def test_an_unknown_format_is_refused(self):
        with self.assertRaises(CatalogueError):
            catalogue.wake_words("tensorflow_lite_v9")

    def test_find_needs_the_right_format(self):
        self.assertEqual(catalogue.find("okay_nabu", catalogue.TFLITE).word, "okay nabu")
        with self.assertRaises(CatalogueError):
            catalogue.find("okay_nabu", catalogue.ONNX)

    def test_entries_without_measurements_say_so_instead_of_guessing(self):
        entry = catalogue.find("hey_hermes", catalogue.TFLITE)
        self.assertIsNone(entry.recall)
        self.assertEqual(entry.metrics_line, "no published measurements")

    def test_the_caveat_exists_and_mentions_the_room(self):
        self.assertIn("not your room", catalogue.METRIC_CAVEAT)


class VoicesCatalogue(unittest.TestCase):
    def test_the_default_voice_is_the_female_hfc(self):
        voice = catalogue.default_voice()
        self.assertEqual(voice.id, "en_US-hfc_female-medium")
        self.assertTrue(voice.default)

    def test_both_hfc_voices_are_present_with_a_preview(self):
        ids = {voice.id for voice in catalogue.voices()}
        self.assertIn("en_US-hfc_male-medium", ids)
        self.assertIn("en_US-hfc_female-medium", ids)
        for voice in catalogue.voices():
            self.assertTrue(voice.sample_url, voice.id)

    def test_the_default_voices_are_audible_with_no_network(self):
        # Decision: the default voices bring a sample MP3, bundled, so the choice works offline.
        web = ROOT / "packages" / "installer" / "src" / "hermes_installer" / "web"
        for voice in catalogue.voices():
            if not voice.sample_url.startswith("/samples/"):
                continue
            bundled = web / voice.sample_url.lstrip("/")
            self.assertTrue(bundled.exists(), f"{voice.id} claims a bundled sample that is missing")
            self.assertGreater(bundled.stat().st_size, 10_000, voice.id)
            # A real MP3, not an empty file or an error page.
            self.assertTrue(bundled.read_bytes()[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3"))

    def test_every_voice_still_has_a_remote_sample_to_fall_back_to(self):
        for voice in catalogue.voices():
            if voice.sample_url.startswith("/samples/"):
                self.assertTrue(voice.remote_sample_url.startswith("https://"), voice.id)

    def test_exactly_one_default(self):
        self.assertEqual(sum(1 for v in catalogue.voices() if v.default), 1)


class Downloads(unittest.TestCase):
    def payload(self) -> bytes:
        return b"MODEL-BYTES" * 100

    def test_a_verified_download_is_kept(self):
        digest = hashlib.sha256(self.payload()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "model.tflite"
            path = catalogue.download("https://example/model", target, expect_sha256=digest,
                                      opener=lambda url: self.payload())
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), self.payload())

    def test_a_download_that_fails_its_hash_is_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "model.tflite"
            with self.assertRaises(CatalogueError):
                catalogue.download("https://example/model", target,
                                   expect_sha256="0" * 64,
                                   opener=lambda url: self.payload())
            self.assertFalse(target.exists(), "a model that failed verification was left on disk")
            self.assertFalse(list(Path(tmp).glob("*.part")), "a partial download was left behind")

    def test_an_empty_download_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CatalogueError):
                catalogue.download("https://example/model", Path(tmp) / "m.tflite",
                                   opener=lambda url: b"")


class LiveMeasurements(unittest.TestCase):
    def test_a_stubbed_api_is_parsed_into_figures(self):
        def get_json(url):
            if "search" in url:
                return {"results": [{"id": 2654, "word": "Hey Hermes", "download_count": 35}]}
            return {"id": 2654, "word": "Hey Hermes", "download_count": 35, "has_tflite": True,
                    "benchmarks": {"recall_pct": 74.2, "fa_per_hour": 0.87}}

        results = catalogue.refresh_measurements("hermes", get_json=get_json)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["recall"], 74.2)
        self.assertEqual(results[0]["fa_per_hour"], 0.87)

    def test_no_network_means_no_results_and_no_crash(self):
        def explode(url):
            raise OSError("no route to host")

        self.assertEqual(catalogue.refresh_measurements("hermes", get_json=explode), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
