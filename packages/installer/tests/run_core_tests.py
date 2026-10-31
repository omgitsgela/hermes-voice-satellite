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

    def test_a_quoted_false_is_not_a_boolean(self):
        # "false" is a truthy string, so casting it before validation advertised a cable-only
        # setting as remotely changeable.
        with self.assertRaises(ValueError) as caught:
            profile_from_dict({
                "id": "x", "display_name": "X", "vendor": "V", "flashing": "usb_serial",
                "wake_formats": ["microwakeword_tflite"], "voice": "none",
                "settings": {"wifi": {"apply": "cable", "remote": "false"}},
            })
        self.assertIn("remote", str(caught.exception))

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


class CredentialStoreSafety(unittest.TestCase):
    def test_the_file_fallback_really_is_owner_only(self):
        import os
        import stat
        import tempfile

        from hermes_installer.secrets import FileBackend

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            backend = FileBackend(path)
            backend.save("gw", "secret")
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if os.name == "posix":
                self.assertEqual(mode, 0o600)
                self.assertIn("0600", backend.name)
            self.assertEqual(backend.load("gw"), "secret")

    def test_a_failing_store_cannot_claim_the_words_were_forgotten(self):
        from hermes_installer.secrets import CredentialStore, StoreError

        class BrokenBackend:
            name = "broken"

            def save(self, account, secret):
                raise StoreError("locked")

            def load(self, account):
                return None

            def forget(self, account):
                raise StoreError("could not remove")

        store = CredentialStore(backend=BrokenBackend())
        with self.assertRaises(StoreError):
            store.apply_choice("gw", 10702, "words", remember=False)

    def test_an_unusable_keyring_falls_back_instead_of_failing_mid_pairing(self):
        import sys
        import types

        from hermes_installer.secrets import CredentialStore, FileBackend

        class LockedKeyring(types.ModuleType):
            def set_password(self, *args):
                raise RuntimeError("keyring is locked")

            def get_password(self, *args):
                return None

            def delete_password(self, *args):
                raise RuntimeError("keyring is locked")

        original = sys.modules.get("keyring")
        sys.modules["keyring"] = LockedKeyring("keyring")
        try:
            store = CredentialStore()
        finally:
            if original is None:
                sys.modules.pop("keyring", None)
            else:
                sys.modules["keyring"] = original
        self.assertIsInstance(store.backend, FileBackend)

    def test_the_read_modify_write_region_is_mutually_exclusive(self):
        # The bug this pins: a save reads the file, another request deletes an account, the save
        # writes its stale snapshot back, and the deleted secret returns. Counting overlapping
        # read-to-write regions across threads measures that property directly instead of hoping a
        # race reproduces.
        import tempfile
        import threading
        import time

        from hermes_installer.secrets import FileBackend

        with tempfile.TemporaryDirectory() as tmp:
            backend = FileBackend(Path(tmp) / "creds.json")
            backend.save("seed", "x")

            state = {"active": 0, "peak": 0}
            guard = threading.Lock()
            original_read, original_write = backend._read, backend._write

            def tagged_read():
                with guard:
                    state["active"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                return original_read()

            def tagged_write(data):
                try:
                    return original_write(data)
                finally:
                    with guard:
                        state["active"] -= 1

            backend._read, backend._write = tagged_read, tagged_write

            def worker(index):
                for step in range(25):
                    backend.save(f"gw{index}-{step}", "v")
                    if step % 5 == 0:
                        backend.forget(f"gw{index}-{step}")
                    time.sleep(0.001)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(state["peak"], 1,
                             "two read-modify-write regions overlapped, so a delete can be undone")

    def test_a_keyring_delete_that_fails_is_reported_not_swallowed(self):
        from hermes_installer.secrets import KeyringBackend, StoreError

        class Stub:
            def delete_password(self, service, account):
                raise RuntimeError("keyring daemon went away")

            def set_password(self, *args):
                pass

            def get_password(self, *args):
                return None

        backend = object.__new__(KeyringBackend)
        backend._keyring = Stub()
        with self.assertRaises(StoreError):
            backend.forget("gw")

    def test_a_keyring_entry_that_is_already_absent_is_not_an_error(self):
        from hermes_installer.secrets import KeyringBackend

        class PasswordDeleteError(Exception):
            pass

        class Stub:
            def delete_password(self, service, account):
                raise PasswordDeleteError("no such entry")

        backend = object.__new__(KeyringBackend)
        backend._keyring = Stub()
        backend.forget("gw")  # must not raise: absence is the desired end state


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
