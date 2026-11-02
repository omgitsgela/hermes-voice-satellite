#!/usr/bin/env python3
"""Workbench → real pairing TLS → real host HTTP config plane, with device reports as fixtures.

All listeners, identities, tokens, state and model files belong to this disposable test.
No installed service or real device is read or changed.
"""
import asyncio
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for package in ('installer', 'gateway', 'device-esp32', 'device-pi'):
    sys.path.insert(0, str(ROOT / 'packages' / package / 'src'))
sys.path.insert(0, str(ROOT / 'packages/installer/tests'))
from run_e2e_pairing_tests import make_certificate
from hermes_gateway.pairing_server import PairingConfig, PairingService, make_pairing_server
from hermes_installer.session import Session
from hermes_installer.identity import identity_code
from hermes_installer import catalogue
from satellite_gateway.config_plane import ConfigPlane
from satellite_gateway.announce import Announcer
from satellite_gateway import tts


class NetworkFleetTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_tls_proxy_lists_updates_checks_generation_and_assigns_voice(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            tls = base / 'registry-tls'
            tls.mkdir()
            cert, key, der = await asyncio.to_thread(make_certificate, tls)
            (base / 'enroll-token.txt').write_text('one two three four five')
            (base / 'announce-token.txt').write_text('test-only-token')
            plane = ConfigPlane(base / 'fleet.json')
            record = plane.ensure_device('satellite-a', metadata={'name':'Example satellite'})
            plane.report('satellite-a', generation=record['generation'], actual=plane.device_settings(record), health='healthy', error=None)
            async def dispatch(device_id, record):
                plane.report(device_id, generation=record['generation'], actual=plane.device_settings(record), health='healthy', error=None)
                return True
            import json
            (base / "device-credentials.json").write_text(json.dumps({"test-fingerprint":"never-connected"}))
            class Enrollment:
                credentials_file = base / "device-credentials.json"
                def register(self, app):
                    pass
            announcer = Announcer(sat=None, turn_started_at=lambda:None, config_plane=plane,
                connected_device_ids=lambda:['satellite-a'], config_dispatch=dispatch,
                enrollment=Enrollment(), host='127.0.0.1', port=0, timer_state_path=base / 'timers.json')
            announcer._load_token = lambda:'test-only-token'
            await announcer.start()
            server = make_pairing_server(PairingService(PairingConfig(base, 'tls://gateway.example:10702', min_interval_seconds=0)),
                bind='127.0.0.1', port=0, certfile=cert, keyfile=key)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            session = await asyncio.to_thread(Session, base, log_dir=base / "logs")
            port = announcer._runner.addresses[0][1]
            try:
                with patch.dict(os.environ, {'VOICE_CONFIG_LOCAL_URL':f'http://127.0.0.1:{port}', 'VOICE_ANNOUNCE_TOKEN':'test-only-token'}):
                    result = await asyncio.to_thread(session.fleet_authenticate, '127.0.0.1', server.server_port,
                        'one two three four five', identity_code(der))
                    self.assertTrue(result['ok'], result)
                    self.assertEqual({d['device_id'] for d in result['devices']}, {'satellite-a','never-connected'})
                    known = next(d for d in result['devices'] if d['device_id']=='satellite-a')
                    unreported = next(d for d in result['devices'] if d['device_id']=='never-connected')
                    self.assertEqual(known['name'], 'Example satellite')
                    self.assertEqual(unreported['health'], 'unknown')
                    self.assertFalse(unreported['converged'])
                    self.assertEqual(unreported['remote_settings'], [])
                    self.assertEqual(len(list(tls.glob('*.crt'))), 1, 'management must not enroll a certificate')
                    result = await asyncio.to_thread(session.fleet_update, 'satellite-a', {'playback_volume':42}, 1)
                    self.assertTrue(result['device']['converged'], result)
                    self.assertEqual(result['device']['reported']['playback_volume'], 42)
                    stale = await asyncio.to_thread(session.fleet_update, 'satellite-a', {'playback_volume':23}, 1)
                    self.assertFalse(stale['ok'])
                    self.assertIn('generation', stale['error'])
                    blocked = await asyncio.to_thread(session.fleet_update, 'satellite-a', {'tls_credentials':'bad'}, 2)
                    self.assertFalse(blocked['ok'])
                    (base / 'voice-one.onnx').write_bytes(b'fixture')
                    (base / 'voice-one.onnx.json').write_text('{}')
                    with patch.object(tts, 'VOICE_MODEL', base / 'default.onnx'), patch.object(catalogue, 'refresh_voices', return_value=(catalogue.Voice('voice-one','Voice one'),)):
                        voices = await asyncio.to_thread(session.fleet_voices)
                        self.assertIn('voice-one', voices['installed'])
                        assigned = await asyncio.to_thread(session.fleet_update, 'satellite-a', {'voice':'voice-one'}, 2)
                        self.assertTrue(assigned['device']['converged'], assigned)
                        self.assertEqual(plane.voice_for('satellite-a'), 'voice-one')
                        denied = await asyncio.to_thread(session.fleet_voices, 'voice-one', False)
                        self.assertFalse(denied['ok'])
                        # Exercise the actual download HTTP route and SHA verifier, not a fake
                        # "installed" response. Only upstream file bytes are offline fixtures.
                        import hashlib
                        from io import BytesIO
                        new_voice = 'xx_XX-voice-low'
                        files = {new_voice+'.onnx': b'model fixture', new_voice+'.onnx.json': b'{}'}
                        trust = {'revision':'a'*40, 'sha256':{
                            'xx/xx_XX/voice/low/'+name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}
                        original_open = catalogue.urllib.request.urlopen
                        def upstream(url, **kwargs):
                            if isinstance(url, str) and url.startswith('https://huggingface.co/'):
                                return BytesIO(files[url.rsplit('/',1)[-1]])
                            return original_open(url, **kwargs)
                        absent = await asyncio.to_thread(session.fleet_update, 'satellite-a', {'voice':new_voice}, 3)
                        self.assertFalse(absent['ok'])
                        refused = await asyncio.to_thread(session._fleet_request, '/config/voices', 'POST',
                            {'voice':new_voice, 'consent':False})
                        self.assertFalse(refused['ok'])
                        with patch.object(catalogue, 'voice_trust_snapshot', return_value=trust), patch.object(catalogue.urllib.request, 'urlopen', side_effect=upstream):
                            downloaded = await asyncio.to_thread(session.fleet_voices, new_voice, True)
                        self.assertIn(new_voice, downloaded['installed'])
                        self.assertEqual((base/(new_voice+'.onnx')).read_bytes(), b'model fixture')
                        selected = await asyncio.to_thread(session.fleet_update, 'satellite-a', {'voice':new_voice}, 3)
                        self.assertTrue(selected['device']['converged'])
                        self.assertEqual(plane.voice_for('satellite-a'), new_voice)
                        self.assertTrue(session.run_log_path.exists())
                    session.close()
                    # Wrong certificate identity refuses before the words leave the workbench.
                    wrong = await asyncio.to_thread(session.fleet_authenticate, '127.0.0.1', server.server_port,
                        'one two three four five', '0000-0000')
                    self.assertFalse(wrong['ok'])
                    self.assertIn('identity', wrong['error'])
            finally:
                session.close()
                await asyncio.to_thread(server.shutdown)
                server.server_close()
                thread.join(timeout=5)
                await announcer.stop()


if __name__ == '__main__':
    unittest.main(verbosity=2)
