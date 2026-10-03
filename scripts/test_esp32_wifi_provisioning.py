#!/usr/bin/env python3
"""Host tests for Wi-Fi credential staging: validation, storage, and never echoing the secret."""
import ctypes as C
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'firmware/esp32/components/provisioning'
SAVE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p)
CLEAR = C.CFUNCTYPE(C.c_int, C.c_void_p)
LOAD = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t)

#: A real-shaped secret, used only to prove it never appears in a response.
SECRET = b'SuperSecret123'


class WifiProvisioningTests(unittest.TestCase):
    def library(self):
        self.assertTrue((CORE / 'provisioning.c').exists(), 'provisioning core missing')
        self.assertTrue((CORE / 'provisioning.h').exists(), 'provisioning header missing')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'provision.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(CORE / 'provisioning.c'), '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        lib.provision_wifi_ssid_valid.argtypes = [C.c_char_p]
        lib.provision_wifi_psk_valid.argtypes = [C.c_char_p]
        return lib

    def harness(self, lib):
        state = {'ssid': b'', 'psk': b'', 'fail': False, 'calls': [],
                 'lie_on_save': False, 'lie_on_clear': False}

        @SAVE
        def save_ssid(ctx, value):
            if state['fail']:
                return -1
            if state['lie_on_save']:
                state['calls'].append(('ssid-lie', value))
                return 0  # reports success without storing anything
            state['ssid'] = value
            state['calls'].append(('ssid', value))
            return 0

        @SAVE
        def save_psk(ctx, value):
            if state['fail']:
                return -1
            if state['lie_on_save']:
                state['calls'].append(('psk-lie', value))
                return 0
            state['psk'] = value
            state['calls'].append(('psk', value))
            return 0

        @CLEAR
        def clear(ctx):
            if state['fail']:
                return -1
            if state['lie_on_clear']:
                state['calls'].append(('clear-lie', None))
                return 0  # reports success without erasing anything
            state['ssid'] = b''
            state['psk'] = b''
            state['calls'].append(('clear', None))
            return 0

        @LOAD
        def load(ctx, ssid_out, ssid_capacity, psk_out, psk_capacity):
            for value, target, capacity in ((state['ssid'], ssid_out, ssid_capacity),
                                            (state['psk'], psk_out, psk_capacity)):
                data = value + b'\0'
                if len(data) > capacity:
                    return -1
                C.memmove(target, data, len(data))
            return 0

        lib.provision_wifi_handle.argtypes = [C.c_char_p, SAVE, SAVE, CLEAR, LOAD,
                                              C.c_void_p, C.c_void_p, C.c_size_t]

        def request(command):
            output = C.create_string_buffer(256)
            lib.provision_wifi_handle(command, save_ssid, save_psk, clear, load, None,
                                      output, len(output))
            return output.value

        return state, request

    def test_ssid_validation(self):
        lib = self.library()
        for value, expected in [(b'GL-Test-AP', True), (b'a', True), (b'a' * 32, True),
                                (b'a' * 33, False), (b'', False), (b'has space', True),
                                (b'bad\x01char', False), (b'bad\x7fchar', False),
                                (b'tab\there', False), (b'utf8\xc3\xa9', False)]:
            with self.subTest(ssid=value):
                self.assertEqual(bool(lib.provision_wifi_ssid_valid(value)), expected)

    def test_psk_validation(self):
        lib = self.library()
        for value, expected in [(b'12345678', True), (b'1234567', False), (b'p' * 63, True),
                                (b'p' * 64, False), (b'a' * 64, True), (b'0123456789abcdef' * 4, True),
                                (b'z' * 64, False), (b'has space here', True),
                                (b'bad\x01char', False), (b'', False)]:
            with self.subTest(psk=value):
                self.assertEqual(bool(lib.provision_wifi_psk_valid(value)), expected)

    def test_staging_each_field_and_never_echoing_the_secret(self):
        lib = self.library()
        state, request = self.harness(lib)
        self.assertEqual(request(b'WIFI_STATUS'), b'OK WIFI UNCONFIGURED')
        self.assertEqual(request(b'SET_WIFI_SSID GL-Test-AP'), b'OK WIFI SSID STAGED')
        self.assertEqual(state['ssid'], b'GL-Test-AP')
        self.assertEqual(state['psk'], b'', 'staging the SSID must not touch the PSK')
        # A partial configuration is reported as partial, never as ready.
        self.assertEqual(request(b'WIFI_STATUS'), b'OK WIFI ssid=GL-Test-AP psk=missing')

        reply = request(b'SET_WIFI_PSK ' + SECRET)
        self.assertEqual(reply, b'OK WIFI PSK STAGED')
        self.assertNotIn(SECRET, reply, 'the acknowledgment must never carry the secret')
        self.assertEqual(state['psk'], SECRET)
        self.assertEqual(request(b'WIFI_STATUS'), b'OK WIFI ssid=GL-Test-AP psk=staged')
        self.assertNotIn(SECRET, request(b'WIFI_STATUS'))

    def test_rejected_values_do_not_reach_storage(self):
        lib = self.library()
        state, request = self.harness(lib)
        self.assertEqual(request(b'SET_WIFI_SSID ' + b'a' * 33), b'ERR WIFI SSID')
        self.assertEqual(request(b'SET_WIFI_SSID'), b'ERR WIFI SSID')
        self.assertEqual(request(b'SET_WIFI_PSK 1234567'), b'ERR WIFI PSK')
        self.assertEqual(request(b'SET_WIFI_PSK'), b'ERR WIFI PSK')
        self.assertEqual(request(b'SET_WIFI_PSK ' + b'z' * 64), b'ERR WIFI PSK')
        self.assertEqual(state['calls'], [], 'nothing invalid may be written')

    def test_storage_failure_is_reported_and_not_hidden(self):
        lib = self.library()
        state, request = self.harness(lib)
        state['fail'] = True
        self.assertEqual(request(b'SET_WIFI_SSID GL-Test-AP'), b'ERR STORAGE')
        self.assertEqual(state['ssid'], b'')
        state['fail'] = False
        self.assertEqual(request(b'SET_WIFI_SSID GL-Test-AP'), b'OK WIFI SSID STAGED')
        state['fail'] = True
        self.assertEqual(request(b'CLEAR_WIFI'), b'ERR STORAGE')
        self.assertEqual(state['ssid'], b'GL-Test-AP', 'a failed clear must not pretend to have cleared')

    def test_clear_removes_both_fields(self):
        lib = self.library()
        state, request = self.harness(lib)
        request(b'SET_WIFI_SSID GL-Test-AP')
        request(b'SET_WIFI_PSK ' + SECRET)
        self.assertEqual(request(b'CLEAR_WIFI'), b'OK WIFI CLEARED')
        self.assertEqual(state['ssid'], b'')
        self.assertEqual(state['psk'], b'')
        self.assertEqual(request(b'WIFI_STATUS'), b'OK WIFI UNCONFIGURED')

    def test_a_store_that_lies_is_caught_by_the_readback(self):
        """A save that reports success without storing must not be acknowledged."""
        lib = self.library()
        state, request = self.harness(lib)
        state['lie_on_save'] = True
        self.assertEqual(request(b'SET_WIFI_SSID GL-Test-AP'), b'ERR STORAGE')
        self.assertEqual(request(b'SET_WIFI_PSK ' + SECRET), b'ERR STORAGE')
        self.assertNotIn(SECRET, request(b'WIFI_STATUS'))
        self.assertEqual(state['ssid'], b'', 'nothing was actually stored')

    def test_a_clear_that_lies_is_caught_by_the_readback(self):
        """A clear that reports success without erasing must not be acknowledged."""
        lib = self.library()
        state, request = self.harness(lib)
        request(b'SET_WIFI_SSID GL-Test-AP')
        request(b'SET_WIFI_PSK ' + SECRET)
        state['lie_on_clear'] = True
        self.assertEqual(request(b'CLEAR_WIFI'), b'ERR STORAGE')
        self.assertEqual(request(b'WIFI_STATUS'), b'OK WIFI ssid=GL-Test-AP psk=staged',
                         'a failed clear must still report the credentials that are really there')

    def test_unknown_commands_are_refused_not_guessed(self):
        lib = self.library()
        state, request = self.harness(lib)
        for command in (b'SET_WIFI', b'SET_WIFI_SSIDX rawr', b'WIFI', b'', b'SET_WIFI_PSKX abc'):
            with self.subTest(command=command):
                self.assertEqual(request(command), b'ERR COMMAND')
        self.assertEqual(state['calls'], [])


if __name__ == '__main__':
    unittest.main()
