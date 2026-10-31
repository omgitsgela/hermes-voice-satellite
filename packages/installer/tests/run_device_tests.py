"""Offline checks for the device packages: the exact commands each driver builds.

No board, no card, no gateway. A driver's whole job is to produce argv the real scripts accept, and
the right refusal when something is missing. These tests exist because the first draft got the
script contracts wrong in ways that only reading the scripts could catch:

- `install-esp32.sh` takes `--port` (not `--device`) and needs an explicit `--flash` or `--provision`;
- `install-pi.sh --base` is the software directory on the device, NOT a mounted card. The card
  writer is `deploy-to-card.sh`, which copies files onto the mount and writes a placeholder env.

    python packages/installer/tests/run_device_tests.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for package in ("installer", "device-esp32", "device-pi"):
    sys.path.insert(0, str(ROOT / "packages" / package / "src"))

from hermes_installer.driver import (  # noqa: E402
    DeviceRequest,
    Runner,
    UnsupportedOnThisPlatform,
)
import hermes_device_esp32 as esp32  # noqa: E402
import hermes_device_pi as pi  # noqa: E402

#: The device scripts are shell and systemd work, so a driver refuses on Windows rather than
#: half-running. Tests that assert a command was built are therefore POSIX-only; the refusal itself is
#: asserted on both platforms.
POSIX = sys.platform != "win32"
posix_only = unittest.skipUnless(POSIX, "device operations run the repository's shell scripts")


def dry() -> Runner:
    return Runner(dry_run=True, repo_root=ROOT)


def noop_executor(argv, cwd):
    """Pretends every command succeeded, so a card flow can be exercised without hardware."""
    return 0, "ok"


class Esp32Commands(unittest.TestCase):
    def request(self, **overrides) -> DeviceRequest:
        base = dict(
            device_name="kitchen",
            dial_address="tls://gateway.example:10702",
            wake_word="hey_hermes",
            port="/dev/ttyACM0",
            device_id="esp32-0011223344",
        )
        base.update(overrides)
        return DeviceRequest(**base)

    @posix_only
    def test_flash_uses_the_port_flag_and_an_explicit_action(self):
        argv = esp32.DRIVER.flash(self.request(), dry()).commands[0].argv
        self.assertIn("scripts/install-esp32.sh", argv[0])
        self.assertEqual(argv[argv.index("--port") + 1], "/dev/ttyACM0")
        # `--port` alone does nothing in the script: it needs an action.
        self.assertIn("--flash", argv)
        # The flag the first draft invented; the script rejects it outright.
        self.assertNotIn("--device", argv)

    @posix_only
    def test_flash_carries_the_board_identity_and_the_friendly_name(self):
        argv = esp32.DRIVER.flash(self.request(), dry()).commands[0].argv
        self.assertEqual(argv[argv.index("--device-id") + 1], "esp32-0011223344")
        self.assertEqual(argv[argv.index("--device-name") + 1], "kitchen")

    @posix_only
    def test_a_chosen_wake_word_travels_as_model_plus_name(self):
        argv = esp32.DRIVER.flash(
            self.request(wake_model="models/wake.tflite", wake_word="hey_hermes"), dry()
        ).commands[0].argv
        self.assertEqual(argv[argv.index("--model") + 1], "models/wake.tflite")
        self.assertEqual(argv[argv.index("--model-name") + 1], "hey_hermes")
        self.assertNotIn("--wake-word", argv)

    @posix_only
    def test_wifi_is_staged_with_the_passphrase_in_a_file_never_an_argument(self):
        result = esp32.DRIVER.flash(self.request(wifi_ssid="home", wifi_psk_file="/run/psk"), dry())
        argv = result.commands[0].argv
        self.assertEqual(argv[argv.index("--wifi-ssid") + 1], "home")
        self.assertEqual(argv[argv.index("--wifi-psk-file") + 1], "/run/psk")
        self.assertEqual(result.commands[0].secret_file, "/run/psk")

    @posix_only
    def test_certs_use_the_real_flag_name(self):
        argv = esp32.DRIVER.flash(self.request(certs_dir="/certs"), dry()).commands[0].argv
        self.assertIn("--tls-certs", argv)
        self.assertNotIn("--certs", argv)

    @posix_only
    def test_the_registry_record_travels_with_the_certificates(self):
        # The provisioner refuses without it, so a paired device would flash and then fail to stage.
        argv = esp32.DRIVER.flash(
            self.request(certs_dir="/certs", registry_file="/certs/device-credentials.json"), dry()
        ).commands[0].argv
        self.assertEqual(argv[argv.index("--registry") + 1], "/certs/device-credentials.json")

    def test_no_port_is_a_refusal_not_a_crash(self):
        result = esp32.DRIVER.flash(self.request(port=None), dry())
        self.assertFalse(result.ok)
        self.assertIn("USB port", result.summary)

    def test_no_gateway_address_is_a_refusal(self):
        result = esp32.DRIVER.flash(self.request(dial_address=""), dry())
        self.assertFalse(result.ok)
        self.assertIn("gateway address", result.summary)

    def test_provision_requires_the_board_identity(self):
        result = esp32.DRIVER.provision(self.request(device_id=None), dry())
        self.assertFalse(result.ok)
        self.assertIn("identity", result.summary)

    @posix_only
    def test_provision_uses_the_provision_action(self):
        argv = esp32.DRIVER.provision(self.request(), dry()).commands[0].argv
        self.assertIn("--provision", argv)
        self.assertNotIn("--flash", argv)

    def test_verify_reads_the_board_identity(self):
        argv = esp32.DRIVER.verify(self.request(), dry()).commands[0].argv
        self.assertTrue(argv[0].endswith("scripts/esp32_provision.py"))
        self.assertEqual(argv[-1], "device-id")


class CardFlow(unittest.TestCase):
    """The Pi is set up by writing a card, and the card writer is deploy-to-card.sh."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.mount = Path(self._tmp.name)
        (self.mount / "etc").mkdir()
        (self.mount / "home" / "satellite").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def request(self, **overrides) -> DeviceRequest:
        base = dict(
            device_name="bedroom",
            dial_address="tls://gateway.example:10702",
            wake_word="hey_hermes",
            card_mount=str(self.mount),
        )
        base.update(overrides)
        return DeviceRequest(**base)

    @posix_only
    def test_flash_uses_the_card_writer_not_the_on_device_installer(self):
        result = pi.DRIVER.flash(self.request(), dry())
        argv = result.commands[0].argv
        self.assertTrue(argv[0].endswith("scripts/deploy-to-card.sh"))
        # install-pi.sh installs systemd units into /etc on the RUNNING machine and is therefore
        # never the right script for a mounted card.
        self.assertNotIn("install-pi.sh", " ".join(argv))
        self.assertEqual(argv[1], str(self.mount))

    @posix_only
    def test_flash_fills_the_env_file_the_card_needs(self):
        result = pi.DRIVER.flash(self.request(), dry())
        self.assertTrue(result.ok)
        # A dry run must not write anything.
        self.assertFalse((self.mount / "home" / "satellite" / "gateway-link.env").exists())

    @posix_only
    def test_a_real_run_fills_the_env_and_keeps_other_values(self):
        env = self.mount / "home" / "satellite" / "gateway-link.env"
        env.write_text("VOICE_DEVICE_ID=CHANGE-ME\nVOICE_DEVICE_AREA=hallway\n")
        runner = Runner(dry_run=False, executor=noop_executor, repo_root=ROOT)
        result = pi.DRIVER.flash(self.request(), runner)
        self.assertTrue(result.ok, result.summary)
        text = env.read_text()
        self.assertIn("VOICE_GATEWAY_HOST=gateway.example", text)
        self.assertIn("VOICE_GATEWAY_PORT=10702", text)
        self.assertIn("VOICE_DEVICE_NAME=bedroom", text)
        self.assertNotIn("CHANGE-ME", text)
        # A line the driver does not own must survive untouched.
        self.assertIn("VOICE_DEVICE_AREA=hallway", text)

    @posix_only
    def test_the_card_writer_runs_twice_so_the_unit_can_be_enabled(self):
        runner = Runner(dry_run=False, executor=noop_executor, repo_root=ROOT)
        result = pi.DRIVER.flash(self.request(), runner)
        deploys = [c for c in result.commands if c.argv[0].endswith("deploy-to-card.sh")]
        self.assertEqual(len(deploys), 2)

    def test_a_missing_card_is_a_refusal(self):
        result = pi.DRIVER.flash(self.request(card_mount=None), dry())
        self.assertFalse(result.ok)
        self.assertIn("SD card", result.summary)

    @posix_only
    def test_the_card_is_pointed_at_paths_that_exist_once_it_is_booted(self):
        """The mount prefix is the workbench's view, not the Pi's.

        Writing `<mount>/home/<user>/certs/...` into the environment produced a satellite that
        booted, found no certificate, and could not authenticate -- while the files were on the card
        exactly where they belonged.
        """
        certs = Path(str(self.mount)) / "issued"
        certs.mkdir(parents=True, exist_ok=True)
        identity = "pi-0011223344"
        (certs / "client-ca.crt").write_text("ca")
        (certs / f"{identity}.crt").write_text("cert")
        (certs / f"{identity}.key").write_text("key")

        runner = Runner(dry_run=False, executor=noop_executor, repo_root=ROOT)
        result = pi.DRIVER.provision(
            self.request(certs_dir=str(certs), device_id=identity), runner
        )
        self.assertTrue(result.ok, result.summary)

        env = (Path(str(self.mount)) / "home" / "satellite" / "gateway-link.env").read_text()
        device_home = os.path.join(os.sep, "home", "satellite")
        self.assertIn(f"VOICE_GATEWAY_TLS_CA={device_home}/certs/ca.crt", env)
        self.assertIn(f"VOICE_GATEWAY_TLS_CERT={device_home}/certs/cert.pem", env)
        self.assertIn(f"VOICE_GATEWAY_TLS_KEY={device_home}/certs/key.pem", env)
        self.assertNotIn(str(self.mount), env,
                         "the card kept a workbench mount path it will never have again")

    @posix_only
    def test_a_chosen_wake_word_is_copied_onto_the_card(self):
        """A wake word is a model file and a selector; staging only the selector leaves it deaf."""
        model = self.mount / "chosen.onnx"
        model.write_bytes(b"onnx-model-bytes")
        runner = Runner(dry_run=False, executor=noop_executor, repo_root=ROOT)
        result = pi.DRIVER.provision(
            self.request(wake_word="kitchen_bell", wake_model=str(model)), runner
        )
        self.assertTrue(result.ok, result.summary)
        staged = Path(str(self.mount)) / "home" / "satellite" / "wake-models" / "chosen.onnx"
        self.assertEqual(staged.read_bytes(), b"onnx-model-bytes")
        env = (Path(str(self.mount)) / "etc" / "voice-satellite" / "satellite.env").read_text()
        self.assertIn("VS_WAKE_WORD=kitchen_bell", env)
        self.assertIn(f"VS_WAKE_MODELS={os.path.join(os.sep, 'home', 'satellite')}/wake-models", env)

    @posix_only
    def test_a_wake_word_without_its_model_is_refused_not_faked(self):
        runner = Runner(dry_run=False, executor=noop_executor, repo_root=ROOT)
        result = pi.DRIVER.provision(self.request(wake_word="kitchen_bell"), runner)
        self.assertFalse(result.ok)
        self.assertIn("no wake word model called kitchen_bell", result.summary)

    @posix_only
    def test_wifi_provisioning_copies_a_saved_network_and_needs_elevation(self):
        result = pi.DRIVER.provision(self.request(wifi_ssid="home"), dry())
        self.assertTrue(result.ok)
        command = result.commands[0]
        self.assertTrue(command.requires_elevation)
        self.assertEqual(command.argv[command.argv.index("--source") + 1], "home")

    @posix_only
    def test_a_card_with_nothing_to_stage_is_not_an_error(self):
        # Changing only, say, nothing at all must not fail: provisioning now covers credentials and
        # Wi-Fi independently, and a card on Ethernet still needs neither.
        result = pi.DRIVER.provision(self.request(), dry())
        self.assertTrue(result.ok)
        self.assertIn("nothing to stage", result.summary)

    def test_verify_refuses_a_directory_that_is_not_a_linux_root(self):
        # Any directory without an etc/ is the case under test; a hard-coded /tmp was a Linux
        # assumption that failed on Windows.
        import tempfile

        with tempfile.TemporaryDirectory() as empty:
            result = pi.DRIVER.verify(self.request(card_mount=empty), Runner(dry_run=False))
        self.assertFalse(result.ok)
        self.assertIn("not the card's root filesystem", result.summary)

    def test_verify_refuses_a_card_that_still_says_change_me(self):
        env = self.mount / "home" / "satellite" / "gateway-link.env"
        env.write_text("VOICE_DEVICE_ID=CHANGE-ME\n")
        result = pi.DRIVER.verify(self.request(), Runner(dry_run=False))
        self.assertFalse(result.ok)
        self.assertIn("CHANGE-ME", result.summary)

    def test_verify_accepts_a_filled_in_card(self):
        env = self.mount / "home" / "satellite" / "gateway-link.env"
        env.write_text("VOICE_DEVICE_ID=satellite\nVOICE_GATEWAY_HOST=gateway.example\n")
        result = pi.DRIVER.verify(self.request(), Runner(dry_run=False))
        self.assertTrue(result.ok, result.summary)


class EnvRendering(unittest.TestCase):
    def test_dial_address_is_split_into_host_and_port(self):
        self.assertEqual(pi.split_dial("tls://gateway.example:10702"),
                         ("gateway.example", 10702))
        self.assertEqual(pi.split_dial("gateway.example:10704"), ("gateway.example", 10704))

    def test_rendering_sets_keys_without_duplicating_them(self):
        rendered = pi.render_bridge_env(
            "VOICE_GATEWAY_HOST=CHANGE-ME\nVOICE_GATEWAY_PORT=10701\n",
            device_id="kitchen", host="gw", port=10702, name="kitchen",
        )
        self.assertEqual(rendered.count("VOICE_GATEWAY_HOST="), 1)
        self.assertIn("VOICE_GATEWAY_HOST=gw", rendered)
        self.assertIn("VOICE_GATEWAY_PORT=10702", rendered)

    def test_commented_keys_are_not_treated_as_settings(self):
        rendered = pi.render_bridge_env(
            "# VOICE_GATEWAY_HOST=CHANGE-ME\n", device_id="k", host="gw", port=10702, name="k"
        )
        self.assertIn("# VOICE_GATEWAY_HOST=CHANGE-ME", rendered)
        self.assertIn("\nVOICE_GATEWAY_HOST=gw", rendered)


class PlatformRefusal(unittest.TestCase):
    """Neither driver pretends to work where the shell scripts cannot run."""

    def test_writing_a_card_refuses_on_windows(self):
        if POSIX:
            self.skipTest("this assertion is about Windows")
        with self.assertRaises(UnsupportedOnThisPlatform):
            pi.DRIVER.flash(DeviceRequest(device_name="x", dial_address="tls://h:10702",
                                          card_mount="D:\\"), Runner(dry_run=True, repo_root=ROOT))

    def test_flashing_a_board_refuses_on_windows(self):
        if POSIX:
            self.skipTest("this assertion is about Windows")
        with self.assertRaises(UnsupportedOnThisPlatform):
            esp32.DRIVER.flash(DeviceRequest(device_name="x", dial_address="tls://h:10702",
                                             port="COM3"), Runner(dry_run=True, repo_root=ROOT))

    def test_the_refusal_names_a_way_forward(self):
        from hermes_installer.driver import require_posix

        if POSIX:
            self.skipTest("this assertion is about Windows")
        with self.assertRaises(UnsupportedOnThisPlatform) as caught:
            require_posix("Flashing")
        self.assertIn("WSL", str(caught.exception))


class ProfilesAgree(unittest.TestCase):
    def test_the_default_wake_word_is_the_same_phrase_on_both(self):
        self.assertEqual(esp32.PROFILE.wake_default, pi.PROFILE.wake_default)
        self.assertEqual(esp32.PROFILE.wake_default, "hey_hermes")

    def test_the_wake_formats_differ_and_that_is_the_point(self):
        self.assertNotEqual(esp32.PROFILE.wake_catalogue_format(),
                            pi.PROFILE.wake_catalogue_format())

    def test_both_report_which_settings_need_a_cable(self):
        for profile in (esp32.PROFILE, pi.PROFILE):
            self.assertIn("wifi", profile.cable_settings, profile.id)
            self.assertIn("playback_volume", profile.remote_settings, profile.id)

    def test_ids_are_unique(self):
        self.assertNotEqual(esp32.PROFILE.id, pi.PROFILE.id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
