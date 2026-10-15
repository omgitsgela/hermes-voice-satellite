#!/usr/bin/env python3
"""Offline tests for the LED vocabulary core.

The ring is driven by the XMOS chip over the same I2C bus as the audio, so the whole feature is
testable here: compile the real wy_led.c with -Wall -Wextra -Werror, feed it lifecycle signals,
and assert the exact bytes that would go out on the wire.

What matters most is not that commands are emitted but that they are the RIGHT ones, so the
assertions compare colours and effects against pi/2mic_service.py byte for byte. Two satellites
that disagree about what red means are worse than one that has no lights at all.
"""
import ctypes as C
import subprocess
import tempfile
from pathlib import Path
import unittest

COMP = Path(__file__).resolve().parents[1] / 'firmware/esp32/components'

EMIT = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint8, C.c_uint8, C.c_void_p, C.c_size_t)


class Write:
    def __init__(self, resid, cmd, params):
        self.resid, self.cmd, self.params = resid, cmd, bytes(params)

    def __repr__(self):
        return f"resid={self.resid} cmd={self.cmd} params={self.params.hex()}"


class LedHarness(unittest.TestCase):
    def library(self):
        self.assertTrue((COMP / 'wy_led/wy_led.c').exists(), 'led core missing')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'led.so'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        '-I' + str(COMP / 'wy_led'),
                        str(COMP / 'wy_led/wy_led.c'), '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        lib.wy_led_size.restype = C.c_size_t
        lib.wy_led_init.argtypes = [C.c_void_p, EMIT, C.c_void_p]
        lib.wy_led_state.argtypes = [C.c_void_p]
        lib.wy_led_state.restype = C.c_uint
        lib.wy_led_red_pending.argtypes = [C.c_void_p]
        lib.wy_led_tick.argtypes = [C.c_void_p, C.c_uint32]
        lib.wy_led_scale_brightness.argtypes = [C.c_uint]
        lib.wy_led_scale_brightness.restype = C.c_uint8
        for name in ('boot', 'wake', 'listening', 'voice_started', 'transcript', 'thinking',
                     'speaking', 'followup', 'idle', 'error', 'disconnected'):
            getattr(lib, 'wy_led_' + name).argtypes = [C.c_void_p]
        lib.wy_led_connected.argtypes = [C.c_void_p, C.c_int]
        lib.wy_led_set_brightness.argtypes = [C.c_void_p, C.c_uint]
        return lib

    def setUp(self):
        self.lib = self.library()
        self.writes = []

        @EMIT
        def emit(_user, resid, cmd, params, nparams):
            raw = C.string_at(params, nparams) if nparams else b''
            self.writes.append(Write(resid, cmd, raw))
            return 0

        self._emit = emit   # keep the trampoline alive
        self.buf = C.create_string_buffer(self.lib.wy_led_size())
        self.assertEqual(self.lib.wy_led_init(self.buf, emit, None), 0)

    def last(self, cmd):
        """The most recent write of this command, or None. None is meaningful: some phases
        deliberately emit nothing (a disconnect shows no colour at all)."""
        for w in reversed(self.writes):
            if w.cmd == cmd:
                return w
        return None

    def effect_of(self):
        """The effect currently commanded, or None if no effect has been commanded yet."""
        w = self.last(12)
        return w.params[0] if w else None

    def colour_of(self):
        """The most recent colour written, or None if no colour has been written yet."""
        w = self.last(16)
        return w.params[:3] if w else None

    def tick(self, ms):
        self.lib.wy_led_tick(self.buf, ms)

    # --- the wire format ---

    def test_commands_go_to_the_gpo_servicer_resource(self):
        self.lib.wy_led_idle(self.buf)
        self.assertTrue(self.writes, 'nothing was emitted')
        for w in self.writes:
            self.assertEqual(w.resid, 20, 'LED commands belong on the GPO servicer resource')

    def test_a_colour_write_is_four_bytes_not_three(self):
        """The device expects R,G,B,reserved. Three bytes is a truncated command."""
        self.lib.wy_led_error(self.buf)
        colour = self.last(16)
        self.assertEqual(len(colour.params), 4, 'the colour payload must be 4 bytes')
        self.assertEqual(colour.params[3], 0, 'the fourth byte is reserved and must be zero')

    def test_the_command_ids_match_seeeds_published_example(self):
        """EFFECT 12, BRIGHTNESS 13, SPEED 15, COLOR 16: from Seeed's own Arduino sketch."""
        self.lib.wy_led_error(self.buf)
        self.assertIsNotNone(self.last(12))
        self.assertIsNotNone(self.last(16))

    # --- the palette, byte for byte against pi/2mic_service.py ---

    def test_error_is_red(self):
        self.lib.wy_led_error(self.buf)
        self.assertEqual(self.last(16).params[:3], bytes([255, 0, 0]))
        self.assertEqual(self.effect_of(), 3, 'a solid colour is effect 3')

    def test_thinking_is_the_working_violet(self):
        self.lib.wy_led_thinking(self.buf)
        self.assertEqual(self.last(16).params[:3], bytes([150, 70, 255]))
        self.assertEqual(self.effect_of(), 1, 'the thinking glow is a breath, not a flat colour')

    def test_followup_is_dim_teal(self):
        self.lib.wy_led_followup(self.buf)
        self.assertEqual(self.last(16).params[:3], bytes([0, 64, 64]))
        self.assertEqual(self.effect_of(), 3, 'steady, so it cannot be mistaken for the glow')

    def test_voice_started_is_yellow(self):
        self.lib.wy_led_voice_started(self.buf)
        self.assertEqual(self.last(16).params[:3], bytes([255, 255, 0]))

    # --- the boot and connection handshake ---

    def test_boot_is_a_white_breath(self):
        self.lib.wy_led_boot(self.buf)
        self.assertEqual(self.last(16).params[:3], bytes([255, 255, 255]))
        self.assertEqual(self.effect_of(), 1, 'the startup look pulses')

    def test_the_first_connect_flashes_green_three_times(self):
        self.lib.wy_led_boot(self.buf)
        self.writes.clear()
        self.lib.wy_led_connected(self.buf, 1)
        greens = [w for w in self.writes if w.cmd == 16 and w.params[:3] == bytes([0, 255, 0])]
        self.assertEqual(len(greens), 1, 'the colour is set once and then the effect is toggled')
        # Count the commands, not the sampled state: a phase that runs past its last change would
        # otherwise be counted once per tick. Three flashes is exactly six phases, and the first
        # one starts at the moment of connection rather than on the first tick.
        initial = self.effect_of()
        self.writes.clear()
        for _ in range(12):
            self.tick(300)
        effects = [w.params[0] for w in self.writes if w.cmd == 12]
        self.assertEqual([initial] + effects, [3, 0, 3, 0, 3, 0],
                         'three green flashes: on off on off on off')
        self.assertEqual(self.lib.wy_led_state(self.buf), 0, 'ends dark')

    def test_a_later_reconnect_is_silent(self):
        self.lib.wy_led_connected(self.buf, 0)
        self.assertEqual(self.effect_of(), 0, 'a reconnect goes dark and says nothing')
        self.assertEqual(self.lib.wy_led_state(self.buf), 0)

    def test_the_startup_look_ends_when_the_host_connects(self):
        """A light show that outlives its welcome is noise: the green flash replaces it."""
        self.lib.wy_led_boot(self.buf)
        self.lib.wy_led_connected(self.buf, 1)
        self.assertEqual(self.effect_of(), 3, 'the startup pulse is over')

    # --- red is an OUTAGE, never a blink ---

    def test_a_disconnect_does_not_show_red_immediately(self):
        self.lib.wy_led_disconnected(self.buf)
        self.assertIsNone(self.effect_of(), 'nothing at all is commanded at the moment of disconnect')
        self.assertEqual(self.lib.wy_led_red_pending(self.buf), 1)

    def test_red_appears_only_after_the_grace_period(self):
        self.lib.wy_led_disconnected(self.buf)
        self.tick(29999)
        self.assertEqual(self.lib.wy_led_red_pending(self.buf), 1, 'still waiting')
        self.assertNotEqual(self.colour_of(), bytes([255, 0, 0]), 'red must not appear early')
        self.tick(2)
        self.assertEqual(self.effect_of(), 3, 'an outage that survives the grace is red')
        self.assertEqual(self.colour_of(), bytes([255, 0, 0]))
        self.assertEqual(self.lib.wy_led_red_pending(self.buf), 0)

    def test_a_reconnect_cancels_the_pending_red(self):
        self.lib.wy_led_disconnected(self.buf)
        self.tick(10000)
        self.lib.wy_led_connected(self.buf, 0)
        self.assertEqual(self.lib.wy_led_red_pending(self.buf), 0, 'the reconnect cancelled it')
        self.writes.clear()                      # only what happens FROM HERE may be judged
        self.tick(30000)
        reds = [w for w in self.writes if w.cmd == 16 and w.params[:3] == bytes([255, 0, 0])]
        self.assertEqual(reds, [], 'the cancelled red still appeared')

    def test_the_grace_period_is_thirty_seconds(self):
        self.assertEqual(self.lib.wy_led_red_pending(self.buf), 0)
        self.lib.wy_led_disconnected(self.buf)
        self.tick(29999)
        self.assertEqual(self.lib.wy_led_red_pending(self.buf), 1)
        self.tick(30000 - 29999)
        self.assertEqual(self.lib.wy_led_red_pending(self.buf), 0)

    # --- the direction-finding behaviour ---

    def test_wake_is_a_circular_sweep(self):
        self.lib.wy_led_wake(self.buf)
        self.assertEqual(self.effect_of(), 2, 'the activation tone rings under a rainbow sweep')

    def test_the_sweep_settles_into_direction_mode(self):
        """The brightest point goes where the person is, which the XMOS computes for us."""
        self.lib.wy_led_wake(self.buf)
        self.tick(399)
        self.assertEqual(self.effect_of(), 2, 'the sweep runs for as long as the tone rings')
        self.tick(2)
        self.assertEqual(self.effect_of(), 4, 'after the tone, the ring shows the direction')
        self.assertEqual(self.lib.wy_led_state(self.buf), 4)   # WY_LED_STATE_LISTENING

    def test_direction_mode_pairs_a_quiet_ring_with_a_bright_marker(self):
        self.lib.wy_led_listening(self.buf)
        doa = self.last(17)
        self.assertEqual(len(doa.params), 6, 'two RGB colours: base then marker')
        self.assertEqual(doa.params[:3], bytes([0, 0, 0]), 'the ring itself stays quiet')
        self.assertEqual(doa.params[3:6], bytes([255, 255, 255]), 'the marker is the bright part')

    def test_listening_is_idempotent_with_the_sweep(self):
        self.lib.wy_led_wake(self.buf)
        self.lib.wy_led_listening(self.buf)
        self.assertEqual(self.effect_of(), 4)

    # --- the rest of the lifecycle ---

    def test_transcript_is_green_and_brief(self):
        self.lib.wy_led_transcript(self.buf)
        self.assertEqual(self.last(16).params[:3], bytes([0, 255, 0]))
        self.tick(1001)
        self.assertEqual(self.effect_of(), 0, 'it does not stay green')

    def test_idle_is_dark(self):
        self.lib.wy_led_idle(self.buf)
        self.assertEqual(self.effect_of(), 0)

    def test_speaking_is_the_working_colour(self):
        self.lib.wy_led_speaking(self.buf)
        self.assertEqual(self.last(16).params[:3], bytes([150, 70, 255]))

    # --- brightness ---

    def test_brightness_scales_from_the_fleet_scale(self):
        """The fleet's led_brightness is 1-31; the ring wants 0-255. 31 must be full."""
        self.assertEqual(self.lib.wy_led_scale_brightness(31), 255)
        self.assertEqual(self.lib.wy_led_scale_brightness(1), 8)
        self.assertEqual(self.lib.wy_led_scale_brightness(0), 8, '0 clamps to the dimmest, not off')
        self.assertEqual(self.lib.wy_led_scale_brightness(99), 255, 'out of range clamps to full')

    def test_set_brightness_writes_the_scaled_value(self):
        self.lib.wy_led_set_brightness(self.buf, 31)
        self.assertEqual(self.last(13).params, bytes([255]))

    # --- robustness ---

    def test_a_failing_emit_does_not_crash_the_core(self):
        """The LEDs are feedback, never a link fault. A dead ring must not take the voice path."""
        @EMIT
        def failing(_user, _resid, _cmd, _params, _nparams):
            return -5
        buf = C.create_string_buffer(self.lib.wy_led_size())
        self.assertEqual(self.lib.wy_led_init(buf, failing, None), 0)
        for name in ('boot', 'wake', 'listening', 'thinking', 'error', 'idle', 'disconnected'):
            getattr(self.lib, 'wy_led_' + name)(buf)
        self.lib.wy_led_tick(buf, 40000)

    def test_the_core_rejects_a_missing_callback(self):
        buf = C.create_string_buffer(self.lib.wy_led_size())
        self.assertNotEqual(self.lib.wy_led_init(buf, EMIT(0), None), 0,
                            'a core with nowhere to emit is refused, not accepted and silent')


if __name__ == '__main__':
    unittest.main()
