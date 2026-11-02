"""Offline UX contracts and safe existing-device command plans (no hardware)."""
from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for package in ("installer", "gateway", "device-esp32", "device-pi"):
    sys.path.insert(0, str(ROOT / "packages" / package / "src"))

from hermes_installer.session import Session  # noqa: E402


class ExistingDevice(unittest.TestCase):
    def test_network_only_does_not_require_pairing_or_write_a_model(self):
        session = Session(ROOT, dry_run=True)
        result = session.manage_device(
            "esp32-xvf3800", port="/dev/test-satellite", device_id="esp32-test",
            wifi_ssid="Example network", wifi_password="example-passphrase", dry_run=True,
        )
        self.assertTrue(result["ok"], result)
        commands = " ".join(" ".join(c["argv"]) for c in result["commands"])
        self.assertIn("save-profile", commands)
        for forbidden in ("install-esp32.sh", "--flash", "provision-device-tls", "set-wake",
                          "provision-device-model", "set-endpoint", "example-passphrase"):
            self.assertNotIn(forbidden, commands)

    def test_custom_voice_install_without_apply_preserves_the_shared_voice(self):
        with tempfile.TemporaryDirectory() as directory:
            from unittest.mock import patch

            root = Path(directory)
            model = root / "custom.onnx"
            model.write_bytes(b"model")
            Path(str(model) + ".json").write_text("{}")
            session = Session(root, dry_run=False)
            with patch.object(session, "_set_gateway_voice") as select:
                result = session.install_voice_file(str(model), apply=False)
            self.assertTrue(result["ok"])
            select.assert_not_called()

    def test_pi_can_restore_the_default_wake_word_without_reinstalling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            card = root / "card"
            config = card / "etc/voice-satellite/satellite.env"
            config.parent.mkdir(parents=True)
            config.write_text("VS_WAKE_WORD=previous_phrase\nOTHER_SETTING=keep\n")
            session = Session(root, dry_run=False)
            result = session.manage_device("pi-reSpeaker-2mic", card_mount=str(card),
                                           wake_word="hey_hermes", dry_run=False)
            self.assertTrue(result["ok"], result)
            self.assertIn("VS_WAKE_WORD=hey_hermes", config.read_text())
            self.assertIn("OTHER_SETTING=keep", config.read_text())
            self.assertNotIn("deploy-to-card", str(result.get("commands")))

    def test_catalogue_distinguishes_installed_models_from_sample_clips(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = Session(root, dry_run=True)
            voices = session.voice_catalogue()["voices"]
            self.assertFalse(voices[0]["installed"])
            (root / "voices").mkdir()
            for suffix in (".onnx", ".onnx.json"):
                (root / "voices" / (voices[0]["id"] + suffix)).write_bytes(b"fixture")
            self.assertTrue(session.voice_catalogue()["voices"][0]["installed"])


class HelperRuntime(unittest.TestCase):
    def test_non_executable_python_helper_runs_on_the_installer_runtime(self):
        from hermes_installer.driver import Command, Runner

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "helper.py"
            helper.write_text("print('helper executed')\n")
            helper.chmod(0o600)
            result = Runner(repo_root=root).run(Command(("helper.py",), "exercise helper"))
        self.assertTrue(result.ok, result.output)
        self.assertIn("helper executed", result.output)

    def test_frozen_helpers_use_the_executable_dispatch_not_a_system_python(self):
        from hermes_installer.driver import Runner
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "helper.py").write_text("pass")
            with patch.object(sys, "frozen", True, create=True):
                argv = Runner(repo_root=root).resolve(("helper.py", "argument"))
            self.assertEqual(argv, (sys.executable, "--run-python", str(root / "helper.py"), "argument"))


class ConsoleSpeech(unittest.TestCase):
    def test_console_can_download_a_catalogue_voice_without_selecting_it(self):
        from hermes_installer import cli
        from unittest.mock import Mock

        self.assertTrue(hasattr(cli, "configure_speech"), "console has no speech choices")
        session = Mock()
        session.voice_catalogue.return_value = {"voices": [
            {"id": "en_US-amy-medium", "name": "Amy", "language": "en_US",
             "size_mb": 60, "sample_url": "https://example.com/sample.mp3", "installed": False}
        ]}
        session.download_voice.return_value = {"ok": True}
        prompt = cli.ScriptedPrompt(["n", "y", "amy", "1", "y", "n"])
        fields = {}
        self.assertTrue(cli.configure_speech(session, prompt, lambda *args: None,
                                             {"wake_format": "microwakeword_tflite"}, fields))
        session.download_voice.assert_called_once_with("en_US-amy-medium", apply=False)
        self.assertEqual(fields["voice"], "en_US-amy-medium")


class GatewayDefaults(unittest.TestCase):
    def test_status_reuses_the_guided_lan_detector(self):
        import subprocess
        from unittest.mock import patch

        session = Session(ROOT, dry_run=True)
        with patch("subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, stdout="192.0.2.24\n", stderr="")) as run:
            self.assertEqual(session.status()["lan_address"], "192.0.2.24")
        argv = run.call_args.args[0]
        self.assertIn("guided_detect_lan_ip", argv[2])
        self.assertEqual(Path(argv[-1]), ROOT / "scripts/lib/guided.sh")

    def test_unavailable_detector_does_not_supply_a_fake_address(self):
        import subprocess
        from unittest.mock import patch

        session = Session(ROOT, dry_run=True)
        for output in ("", "127.0.0.1\n", "::1\n"):
            with patch("subprocess.run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout=output, stderr="")):
                self.assertEqual(session.status()["lan_address"], "")
        with patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(session.status()["lan_address"], "")


class GuidedPages(unittest.TestCase):
    def test_welcome_and_short_pages_have_navigation_and_descriptions(self):
        from html.parser import HTMLParser

        class Pages(HTMLParser):
            def __init__(self):
                super().__init__()
                self.pages = []
                self.ids = set()

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                self.ids.add(attrs.get("id"))
                if tag == "section" and "data-page" in attrs:
                    self.pages.append(attrs["id"])

        html = (ROOT / "packages/installer/src/hermes_installer/web/index.html").read_text()
        parsed = Pages()
        parsed.feed(html)
        self.assertIn("welcome", parsed.pages)
        for name in ("connect", "settings", "network", "wake", "voice", "review", "done"):
            self.assertIn(name, parsed.pages)
        for name in ("back", "next", "voice-search", "voice-language", "voice-download"):
            self.assertIn(name, parsed.ids)
        self.assertIn("prefers-reduced-motion", html)
        self.assertIn("aria-current", html)
        self.assertIn("fade-out", html)
        self.assertNotIn('behavior: "smooth"', html)


class NamesAndPackages(unittest.TestCase):
    """The identity a room name becomes, and what a broken device package says."""

    def test_a_room_name_becomes_an_identity_the_gateway_accepts(self):
        from hermes_gateway.pairing_server import device_id_from_name, valid_device_id
        for typed, identity in {
            "Living Room": "living-room",
            "Study": "study",
            "Kitchen-1": "kitchen-1",
            "  Primary   Bedroom  ": "primary-bedroom",
            "Attic/loft": "attic-loft",
            "con": "con-device",
        }.items():
            self.assertEqual(device_id_from_name(typed), identity, typed)
            self.assertTrue(valid_device_id(identity), typed)

    def test_a_name_with_nothing_usable_is_refused_rather_than_invented(self):
        from hermes_gateway.pairing_server import device_id_from_name
        for typed in ("", "   ", "!!!", "---"):
            self.assertEqual(device_id_from_name(typed), "", repr(typed))

    def test_pairing_normalises_the_name_and_reports_the_identity_it_issued(self):
        session = Session(ROOT, dry_run=True)
        result = session.pair("gateway.example", 10704, "amber lantern copper thrush meadow",
                              "Living Room", expected_code="XK7F-9Q2M")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["device_id"], "living-room")

    def test_pairing_refuses_a_name_that_cannot_be_an_identity(self):
        session = Session(ROOT, dry_run=True)
        result = session.pair("gateway.example", 10704, "words", "!!!", expected_code="XK7F-9Q2M")
        self.assertFalse(result["ok"])
        self.assertIn("letter or digit", result["error"])

    def test_card_provisioning_uses_the_identity_pairing_issued(self):
        from unittest.mock import patch

        from hermes_installer.driver import StepResult

        session = Session(ROOT, dry_run=True)
        profile = session.registry.get("pi-reSpeaker-2mic")
        captured = []

        class RecordingDriver:
            PROFILE = profile

            def flash(self, request, runner):
                captured.append(request)
                return StepResult(ok=True, summary="recorded", dry_run=True)

        class Registry:
            def get(self, _):
                return profile

            def driver(self, _):
                return RecordingDriver()

        with patch.object(session, "registry", Registry()):
            session.flash("pi-reSpeaker-2mic", device_name="Living Room",
                          card_mount="/media/example/rootfs", dial_address="")
        self.assertEqual(captured[0].device_id, "living-room")

    def test_a_broken_device_package_is_recorded_not_silently_dropped(self):
        import sys
        from hermes_installer import registry as registry_module

        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "hermes_device_broken.py"
            package.write_text("import module_that_is_not_installed\n")
            sys.path.insert(0, directory)
            try:
                found = registry_module.DeviceRegistry()
                with unittest.mock.patch.object(
                        registry_module.DeviceRegistry, "FIRST_PARTY", ("hermes_device_broken",)):
                    found.discover()
            finally:
                sys.path.remove(directory)
                sys.modules.pop("hermes_device_broken", None)
        self.assertEqual(len(found.load_errors), 1, found.load_errors)
        self.assertIn("hermes_device_broken", found.load_errors[0])
        self.assertIn("module_that_is_not_installed", found.load_errors[0])


class ShippedCopy(unittest.TestCase):
    """The interface must not deny a feature the same interface offers."""

    def setUp(self):
        self.html = (ROOT / "packages/installer/src/hermes_installer/web/index.html").read_text()

    def test_the_remote_flow_is_offered_and_the_settings_page_points_at_it(self):
        self.assertIn('value="fleet"', self.html)
        self.assertNotIn("Remote editing and enrolled-device discovery are not available", self.html)
        self.assertIn("Manage satellites over the network", self.html)

    def test_per_device_voice_is_offered_and_the_voice_page_says_so(self):
        self.assertIn("Assign this voice to this satellite", self.html)
        self.assertNotIn("one shared voice, not one per device", self.html)

    def test_a_device_package_that_failed_to_load_is_shown(self):
        self.assertIn("state.status.load_errors", self.html)
        self.assertIn("A device package could not be loaded", self.html)


if __name__ == "__main__":
    unittest.main()
