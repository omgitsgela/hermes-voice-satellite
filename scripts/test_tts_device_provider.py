#!/usr/bin/env python3
"""The production speech entry points select the destination device's voice."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from satellite_gateway import tts, announce
from satellite_gateway.satellite import SatelliteClient


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_and_wav_use_each_device_voice_without_shared_mutation(self):
        calls = []
        class Provider:
            def chunks(self, text, voice=None):
                calls.append(('chunks', text, voice))
                yield bytes(8), 22050, 2, 1
            def wav(self, text, path, voice=None):
                calls.append(('wav', text, voice))
                import wave
                with wave.open(str(path), "wb") as output:
                    output.setparams((1,2,22050,0,"NONE","not compressed"))
                    output.writeframes(bytes(8))
        resolver = lambda device: {'satellite-a': 'voice-a', 'satellite-b': 'voice-b'}.get(device)
        async def send(event):
            pass
        with tempfile.TemporaryDirectory() as directory, patch.object(tts, 'PROVIDER', Provider()), patch.object(tts, '_voice_resolver', resolver):
            for device in ('satellite-a', 'satellite-b'):
                sat = SatelliteClient('example.org', 10700)
                sat.device_id = device
                sat.send = send
                async with sat.tts_stream(pace=0) as stream:
                    await stream.say('hello')
                tts.synthesize_to_wav('hello', Path(directory) / 'speech.wav', device_id=device)
        self.assertEqual(calls, [('chunks', 'hello', 'voice-a'), ('wav', 'hello', 'voice-a'),
                                 ('chunks', 'hello', 'voice-b'), ('wav', 'hello', 'voice-b')])

    async def test_non_native_piper_voice_wav_is_converted_for_announcement_tones(self):
        import wave
        class Provider:
            def wav(self, text, path, voice=None):
                with wave.open(str(path), "wb") as output:
                    output.setparams((1,2,16000,0,"NONE","not compressed"))
                    output.writeframes(bytes(3200))
        with tempfile.TemporaryDirectory() as directory, patch.object(tts, "PROVIDER", Provider()):
            target = Path(directory) / "speech.wav"
            tts.synthesize_to_wav("hello", target)
            with wave.open(str(target), "rb") as source:
                self.assertEqual((source.getframerate(),source.getsampwidth(),source.getnchannels()), tts.SND_FORMAT)
                self.assertGreater(source.getnframes(), 1600)

    async def test_announcement_files_do_not_overwrite_another_devices_voice(self):
        item = announce.Announcement(id="test", text="hello", sound="none", source="test", key="test", received_at=0)
        announcer = announce.Announcer.__new__(announce.Announcer)
        def synthesize(text, path, *, device_id=None):
            path.write_text(device_id or "default")
        with tempfile.TemporaryDirectory() as directory, patch.object(announce, "AUDIO_OUT", Path(directory)), patch.object(tts, "synthesize_to_wav", side_effect=synthesize):
            default = announcer._build_wav(item)
            first = announcer._build_wav(item, "satellite-a")
            second = announcer._build_wav(item, "satellite-b")
            self.assertEqual(len({default, first, second}), 3)
            self.assertEqual(default.read_text(), "default")
            self.assertEqual(first.read_text(), "satellite-a")
            self.assertEqual(second.read_text(), "satellite-b")

    async def test_piper_model_cache_is_keyed_by_voice_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for voice in ('voice-a', 'voice-b'):
                (base / (voice + '.onnx')).write_bytes(b'model')
                (base / (voice + '.onnx.json')).write_text('{}')
            with patch.object(tts, 'VOICE_MODEL', base / 'default.onnx'):
                self.assertEqual(tts.voice_paths('voice-a')[0], base / 'voice-a.onnx')
                self.assertEqual(tts.voice_paths('voice-b')[0], base / 'voice-b.onnx')
                with self.assertRaises(ValueError):
                    tts.voice_paths('../escape')
                with self.assertRaises(ValueError):
                    tts.voice_paths('not-installed')


if __name__ == '__main__':
    unittest.main()
