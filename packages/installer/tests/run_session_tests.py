"""Session and HTTP interface checks, against the real server on loopback.

    python packages/installer/tests/run_session_tests.py
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for package in ("installer", "gateway", "device-esp32", "device-pi"):
    sys.path.insert(0, str(ROOT / "packages" / package / "src"))

import hermes_device_esp32  # noqa: E402
import hermes_device_pi  # noqa: E402
from hermes_installer.registry import DeviceRegistry  # noqa: E402
from hermes_installer.secrets import CredentialStore  # noqa: E402
from hermes_installer.server import Handler  # noqa: E402
from hermes_installer.session import Session  # noqa: E402


class MemoryBackend:
    name = "memory"

    def __init__(self):
        self.data = {}

    def save(self, account, secret):
        self.data[account] = secret

    def load(self, account):
        return self.data.get(account)

    def forget(self, account):
        self.data.pop(account, None)


def build_session(dry_run=True):
    registry = DeviceRegistry()
    registry.register(hermes_device_esp32.PROFILE, hermes_device_esp32.DRIVER)
    registry.register(hermes_device_pi.PROFILE, hermes_device_pi.DRIVER)
    return Session(ROOT, registry=registry, store=CredentialStore(backend=MemoryBackend()),
                   dry_run=dry_run)


class SessionBehaviour(unittest.TestCase):
    def setUp(self):
        self.session = build_session()

    def test_status_explains_what_it_found(self):
        status = self.session.status()
        self.assertIn(status["hermes"], ("desktop", "service", "none"))
        self.assertTrue(status["hermes_explained"])

    def test_both_devices_are_offered_with_their_capabilities(self):
        devices = {d["id"]: d for d in self.session.status()["devices"]}
        self.assertEqual(set(devices), {"esp32-xvf3800", "pi-reSpeaker-2mic"})
        self.assertIn("playback_volume", devices["esp32-xvf3800"]["remote_settings"])
        self.assertIn("wifi", devices["esp32-xvf3800"]["cable_settings"])

    def test_a_loopback_dial_address_is_refused(self):
        result = self.session.install_gateway("127.0.0.1")
        self.assertFalse(result["ok"])
        self.assertIn("cannot be reached by a device", result["error"])

    def test_the_gateway_plan_matches_the_script_flags(self):
        result = self.session.install_gateway("192.0.2.10")
        self.assertTrue(result["ok"])
        argv = result["commands"][0]
        self.assertIn("--device-reachable-as", argv)
        self.assertEqual(argv[argv.index("--device-reachable-as") + 1], "192.0.2.10")

    def test_a_dry_run_never_executes(self):
        result = self.session.install_gateway("192.0.2.10")
        self.assertTrue(result["dry_run"])

    def test_flash_previews_the_command_for_a_cable_device(self):
        result = self.session.flash(
            "esp32-xvf3800", device_name="kitchen",
            dial_address="tls://192.0.2.10:10702", port="/dev/ttyACM0",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertIn("install-esp32.sh", result["commands"][0]["argv"][0])

    def test_flash_without_a_port_refuses_cleanly(self):
        result = self.session.flash("esp32-xvf3800", device_name="k",
                                    dial_address="tls://192.0.2.10:10702")
        self.assertFalse(result["ok"])
        self.assertIn("USB port", result["summary"])

    def test_a_platform_that_cannot_do_it_is_an_answer_not_a_traceback(self):
        import sys
        from unittest import mock

        real = sys.platform
        with mock.patch.object(sys, "platform", "win32"):
            result = self.session.flash("esp32-xvf3800", device_name="k",
                                        dial_address="tls://192.0.2.10:10702",
                                        port="COM3", dry_run=False)
        self.assertEqual(sys.platform, real)
        self.assertFalse(result["ok"])
        self.assertIn("Linux or macOS", result["error"])

    def test_unknown_device_lists_the_known_ones(self):
        result = self.session.flash("nope", device_name="k")
        self.assertFalse(result["ok"])
        self.assertIn("esp32-xvf3800", result["error"])

    def test_a_preview_session_cannot_be_talked_into_a_real_run(self):
        # The session's dry-run is a ceiling. A client sending dry_run:false must not beat it, so
        # this intercepts the one function that would actually execute.
        import hermes_gateway

        seen = {}
        original = hermes_gateway.run_commands

        def spy(commands, dry_run=False, executor=None):
            seen["dry_run"] = dry_run
            return True, "preview"

        hermes_gateway.run_commands = spy
        try:
            build_session(dry_run=True).install_gateway("192.0.2.10", dry_run=False)
        finally:
            hermes_gateway.run_commands = original
        self.assertIs(seen.get("dry_run"), True, "a preview session asked to execute for real")

    def test_pairing_without_a_confirmed_code_is_refused(self):
        result = self.session.pair("192.0.2.10", 10702, "a b c d e", "kitchen")
        self.assertFalse(result["ok"])
        self.assertIn("confirm the gateway's identity code", result["error"])
        # And nothing was remembered.
        self.assertIsNone(self.session.store.recall("192.0.2.10", 10702))


class HttpInterface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        session = build_session()
        cls.token = "test-token-not-a-secret"
        handler = type("Bound", (Handler,), {"session": session, "token": cls.token})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get_json(self, path):
        request = urllib.request.Request(
            self.base + path, headers={"X-Installer-Token": self.token}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def post_json(self, path, payload, token=None, content_type="application/json"):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": content_type,
                     "X-Installer-Token": self.token if token is None else token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            return json.loads(error.read())

    def test_the_page_is_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            body = response.read().decode()
        self.assertIn("Set up Hermes voice", body)
        self.assertIn("identity code", body)
        # The first screen must not tell a person they can set up a gateway they cannot.
        self.assertIn("Hermes was not found on this computer", body)

    def test_status_is_json(self):
        status = self.get_json("/api/status")
        self.assertEqual(len(status["devices"]), 2)

    def test_gateway_preview_over_http(self):
        result = self.post_json("/api/gateway", {"dial_address": "192.0.2.10", "dry_run": True})
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])

    def test_a_bad_address_is_a_clean_error(self):
        result = self.post_json("/api/gateway", {"dial_address": "127.0.0.1"})
        self.assertFalse(result["ok"])
        self.assertIn("cannot be reached", result["error"])

    def test_flash_over_http(self):
        result = self.post_json("/api/flash", {
            "device_id": "pi-reSpeaker-2mic", "device_name": "bedroom",
            "dial_address": "tls://192.0.2.10:10702", "card_mount": "/media/user/rootfs",
            "dry_run": True,
        })
        self.assertTrue(result["ok"])
        # The card writer, not the on-device installer: install-pi.sh writes to the running
        # machine's /etc and would have installed services on the workbench.
        self.assertIn("deploy-to-card.sh", result["commands"][0]["argv"][0])

    def test_a_request_without_the_token_is_refused(self):
        # Binding to loopback is not authorisation: any page in the same browser can POST here.
        result = self.post_json("/api/gateway", {"dial_address": "192.0.2.10"}, token="")
        self.assertEqual(result.get("error"), "missing or wrong installer token")

    def test_a_form_encoded_body_is_refused(self):
        # text/plain and form encodings can be sent cross-origin without a preflight.
        result = self.post_json("/api/gateway", {"dial_address": "192.0.2.10"},
                                content_type="text/plain")
        self.assertEqual(result.get("error"), "expected application/json")

    def test_the_served_page_carries_the_token_for_its_own_scripts(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            body = response.read().decode()
        self.assertIn(f'window.INSTALLER_TOKEN="{self.token}"', body)

    def test_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/nope", timeout=10)
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
