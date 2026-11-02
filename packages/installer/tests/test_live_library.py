import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from hermes_installer import catalogue


class LiveLibraryTests(unittest.TestCase):
    def test_live_index_filters_and_keeps_bundled_defaults_offline(self):
        index = {'de_DE-test-low': {'name': 'test', 'language': {'code': 'de_DE'},
                 'quality': 'low', 'files': {'de/de_DE/test/low/de_DE-test-low.onnx':
                 {'size_bytes': 100, 'md5_digest': 'a' * 32}}}}
        result = catalogue.refresh_voices(get_json=lambda url: index)
        self.assertIn('de_DE-test-low', [v.id for v in result])
        self.assertTrue(next(v for v in result if v.default).sample_is_local)
        catalogue.refresh_voices(get_json=lambda url: (_ for _ in ()).throw(OSError('offline')))
        self.assertIn('en_US-hfc_male-medium', [v.id for v in catalogue.voices()])

    def test_free_live_wake_sources_remain_format_scoped(self):
        def source(url):
            if "micro-wake" in url:
                return {"tree": [{"path": "models/v2/hey_new.tflite", "type": "blob"}]}
            return {"assets": [{"name": "hey_new_v0.1.onnx", "browser_download_url": "https://example.org/hey_new.onnx", "digest": "sha256:" + "a" * 64}]}
        catalogue.refresh_wake_words(get_json=source)
        self.assertIn("hey_new", [w.id for w in catalogue.wake_words(catalogue.ONNX)])
        self.assertNotIn("hey_new_v0.1", [w.id for w in catalogue.wake_words(catalogue.TFLITE)])

    def test_both_voice_files_are_verified_and_corruption_is_repaired(self):
        from unittest.mock import patch
        from io import BytesIO
        payloads = {"xx_XX-test-low.onnx":b"model fixture", "xx_XX-test-low.onnx.json":b"{}"}
        hashes = {"xx/xx_XX/test/low/"+name:hashlib.sha256(data).hexdigest() for name,data in payloads.items()}
        trust = {"revision":"a"*40,"sha256":hashes}
        def response(url, **kwargs):
            return BytesIO(payloads[url.rsplit("/",1)[-1]])
        with tempfile.TemporaryDirectory() as directory, patch.object(catalogue, "voice_trust_snapshot", return_value=trust), patch.object(catalogue.urllib.request, "urlopen", side_effect=response) as network:
            destination = Path(directory)
            result = catalogue.download_voice_files("xx_XX-test-low", destination)
            self.assertTrue(result["sha256_verified"])
            self.assertEqual(network.call_count, 2)
            for name,data in payloads.items():
                self.assertEqual((destination/name).read_bytes(), data)
            (destination/"xx_XX-test-low.onnx").write_bytes(b"corrupted")
            catalogue.download_voice_files("xx_XX-test-low", destination)
            self.assertEqual(network.call_count, 3)
            self.assertEqual((destination/"xx_XX-test-low.onnx").read_bytes(), b"model fixture")
            with self.assertRaises(catalogue.CatalogueError):
                catalogue.download_voice_files("xx_XX-unknown-low", destination)
            payloads["xx_XX-test-low.onnx"] = b"tampered"
            (destination/"xx_XX-test-low.onnx").unlink()
            with self.assertRaises(catalogue.CatalogueError):
                catalogue.download_voice_files("xx_XX-test-low", destination)
            self.assertFalse((destination/"xx_XX-test-low.onnx").exists())

    def test_voice_download_refuses_unverified_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(catalogue.CatalogueError):
                catalogue.download('https://example.org/model', Path(directory) / 'model',
                                   require_hash=True, opener=lambda _: b'unsafe')
            self.assertFalse((Path(directory) / 'model').exists())


if __name__ == '__main__':
    unittest.main()
