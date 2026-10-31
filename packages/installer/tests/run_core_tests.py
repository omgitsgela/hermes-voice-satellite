"""Offline checks for the installer core: identity code, profile contract, pairing, remember box.

These run with the standard library only, on any OS, with no gateway and no device:

    python packages/installer/tests/run_core_tests.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from hermes_installer.identity import (  # noqa: E402
    ALPHABET,
    format_code,
    identity_code,
    matches,
    normalise_code,
)
from hermes_installer.pairing import (  # noqa: E402
    GatewayRef,
    PairingCancelled,
    PairingError,
    pair,
)
from hermes_installer.profile import DeviceProfile, Setting, profile_from_dict  # noqa: E402
from hermes_installer.registry import DeviceRegistry, RegistryError  # noqa: E402
from hermes_installer.secrets import Backend, CredentialStore  # noqa: E402

CERT_A = b"\x30\x82\x01\x0a" + bytes(range(256))
CERT_B = b"\x30\x82\x01\x0a" + bytes(reversed(range(256)))


class IdentityCode(unittest.TestCase):
    def test_shape_is_two_groups_of_four(self):
        code = identity_code(CERT_A)
        self.assertEqual(len(code), 9, code)
        self.assertEqual(code[4], "-")
        self.assertRegex(code, r"^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$")

    def test_alphabet_excludes_the_confusable_letters(self):
        for letter in "ILOU":
            self.assertNotIn(letter, ALPHABET)

    def test_deterministic_and_certificate_specific(self):
        self.assertEqual(identity_code(CERT_A), identity_code(CERT_A))
        self.assertNotEqual(identity_code(CERT_A), identity_code(CERT_B))

    def test_typing_is_forgiving_about_case_hyphens_and_lookalikes(self):
        code = identity_code(CERT_A)
        bare = code.replace("-", "")
        self.assertEqual(normalise_code(code), bare)
        self.assertEqual(normalise_code(bare.lower()), bare)
        self.assertEqual(normalise_code(f" {bare} "), bare)
        # A person who writes O for 0 or I for 1 still gets the right answer.
        swapped = bare.replace("0", "O").replace("1", "I")
        self.assertEqual(normalise_code(swapped), bare)

    def test_rubbish_is_rejected_with_a_reason(self):
        with self.assertRaises(ValueError):
            normalise_code("XK7F-9Q2$")

    def test_matching(self):
        code = identity_code(CERT_A)
        self.assertTrue(matches(CERT_A, code))
        self.assertTrue(matches(CERT_A, code.lower()))
        self.assertFalse(matches(CERT_B, code))
        self.assertFalse(matches(CERT_A, "0000-0000"))

    def test_no_certificate_is_an_error_not_a_silent_pass(self):
        with self.assertRaises(ValueError):
            identity_code(b"")

    def test_format_round_trip(self):
        self.assertEqual(format_code("XK7F9Q2M"), "XK7F-9Q2M")


class ProfileContract(unittest.TestCase):
    def profile(self, **overrides):
        base = dict(
            id="test-device",
            display_name="Test Device",
            vendor="Test Vendor",
            flashing="usb_serial",
            wake_formats=("microwakeword_tflite",),
            voice="none",
            settings={
                "playback_volume": Setting("playback_volume", "immediate", remote=True),
                "wifi": Setting("wifi", "cable", remote=False),
            },
        )
        base.update(overrides)
        return DeviceProfile(**base)

    def test_capabilities_partition_honestly(self):
        p = self.profile()
        self.assertEqual(p.remote_settings, ("playback_volume",))
        self.assertEqual(p.cable_settings, ("wifi",))

    def test_a_cable_setting_cannot_claim_to_be_remote(self):
        with self.assertRaises(ValueError) as caught:
            Setting("wifi", "cable", remote=True)
        self.assertIn("cannot also be remote", str(caught.exception))

    def test_unknown_apply_mode_names_the_setting(self):
        with self.assertRaises(ValueError) as caught:
            Setting("volume", "eventually", remote=True)
        self.assertIn("volume", str(caught.exception))

    def test_unknown_flashing_method_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.profile(flashing="telepathy")
        self.assertIn("flashing", str(caught.exception))

    def test_unknown_wake_format_is_refused(self):
        with self.assertRaises(ValueError):
            self.profile(wake_formats=("telepathy",))

    def test_sd_card_devices_are_flagged_as_needing_a_reader(self):
        self.assertTrue(self.profile(flashing="sd_card").needs_card_reader)
        self.assertFalse(self.profile().needs_card_reader)

    def test_from_dict_names_the_missing_field(self):
        with self.assertRaises(ValueError) as caught:
            profile_from_dict({"id": "x", "display_name": "X"})
        message = str(caught.exception)
        for field in ("vendor", "flashing", "wake_formats", "voice"):
            self.assertIn(field, message)

    def test_from_dict_builds_a_usable_profile(self):
        p = profile_from_dict(
            {
                "id": "esp32-test",
                "display_name": "ESP32 Test",
                "vendor": "Vendor",
                "flashing": "usb_serial",
                "wake_formats": ["microwakeword_tflite"],
                "voice": "local_piper",
                "settings": {"playback_volume": {"apply": "immediate", "remote": True}},
            }
        )
        self.assertEqual(p.remote_settings, ("playback_volume",))
        self.assertEqual(p.wake_catalogue_format(), "microwakeword_tflite")


class PairingOrder(unittest.TestCase):
    """The point of pairing is that no secret is sent before the gateway has proved itself."""

    class FakeTransport:
        def __init__(self, der=CERT_A, response=None):
            self.der = der
            self.response = response or {
                "ok": True,
                "ca_pem": "CA",
                "cert_pem": "CERT",
                "key_pem": "KEY",
                "server_pin": "PIN",
                "dial_address": "tls://gateway.example:10702",
            }
            self.posts = []
            self.closed = False

        def peer_certificate_der(self):
            return self.der

        def post_json(self, path, payload):
            self.posts.append((path, dict(payload)))
            return self.response

        def close(self):
            self.closed = True

    def test_nothing_is_sent_until_the_code_is_confirmed(self):
        seen = []
        transport = self.FakeTransport()

        def confirm(code):
            seen.append(code)
            # At this moment, the transport must not have been asked to post anything.
            self.assertEqual(transport.posts, [])
            return True

        creds = pair(
            GatewayRef("gateway.example"), "word1 word2 word3 word4 word5", "kitchen",
            confirm=confirm, transport=transport,
        )
        self.assertEqual(seen, [identity_code(CERT_A)])
        self.assertEqual(len(transport.posts), 1)
        self.assertEqual(transport.posts[0][0], "/pair")
        self.assertEqual(transport.posts[0][1]["device"], "kitchen")
        self.assertEqual(creds.identity_code, identity_code(CERT_A))
        self.assertEqual(creds.dial_address, "tls://gateway.example:10702")
        self.assertTrue(creds.is_complete())

    def test_a_declined_code_sends_nothing_at_all(self):
        transport = self.FakeTransport()
        with self.assertRaises(PairingCancelled):
            pair(GatewayRef("h"), "a b c d e", "kitchen", confirm=lambda code: False,
                 transport=transport)
        self.assertEqual(transport.posts, [])

    def test_the_words_never_travel_to_a_host_that_fails_the_comparison(self):
        transport = self.FakeTransport()
        with self.assertRaises(PairingCancelled):
            pair(GatewayRef("h"), "a b c d e", "kitchen", confirm=lambda code: True,
                 expected_code="0000-0000", transport=transport)
        self.assertEqual(transport.posts, [])

    def test_a_matching_expected_code_pairs_without_a_prompt(self):
        transport = self.FakeTransport()
        creds = pair(GatewayRef("h"), "a b c d e", "kitchen",
                     confirm=lambda code: self.fail("should not prompt when a code is expected"),
                     expected_code=identity_code(CERT_A), transport=transport)
        self.assertTrue(creds.is_complete())

    def test_empty_words_are_refused_before_connecting(self):
        with self.assertRaises(PairingError):
            pair(GatewayRef("h"), "   ", "kitchen", confirm=lambda code: True,
                 transport=self.FakeTransport())

    def test_a_missing_name_is_refused_before_connecting(self):
        with self.assertRaises(PairingError):
            pair(GatewayRef("h"), "a b c d e", "  ", confirm=lambda code: True,
                 transport=self.FakeTransport())

    def test_a_half_answer_is_an_error_not_a_partial_credential(self):
        transport = self.FakeTransport(response={"ok": True, "ca_pem": "CA", "cert_pem": "CERT"})
        with self.assertRaises(PairingError) as caught:
            pair(GatewayRef("h"), "a b c d e", "kitchen", confirm=lambda code: True,
                 transport=transport)
        self.assertIn("nothing has been written", str(caught.exception))


class RememberBox(unittest.TestCase):
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

    def test_unticking_the_box_removes_anything_previously_saved(self):
        backend = self.MemoryBackend()
        store = CredentialStore(backend=backend)
        self.assertEqual(store.apply_choice("gw", 10702, "w1 w2 w3 w4 w5", True), "memory")
        self.assertEqual(store.recall("gw", 10702), "w1 w2 w3 w4 w5")
        self.assertIsNone(store.apply_choice("gw", 10702, "w1 w2 w3 w4 w5", False))
        self.assertIsNone(store.recall("gw", 10702))

    def test_remembering_is_per_gateway(self):
        backend = self.MemoryBackend()
        store = CredentialStore(backend=backend)
        store.apply_choice("gw-a", 10702, "aaa", True)
        store.apply_choice("gw-b", 10702, "bbb", True)
        self.assertEqual(store.recall("gw-a", 10702), "aaa")
        self.assertEqual(store.recall("gw-b", 10702), "bbb")
        self.assertIsNone(store.recall("gw-c", 10702))

    def test_nothing_is_saved_when_the_box_was_never_ticked(self):
        backend = self.MemoryBackend()
        store = CredentialStore(backend=backend)
        store.apply_choice("gw", 10702, "secret", False)
        self.assertEqual(backend.data, {})


class Discovery(unittest.TestCase):
    def test_duplicate_ids_are_refused(self):
        registry = DeviceRegistry()
        profile = profile_from_dict(
            {"id": "dup", "display_name": "D", "vendor": "V", "flashing": "usb_serial",
             "wake_formats": ["microwakeword_tflite"], "voice": "none"}
        )
        registry.register(profile)
        with self.assertRaises(RegistryError):
            registry.register(profile)

    def test_a_profile_without_a_driver_says_so(self):
        registry = DeviceRegistry()
        registry.register(
            profile_from_dict(
                {"id": "no-driver", "display_name": "N", "vendor": "V", "flashing": "sd_card",
                 "wake_formats": ["openwakeword_onnx"], "voice": "local_piper"}
            )
        )
        with self.assertRaises(RegistryError) as caught:
            registry.driver("no-driver")
        self.assertIn("cannot be flashed yet", str(caught.exception))

    def test_unknown_device_lists_what_is_known(self):
        registry = DeviceRegistry()
        registry.register(
            profile_from_dict(
                {"id": "known", "display_name": "K", "vendor": "V", "flashing": "usb_serial",
                 "wake_formats": ["microwakeword_tflite"], "voice": "none"}
            )
        )
        with self.assertRaises(RegistryError) as caught:
            registry.get("unknown")
        self.assertIn("known", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
