#!/usr/bin/env python3
"""Compile production session core; exchange canonical Wyoming control frames."""
import asyncio
import ctypes as C
import socket
import struct
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


class Identity(C.Structure):
    """The ctypes mirror of wy_session_identity (the device identity the glue sets)."""
    _fields_ = [('name', C.c_char_p), ('area', C.c_char_p),
                ('firmware_version', C.c_char_p), ('wake_name', C.c_char_p)]


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
        lib.wy_session_playback_configure.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t, C.c_int]
        lib.wy_session_playback_bytes.restype = C.c_size_t
        lib.wy_session_playback_read.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t]
        lib.wy_session_playback_read.restype = C.c_size_t
        lib.wy_session_local_play.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t]
        lib.wy_session_local_play.restype = C.c_int
        lib.wy_session_set_identity.argtypes = [C.c_void_p, C.POINTER(Identity)]
        lib.wy_session_set_identity.restype = None
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
        self.assertEqual(registration.data['capabilities'], ['microphone', 'speaker'])
        self.assertIsNone(read_event(output))

    def test_identity_is_configurable_like_the_pi_env(self):
        """The Pi reads name/area/firmware_version from env and reports the model name as the
        detection name; the ESP32 reads them from NVS + the loaded model
        (wy_session_set_identity) so the two variants report the same fields on the wire. This
        pins that a configured identity flows into the describe/info and registration payloads,
        and that it survives a reconnect the way the other installed-once config does."""
        lib = self.library()
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)
        # The identity the glue read from NVS + the loaded model's name.
        identity = Identity(name=b'Kitchen', area=b'Kitchen',
                            firmware_version=b'2.0.0', wake_name=b'hey_hermes')
        lib.wy_session_set_identity(session, C.byref(identity))
        source = io.BytesIO()
        for event in [Event(type='describe'), Event(type='hermes.register.ok',
                      data={'device_id': 'control-fixture', 'protocol_version': 1})]:
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

        for expected in [1, 2]:
            self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
            self.assertEqual(lib.wy_session_state(session), expected)
        output.seek(0)
        info = read_event(output)
        registration = read_event(output)
        assert info is not None and registration is not None
        satellite = Info.from_event(info).satellite
        assert satellite is not None
        self.assertEqual(satellite.name, 'Kitchen', 'describe/info reports the configured name')
        self.assertEqual(registration.data['name'], 'Kitchen')
        self.assertEqual(registration.data['area'], 'Kitchen')
        self.assertEqual(registration.data['firmware_version'], '2.0.0')

        # A re-init (the supervisor does this per reconnect) must keep the identity, exactly as
        # it keeps the writer-lock callbacks and the playback ring.
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-2'), 0)
        output2 = io.BytesIO()
        source2 = io.BytesIO()
        write_event(Event(type='describe'), source2)
        source2.seek(0)

        @IO
        def reader2(ctx, target, length):
            chunk = source2.read(min(3, length))
            C.memmove(target, chunk, len(chunk))
            return len(chunk)

        @IO
        def writer2(ctx, target, length):
            chunk = C.string_at(target, min(2, length))
            output2.write(chunk)
            return len(chunk)

        self.assertEqual(lib.wy_session_step(session, reader2, writer2, None), 0)
        output2.seek(0)
        info2 = read_event(output2)
        assert info2 is not None
        satellite2 = Info.from_event(info2).satellite
        assert satellite2 is not None
        self.assertEqual(satellite2.name, 'Kitchen', 'the identity survives a re-init')

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
        pcm = bytes(range(256)) * 8   # 2048 bytes: a full-size (maximum) chunk

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

    def test_wake_now_before_run_satellite_is_dropped(self):
        """The microphone is armed by run-satellite, and only then. A wake-now that lands
        registered but still unarmed (after hermes.register.ok, before the first run-satellite)
        must be dropped, never opened: it would run the wake sequence and set streaming with no
        capture task running, so the bridge would hold a live mic stream carrying zero chunks
        until it gave up. The drop is not a fault, and it writes nothing."""
        lib = self.library()
        lib.wy_session_streaming.argtypes = [C.c_void_p]
        lib.wy_session_streaming.restype = C.c_int
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)
        source = io.BytesIO()
        for event in [Event(type='describe'),
                      Event(type='hermes.register.ok',
                            data={'device_id': 'control-fixture', 'protocol_version': 1}),
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

        for expected in [1, 2]:
            self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
            self.assertEqual(lib.wy_session_state(session), expected)
        # The wake-now lands registered but unarmed: dropped, not a fault.
        self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
        self.assertEqual(lib.wy_session_state(session), 2,
                         'a dropped wake-now must not move the state')
        self.assertEqual(lib.wy_session_streaming(session), 0,
                         'no capture task can feed a stream opened while unarmed')
        output.seek(0)
        self.assertEqual(read_event(output).type, 'info')
        self.assertEqual(read_event(output).type, 'hermes.register')
        self.assertIsNone(read_event(output),
                          'a dropped wake-now writes no detection or stream events')

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
        output_start = output.tell()  # a dropped wake-now must write NOTHING: no second detection
        self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
        self.assertEqual(lib.wy_session_streaming(session), 1, 'a wake-now mid-stream is dropped, not a fault')
        self.assertEqual(output.tell(), output_start, 'the dropped wake-now re-ran the wake sequence')

    def test_reinit_preserves_the_writer_lock_callbacks(self):
        """The device glue installs the writer-lock callbacks once (the gateway task and the
        capture task both write the link). The supervisor re-inits the SAME session buffer on
        every reconnect, so an init that memsets the lock fields would leave both writers
        sending unserialized for the rest of the link. The init must preserve them."""
        lib = self.library()
        WY_HEADER_MAX, WY_DATA_MAX = 1024, 4096
        class WYSession(C.Structure):
            _fields_ = [('state', C.c_int), ('streaming', C.c_int), ('paused', C.c_int),
                        ('device_id', C.c_char * 65), ('boot_id', C.c_char * 129),
                        ('name', C.c_char * 65),
                        ('area', C.c_char * 65),
                        ('firmware_version', C.c_char * 33),
                        ('wake_name', C.c_char * 65),
                        ('header', C.c_char * WY_HEADER_MAX), ('body', C.c_char * (WY_DATA_MAX + 1)),
                        ('lock_writer', C.CFUNCTYPE(C.c_int, C.c_void_p)),
                        ('unlock_writer', C.CFUNCTYPE(None, C.c_void_p)),
                        ('writer_lock_user', C.c_void_p),
                        ('playback_active', C.c_int), ('playback_ended', C.c_int),
                        ('playback_volume', C.c_int), ('playback_rate', C.c_int),
                        ('playback_rs', C.c_double * 3),
                        ('ring', C.c_void_p), ('ring_capacity', C.c_size_t),
                        ('ring_head', C.c_size_t), ('ring_tail', C.c_size_t),
                        ('pcm', C.c_void_p), ('mono', C.c_void_p), ('frames', C.c_void_p),
                        ('config_handler', C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.c_char_p, C.c_size_t)),
                        ('config_user', C.c_void_p)]
        self.assertEqual(C.sizeof(WYSession), lib.wy_session_size(), 'the host struct must match the production layout')
        session = WYSession()
        self.assertEqual(lib.wy_session_init(C.byref(session), b'control-fixture', b'boot-1'), 0)
        # A device-style install (what audio_capture_install_writer_lock does).
        acquired = [0]
        @C.CFUNCTYPE(C.c_int, C.c_void_p)
        def acquire(user):
            acquired[0] += 1
            return 0
        @C.CFUNCTYPE(None, C.c_void_p)
        def release(user):
            pass
        session.lock_writer = acquire
        session.unlock_writer = release
        # Re-init, exactly as the supervisor does on a reconnect: the lock must survive.
        self.assertEqual(lib.wy_session_init(C.byref(session), b'control-fixture', b'boot-2'), 0)
        self.assertEqual(session.state, 0, 'state is still reset to describe')
        # Proof it survived, not by pointer equality (ctypes re-wraps a function on readback) but
        # by calling the preserved callback: a wiped field would be NULL and crash, a preserved
        # one runs the install. The install ran once; the call below is the second hit.
        session.lock_writer(None)
        self.assertEqual(acquired[0], 1, 'the re-init wiped the writer-lock callback')

    def test_reinit_preserves_the_playback_ring(self):
        """The device glue configures the playback ring (a PSRAM buffer it owns) once, and the
        supervisor re-inits the SAME session buffer on every reconnect. An init that memsets the
        ring fields would leave the session's single writer appending into a NULL ring after the
        first reconnect. The ring pointer, capacity, and volume must survive a re-init, the way
        the writer-lock fields already do; the live stream state (active/ended) must be reset,
        because a reconnect ends the answer. Proven end-to-end: after the re-init, a canonical
        answer stream must land its converted frames in the ORIGINAL ring buffer."""
        lib = self.library()
        WY_HEADER_MAX, WY_DATA_MAX = 1024, 4096
        class WYSession(C.Structure):
            _fields_ = [('state', C.c_int), ('streaming', C.c_int), ('paused', C.c_int),
                        ('device_id', C.c_char * 65), ('boot_id', C.c_char * 129),
                        ('name', C.c_char * 65),
                        ('area', C.c_char * 65),
                        ('firmware_version', C.c_char * 33),
                        ('wake_name', C.c_char * 65),
                        ('header', C.c_char * WY_HEADER_MAX), ('body', C.c_char * (WY_DATA_MAX + 1)),
                        ('lock_writer', C.CFUNCTYPE(C.c_int, C.c_void_p)),
                        ('unlock_writer', C.CFUNCTYPE(None, C.c_void_p)),
                        ('writer_lock_user', C.c_void_p),
                        ('playback_active', C.c_int), ('playback_ended', C.c_int),
                        ('playback_volume', C.c_int), ('playback_rate', C.c_int),
                        ('playback_rs', C.c_double * 3),
                        ('ring', C.c_void_p), ('ring_capacity', C.c_size_t),
                        ('ring_head', C.c_size_t), ('ring_tail', C.c_size_t),
                        ('pcm', C.c_void_p), ('mono', C.c_void_p), ('frames', C.c_void_p),
                        ('config_handler', C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.c_char_p, C.c_size_t)),
                        ('config_user', C.c_void_p)]
        self.assertEqual(C.sizeof(WYSession), lib.wy_session_size(), 'the host struct must match the production layout')
        session = WYSession()
        self.assertEqual(lib.wy_session_init(C.byref(session), b'control-fixture', b'boot-1'), 0)
        ring = (C.c_int32 * 4096)()
        ring_ptr = C.cast(ring, C.c_void_p).value
        self.assertEqual(lib.wy_session_playback_configure(C.byref(session), ring, 4096 * 4, 30), 0)
        # Re-init, exactly as the supervisor does on a reconnect.
        self.assertEqual(lib.wy_session_init(C.byref(session), b'control-fixture', b'boot-2'), 0)
        # The ring pointer and capacity survive: a wiped field is NULL/0, and the next answer's
        # ring-append would write through NULL.
        self.assertEqual(session.ring, ring_ptr, 'the re-init wiped the ring pointer')
        self.assertEqual(session.ring_capacity, 4096 * 4, 'the re-init wiped the ring capacity')
        self.assertEqual(session.playback_volume, 30, 'the re-init wiped the volume')
        # The live stream state must be reset by the re-init, not preserved.
        self.assertEqual(session.playback_active, 0, 'a re-init ends any live stream')
        self.assertEqual(session.playback_ended, 0, 'a re-init clears the ended flag')
        # End-to-end: a canonical answer after the re-init must convert and land in the ORIGINAL
        # ring buffer. If the ring were wiped, playback_bytes would be 0 and nothing would arrive.
        pcm = struct.pack('<8h', *range(8))
        source = io.BytesIO()
        for event in [Event(type='describe'),
                      Event(type='hermes.register.ok',
                            data={'device_id': 'control-fixture', 'protocol_version': 1}),
                      Event(type='audio-start', data={'rate': 22050, 'width': 2, 'channels': 1}),
                      Event(type='audio-chunk', data={'rate': 22050, 'width': 2, 'channels': 1,
                                                      'timestamp': 0}, payload=pcm),
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
            out = C.string_at(target, length)
            output.write(out)
            return length
        for expected in [1, 2, 2, 2, 2]:
            self.assertEqual(lib.wy_session_step(C.byref(session), reader, writer, None), 0)
            self.assertEqual(lib.wy_session_state(C.byref(session)), expected)
        self.assertEqual(session.playback_active, 0, 'the answer has ended')
        self.assertEqual(session.playback_ended, 1, 'the answer has ended')
        self.assertTrue(lib.wy_session_playback_bytes(C.byref(session)) > 0,
                        'the converted frames did not reach the original ring after the re-init')
        # And the frames really are in the buffer the test owns (not a phantom count).
        got = (C.c_int32 * 16)()
        self.assertTrue(lib.wy_session_playback_read(C.byref(session), got, len(got) * 4) > 0)
        self.assertEqual(ring[0], got[0], 'the ring the test owns holds the first frame')

    def test_local_play_writes_chime_frames_to_the_ring(self):
        """The detector's chime seam (D4) renders a 16 kHz mono tone and must sound it
        through the SAME speaker path as a bridge answer: the pump task reads the session
        ring and owns the amplifier, so the only way a local tone reaches the speaker is by
        being written into that ring. local_play is the device-local half of the conversion:
        no resample (the chime is already at the board's 16 kHz), but the SAME volume
        (the config plane's level, the floor never silences) and the SAME stereo-32 expand
        as the inbound audio-chunk path, appended under the ring lock. The frames the pump
        will read must be exactly the I2S-ready stereo-32 the chime samples expand to."""
        lib = self.library()
        WY_HEADER_MAX, WY_DATA_MAX = 1024, 4096
        class WYSession(C.Structure):
            _fields_ = [('state', C.c_int), ('streaming', C.c_int), ('paused', C.c_int),
                        ('device_id', C.c_char * 65), ('boot_id', C.c_char * 129),
                        ('name', C.c_char * 65),
                        ('area', C.c_char * 65),
                        ('firmware_version', C.c_char * 33),
                        ('wake_name', C.c_char * 65),
                        ('header', C.c_char * WY_HEADER_MAX), ('body', C.c_char * (WY_DATA_MAX + 1)),
                        ('lock_writer', C.CFUNCTYPE(C.c_int, C.c_void_p)),
                        ('unlock_writer', C.CFUNCTYPE(None, C.c_void_p)),
                        ('writer_lock_user', C.c_void_p),
                        ('playback_active', C.c_int), ('playback_ended', C.c_int),
                        ('playback_volume', C.c_int), ('playback_rate', C.c_int),
                        ('playback_rs', C.c_double * 3),
                        ('ring', C.c_void_p), ('ring_capacity', C.c_size_t),
                        ('ring_head', C.c_size_t), ('ring_tail', C.c_size_t),
                        ('pcm', C.c_void_p), ('mono', C.c_void_p), ('frames', C.c_void_p),
                        ('config_handler', C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.c_char_p, C.c_size_t)),
                        ('config_user', C.c_void_p)]
        self.assertEqual(C.sizeof(WYSession), lib.wy_session_size(), 'host struct must match the production layout')
        session = WYSession()
        self.assertEqual(lib.wy_session_init(C.byref(session), b'control-fixture', b'boot-1'), 0)
        ring = (C.c_int32 * 4096)()
        self.assertEqual(lib.wy_session_playback_configure(C.byref(session), ring, 4096 * 4, 100), 0)
        pcm = (C.c_int16 * 8)(*range(8))   # 8 samples of 16 kHz mono S16
        self.assertEqual(lib.wy_session_local_play(C.byref(session), pcm, 8), 0)
        # 8 mono samples -> 16 stereo-32 frames -> 64 bytes in the ring.
        self.assertEqual(lib.wy_session_playback_bytes(C.byref(session)), 64)
        got = (C.c_int32 * 16)()
        self.assertEqual(lib.wy_session_playback_read(C.byref(session), got, 16 * 4), 64)
        # x << 16, L=R: sample 0 is silence, sample 1 is full-scale one step up.
        self.assertEqual(got[0], 0)
        self.assertEqual(got[1], 0)
        self.assertEqual(got[2], 1 << 16)
        self.assertEqual(got[3], 1 << 16)
        # The ring the test owns holds those frames (not a phantom count).
        self.assertEqual(ring[2], 1 << 16)
        # Bad arguments are INVALID, not a write: NULL session / pcm, or no ring.
        self.assertEqual(lib.wy_session_local_play(None, pcm, 8), -1)
        bare = WYSession()
        self.assertEqual(lib.wy_session_init(C.byref(bare), b'cf', b'b1'), 0)
        self.assertEqual(lib.wy_session_local_play(C.byref(bare), pcm, 8), -1,
                         'no ring configured: a local play has nowhere to go')

    def test_local_play_applies_the_config_volume(self):
        """local_play must honor the config plane's playback volume exactly as the bridge's
        answer does (wy_apply_volume, floor never silences): the chime is part of the
        device's own voice and is level-matched to it, not a full-scale tone that could
        blast at a level the answer would not."""
        lib = self.library()
        WY_HEADER_MAX, WY_DATA_MAX = 1024, 4096
        class WYSession(C.Structure):
            _fields_ = [('state', C.c_int), ('streaming', C.c_int), ('paused', C.c_int),
                        ('device_id', C.c_char * 65), ('boot_id', C.c_char * 129),
                        ('name', C.c_char * 65),
                        ('area', C.c_char * 65),
                        ('firmware_version', C.c_char * 33),
                        ('wake_name', C.c_char * 65),
                        ('header', C.c_char * WY_HEADER_MAX), ('body', C.c_char * (WY_DATA_MAX + 1)),
                        ('lock_writer', C.CFUNCTYPE(C.c_int, C.c_void_p)),
                        ('unlock_writer', C.CFUNCTYPE(None, C.c_void_p)),
                        ('writer_lock_user', C.c_void_p),
                        ('playback_active', C.c_int), ('playback_ended', C.c_int),
                        ('playback_volume', C.c_int), ('playback_rate', C.c_int),
                        ('playback_rs', C.c_double * 3),
                        ('ring', C.c_void_p), ('ring_capacity', C.c_size_t),
                        ('ring_head', C.c_size_t), ('ring_tail', C.c_size_t),
                        ('pcm', C.c_void_p), ('mono', C.c_void_p), ('frames', C.c_void_p),
                        ('config_handler', C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.c_char_p, C.c_size_t)),
                        ('config_user', C.c_void_p)]
        session = WYSession()
        lib.wy_session_init(C.byref(session), b'cf', b'b1')
        ring = (C.c_int32 * 4096)()
        self.assertEqual(lib.wy_session_playback_configure(C.byref(session), ring, 4096 * 4, 50), 0)
        pcm = (C.c_int16 * 4)()
        for i in range(4):
            pcm[i] = 1000
        self.assertEqual(lib.wy_session_local_play(C.byref(session), pcm, 4), 0)
        got = (C.c_int32 * 8)()
        self.assertEqual(lib.wy_session_playback_read(C.byref(session), got, 8 * 4), 8 * 4)
        # 1000 at 50% volume -> ~500 -> 500 << 16 (the exact wy_apply_volume result).
        expected = (1000 * 50 // 100) << 16
        self.assertEqual(got[0], expected)

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


    def test_oversized_inbound_payload_is_drained_not_overwritten(self):
        """An inbound frame whose payload is at the wire bound (16 KB) is far larger than the
        session's 4 KB parse body. It must be drained off the wire in bounded slices and
        DISCARDED -- never written whole into the parse body -- and the next header must still
        line up. A single read into the 4 KB body (the old shape) wrote 12 KB past it; the
        regression is proven under AddressSanitizer in the build gate, and here by the fact
        that the frame after the 16 KB payload still parses to the right state."""
        lib = self.library()
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)
        big = (16384 * b'\x01')           # exactly WY_PAYLOAD_MAX (16 KiB of one byte)
        source = io.BytesIO()
        for event in [Event(type='describe'),
                      Event(type='hermes.register.ok',
                            data={'device_id': 'control-fixture', 'protocol_version': 1}),
                      Event(type='audio-chunk', data={'rate': 22050, 'width': 2, 'channels': 1},
                            payload=big),
                      Event(type='run-satellite')]:
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

        # describe -> info (state 1); register.ok (state 2); the 16 KB audio-chunk is accepted
        # and drained (state stays 2); run-satellite then arms (state 3). If the payload had
        # desynced the stream, run-satellite would read the tail of the payload and fail.
        for expected in [1, 2, 2, 3]:
            self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
            self.assertEqual(lib.wy_session_state(session), expected,
                             'the frame after a 16 KB payload must still parse')


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
                self.assertEqual(slot.identity.capabilities, ('microphone', 'speaker'))
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


    def playback_lib(self):
        lib = self.library()
        lib.wy_session_playback_configure.argtypes = [C.c_void_p, C.POINTER(C.c_int32),
                                                      C.c_size_t, C.c_int]
        lib.wy_session_playback_configure.restype = C.c_int
        lib.wy_session_playback_active.argtypes = [C.c_void_p]
        lib.wy_session_playback_active.restype = C.c_int
        lib.wy_session_playback_ended.argtypes = [C.c_void_p]
        lib.wy_session_playback_ended.restype = C.c_int
        lib.wy_session_playback_bytes.argtypes = [C.c_void_p]
        lib.wy_session_playback_bytes.restype = C.c_size_t
        lib.wy_session_playback_read.argtypes = [C.c_void_p, C.POINTER(C.c_int32), C.c_size_t]
        lib.wy_session_playback_read.restype = C.c_size_t
        return lib

    @staticmethod
    def _c_llround(x):
        import math
        return int(math.floor(x + 0.5)) if x >= 0 else int(math.ceil(x - 0.5))

    @classmethod
    def _expected_playback_frames(cls, pcm, volume=50):
        """The canonical reference for the playback conversion, computed independently:
        resample 22050->16000 (absolute cursor, linear interpolation, the device's exact grid),
        apply the clamped volume (llround), and expand to stereo 32-bit full-scale (x << 16).
        Returns the flat list of int32 (L,R interleaved). This is the interop oracle: the device
        must produce byte-identical frames to what a correct implementation would."""
        import math
        R = 22050.0 / 16000.0
        mono = []
        k = 0
        while True:
            p = k * R
            i0 = int(math.floor(p))
            if i0 + 1 >= len(pcm):
                break
            frac = p - i0
            s = pcm[i0] * (1.0 - frac) + pcm[i0 + 1] * frac   # the device's exact lerp
            v = cls._c_llround(s)
            if v > 32767: v = 32767
            if v < -32768: v = -32768
            mono.append(v)
            k += 1
        vol = volume
        if vol < 25: vol = 25
        if vol > 100: vol = 100
        mono = [cls._c_llround(m * vol / 100.0) for m in mono]
        frames = []
        for m in mono:
            frames.append(m << 16)
            frames.append(m << 16)
        return frames

    def test_playback_converts_a_canonical_answer_to_the_ring(self):
        """The full playback path, canonical bridge -> device ring: an audio-start (22050/2/1),
        an audio-chunk (a known 1024-sample PCM), and an audio-stop. The session resamples the
        chunk to 16 kHz, applies the configured volume, and expands it to stereo 32-bit
        full-scale, appending the exact frames to the ring -- byte-identical to an independent
        reference. This is the device half of 'the bridge speaks, the speaker plays'."""
        lib = self.playback_lib()
        pcm = list(range(1024))   # a 1024-sample identity ramp, 22050 Hz mono (the input)
        pcm_bytes = b''.join(b'%d%02d' % (abs(s) % 256, (abs(s) >> 8) % 256) for s in pcm)
        # encode as S16_LE properly
        import struct
        pcm_bytes = struct.pack('<%dh' % len(pcm), *pcm)
        expected = self._expected_playback_frames(pcm, volume=50)
        self.assertGreater(len(expected), 0)

        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)
        ring = (C.c_int32 * 16384)()   # 64 KB of 32-bit frames: room for the answer
        self.assertEqual(lib.wy_session_playback_configure(session, ring, 65536, 50), 0)

        source = io.BytesIO()
        from wyoming.audio import AudioStart, AudioChunk, AudioStop
        write_event(AudioStart(rate=22050, width=2, channels=1).event(), source)
        write_event(AudioChunk(rate=22050, width=2, channels=1, audio=pcm_bytes).event(), source)
        write_event(AudioStop().event(), source)
        source.seek(0)
        output = io.BytesIO()

        @IO
        def reader(ctx, target, length):
            chunk = source.read(min(3, length))
            C.memmove(target, chunk, len(chunk))
            return len(chunk)

        @IO
        def writer(ctx, target, length):
            output.write(C.string_at(target, length))
            return length

        # audio-start: arms the playback
        self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
        self.assertEqual(lib.wy_session_playback_active(session), 1)
        # audio-chunk: converted into the ring
        self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
        self.assertEqual(lib.wy_session_playback_active(session), 1)
        self.assertEqual(lib.wy_session_playback_bytes(session), len(expected) * 4,
                         'the ring must hold exactly the converted frames')
        # audio-stop: ends the stream (the ring still holds the frames to drain)
        self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
        self.assertEqual(lib.wy_session_playback_active(session), 0)
        self.assertEqual(lib.wy_session_playback_ended(session), 1)
        self.assertEqual(lib.wy_session_playback_bytes(session), len(expected) * 4)
        # drain the ring and compare byte-for-byte to the canonical reference
        got = (C.c_int32 * len(expected))()
        self.assertEqual(lib.wy_session_playback_read(session, got, len(expected) * 4),
                         len(expected) * 4)
        self.assertEqual(list(got), expected,
                         'the converted ring must be byte-identical to the canonical reference')
        self.assertEqual(lib.wy_session_playback_bytes(session), 0, 'the ring drains to empty')


    def config_library(self):
        """Session core + the REAL config agent in one shared object, so the desired->reported
        round-trip is tested end-to-end through the production agent (not a stand-in)."""
        self.assertTrue((COMP / 'wyoming_config/wyoming_config.c').exists(), 'config agent missing')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'session_cfg.so'
        parser = COMP / 'wyoming_json'
        includes = [COMP / 'wyoming_wire', parser, parser / 'vendor/cjson', COMP / 'wyoming_session',
                    COMP / 'wyoming_audio', COMP / 'wyoming_config']
        sources = [COMP / 'wyoming_wire/wyoming_wire.c', parser / 'wyoming_json.c',
                   parser / 'vendor/cjson/cJSON.c', COMP / 'wyoming_session/wyoming_session.c',
                   COMP / 'wyoming_audio/wyoming_audio.c', COMP / 'wyoming_config/wyoming_config.c']
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        '-DCJSON_NESTING_LIMIT=16', *['-I' + str(p) for p in includes],
                        *map(str, sources), '-lm', '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        lib.wy_session_size.restype = C.c_size_t
        lib.wy_session_init.argtypes = [C.c_void_p, C.c_char_p, C.c_char_p]
        lib.wy_session_state.argtypes = [C.c_void_p]
        lib.wy_session_step.argtypes = [C.c_void_p, IO, IO, C.c_void_p]
        lib.wy_config_size.restype = C.c_size_t
        lib.wy_config_init.argtypes = [C.c_void_p, C.c_char_p]
        return lib

    def _install_real_config_agent(self, lib, session, volume):
        """Back the session's config handler with the REAL agent, NVS-like in-memory storage, and
        a device-accurate apply adapter (only playback_volume is applied; the rest degrade).
        Returns (keepalive, the desired profile, the expected actual)."""
        import json as _j
        keep = []
        cfg = C.create_string_buffer(lib.wy_config_size())
        self.assertEqual(lib.wy_config_init(cfg, b'control-fixture'), 0)

        SAVE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.c_size_t)
        LOAD = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(C.c_char), C.c_size_t, C.POINTER(C.c_size_t))
        st = {'blob': bytearray()}

        @SAVE
        def save(user, js, length):
            st['blob'] = bytearray(C.string_at(js, length))
            return 0

        @LOAD
        def load(user, out, cap, outlen):
            if not st['blob'] or len(st['blob']) + 1 >= cap:
                return -1
            C.memmove(out, bytes(st['blob']), len(st['blob']))
            outlen[0] = len(st['blob'])
            return 0

        APPLY = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.POINTER(C.c_char), C.c_size_t,
                            C.POINTER(C.c_int), C.POINTER(C.c_char), C.c_size_t)

        @APPLY
        def apply(user, settings_json, out_actual, actual_cap, out_health, out_error, err_cap):
            try:
                settings = _j.loads(C.string_at(settings_json).decode('utf-8'))
            except Exception:
                out_health[0] = 3
                return -1
            actual = {}
            if isinstance(settings.get('playback_volume'), int) and \
               not isinstance(settings.get('playback_volume'), bool):
                actual['playback_volume'] = settings['playback_volume']
            s = _j.dumps(actual).encode('utf-8')
            C.memmove(out_actual, s, len(s))
            out_actual[len(s)] = 0
            out_health[0] = 2   # degraded: the volume applied, the rest have no adapter yet
            out_error[0] = 0
            return 0

        keep += [save, load, apply, cfg]
        lib.wy_config_set_storage.argtypes = [C.c_void_p, SAVE, LOAD, C.c_void_p]
        lib.wy_config_set_storage(cfg, save, load, None)
        lib.wy_config_set_apply.argtypes = [C.c_void_p, APPLY, C.c_void_p]
        lib.wy_config_set_apply(cfg, apply, None)

        profile = {'wake_word': 'hey-hermes', 'wake_threshold': 0.5, 'mic_gain': 40,
                   'playback_volume': volume, 'led_brightness': 10, 'locale': 'en-US',
                   'update_channel': 'stable'}
        expected_actual = {'playback_volume': volume}
        # The session installs the handler once; the glue (or this test) owns the agent's lifetime.
        # The report buffer is an OUT parameter: ctypes must hand the C code a WRITABLE pointer,
        # which means POINTER(c_char), never c_char_p (which converts to an immutable bytes copy
        # that C must not write through). wy_config_step's own argtypes are set so the forwarder
        # marshals the out buffer the same way when it calls back into the agent.
        CFGSTEP = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.POINTER(C.c_char), C.c_size_t)
        lib.wy_config_step.argtypes = [C.c_void_p, C.c_char_p, C.POINTER(C.c_char), C.c_size_t]
        lib.wy_config_step.restype = C.c_int
        handler = CFGSTEP(lambda config, desired, out, cap:
                          lib.wy_config_step(config, desired, out, cap))
        lib.wy_session_set_config.argtypes = [C.c_void_p, CFGSTEP, C.c_void_p]
        lib.wy_session_set_config(session, handler, C.cast(cfg, C.c_void_p))
        keep.append(handler)
        return keep, profile, expected_actual

    def _register_then_desired(self, lib, session, profile, generation, output):
        """Feed describe, register.ok, and one hermes.config.desired; return the sent events."""
        source = io.BytesIO()
        for event in [Event(type='describe'),
                      Event(type='hermes.register.ok',
                            data={'device_id': 'control-fixture', 'protocol_version': 1}),
                      Event(type='hermes.config.desired',
                            data={'schema_version': 1, 'device_id': 'control-fixture',
                                  'generation': generation, 'settings': profile})]:
            write_event(event, source)
        source.seek(0)

        @IO
        def reader(ctx, target, length):
            chunk = source.read(min(4, length))
            C.memmove(target, chunk, len(chunk))
            return len(chunk)

        @IO
        def writer(ctx, target, length):
            output.write(C.string_at(target, length))
            return length

        for _ in range(3):
            self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
        output.seek(0)
        events = []
        while (event := read_event(output)) is not None:
            events.append(event)
        return events

    def test_set_playback_volume_updates_the_live_level(self):
        """The config plane's apply adapter (the device glue) must change the LIVE playback level
        for the next audio slice, and it does so under the playback ring lock (the same lock that
        guards the ring pointers and the volume read in the audio-chunk conversion). A level the
        bridge requests is applied the moment the config dispatch is processed -- the next answer
        plays at the new level without a reconnect. The value is stored raw; wy_apply_volume
        clamps it at use (the no-silence floor), so the setter does not clamp here."""
        lib = self.playback_lib()
        lib.wy_session_set_playback_volume.argtypes = [C.c_void_p, C.c_int]
        lib.wy_session_set_playback_volume.restype = C.c_int
        WY_HEADER_MAX, WY_DATA_MAX = 1024, 4096
        class WYSession(C.Structure):
            _fields_ = [('state', C.c_int), ('streaming', C.c_int), ('paused', C.c_int),
                        ('device_id', C.c_char * 65), ('boot_id', C.c_char * 129),
                        ('name', C.c_char * 65),
                        ('area', C.c_char * 65),
                        ('firmware_version', C.c_char * 33),
                        ('wake_name', C.c_char * 65),
                        ('header', C.c_char * WY_HEADER_MAX), ('body', C.c_char * (WY_DATA_MAX + 1)),
                        ('lock_writer', C.CFUNCTYPE(C.c_int, C.c_void_p)),
                        ('unlock_writer', C.CFUNCTYPE(None, C.c_void_p)),
                        ('writer_lock_user', C.c_void_p),
                        ('playback_active', C.c_int), ('playback_ended', C.c_int),
                        ('playback_volume', C.c_int), ('playback_rate', C.c_int),
                        ('playback_rs', C.c_double * 3),
                        ('ring', C.c_void_p), ('ring_capacity', C.c_size_t),
                        ('ring_head', C.c_size_t), ('ring_tail', C.c_size_t),
                        ('pcm', C.c_void_p), ('mono', C.c_void_p), ('frames', C.c_void_p),
                        ('config_handler', C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.c_char_p, C.c_size_t)),
                        ('config_user', C.c_void_p)]
        self.assertEqual(C.sizeof(WYSession), lib.wy_session_size(), 'the host struct must match the production layout')
        session = WYSession()
        self.assertEqual(lib.wy_session_init(C.byref(session), b'control-fixture', b'boot-1'), 0)
        ring = (C.c_int32 * 16)()
        self.assertEqual(lib.wy_session_playback_configure(C.byref(session), ring, 64, 50), 0)
        self.assertEqual(session.playback_volume, 50)
        self.assertEqual(lib.wy_session_set_playback_volume(C.byref(session), 30), 0)
        self.assertEqual(session.playback_volume, 30, 'the live level must change for the next slice')
        self.assertEqual(lib.wy_session_set_playback_volume(C.byref(session), 0), 0)
        self.assertEqual(session.playback_volume, 0, '0 is stored raw; the floor applies at use, not here')
        self.assertEqual(lib.wy_session_set_playback_volume(None, 30), -1, 'a NULL session is refused')

    def test_config_desired_with_a_payload_fails_closed(self):
        """A hermes.config.desired carries its state in `data` ONLY. A frame that also carries
        a payload is malformed, and the session must refuse it BEFORE the handler runs -- the
        payload drain reuses the parse body, so by dispatch time the body holds the payload,
        not the desired: handing it to the handler would let a payload tail that happens to
        parse as a desired be silently APPLIED, and a non-parsing one is nondeterministic
        garbage. Failing closed (the same policy as an unknown-with-payload) is the only
        honest answer: no handler call, no report, the link is down, the bridge reconnects
        and re-dispatches the well-formed desired."""
        lib = self.config_library()
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)
        calls = [0]
        REPORT = b'{"generation":1,"actual":{},"health":"degraded","error":null}'
        CFGSTEP = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.POINTER(C.c_char), C.c_size_t)

        @CFGSTEP
        def handler(user, desired, out, cap):
            calls[0] += 1
            C.memmove(out, REPORT, len(REPORT))
            out[len(REPORT)] = 0
            return 0

        lib.wy_session_set_config.argtypes = [C.c_void_p, CFGSTEP, C.c_void_p]
        lib.wy_session_set_config(session, handler, None)
        profile = {'playback_volume': 42}
        source = io.BytesIO()
        for event in [Event(type='describe'),
                      Event(type='hermes.register.ok',
                            data={'device_id': 'control-fixture', 'protocol_version': 1}),
                      Event(type='hermes.config.desired',
                            data={'schema_version': 1, 'device_id': 'control-fixture',
                                  'generation': 1, 'settings': profile},
                            payload=b'\x00\x01\x02\x03')]:
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

        self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
        self.assertEqual(lib.wy_session_step(session, reader, writer, None), 0)
        self.assertEqual(lib.wy_session_step(session, reader, writer, None), -1,
                         'a config frame carrying a payload must fail closed')
        self.assertEqual(lib.wy_session_state(session), 5, 'a malformed config frame fails the link')
        self.assertEqual(calls[0], 0, 'the handler must not run on a malformed config frame')
        output.seek(0)
        events = []
        while (event := read_event(output)) is not None:
            events.append(event)
        self.assertEqual([e.type for e in events], ['info', 'hermes.register'],
                         'a refused config frame sends no report')

    def test_config_desired_produces_reported(self):
        """The bridge sends hermes.config.desired right after registration. The session must hand
        the decoded data to the (injected) config agent and answer with hermes.config.reported --
        the agent's real report (the device's actual + health), never the rejected desired. This
        is the wiring; the agent's own logic is proven in test_esp32_config.py."""
        lib = self.config_library()
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)
        keep, profile, expected_actual = self._install_real_config_agent(lib, session, 42)
        self.addCleanup(lambda: keep)
        output = io.BytesIO()
        events = self._register_then_desired(lib, session, profile, 1, output)
        self.assertEqual([e.type for e in events],
                         ['info', 'hermes.register', 'hermes.config.reported'])
        reported = events[2].data
        self.assertEqual(reported['generation'], 1)
        self.assertEqual(reported['health'], 'degraded')
        self.assertEqual(reported['actual'], expected_actual,
                         'the report carries what the agent really applied, not the desired')
        self.assertEqual(lib.wy_session_state(session), 2, 'a config event does not move the state')

    def test_config_handler_survives_reinit(self):
        """The glue installs the config handler once; the supervisor re-inits the SAME session
        buffer on every reconnect. A re-init that wiped the handler would leave a device that
        ignores every config dispatch after its first reconnect -- so the handler (and the agent
        it points at) must survive, the way the writer-lock fields already do."""
        lib = self.config_library()
        session = C.create_string_buffer(lib.wy_session_size())
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-1'), 0)
        keep, profile, expected_actual = self._install_real_config_agent(lib, session, 42)
        self.addCleanup(lambda: keep)
        # First link: a desired produces a reported.
        first = io.BytesIO()
        events = self._register_then_desired(lib, session, profile, 1, first)
        self.assertEqual([e.type for e in events],
                         ['info', 'hermes.register', 'hermes.config.reported'])
        # The reconnect: the supervisor re-inits the same buffer.
        self.assertEqual(lib.wy_session_init(session, b'control-fixture', b'boot-2'), 0)
        self.assertEqual(lib.wy_session_state(session), 0, 'a re-init resets to describe')
        # The handler must still be wired: the bridge's reconnect re-dispatch still gets an answer.
        second = io.BytesIO()
        events = self._register_then_desired(lib, session, profile, 1, second)
        self.assertEqual([e.type for e in events],
                         ['info', 'hermes.register', 'hermes.config.reported'],
                         'the config handler did not survive the re-init')
        self.assertEqual(events[2].data['generation'], 1)
        self.assertEqual(events[2].data['actual'], expected_actual)


if __name__ == '__main__':
    unittest.main()