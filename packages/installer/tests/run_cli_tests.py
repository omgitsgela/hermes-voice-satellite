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

    def manage_device(self, profile_id, **fields):
        self.calls.append(("manage", profile_id, fields))
        return {"ok": True, "summary": "changed", "commands": [], "dry_run": True,
                "mode": "manage"}

    def install_gateway(self, *args, **kwargs):  # pragma: no cover - not exercised here
        raise AssertionError("the gateway install should not run on the device route")


def drive(session, answers):
    prompt = ScriptedPrompt(answers)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = run(session, prompt, out=lambda *a, **k: None)
    return code, prompt, buffer.getvalue()


class LauncherDispatch(unittest.TestCase):
    """The packaged executable has to be able to stand in for Python.

    The install scripts call Python helpers, so a machine with no Python cannot finish a flash unless
    the bundle runs those helpers itself. These pin the three helper modes and the install-interface
    fall-through that must survive them.
    """

    LAUNCH = ROOT / "packaging" / "launch.py"

    def run_launcher(self, *args):
        import os
        import subprocess

        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            str(ROOT / "packages" / name / "src")
            for name in ("installer", "gateway", "device-esp32", "device-pi")
        )
        return subprocess.run(
            [sys.executable, str(self.LAUNCH), *args], capture_output=True, text=True, env=env
        )

    def test_run_code_executes_on_this_runtime(self):
        finished = self.run_launcher("--run-code", "import sys; print('mode', sys.argv[1])", "x")
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("mode x", finished.stdout)

    def test_run_python_executes_a_helper_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "helper.py"
            helper.write_text("import sys\nprint('helper', sys.argv[1])\n")
            finished = self.run_launcher("--run-python", str(helper), "arg")
            self.assertEqual(finished.returncode, 0, finished.stderr)
            self.assertIn("helper arg", finished.stdout)

    def test_run_python_reads_a_script_from_stdin(self):
        import os
        import subprocess

        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            str(ROOT / "packages" / name / "src")
            for name in ("installer", "gateway", "device-esp32", "device-pi")
        )
        finished = subprocess.run(
            [sys.executable, str(self.LAUNCH), "--run-python", "-"],
            input="print('from stdin')\n", capture_output=True, text=True, env=env,
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("from stdin", finished.stdout)

    def test_an_unknown_argument_still_reaches_the_installer(self):
        finished = self.run_launcher("--definitely-not-a-mode")
        # The CLI owns its own argument errors; the launcher must not swallow them as a dispatch.
        self.assertNotIn("Traceback", finished.stderr)


class StartOverRoute(unittest.TestCase):
    def test_start_over_is_available_at_text_choice_confirmation_and_secret_prompts(self):
        from hermes_installer import cli
        from unittest.mock import patch

        for method, args in (("text", ("Address",)), ("choice", ("Task", ["One"])),
                             ("yes_no", ("Apply?",)), ("secret", ("Words",))):
            with patch("builtins.input", return_value=":start-over"), \
                    patch("getpass.getpass", return_value=":start-over"):
                with self.assertRaises(cli.StartOver):
                    getattr(cli.TerminalPrompt(), method)(*args)

    def test_start_over_discards_the_current_task_and_returns_to_welcome(self):
        from hermes_installer import cli
        from unittest.mock import Mock

        session = FakeSession()
        session.close = Mock()
        prompt = cli.ScriptedPrompt([":start-over", "4", "1", "1", "Example network",
                                     "example-passphrase", "2", "y"])
        lines = []
        self.assertEqual(cli.run(session, prompt, out=lines.append), 0)
        session.close.assert_called_once()
        self.assertEqual(lines.count("  Hermes Voice Satellite"), 2)
        self.assertFalse(any(c[0] == "install" for c in session.calls))


class GatewayContinuation(unittest.TestCase):
    def test_new_gateway_offers_a_satellite_and_reuses_its_record(self):
        class GatewaySession(FakeSession):
            dry_run = False

            def status(self):
                return {**super().status(), "lan_address": "192.0.2.24"}

            def install_gateway(self, address, **fields):
                self.calls.append(("gateway", address, fields))
                return {"ok": True, "commands": [], "card": {
                    "identity_code": "XK7F-9Q2M", "words": "private pairing words",
                    "lines": ["Identity code: XK7F-9Q2M"]}, "pairing_port": 10704}

            def pair(self, host, port, words, name, **kwargs):
                self.used_words = words
                return super().pair(host, port, words, name, **kwargs)

        session = GatewaySession()
        prompt = ScriptedPrompt(["1", "", "", "y", "y", "1", "1", "", "", "y",
                                 "study", "n", "", "n", "n", "y"])
        lines = []
        self.assertEqual(run(session, prompt, out=lines.append), 0)
        self.assertEqual(session.calls[0][1], "192.0.2.24")
        self.assertEqual(session.used_words, "private pairing words")
        self.assertNotIn("Pairing words", prompt.asked)
        self.assertTrue(any(c[0] == "install" for c in session.calls))
        self.assertIn("security boundary", " ".join(lines))
        self.assertIn("another workbench", " ".join(lines))


class DeviceRoute(unittest.TestCase):
    def test_a_workbench_without_hermes_can_still_set_up_a_device(self):
        session = FakeSession(hermes="none")
        # Role (set one up), device choice, port choice, address, port, code match, words, name,
        # remember, wifi. A workbench still gets asked WHICH job it is doing, because changing an
        # existing device is the other thing this machine can do.
        code, prompt, _ = drive(session, ["1", "1", "1", "gateway.example", "10704", "y",
                                          "amber lantern copper thrush meadow", "kitchen",
                                          "n", "", "n", "n", "y"])
        self.assertEqual(code, 0)
        self.assertTrue(any(call[0] == "install" for call in session.calls))

    def test_changing_an_existing_device_never_flashes_it(self):
        """The console promised this and did not have it: re-running routed an existing device
        through a firmware flash instead of a configuration change."""
        session = FakeSession(hermes="none")
        code, prompt, _ = drive(session, [
            "2",          # change a device already set up
            "1",          # device
            "1",          # port
            "Example network",
            "example-passphrase",
            "2",          # only the network above
            "y",          # approve the reviewed settings
        ])
        self.assertEqual(code, 0)
        names = [call[0] for call in session.calls]
        self.assertIn("manage", names)
        self.assertNotIn("install", names, "a configuration change must not reflash the device")
        self.assertNotIn("pair", names, "an enrolled device does not pair again to change a setting")

    def test_the_device_identity_is_read_before_pairing(self):
        session = FakeSession()
        drive(session, ["2", "1", "1", "gateway.example", "10704", "y",
                        "amber lantern copper thrush meadow", "kitchen", "n", "", "n", "n", "y"])
        order = [call[0] for call in session.calls]
        self.assertLess(order.index("identity"), order.index("pair"),
                        "pairing ran before the device identity was read")
        pair_call = next(call for call in session.calls if call[0] == "pair")
        self.assertEqual(pair_call[4], "esp32-0011223344",
                         "the certificate would be issued for the pet name, not the board")

    def test_the_pairing_port_defaults_to_the_pairing_listener(self):
        session = FakeSession()
        prompt = ScriptedPrompt(["2", "1", "1", "gateway.example", "", "y",
                                 "amber lantern copper thrush meadow", "kitchen", "n", "",
                                 "n", "n", "y"])
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
                                          "home", "n", "n", "y"])
        self.assertEqual(code, 0)
        install = next(call for call in session.calls if call[0] == "install")
        fields = install[2]
        self.assertEqual(fields["card_mount"], "/media/user/rootfs")
        self.assertEqual(fields["wifi_ssid"], "home")
        # A card copies a saved network; a passphrase file is not part of that flow.
        self.assertNotIn("wifi_password", fields)


if __name__ == "__main__":
    unittest.main(verbosity=2)
