#!/usr/bin/env python3
"""Host tests for register-on-boot: the device should dial at boot only when it is both
provisioned (a gateway endpoint) and joinable (a network), and stay idle otherwise.

The whole policy is the pure function `provision_boot_decision`. It is the one place the boot
decision lives, so these tests pin down every "idle" reason, their order, and the rule that a
stored-but-currently-absent network still counts as "configured" (dialing it is what finds that
out; retrying the join is the caller's job, not the decision's).
"""
import ctypes as C
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'firmware/esp32/components/provisioning'

TLS_ENDPOINT = b'tls://192.0.2.10:10702'
TCP_ENDPOINT = b'tcp://192.0.2.10:10701'


class BootDecisionTests(unittest.TestCase):
    def library(self):
        self.assertTrue((CORE / 'provisioning.c').exists(), 'provisioning core missing')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'provision.so'
        # The production translation unit, compiled with the same warnings-as-errors the rest of
        # the host tests use, so a regression here is caught the same way it would be on target.
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(CORE / 'provisioning.c'), '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        lib.provision_boot_decision.argtypes = [C.c_char_p, C.c_int, C.c_char_p, C.c_size_t]
        lib.provision_boot_decision.restype = C.c_int
        return lib

    def decide(self, lib, endpoint, candidates):
        reason = C.create_string_buffer(24)
        code = lib.provision_boot_decision(endpoint, candidates, reason, len(reason))
        return code, reason.value

    def test_provisioned_and_joinable_is_go(self):
        lib = self.library()
        for endpoint in (TLS_ENDPOINT, TCP_ENDPOINT):
            for count in (1, 2, 4):
                with self.subTest(endpoint=endpoint, count=count):
                    self.assertEqual(self.decide(lib, endpoint, count), (0, b''),
                                     'a configured endpoint with a network is a GO')

    def test_no_network_is_idle(self):
        lib = self.library()
        self.assertEqual(self.decide(lib, TLS_ENDPOINT, 0), (1, b'NO_NETWORK'))

    def test_no_endpoint_is_idle(self):
        lib = self.library()
        # Nothing stored at all (""), and the NULL case (a read that returned empty).
        self.assertEqual(self.decide(lib, b'', 1), (1, b'NO_ENDPOINT'))
        self.assertEqual(self.decide(lib, None, 1), (1, b'NO_ENDPOINT'))

    def test_bad_endpoint_is_idle(self):
        lib = self.library()
        for value in (b'ftp://192.0.2.10:10702', b'tls://:10702', b'tls://192.0.2.10',
                      b'tls://192.0.2.10:0070', b'tls://192.0.2.10:65536', b'not an endpoint'):
            with self.subTest(value=value):
                self.assertEqual(self.decide(lib, value, 1), (1, b'BAD_ENDPOINT'),
                                 'an endpoint the parser refuses is not something to dial')

    def test_wifi_is_named_before_the_endpoint(self):
        """The idle reasons follow the console's START_GATEWAY order, so a boot report and a manual
        command tell the same story for the same configuration."""
        lib = self.library()
        # No network AND no endpoint: the Wi-Fi problem is what gets named.
        self.assertEqual(self.decide(lib, b'', 0), (1, b'NO_NETWORK'))
        # An unreadable store is named before any endpoint problem.
        self.assertEqual(self.decide(lib, b'', -1), (1, b'WIFI_UNREADABLE'))
        self.assertEqual(self.decide(lib, b'not an endpoint', -1), (1, b'WIFI_UNREADABLE'))
        # A bad endpoint is named only when the Wi-Fi is actually configured.
        self.assertEqual(self.decide(lib, b'not an endpoint', 1), (1, b'BAD_ENDPOINT'))

    def test_a_stored_network_that_is_off_is_still_go(self):
        """The decision is about what is *configured*, not what is in the air. A saved network whose
        AP is currently off is still one network, so the device GOs -- dialing is what discovers the
        absence, and the caller retries the join. Idling here would leave a provisioned satellite
        dark until someone poked it."""
        lib = self.library()
        # candidates is the configured count (>=1) regardless of whether the AP is beaconing now.
        self.assertEqual(self.decide(lib, TLS_ENDPOINT, 1), (0, b''))

    def test_the_reason_is_never_a_secret_and_never_truncated_mid_token(self):
        lib = self.library()
        # Even when the buffer is too small, the decision (return code) never depends on it.
        small = C.create_string_buffer(1)
        code = lib.provision_boot_decision(TLS_ENDPOINT, 0, small, len(small))
        self.assertEqual(code, 1, 'the decision stands even with no room for the reason')
        self.assertEqual(small.value, b'', 'no room, no reason; never a partial token')


if __name__ == '__main__':
    unittest.main()
