"""Offline checks for the device packages: the exact commands each driver builds.

No board, no card, no gateway. A driver's whole job is to produce the right argv and the right
refusal when something is missing, and both are assertable.

    python packages/installer/tests/run_device_tests.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for package in ("installer", "device-esp32", "device-pi"):
    sys.path.insert(0, str(ROOT / "packages" / package / "src"))

from hermes_installer.driver import DeviceRequest, Runner  # noqa: E402
import hermes_device_esp32 as esp32  # noqa: E402
import hermes_device_pi as pi  # noqa: E402


def dry() -> Runner:
    return Runner(dry_run=True)


class Esp32Commands(unittest.TestCase):
    def request(self, **overrides) -> DeviceRequest:
        base = dict(
            device_name="kitchen",
            dial_address="tls://gateway.example:10702",
            wake_word="hey_hermes",
            port="/dev/ttyACM0",
        )
        base.update(overrides)
        return DeviceRequest(**base)

    def test_flash_uses_the_flags_the_script_actually_has(self):
        result = esp32.DRIVER.flash(self.request(), dry())
        self.assertTrue(result.ok)
        argv = result.commands[0].argv
        self.assertIn("scripts/install-esp32.sh", argv)
        self.assertEqual(argv[argv.index("--device") + 1], "/dev/ttyACM0")
        self.assertEqual(argv[argv.index("--endpoint") + 1], "tls://gateway.example:10702")
        # The wake word is the model's detection name, not a flag of its own.
        self.assertNotIn("--wake-word", argv)

    def test_a_chosen_wake_word_travels_as_model_plus_name(self):
        result = esp32.DRIVER.flash(
            self.request(wake_model="models/wake.tflite", wake_word="hey_hermes"), dry()
        )
        argv = result.commands[0].argv
        self.assertEqual(argv[argv.index("--model") + 1], "models/wake.tflite")
        self.assertEqual(argv[argv.index("--model-name") + 1], "hey_hermes")

    def test_wifi_is_staged_with_the_passphrase_in_a_file_never_an_argument(self):
        result = esp32.DRIVER.flash(
            self.request(wifi_ssid="home", wifi_psk_file="/tmp/psk"), dry()
        )
        argv = result.commands[0].argv
        self.assertEqual(argv[argv.index("--wifi-ssid") + 1], "home")
        self.assertEqual(argv[argv.index("--wifi-psk-file") + 1], "/tmp/psk")
        self.assertNotIn("secretpass", " ".join(argv))
        self.assertEqual(result.commands[0].secret_file, "/tmp/psk")

    def test_certs_use_the_real_flag_name(self):
        argv = esp32.DRIVER.flash(self.request(certs_dir="/certs"), dry()).commands[0].argv
        self.assertIn("--tls-certs", argv)
        self.assertNotIn("--certs", argv)

    def test_no_port_is_a_refusal_not_a_crash(self):
        result = esp32.DRIVER.flash(self.request(port=None), dry())
        self.assertFalse(result.ok)
        self.assertIn("USB port", result.summary)

    def test_no_gateway_address_is_a_refusal(self):
        result = esp32.DRIVER.flash(self.request(dial_address=""), dry())
        self.assertFalse(result.ok)
        self.assertIn("gateway address", result.summary)

    def test_provision_reuses_install_with_the_provision_flag(self):
        argv = esp32.DRIVER.provision(self.request(), dry()).commands[0].argv
        self.assertIn("--provision", argv)

    def test_verify_reads_the_device_report(self):
        argv = esp32.DRIVER.verify(self.request(), dry()).commands[0].argv
        self.assertEqual(argv[0], "scripts/esp32_provision.py")
        self.assertEqual(argv[-1], "status")


class PiCommands(unittest.TestCase):
    def request(self, **overrides) -> DeviceRequest:
        base = dict(
            device_name="bedroom",
            dial_address="tls://gateway.example:10702",
            wake_word="hey_hermes",
            card_mount="/media/user/rootfs",
        )
        base.update(overrides)
        return DeviceRequest(**base)

    def test_profile_says_the_card_must_come_out(self):
        self.assertTrue(pi.PROFILE.needs_card_reader)
        self.assertIn("openwakeword_onnx", pi.PROFILE.wake_formats)

    def test_flash_targets_the_mounted_card_with_the_pi_flags(self):
        argv = pi.DRIVER.flash(self.request(), dry()).commands[0].argv
        self.assertEqual(argv[0], "scripts/install-pi.sh")
        self.assertEqual(argv[argv.index("--base") + 1], "/media/user/rootfs")
        self.assertEqual(argv[argv.index("--wake-word") + 1], "hey_hermes")
        self.assertEqual(argv[argv.index("--device-name") + 1], "bedroom")

    def test_a_missing_card_is_a_refusal(self):
        result = pi.DRIVER.flash(self.request(card_mount=None), dry())
        self.assertFalse(result.ok)
        self.assertIn("SD card", result.summary)

    def test_wifi_provisioning_needs_elevation_and_names_the_network(self):
        result = pi.DRIVER.provision(self.request(wifi_ssid="home"), dry())
        self.assertTrue(result.ok)
        command = result.commands[0]
        self.assertTrue(command.requires_elevation)
        self.assertEqual(command.argv[command.argv.index("--source") + 1], "home")

    def test_wifi_without_a_network_is_a_refusal(self):
        result = pi.DRIVER.provision(self.request(), dry())
        self.assertFalse(result.ok)
        self.assertIn("Wi-Fi", result.summary)

    def test_verify_refuses_a_directory_that_is_not_a_linux_root(self):
        result = pi.DRIVER.verify(self.request(card_mount="/tmp"), Runner(dry_run=False))
        self.assertFalse(result.ok)
        self.assertIn("not the card's root filesystem", result.summary)

    def test_verify_accepts_a_linux_root(self):
        # / has an etc directory, which is the shape check this verb performs.
        result = pi.DRIVER.verify(self.request(card_mount="/"), Runner(dry_run=False))
        self.assertTrue(result.ok)


class ProfilesAgree(unittest.TestCase):
    def test_the_default_wake_word_is_the_same_phrase_on_both(self):
        self.assertEqual(esp32.PROFILE.wake_default, pi.PROFILE.wake_default)
        self.assertEqual(esp32.PROFILE.wake_default, "hey_hermes")

    def test_the_wake_formats_differ_and_that_is_the_point(self):
        self.assertNotEqual(esp32.PROFILE.wake_catalogue_format(), pi.PROFILE.wake_catalogue_format())

    def test_both_report_which_settings_need_a_cable(self):
        for profile in (esp32.PROFILE, pi.PROFILE):
            self.assertIn("wifi", profile.cable_settings, profile.id)
            self.assertIn("playback_volume", profile.remote_settings, profile.id)

    def test_ids_are_unique(self):
        self.assertNotEqual(esp32.PROFILE.id, pi.PROFILE.id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
