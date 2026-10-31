"""Offline checks for the console flow: same order and same decisions as the interface.

The GUI and the console must not drift, so this drives the console with scripted answers against a
stand-in session and asserts what the flow asked for, in order.

    python packages/installer/tests/run_cli_tests.py
"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for package in ("installer", "gateway", "device-esp32", "device-pi"):
    sys.path.insert(0, str(ROOT / "packages" / package / "src"))

from hermes_installer.cli import ScriptedPrompt, run  # noqa: E402


DEVICES = [
    {
        "id": "esp32-xvf3800", "name": "ESP32-S3 with reSpeaker XVF3800", "vendor": "Seeed",
        "flashing": "usb_serial", "needs_card_reader": False, "voice": "local_piper",
        "wake_format": "microwakeword_tflite", "wake_default": "hey_hermes",
        "description": "", "identify": "Look for XIAO ESP32S3",
        "remote_settings": ["playback_volume"], "cable_settings": ["wifi"],
    },
    {
        "id": "pi-reSpeaker-2mic", "name": "Raspberry Pi Zero 2 W", "vendor": "Raspberry Pi",
        "flashing": "sd_card", "needs_card_reader": True, "voice": "local_piper",
        "wake_format": "openwakeword_onnx", "wake_default": "hey_hermes",
        "description": "", "identify": "A green board",
        "remote_settings": ["playback_volume"], "cable_settings": ["wifi"],
    },
]


class FakeSession:
    dry_run = True
    repo_root = ROOT

    def __init__(self, hermes="desktop"):
        self.hermes = hermes
        self.calls = []

    def platform_report(self):
        return {"name": "linux", "can_flash": True, "can_supervise": True, "elevated": False,
                "notes": []}

    def status(self):
        return {
            "hermes": self.hermes,
            "hermes_explained": "Hermes found",
            "devices": DEVICES,
            "dry_run": True,
            "platform": self.platform_report(),
        }

    def preflight(self, profile_id, **fields):
        self.calls.append(("preflight", profile_id))
        return {"ok": True, "problems": []}

    def usb_ports(self):
        return [{"device": "/dev/ttyACM0", "description": "USB JTAG"}]

    def card_mounts(self):
        return [{"mount": "/media/user/rootfs"}]

    def device_identity(self, profile_id, port=None):
        self.calls.append(("identity", profile_id, port))
        return {"ok": True, "device_id": "esp32-0011223344"}

    def inspect_gateway(self, host, port):
        self.calls.append(("inspect", host, port))
        return {"ok": True, "identity_code": "XK7F-9Q2M"}

    def pair(self, host, port, words, name, **kwargs):
        self.calls.append(("pair", host, port, name, kwargs.get("device_id")))
        return {"ok": True, "identity_code": "XK7F-9Q2M", "remembered_in": None,
                "dial_address": "tls://gateway.example:10702", "certs_dir": "/tmp/certs"}

    def install_device(self, profile_id, **fields):
        self.calls.append(("install", profile_id, fields))
        return {"ok": True, "summary": "installed", "commands": [], "dry_run": True}

    def install_gateway(self, *args, **kwargs):  # pragma: no cover - not exercised here
        raise AssertionError("the gateway install should not run on the device route")


def drive(session, answers):
    prompt = ScriptedPrompt(answers)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = run(session, prompt, out=lambda *a, **k: None)
    return code, prompt, buffer.getvalue()


class DeviceRoute(unittest.TestCase):
    def test_a_workbench_without_hermes_can_still_set_up_a_device(self):
        session = FakeSession(hermes="none")
        # Device choice, port choice, address, port, code match, words, name, remember, wifi.
        code, prompt, _ = drive(session, ["1", "1", "gateway.example", "10704", "y",
                                          "amber lantern copper thrush meadow", "kitchen",
                                          "n", ""])  # no route question: Hermes is absent
        self.assertEqual(code, 0)
        self.assertTrue(any(call[0] == "install" for call in session.calls))

    def test_the_device_identity_is_read_before_pairing(self):
        session = FakeSession()
        drive(session, ["2", "1", "1", "gateway.example", "10704", "y",
                        "amber lantern copper thrush meadow", "kitchen", "n", ""])
        order = [call[0] for call in session.calls]
        self.assertLess(order.index("identity"), order.index("pair"),
                        "pairing ran before the device identity was read")
        pair_call = next(call for call in session.calls if call[0] == "pair")
        self.assertEqual(pair_call[4], "esp32-0011223344",
                         "the certificate would be issued for the pet name, not the board")

    def test_the_pairing_port_defaults_to_the_pairing_listener(self):
        session = FakeSession()
        prompt = ScriptedPrompt(["2", "1", "1", "gateway.example", "", "y",
                                 "amber lantern copper thrush meadow", "kitchen", "n", ""])
        with redirect_stdout(io.StringIO()):
            run(session, prompt, out=lambda *a, **k: None)
        inspect = next(call for call in session.calls if call[0] == "inspect")
        self.assertEqual(inspect[2], 10704)

    def test_a_declined_identity_code_stops_before_anything_is_sent(self):
        session = FakeSession()
        code, _, output = drive(session, ["2", "1", "1", "gateway.example", "10704", "n"])
        self.assertEqual(code, 1)
        self.assertFalse(any(call[0] == "pair" for call in session.calls))

    def test_a_card_device_offers_the_mounted_cards_and_the_saved_networks(self):
        session = FakeSession()
        code, prompt, _ = drive(session, ["2", "2", "1", "gateway.example", "10704", "y",
                                          "amber lantern copper thrush meadow", "bedroom", "n",
                                          "home"])
        self.assertEqual(code, 0)
        install = next(call for call in session.calls if call[0] == "install")
        fields = install[2]
        self.assertEqual(fields["card_mount"], "/media/user/rootfs")
        self.assertEqual(fields["wifi_ssid"], "home")
        # A card copies a saved network; a passphrase file is not part of that flow.
        self.assertNotIn("wifi_password", fields)


if __name__ == "__main__":
    unittest.main(verbosity=2)
