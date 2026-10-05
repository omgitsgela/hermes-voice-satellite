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
from wyoming.audio import AudioChunk
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
        includes = [COMP / 'wyoming_wire', parser, parser / 'vendor/cjson', COMP / 'wyoming_session',
                    COMP / 'wyoming_audio']
        sources = [COMP / 'wyoming_wire/wyoming_wire.c', parser / 'wyoming_json.c',
                   parser / 'vendor/cjson/cJSON.c', COMP / 'wyoming_session/wyoming_session.c',
                   COMP / 'wyoming_audio/wyoming_audio.c']
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
        self.assertEqual(registration.data['capabilities'], ['microphone'])
        self.assertIsNone(read_event(output))

    def test_capture_turn_streams_mic_audio_and_stops_on_transcript(self):
        """The documented voice loop, device side: run-satellite arms the microphone, the local
        wake starts the stream (detection -> run-pipeline -> streaming-started), the capture
        task's audio leaves as 16 kHz / S16 / mono chunks, and the bridge's transcript stops it
        (streaming-stopped) while the microphone stays armed for the next wake."""
        lib = self.library()
        lib.wy_session_wake.argtypes = [C.c_void_p, C.c_char_p, IO, C.c_void_p]
        lib.wy_session_wake.restype = C.c_int
        lib.wy_session_send_audio.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t, IO, C.c_void_p]
        lib.wy_session_send_audio.restype = C.c_int
        lib.wy_session_streaming.argtypes = [C.c_void_p]
        lib.wy_session_streaming.restype = C.c_int
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)
        source = io.BytesIO()
        for event in [Event(type='describe'),
                      Event(type='hermes.register.ok',
                            data={'device_id': 'control-fixture', 'protocol_version': 1}),
                      Event(type='run-satellite'),
                      Event(type='transcript', data={'text': ''})]:
            write_event(event, source)
        source.seek(0)
        output = io.BytesIO()
        pcm = bytes(range(256)) * 8   # 2048 bytes: one full mic chunk

        @IO
        def reader(ctx, target, length):
            chunk = source.read(min(5, length))
            C.memmove(target, chunk, len(chunk))
            return len(chunk)

        @IO
        def writer(ctx, target, length):
            output.write(C.string_at(target, length))
            return length

        for expected in [1, 2, 3]:
            self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
            self.assertEqual(lib.wy_session_state(session), expected)
        # The handshake already wrote info + hermes.register; read them at the head (which also
        # advances the position past them) so the assertions below see only the wake sequence.
        output.seek(0)
        self.assertEqual(read_event(output).type, 'info')
        self.assertEqual(read_event(output).type, 'hermes.register')
        # The mic is closed until the local wake: audio sent now is dropped, not queued.
        self.assertEqual(lib.wy_session_send_audio(session, pcm, len(pcm), writer, None), -1)
        # The local wake fires: the device answers with the three streaming events, in order.
        wake_start = output.tell()   # the writer appends at the current position
        self.assertEqual(lib.wy_session_wake(session, b'hey-hermes', writer, None), 0)
        self.assertEqual(lib.wy_session_streaming(session), 1)
        output.seek(wake_start)
        events = []
        for _ in range(3):
            event = read_event(output)
            self.assertIsNotNone(event)
            events.append(event)
        self.assertEqual([e.type for e in events],
                         ['detection', 'run-pipeline', 'streaming-started'])
        self.assertEqual(events[0].data['name'], 'hey-hermes')
        self.assertEqual(events[1].data, {'start_stage': 'asr', 'end_stage': 'tts',
                                          'restart_on_end': False})
        # The capture task's chunk leaves immediately, on the same link.
        chunk_start = output.tell()
        self.assertEqual(lib.wy_session_send_audio(session, pcm, len(pcm), writer, None), 0)
        output.seek(chunk_start)
        chunk = AudioChunk.from_event(read_event(output))
        self.assertEqual((chunk.rate, chunk.width, chunk.channels), (16000, 2, 1))
        self.assertEqual(chunk.audio, pcm)
        # transcript: the utterance is over; the stream stops, the mic stays armed (state 3),
        # so the very next wake starts a fresh stream without another run-satellite.
        stop_start = output.tell()
        self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
        self.assertEqual(lib.wy_session_state(session), 3)
        self.assertEqual(lib.wy_session_streaming(session), 0)
        output.seek(stop_start)
        self.assertEqual(read_event(output).type, 'streaming-stopped')
        self.assertEqual(lib.wy_session_wake(session, b'hey-hermes', writer, None), 0)
        self.assertEqual(lib.wy_session_streaming(session), 1)

    def test_wake_now_opens_the_microphone(self):
        """The bridge's wake-now (the follow-up window's 'open the microphone') must run the
        wake sequence -- detection, run-pipeline, streaming-started -- with the device's wake
        word, because on this device the session core is where the Pi's wake shim lives. A
        wake-now that lands before the host is ready, or while the stream already runs, is
        dropped, never a link failure."""
        lib = self.library()
        lib.wy_session_send_audio.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t, IO, C.c_void_p]
        lib.wy_session_send_audio.restype = C.c_int
        lib.wy_session_streaming.argtypes = [C.c_void_p]
        lib.wy_session_streaming.restype = C.c_int
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)
        source = io.BytesIO()
        # One wake-now before the host is ready (dropped), and one after run-satellite (the
        # follow-up window opens the microphone). Both in the stream up front.
        for event in [Event(type='wake-now'),
                      Event(type='describe'),
                      Event(type='hermes.register.ok',
                            data={'device_id': 'control-fixture', 'protocol_version': 1}),
                      Event(type='run-satellite'),
                      Event(type='wake-now')]:
            write_event(event, source)
        source.seek(0)
        output = io.BytesIO()

        @IO
        def reader(ctx, target, length):
            chunk = source.read(min(4, length))
            C.memmove(target, chunk, len(chunk))
            return len(chunk)

        @IO
        def writer(ctx, target, length):
            output.write(C.string_at(target, length))
            return length

        # Five events in the stream: the early wake-now (state stays describe), describe,
        # register.ok, run-satellite, and the follow-up wake-now that starts the stream.
        for expected in [0, 1, 2, 3, 3]:
            self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
            self.assertEqual(lib.wy_session_state(session), expected)
        # The early wake-now wrote nothing; the one after run-satellite started the stream.
        output.seek(0)
        events = []
        while True:
            event = read_event(output)
            if event is None:
                break
            events.append(event)
        self.assertEqual([e.type for e in events][:2], ['info', 'hermes.register'])
        self.assertEqual([e.type for e in events][2:],
                         ['detection', 'run-pipeline', 'streaming-started'])
        self.assertEqual(events[2].data['name'], 'hey-hermes')
        self.assertEqual(lib.wy_session_streaming(session), 1)
        # The stream now carries audio; a wake-now mid-stream is dropped, not a fault.
        pcm = bytes(range(256)) * 2
        self.assertEqual(lib.wy_session_send_audio(session, pcm, len(pcm), writer, None), 0)
        append_at = source.tell()    # where the reader's next read will begin
        write_event(Event(type='wake-now'), source)   # appends, then leaves the position at the end
        source.seek(append_at)       # ...so the reader can actually see the appended event
        self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
        self.assertEqual(lib.wy_session_streaming(session), 1, 'a wake-now mid-stream is dropped, not a fault')

    def test_playback_audio_is_accepted_without_tearing_the_link(self):
        """The bridge sends audio-start / audio-chunk / audio-stop for playback, and it sends
        them to a registered device. The session must accept and drain them -- link stays
        registered -- not refuse the frame the way the old core refused every payload."""
        lib = self.library()
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)
        source = io.BytesIO()
        for event in [Event(type='describe'),
                      Event(type='hermes.register.ok',
                            data={'device_id': 'control-fixture', 'protocol_version': 1}),
                      Event(type='audio-start', data={'rate': 22050, 'width': 2, 'channels': 1}),
                      Event(type='audio-chunk', data={'rate': 22050, 'width': 2, 'channels': 1,
                                                      'timestamp': 0},
                            payload=b'\x01\x02\x03\x04'),
                      Event(type='audio-stop')]:
            write_event(event, source)
        source.seek(0)
        output = io.BytesIO()

        @IO
        def reader(ctx, target, length):
            chunk = source.read(min(6, length))
            C.memmove(target, chunk, len(chunk))
            return len(chunk)

        @IO
        def writer(ctx, target, length):
            output.write(C.string_at(target, length))
            return length

        for expected in [1, 2, 2, 2, 2]:
            self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
            self.assertEqual(lib.wy_session_state(session), expected)
        self.assertEqual(lib.wy_session_state(session), 2, 'playback audio must not kill the link')


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
            # Inbound audio-chunk is the bridge's playback: accepted and drained (the speaker
            # path is a later increment), and the link stays registered -- the old core refused
            # every payload and died the first time the bridge spoke.
            ([Event(type='describe'), Event(type='hermes.register.ok', data=good_ack),
               Event(type='audio-chunk', payload=b'not audio')], 0),
            # Fail-soft is limited to well-formed, known event types. An UNKNOWN event carrying a
            # payload is still refused: a payload the device will not consume cannot be dropped
            # silently mid-stream, and failing closed is the honest answer.
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
                terminal_state = {0: 2, -1: 5, -4: 4}[expected]
                self.assertEqual(lib.wy_session_state(session), terminal_state)
                if expected != 0:
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
                self.assertEqual(slot.identity.capabilities, ('microphone',))
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
