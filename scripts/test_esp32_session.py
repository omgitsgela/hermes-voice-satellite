#!/usr/bin/env python3
"""Compile production session core; exchange canonical Wyoming control frames."""
import asyncio
import ctypes as C
import socket
import sys
import time
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from wyoming.event import Event, read_event, write_event
from wyoming.info import Info

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / 'firmware/esp32/components'
IO = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p, C.c_size_t)


class SessionTests(unittest.TestCase):
    def library(self):
        self.assertTrue((COMP / 'wyoming_session/wyoming_session.c').exists(), 'session core missing')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'session.so'
        parser = COMP / 'wyoming_json'
        includes = [COMP / 'wyoming_wire', parser, parser / 'vendor/cjson', COMP / 'wyoming_session']
        sources = [COMP / 'wyoming_wire/wyoming_wire.c', parser / 'wyoming_json.c',
                   parser / 'vendor/cjson/cJSON.c', COMP / 'wyoming_session/wyoming_session.c']
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        '-DCJSON_NESTING_LIMIT=16', *['-I' + str(p) for p in includes],
                        *map(str, sources), '-lm', '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        lib.wy_session_size.restype = C.c_size_t
        lib.wy_session_init.argtypes = [C.c_void_p, C.c_char_p, C.c_char_p]
        lib.wy_session_state.argtypes = [C.c_void_p]
        lib.wy_session_step.argtypes = [C.c_void_p, IO, IO, C.c_void_p]
        return lib

    def test_registration_is_not_arming(self):
        lib = self.library()
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)
        self.assertEqual(lib.wy_session_state(session), 0)
        source = io.BytesIO()
        for event in [Event(type='describe'), Event(type='hermes.register.ok',
                      data={'device_id': 'control-fixture', 'protocol_version': 1}),
                      Event(type='run-satellite'), Event(type='pause-satellite')]:
            write_event(event, source)
        source.seek(0)
        output = io.BytesIO()

        @IO
        def reader(ctx, target, length):
            chunk = source.read(min(3, length))
            C.memmove(target, chunk, len(chunk))
            return len(chunk)

        @IO
        def writer(ctx, target, length):
            chunk = C.string_at(target, min(2, length))
            output.write(chunk)
            return len(chunk)

        for expected in [1, 2, 3, 2]:
            self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
            self.assertEqual(lib.wy_session_state(session), expected)
        output.seek(0)
        info = read_event(output)
        registration = read_event(output)
        assert info is not None and registration is not None
        satellite = Info.from_event(info).satellite
        assert satellite is not None
        self.assertEqual(satellite.name, 'ESP32 control probe')
        self.assertEqual(info.type, 'info')
        self.assertFalse(info.data['satellite']['supports_trigger'])
        self.assertEqual(registration.type, 'hermes.register')
        self.assertEqual(registration.data['device_id'], 'control-fixture')
        self.assertEqual(registration.data['capabilities'], [])
        self.assertIsNone(read_event(output))


    def test_fail_closed_and_fresh_reconnect(self):
        lib = self.library()
        good_ack = {'device_id': 'control-fixture', 'protocol_version': 1}
        for events, expected in [
            ([Event(type='run-satellite')], -1),
            ([Event(type='pause-satellite')], -1),
            ([Event(type='hermes.register.ok', data=good_ack)], -1),
            ([Event(type='describe'), Event(type='hermes.register.ok',
               data={'device_id': 'another-device', 'protocol_version': 1})], -1),
            ([Event(type='describe'), Event(type='hermes.register.ok',
               data={'device_id': 'control-fixture', 'protocol_version': 2})], -1),
            ([Event(type='describe'), Event(type='hermes.register.ok',
               data={'device_id': 'control-fixture', 'protocol_version': True})], -1),
            ([Event(type='describe'), Event(type='hermes.register.refused', data={'reason': 'test'})], -4),
            ([Event(type='describe'), Event(type='hermes.register.ok', data=good_ack),
               Event(type='audio-chunk', payload=b'not audio')], -1),
            # Fail-soft is limited to well-formed control events. Anything carrying a payload is
            # still refused: this core implements no audio and must never pretend otherwise.
            ([Event(type='describe'), Event(type='hermes.register.ok', data=good_ack),
               Event(type='some.unknown.event', payload=b'\x00\x01')], -1),
        ]:
            with self.subTest(events=[e.type for e in events]):
                session = C.create_string_buffer(lib.wy_session_size())
                self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)
                stream = io.BytesIO()
                for event in events:
                    write_event(event, stream)
                stream.seek(0)
                reads = 0

                @IO
                def reader(ctx, target, length):
                    nonlocal reads
                    reads += 1
                    chunk = stream.read(min(7, length))
                    C.memmove(target, chunk, len(chunk))
                    return len(chunk)

                @IO
                def writer(ctx, target, length):
                    return length

                for _ in events[:-1]:
                    self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
                self.assertEqual(lib.wy_session_step(session, reader, writer, None), expected)
                self.assertEqual(lib.wy_session_state(session), 4 if expected == -4 else 5)
                calls = reads
                self.assertEqual(lib.wy_session_step(session, reader, writer, None), expected)
                self.assertEqual(reads, calls, 'terminal state must not read again')
                self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-2'), 0)
                self.assertEqual(lib.wy_session_state(session), 0)
                self.assertNotEqual(lib.wy_session_step(session, reader, writer, None), 0)
                self.assertEqual(lib.wy_session_state(session), 5)

    def test_unrecognised_control_events_are_ignored_and_ping_answered(self):
        """A real bridge sends desired state right after registration.

        Failing on it made the device useless against the actual bridge. Unknown *control* events are
        ignored, a ping is answered, and the session keeps working; only malformed frames and anything
        carrying a payload still end it.
        """
        lib = self.library()
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)
        source = io.BytesIO()
        for event in [Event(type='describe'),
                      Event(type='hermes.register.ok', data={'device_id': 'control-fixture', 'protocol_version': 1}),
                      Event(type='hermes.config.desired', data={'generation': 3, 'settings': {'volume': 5}}),
                      Event(type='ping'),
                      Event(type='describe')]:
            write_event(event, source)
        source.seek(0)
        output = io.BytesIO()

        @IO
        def reader(ctx, target, length):
            chunk = source.read(min(5, length))
            C.memmove(target, chunk, len(chunk))
            return len(chunk)

        @IO
        def writer(ctx, target, length):
            chunk = C.string_at(target, min(9, length))
            output.write(chunk)
            return len(chunk)

        for expected in [1, 2, 2, 2, 2]:
            self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
            self.assertEqual(lib.wy_session_state(session), expected)
        self.assertEqual(lib.wy_session_state(session), 2, 'a registered session stays registered')

        output.seek(0)
        sent = []
        while (event := read_event(output)) is not None:
            sent.append(event)
        self.assertEqual([event.type for event in sent],
                         ['info', 'hermes.register', 'pong', 'info'])
        self.assertEqual(sent[2].data, {}, 'a pong carries no data')
        satellite = Info.from_event(sent[3]).satellite
        assert satellite is not None
        self.assertEqual(satellite.name, 'ESP32 control probe')

    def test_framing_and_transport_errors_stop_session(self):
        lib = self.library()
        for raw, fault in [
            (b'{"type":"hermes.register.ok","data_length":10}\n{}', None),
            (b'{"type":"run-satellite","data_length":4097}\n', None),
            (b'{"type":"run-satellite","data_length":13}\n{"v":1,"v":2}', None),
            (b'{"type":"describe"}\n', 0),
            (b'{"type":"describe"}\n', -1),
            (b'{"type":"describe"}\n', 'overshoot'),
        ]:
            with self.subTest(raw=raw, fault=fault):
                session = C.create_string_buffer(lib.wy_session_size())
                self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)
                stream = io.BytesIO(raw)

                @IO
                def reader(ctx, target, length):
                    chunk = stream.read(min(3, length))
                    C.memmove(target, chunk, len(chunk))
                    return len(chunk)

                @IO
                def writer(ctx, target, length):
                    if fault == 'overshoot':
                        return length + 1
                    return length if fault is None else fault

                self.assertNotEqual(lib.wy_session_step(session, reader, writer, None), 0)
                self.assertEqual(lib.wy_session_state(session), 5)

    def test_invalid_callback_disarms_session(self):
        lib = self.library()
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)

        @IO
        def writer(ctx, target, length):
            return length

        self.assertEqual(lib.wy_session_step(session, IO(), writer, None), -1)
        self.assertEqual(lib.wy_session_state(session), 5)

    def test_real_registry_over_loopback_tcp(self):
        sys.path.insert(0, str(ROOT))
        from bridge.registry import RegistryServer, SatelliteRegistry
        lib = self.library()

        async def fixture():
            registry = SatelliteRegistry()
            identities = []

            async def registered(slot):
                self.assertTrue(slot.ready)
                self.assertEqual(slot.identity.protocol_version, 1)
                self.assertEqual(slot.identity.capabilities, ())
                self.assertEqual(slot.identity.boot_id, 'tcp-boot-1')
                identities.append(slot.device_id)
                await slot.connection.send(Event(type='run-satellite'))
                await slot.connection.send(Event(type='pause-satellite'))

            server = RegistryServer(registry, '127.0.0.1', 0, on_registered=registered,
                                    registration_timeout=3)
            await server.start()
            assert server._server is not None
            tcp_server = server._server._server
            assert isinstance(tcp_server, asyncio.Server)
            port = tcp_server.sockets[0].getsockname()[1]
            callback_errors = []

            def client():
                session = C.create_string_buffer(lib.wy_session_size())
                self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'tcp-boot-1'), 0)
                with socket.create_connection(('127.0.0.1', port), timeout=3) as connection:
                    connection.settimeout(3)
                    deadline = time.monotonic() + 3

                    def remaining():
                        seconds = deadline - time.monotonic()
                        if seconds <= 0:
                            raise TimeoutError('control event deadline')
                        connection.settimeout(seconds)

                    @IO
                    def reader(ctx, target, length):
                        try:
                            remaining()
                            chunk = connection.recv(min(length, 3))
                            C.memmove(target, chunk, len(chunk))
                            return len(chunk)
                        except Exception as error:
                            callback_errors.append(str(error))
                            return -1

                    @IO
                    def writer(ctx, target, length):
                        try:
                            remaining()
                            return connection.send(C.string_at(target, min(length, 2)))
                        except Exception as error:
                            callback_errors.append(str(error))
                            return -1

                    states = []
                    for _ in range(4):
                        deadline = time.monotonic() + 3
                        self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
                        states.append(lib.wy_session_state(session))
                    return states

            try:
                states = await asyncio.wait_for(asyncio.to_thread(client), timeout=10)
                self.assertEqual(states, [1, 2, 3, 2])
                self.assertEqual(identities, ['control-fixture'])
                self.assertEqual(callback_errors, [])
            finally:
                await server.stop()

        asyncio.run(fixture())


    def test_state_and_result_names_cover_every_code(self):
        """A report is only useful if it names what happened, and an unnamed code is a hole."""
        lib = self.library()
        lib.wy_session_state_name.restype = C.c_char_p
        lib.wy_session_state_name.argtypes = [C.c_int]
        lib.wy_result_name.restype = C.c_char_p
        lib.wy_result_name.argtypes = [C.c_int]
        states = [lib.wy_session_state_name(index) for index in range(0, 6)]
        self.assertEqual(states, [b'describe', b'ack', b'registered', b'run_requested',
                                  b'refused', b'failed'])
        self.assertEqual(len(set(states)), 6, 'each state needs its own name')
        self.assertEqual(lib.wy_session_state_name(99), b'unknown')
        results = [lib.wy_result_name(code) for code in (0, -1, -2, -3, -4)]
        self.assertEqual(results, [b'ok', b'invalid', b'io', b'limit', b'refused'])
        self.assertEqual(len(set(results)), 5, 'each result needs its own name')
        self.assertEqual(lib.wy_result_name(7), b'unknown')


if __name__ == '__main__':
    unittest.main()
