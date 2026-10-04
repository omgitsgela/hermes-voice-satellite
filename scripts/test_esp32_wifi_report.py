#!/usr/bin/env python3
"""Host tests for scan-result rendering: ordering, escaping, hidden names, and bounds."""
import ctypes as C
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'firmware/esp32/components/wifi_report'

OPEN, WEP, WPA, WPA2, WPA3, ENTERPRISE = 0, 1, 2, 3, 4, 5


class AccessPoint(C.Structure):
    _fields_ = [('ssid', C.c_char * 32), ('ssid_length', C.c_ubyte),
                ('channel', C.c_ubyte), ('rssi', C.c_byte), ('auth', C.c_ubyte)]


def ap(name, channel=6, rssi=-50, auth=WPA2):
    raw = name if isinstance(name, bytes) else name.encode()
    record = AccessPoint()
    record.ssid = raw.ljust(32, b'\0')[:32]
    record.ssid_length = min(len(raw), 32)
    record.channel = channel
    record.rssi = rssi
    record.auth = auth
    return record


class WifiReportTests(unittest.TestCase):
    def library(self):
        self.assertTrue((CORE / 'wifi_report.c').exists(), 'renderer missing')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'wifi_report.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(CORE / 'wifi_report.c'), '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        lib.wifi_report_render.restype = C.c_size_t
        return lib

    def render(self, lib, points, capacity=2048):
        array = (AccessPoint * max(len(points), 1))()
        for index, point in enumerate(points):
            array[index] = point
        buffer = C.create_string_buffer(capacity + 1)
        buffer[capacity] = b'\xe7'  # canary: nothing may touch this byte
        written = lib.wifi_report_render(array, len(points), buffer, capacity)
        self.assertEqual(buffer[capacity], b'\xe7', 'the renderer wrote past its capacity')
        self.assertLess(written, capacity, 'the reported length must leave room for the terminator')
        self.assertEqual(len(buffer.value), written, 'the reported length must match what was written')
        return buffer.value.decode()

    def test_the_append_primitive_enforces_its_own_bounds(self):
        """The single place bounds are enforced, tested directly with a canary."""
        lib = self.library()
        lib.wifi_report_append.restype = C.c_size_t
        lib.wifi_report_append.argtypes = [C.c_char_p, C.c_size_t, C.c_size_t, C.c_char_p]

        def call(capacity, used, text):
            buffer = C.create_string_buffer(capacity + 1)
            C.memset(buffer, 0x5a, capacity + 1)
            buffer[capacity] = b'\xe7'
            written = lib.wifi_report_append(buffer, capacity, used, text.encode())
            self.assertEqual(buffer[capacity], b'\xe7', 'the append wrote past its capacity')
            self.assertLess(written, capacity, 'the returned length must leave room for a terminator')
            self.assertEqual(buffer[written], b'\0',
                             'the result must be NUL-terminated at the returned length')
            return written, bytes(buffer.raw[:written])

        self.assertEqual(call(16, 0, 'hello'), (5, b'hello'))
        # A full buffer takes nothing more, and says so rather than overrunning.
        written, stored = call(8, 7, 'xyz')
        self.assertEqual(written, 7)
        self.assertEqual(len(stored), 7)
        # Text longer than the room is truncated, never overflowed.
        self.assertEqual(call(8, 0, 'abcdefghij'), (7, b'abcdefg'))
        # Degenerate capacities: only the reported length and the canary are meaningful.
        self.assertEqual(call(2, 1, 'abc')[0], 1)
        self.assertEqual(call(1, 0, 'abc')[0], 0)

    def test_empty_scan_is_explicit(self):
        lib = self.library()
        self.assertEqual(self.render(lib, []), 'NO NETWORKS\n')

    def test_strongest_network_is_first_and_the_rest_follow(self):
        lib = self.library()
        output = self.render(lib, [ap('weak', rssi=-88), ap('strong', rssi=-31),
                                   ap('middling', rssi=-60)])
        self.assertEqual([line.split()[0] for line in output.strip().splitlines()],
                         ['strong', 'middling', 'weak'])

    def test_equal_strength_sorts_by_name_so_runs_are_repeatable(self):
        lib = self.library()
        output = self.render(lib, [ap('zebra', rssi=-50), ap('alpha', rssi=-50)])
        self.assertEqual([line.split()[0] for line in output.strip().splitlines()],
                         ['alpha', 'zebra'])

    def test_fields_are_reported(self):
        lib = self.library()
        output = self.render(lib, [ap('GL-Test-AP', channel=6, rssi=-42, auth=WPA2)])
        self.assertEqual(output, 'GL-Test-AP ch=6 rssi=-42 auth=WPA2\n')

    def test_hidden_networks_are_listed_not_dropped(self):
        lib = self.library()
        output = self.render(lib, [ap(b'', channel=11, rssi=-55)])
        self.assertIn('(hidden)', output)
        self.assertIn('ch=11', output)

    def test_names_with_spaces_or_control_bytes_are_escaped(self):
        lib = self.library()
        output = self.render(lib, [ap('my net', channel=1, rssi=-60),
                                   ap(b'tab\ttab', channel=1, rssi=-70),
                                   ap(b'high\xffbyte', channel=1, rssi=-80)])
        lines = output.strip().splitlines()
        self.assertEqual(lines[0].split()[0], 'my\\x20net')
        self.assertEqual(lines[1].split()[0], 'tab\\x09tab')
        self.assertEqual(lines[2].split()[0], 'high\\xffbyte')
        # Every field must stay whitespace-free so a line splits naively.
        for line in lines:
            self.assertEqual(len(line.split()), 4, line)

    def test_a_full_length_name_still_renders_whole(self):
        lib = self.library()
        long_name = 'a' * 32
        output = self.render(lib, [ap(long_name, channel=36, rssi=-40)])
        self.assertTrue(output.startswith(long_name + ' ch=36'), output)

    def test_all_authentication_modes_have_names(self):
        lib = self.library()
        points = [ap(f'net{i}', rssi=-30 - i, auth=mode) for i, mode in
                  enumerate([OPEN, WEP, WPA, WPA2, WPA3, ENTERPRISE])]
        output = self.render(lib, points)
        for expected in ('OPEN', 'WEP', 'WPA2', 'WPA3', 'ENT'):
            self.assertIn(f'auth={expected}', output)

    def test_a_small_buffer_truncates_safely_and_says_so(self):
        lib = self.library()
        points = [ap(f'network-{i:02d}', rssi=-30 - i) for i in range(20)]
        for capacity in (32, 64, 100, 200, 1024):
            with self.subTest(capacity=capacity):
                output = self.render(lib, points, capacity=capacity)
                lines = output.strip().splitlines() if output.strip() else []
                truncated = [line for line in lines if line.startswith('TRUNCATED')]
                shown = [line for line in lines if not line.startswith('TRUNCATED')]
                for line in shown:
                    self.assertEqual(len(line.split()), 4, line)
                if truncated:
                    omitted = int(truncated[-1].split()[1])
                    self.assertEqual(len(shown) + omitted, len(points),
                                     'every network must be either shown or counted as omitted')
                    self.assertNotIn('network-19', output,
                                     'the weakest network cannot both be omitted and rendered')
                else:
                    self.assertEqual(len(shown), len(points),
                                     'without a truncation line every network must be present')

    def test_truncation_count_matches_what_was_omitted(self):
        lib = self.library()
        points = [ap(f'net-{i:02d}', rssi=-30 - i) for i in range(6)]
        output = self.render(lib, points, capacity=110)
        shown = [line for line in output.strip().splitlines() if not line.startswith('TRUNCATED')]
        omitted = int(output.strip().splitlines()[-1].split()[1])
        self.assertEqual(len(shown) + omitted, len(points))

    def test_no_capacity_is_safe(self):
        lib = self.library()
        array = (AccessPoint * 1)()
        array[0] = ap('anything')
        buffer = C.create_string_buffer(4)
        buffer[0] = b'\xe7'
        self.assertEqual(lib.wifi_report_render(array, 1, buffer, 0), 0)
        self.assertEqual(buffer[0], b'\xe7', 'a zero-capacity call must not write anything')

    def test_no_networks_is_safe(self):
        lib = self.library()
        array = (AccessPoint * 1)()
        buffer = C.create_string_buffer(16)
        written = lib.wifi_report_render(array, 0, buffer, len(buffer))
        self.assertEqual(buffer.value, b'NO NETWORKS\n')
        self.assertEqual(written, len(b'NO NETWORKS\n'))


class ConnectOutcome(C.Structure):
    _fields_ = [('connected', C.c_int), ('ssid', C.c_char_p), ('ip', C.c_char_p),
                ('channel', C.c_int), ('rssi', C.c_int), ('reason', C.c_int)]


class WifiConnectReportTests(unittest.TestCase):
    """Rendering for one join attempt: where it landed, or why it did not.

    A reason code is the difference between "wrong passphrase" and "wrong network", so the
    report names the common ones and falls back to the bare number rather than inventing a
    description for a code this build does not know.
    """

    def library(self):
        self.assertTrue((CORE / 'wifi_report.c').exists(), 'renderer missing')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'wifi_report.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(CORE / 'wifi_report.c'), '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        lib.wifi_connect_report_render.restype = C.c_size_t
        lib.wifi_connect_report_render.argtypes = [C.POINTER(ConnectOutcome), C.c_char_p, C.c_size_t]
        lib.wifi_connect_reason_name.restype = C.c_char_p
        lib.wifi_connect_reason_name.argtypes = [C.c_int]
        return lib

    def render(self, lib, connected, ssid=None, ip=None, channel=-1, rssi=0, reason=0, capacity=256):
        outcome = ConnectOutcome(connected,
                                 ssid.encode() if isinstance(ssid, str) else ssid,
                                 ip.encode() if isinstance(ip, str) else ip,
                                 channel, rssi, reason)
        buffer = C.create_string_buffer(capacity + 1)
        buffer[capacity] = b'\xe7'  # canary
        written = lib.wifi_connect_report_render(C.byref(outcome), buffer, capacity)
        self.assertEqual(buffer[capacity], b'\xe7', 'the renderer wrote past its capacity')
        self.assertLess(written, capacity, 'the reported length must leave room for the terminator')
        self.assertEqual(len(buffer.value), written, 'the reported length must match what was written')
        return buffer.value.decode()

    def test_a_successful_join_reports_where_it_landed(self):
        lib = self.library()
        line = self.render(lib, 1, ssid='GL-Test-AP', ip='192.0.2.51', channel=11, rssi=-40)
        self.assertEqual(line, 'status=connected ssid=GL-Test-AP ip=192.0.2.51 channel=11 rssi=-40')

    def test_a_failed_join_names_the_reason(self):
        lib = self.library()
        self.assertEqual(self.render(lib, 0, reason=201), 'status=failed reason=201:NO_AP_FOUND')
        self.assertEqual(self.render(lib, 0, reason=15), 'status=failed reason=15:4WAY_HANDSHAKE_TIMEOUT')
        self.assertEqual(self.render(lib, 0, reason=202), 'status=failed reason=202:AUTH_FAIL')

    def test_an_unknown_reason_is_reported_as_a_bare_number(self):
        lib = self.library()
        self.assertIsNone(lib.wifi_connect_reason_name(1234))
        self.assertEqual(self.render(lib, 0, reason=1234), 'status=failed reason=1234')

    def test_a_join_that_never_reported_a_disconnect_says_reason_none(self):
        lib = self.library()
        self.assertEqual(self.render(lib, 0, reason=0), 'status=failed reason=none')

    def test_an_absent_outcome_still_renders_a_complete_line(self):
        lib = self.library()
        buffer = C.create_string_buffer(64)
        written = lib.wifi_connect_report_render(None, buffer, 64)
        self.assertEqual(buffer.value, b'status=failed reason=none')
        self.assertEqual(written, len(buffer.value))

    def test_the_joined_name_is_escaped_like_a_scan_line(self):
        """A SSID is arbitrary bytes; the console protocol is printable ASCII fields."""
        lib = self.library()
        line = self.render(lib, 1, ssid=b'my net', ip='10.0.0.2', channel=6, rssi=-60)
        self.assertEqual(line, 'status=connected ssid=my\\x20net ip=10.0.0.2 channel=6 rssi=-60')

    def test_a_missing_name_or_address_is_stated_not_omitted(self):
        lib = self.library()
        self.assertEqual(self.render(lib, 1, ssid=None, ip=None, channel=6, rssi=-60),
                         'status=connected ssid=(none) ip=(none) channel=6 rssi=-60')

    def test_the_connect_report_enforces_its_bounds(self):
        lib = self.library()
        for capacity in (1, 2, 8, 16, 33, 64):
            line = self.render(lib, 1, ssid='GL-Test-AP', ip='192.0.2.51',
                               channel=11, rssi=-40, capacity=capacity)
            self.assertLess(len(line), capacity, f'capacity {capacity} must not be overrun')
            self.assertFalse(line.endswith('\\'), f'capacity {capacity} left a lone backslash')
            self.assertIsNone(re.search(r'\\x[0-9a-f]?$', line),
                              f'capacity {capacity} left a partial escape behind')


class ScanDeliveryContractTests(unittest.TestCase):
    """The console's scan reply path must deliver everything the renderer can honestly produce.

    The scan buffer and the reply cap must be one shared constant: the renderer fills its buffer
    up to capacity-1 bytes (including its TRUNCATED line) when many networks are visible, so a
    reply cap smaller than the buffer silently drops a large-but-valid report between WIFI SCAN
    BEGIN and WIFI SCAN END -- zero networks, no truncation note, exit 0.
    """

    SCAN_HEADER = ROOT / 'firmware/esp32/main/wifi_station.h'
    CONSOLE = ROOT / 'firmware/esp32/main/provisioning_console.c'

    def report_cap(self):
        text = self.SCAN_HEADER.read_text()
        match = re.search(r'#define\s+WIFI_STATION_SCAN_REPORT_MAX\s+(\d+)', text)
        self.assertIsNotNone(match,
                             'wifi_station.h must define WIFI_STATION_SCAN_REPORT_MAX, the single '
                             'size shared by the scan buffer and the console reply cap')
        return int(match.group(1))

    def library(self):
        self.assertTrue((CORE / 'wifi_report.c').exists(), 'renderer missing')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'wifi_report.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(CORE / 'wifi_report.c'), '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        lib.wifi_report_render.restype = C.c_size_t
        return lib

    def test_the_console_shares_one_constant_for_buffer_and_reply_cap(self):
        cap = self.report_cap()
        console = self.CONSOLE.read_text()
        self.assertIn('report[WIFI_STATION_SCAN_REPORT_MAX]', console,
                      'the scan buffer must be sized by the shared constant')
        self.assertIn('length > WIFI_STATION_SCAN_REPORT_MAX', console,
                      'the reply cap must be the shared constant, not a separate literal')
        self.assertNotIn('length > 1200', console,
                         'a separate literal cap can drift back below the buffer and drop reports')

    def test_every_honest_report_fits_the_console_reply_budget(self):
        cap = self.report_cap()
        lib = self.library()
        # Worst case per line: a 32-octet SSID of unprintable bytes escapes to 128 characters,
        # plus a 3-digit channel, a 4-character rssi, and the longest auth name. Fill the whole
        # buffer with exactly that, the way a dense scan does, and the rendered report -- with
        # its TRUNCATED line -- must still fit the console's reply budget.
        points = [AccessPoint() for _ in range(32)]
        for index, point in enumerate(points):
            point.ssid = b'\x01' * 32
            point.ssid_length = 32
            point.channel = 1 + (index % 14)
            point.rssi = -128 + index
            point.auth = ENTERPRISE
        array = (AccessPoint * 32)()
        for index, point in enumerate(points):
            array[index] = point
        buffer = C.create_string_buffer(cap)
        written = lib.wifi_report_render(array, 32, buffer, cap)
        self.assertGreater(written, 1200,
                           'the fixture must actually exercise the drop window above the old '
                           'literal cap; a shorter report would prove nothing')
        self.assertLess(written, cap,
                        'an honest report must always fit the console reply budget, with its '
                        'TRUNCATED line intact')
        self.assertIn(b'TRUNCATED', buffer.value, 'the truncation note must survive the delivery')


if __name__ == '__main__':
    unittest.main()
