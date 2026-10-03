#!/usr/bin/env python3
"""Host tests for bounded USB endpoint staging and persistence acknowledgments."""
import ctypes as C
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'firmware/esp32/components/provisioning'
SAVE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p)
LOAD = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p, C.c_size_t)


class ProvisioningTests(unittest.TestCase):
    def test_staged_endpoint_reads_back_without_connecting(self):
        self.assertTrue((CORE / 'provisioning.c').exists(), 'USB provisioning core missing')
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / 'provision.so'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared',
                            '-fPIC', str(CORE / 'provisioning.c'), '-o', str(library)], check=True)
            lib = C.CDLL(str(library))
            lib.provision_handle.argtypes = [C.c_char_p, SAVE, LOAD, C.c_void_p,
                                            C.c_void_p, C.c_size_t]
            state = {'endpoint': b'', 'fail': False}

            @SAVE
            def save(ctx, endpoint):
                if state['fail']:
                    return -1
                state['endpoint'] = endpoint
                return 0

            @LOAD
            def load(ctx, target, capacity):
                data = state['endpoint'] + b'\0'
                if len(data) > capacity:
                    return -1
                C.memmove(target, data, len(data))
                return 0

            def request(command):
                output = C.create_string_buffer(256)
                lib.provision_handle(command, save, load, None, output, len(output))
                return output.value

            self.assertEqual(request(b'STATUS'), b'OK UNCONFIGURED')
            self.assertEqual(request(b'SET_ENDPOINT tcp://192.0.2.1:10701'), b'OK SAVED STAGED')
            self.assertEqual(request(b'STATUS'), b'OK ENDPOINT tcp://192.0.2.1:10701 STAGED')
            state['fail'] = True
            self.assertEqual(request(b'SET_ENDPOINT tls://example.org:443'), b'ERR STORAGE')
            self.assertIn(b'192.0.2.1', request(b'STATUS'))
            state['fail'] = False
            self.assertEqual(request(b'CLEAR_ENDPOINT'), b'OK SAVED STAGED')
            self.assertEqual(request(b'STATUS'), b'OK UNCONFIGURED')
            invalid = [b'', b'http://example.org:443', b'tcp://user@example.org:10',
                       b'tcp://example.org', b'tcp://example.org:0',
                       b'tcp://example.org:65536', b'tcp://example.org:+10',
                       b'tcp://example.org:1/path', b'tcp://-bad.example:1',
                       b'tcp://bad..example:1', b'tcp://bad_.example:1',
                       b'tcp://' + b'a' * 64 + b':1', b'tcp://example.org:00001',
                       b'tcp://example.org:10\nSTATUS', b'tcp://example.org:10 ']
            for endpoint in invalid:
                with self.subTest(endpoint=endpoint):
                    self.assertEqual(request(b'SET_ENDPOINT ' + endpoint), b'ERR ENDPOINT')
                    self.assertEqual(state['endpoint'], b'')
            for endpoint in [b'tls://example.org:443', b'tcp://localhost:1',
                             b'tcp://192.0.2.1:65535']:
                self.assertEqual(request(b'SET_ENDPOINT ' + endpoint), b'OK SAVED STAGED')
            state['endpoint'] = b'corrupt-stored-value'
            self.assertEqual(request(b'STATUS'), b'ERR STORAGE')

    def test_stream_rejects_oversize_and_control_bytes_then_resynchronizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'lines.c'
            source.write_text('''#include "provisioning.h"
#include <assert.h>
#include <string.h>
int main(void) {
    provision_line line = {0};
    for (int i = 0; i < 300; ++i) assert(provision_line_feed(&line, 'x') == 0);
    assert(provision_line_feed(&line, '\\n') == -1);
    const char *status = "STATUS";
    for (unsigned i = 0; i < strlen(status); ++i) assert(provision_line_feed(&line, status[i]) == 0);
    assert(provision_line_feed(&line, '\\n') == 1);
    assert(strcmp(line.data, "STATUS") == 0);
    assert(provision_line_feed(&line, 0) == 0);
    assert(provision_line_feed(&line, '\\n') == -1);
    assert(provision_line_feed(&line, 'S') == 0);
    provision_line_expire(&line);
    assert(provision_line_feed(&line, '\\n') == -1);
    assert(provision_line_feed(&line, '\\n') == 1);
    return 0;
}
''')
            executable = Path(tmp) / 'lines'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-I' + str(CORE), str(source), str(CORE / 'provisioning.c'),
                            '-o', str(executable)], check=True)
            subprocess.run([str(executable)], check=True)


class EndpointParseTests(unittest.TestCase):
    """Turning a staged endpoint into a host and port, with one source of truth.

    Staging and connecting must agree on what a valid endpoint is. If the validator and the
    parser could disagree, a stored endpoint could pass staging and then be unconnectable,
    which is the worst of both: the console says STAGED and the transport cannot use it.
    """

    VALID = [b'tcp://192.0.2.1:10701', b'tls://example.org:443', b'tcp://a:1',
             b'tcp://host-name.example:65535', b'tcp://H2.Example:80']
    INVALID = [b'', b'tcp://', b'tcp://host', b'tcp://:10701', b'tcp://host:0',
               b'tcp://host:65536', b'tcp://host:007', b'http://host:80', b'tcp://-host:80',
               b'tcp://host-:80', b'tcp://host..name:80', b'tcp://ho st:80',
               b'tcp://' + b'a' * 64 + b':80', b'tcp://host:80x', b'tcp://host:123456',
               b'tcp://host:']

    def library(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'provision.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(CORE / 'provisioning.c'), '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        lib.provision_endpoint_parse.argtypes = [C.c_char_p, C.c_char_p, C.c_size_t,
                                                 C.POINTER(C.c_uint), C.POINTER(C.c_int)]
        lib.provision_endpoint_parse.restype = C.c_int
        return lib

    def parse(self, lib, value, host_capacity=128):
        host = C.create_string_buffer(host_capacity)
        port = C.c_uint(0)
        transport = C.c_int(-1)
        result = lib.provision_endpoint_parse(value, host, host_capacity,
                                              C.byref(port), C.byref(transport))
        return result, (host.value.decode() if result == 0 else None), port.value, transport.value

    def test_a_tcp_endpoint_parses_into_host_and_port(self):
        lib = self.library()
        result, host, port, transport = self.parse(lib, b'tcp://192.0.2.1:10701')
        self.assertEqual((result, host, port, transport), (0, '192.0.2.1', 10701, 0))

    def test_a_tls_endpoint_is_marked_as_tls(self):
        lib = self.library()
        result, host, port, transport = self.parse(lib, b'tls://example.org:443')
        self.assertEqual((result, host, port, transport), (0, 'example.org', 443, 1))

    def test_host_characters_are_preserved_exactly(self):
        """The host is handed to the resolver verbatim, not normalised."""
        lib = self.library()
        result, host, port, _ = self.parse(lib, b'tcp://Host-Name.Example:65535')
        self.assertEqual((result, host, port), (0, 'Host-Name.Example', 65535))

    def test_a_host_that_does_not_fit_is_refused_rather_than_truncated(self):
        """A silently shortened host would connect somewhere else, or nowhere."""
        lib = self.library()
        result, host, _, _ = self.parse(lib, b'tcp://example.org:80', host_capacity=8)
        self.assertEqual(result, -1)
        self.assertIsNone(host)

    def test_the_parser_accepts_exactly_what_staging_accepts(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / 'provision.so'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared',
                            '-fPIC', str(CORE / 'provisioning.c'), '-o', str(library)],
                           check=True)
            lib = C.CDLL(str(library))
            lib.provision_endpoint_parse.argtypes = [C.c_char_p, C.c_char_p, C.c_size_t,
                                                     C.POINTER(C.c_uint), C.POINTER(C.c_int)]
            lib.provision_endpoint_parse.restype = C.c_int
            lib.provision_handle.argtypes = [C.c_char_p, SAVE, LOAD, C.c_void_p,
                                            C.c_void_p, C.c_size_t]
            state = {'endpoint': b''}

            @SAVE
            def save(ctx, endpoint):
                state['endpoint'] = endpoint
                return 0

            @LOAD
            def load(ctx, target, capacity):
                data = state['endpoint'] + b'\0'
                if len(data) > capacity:
                    return -1
                C.memmove(target, data, len(data))
                return 0

            def staging_accepts(value):
                output = C.create_string_buffer(256)
                lib.provision_handle(b'SET_ENDPOINT ' + value, save, load, None,
                                     output, len(output))
                return output.value == b'OK SAVED STAGED'

            for value in self.VALID + self.INVALID:
                parsed = self.parse(lib, value)[0] == 0
                self.assertEqual(parsed, staging_accepts(value),
                                 f'parser and staging disagree about {value!r}')
            self.assertEqual([v for v in self.VALID if not staging_accepts(v)], [],
                             'the valid corpus must actually be accepted')

    def test_the_host_buffer_is_never_written_when_parsing_fails(self):
        lib = self.library()
        host = C.create_string_buffer(b'canary-value', 16)
        port = C.c_uint(0)
        transport = C.c_int(-1)
        for value in [b'not-an-endpoint', b'tcp://host:0', b'tcp://' + b'a' * 64 + b':80']:
            self.assertEqual(lib.provision_endpoint_parse(value, host, 16, C.byref(port),
                                                          C.byref(transport)), -1)
            self.assertEqual(host.value, b'canary-value', f'{value!r} touched the host buffer')


if __name__ == '__main__':
    unittest.main()
