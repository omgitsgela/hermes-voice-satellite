import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from hermes_installer.session import Session
from hermes_gateway.pairing_server import PairingConfig, PairingService, PairingRefused


class FleetTests(unittest.TestCase):
    def test_words_authenticate_management_without_enrolling(self):
        with tempfile.TemporaryDirectory() as directory:
            config = PairingConfig(Path(directory), 'tls://gateway.example:10702', min_interval_seconds=0)
            config.token_path().write_text('one two three four five')
            service = PairingService(config)
            with self.assertRaises(PairingRefused):
                service.manage('wrong words', '/config/devices', 'GET', {})
            with self.assertRaises(PairingRefused):
                service.manage('one two three four five', '/announce', 'POST', {})
            with patch.object(service, '_config_request', return_value={'ok': True, 'devices': []}) as remote:
                result = service.manage('one two three four five', '/config/devices', 'GET', {})
                self.assertEqual(result['devices'], [])
                remote.assert_called_once_with('/config/devices', 'GET', {})

    def test_console_downloads_then_assigns_the_chosen_satellites_voice(self):
        from hermes_installer.cli import manage_fleet, ScriptedPrompt
        with tempfile.TemporaryDirectory() as directory:
            session = Session(log_dir=Path(directory))
            device = {"device_id":"example", "name":"Example", "metadata":{}, "health":"healthy",
                      "connected":True,"desired":{"voice":"default"},"reported":{},
                      "generation":1,"converged":False}
            voice = {"id":"en_US-hfc_female-medium","name":"Voice","language":"en_US","size_mb":63,"installed":False}
            calls = []
            def remote(path, method="GET", payload=None):
                calls.append((path, method, payload))
                if path == "/config/schema":
                    return {"ok":True,"device_settings":{"voice":{"type":"string"}}}
                if path == "/config/voices":
                    if method == "POST":
                        self.assertIs(payload["consent"], True)
                        voice["installed"] = True
                    return {"ok":True,"voices":[voice],"installed":[voice["id"]] if voice["installed"] else []}
                if method == "PUT":
                    device["desired"].update(payload["settings"])
                    device["gateway_reported"] = dict(payload["settings"])
                    device["generation"] += 1
                return {"ok":True,"devices":[device],"device":device}
            answers = ScriptedPrompt(["gateway.example", "", "y", "private words", "1", "voice", "", "1", "y", "y", "Finish"])
            lines = []
            try:
                with patch.object(session, "_fleet_request", side_effect=remote), patch.object(session, "inspect_gateway", return_value={"ok":True,"identity_code":"ABCD"}):
                    self.assertEqual(manage_fleet(session, answers, lines.append), 0)
                self.assertEqual(device["desired"]["voice"], voice["id"])
                self.assertTrue(any(path.endswith("/example/desired") and method=="PUT" for path,method,_ in calls))
                self.assertTrue(any("Satellite Gateway applied" in line and voice["id"] in line for line in lines))
                self.assertIn("remote_voice", session.run_log_path.read_text())
            finally:
                session.close()

    def test_remote_voice_download_requires_installed_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session(log_dir=Path(directory))
            try:
                with patch.object(session, "_fleet_request", side_effect=[{"ok":True}, {"ok":True,"installed":[],"voices":[]}]):
                    result = session.fleet_voices("en_US-hfc_female-medium", True)
                    self.assertFalse(result["ok"])
                    self.assertIn("readback", result["error"])
            finally:
                session.close()

    def test_dry_run_cannot_write_remote_state_or_download_voices(self):
        session = Session(dry_run=True)
        try:
            with patch.object(session, "_fleet_request") as request:
                self.assertFalse(session.fleet_update("example", {"playback_volume":42}, 1)["ok"])
                self.assertFalse(session.fleet_voices("en_US-hfc_female-medium", True)["ok"])
                request.assert_not_called()
        finally:
            session.close()

    def test_console_fleet_route_applies_setting_and_reports_pending(self):
        from hermes_installer.cli import manage_fleet, ScriptedPrompt
        session = Session(dry_run=False)
        device = {"device_id":"example", "name":"Example", "metadata":{}, "health":"healthy",
                  "connected":False, "desired":{"playback_volume":55}, "reported":{},
                  "generation":1, "converged":False}
        calls = []
        def remote(path, method="GET", payload=None):
            calls.append((path, method, payload))
            if path == "/config/schema":
                return {"ok":True,"device_settings":{"playback_volume":{"type":"integer"}}}
            if method == "PUT":
                device["desired"].update(payload["settings"])
                device["generation"] += 1
            return {"ok":True,"devices":[device],"device":device}
        answers = ScriptedPrompt(["gateway.example", "", "y", "private words", "1", "playback_volume", "42", "y", "Finish"])
        lines = []
        try:
            with patch.object(session, "_fleet_request", side_effect=remote), patch.object(session, "inspect_gateway", return_value={"ok":True,"identity_code":"ABCD"}):
                self.assertEqual(manage_fleet(session, answers, lines.append), 0)
            self.assertEqual(device["desired"]["playback_volume"], 42)
            self.assertTrue(any("pending device report" in line for line in lines))
            self.assertTrue(any("Cable/card only" in line and "wifi" in line for line in lines))
        finally:
            session.close()

    def test_profile_remote_flags_enforced_before_write_and_readback(self):
        session = Session(dry_run=False)
        try:
            profile = next(p for p in session.registry.all() if p.remote_settings)
            device = {'device_id': 'example', 'metadata': {'profile_id': profile.id},
                      'desired': {'playback_volume': 55}, 'generation': 2,
                      'health': 'healthy', 'converged': False}
            session._fleet = {'host': 'gateway.example', 'port': 10704, 'code': 'ABCD', 'words': 'private'}
            calls = []
            def remote(path, method='GET', payload=None):
                calls.append((path, method, payload))
                if path == '/config/schema':
                    return {'ok': True, 'device_settings': {'playback_volume': {'type': 'integer'}}}
                return {'ok': True, 'devices': [device], 'device': device}
            with patch.object(session, '_fleet_request', side_effect=remote):
                fleet = session.fleet_devices()
                self.assertIn('playback_volume', fleet['devices'][0]['remote_settings'])
                self.assertIn('wifi', ' '.join(fleet['devices'][0]['cable_settings']))
                refused = session.fleet_update('example', {'wifi_ssid': 'unsafe'}, 2)
                self.assertFalse(refused['ok'])
                result = session.fleet_update('example', {'playback_volume': 33}, 2)
                self.assertTrue(result['ok'])
                self.assertFalse(result['device']['converged'])
                self.assertEqual(calls[-1][0], '/config/devices/example')
                self.assertTrue(any(method == 'PUT' for _, method, _ in calls))
        finally:
            session.close()


if __name__ == '__main__':
    unittest.main()
