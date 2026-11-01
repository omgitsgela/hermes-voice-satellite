"""Offline UX contracts and safe existing-device command plans (no hardware)."""
from __future__ import annotations

import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
