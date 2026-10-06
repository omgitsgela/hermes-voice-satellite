#!/usr/bin/env python3
"""Compile the production config agent; exercise the desired/apply/report state machine.

Host half of the declarative configuration plane (docs/configuration-plane.md),
agent unit level (tasks 1-4). The agent is a pure state machine: in is the
hermes.config.desired `data` JSON string, out is the hermes.config.reported `data`
JSON string. The apply adapter (the device glue) is injected -- on the device it sets
the live volume and stages the wake settings; the test installs a faithful stand-in
that reports which settings it applied. The canonical wyoming events and the session
wire framing are exercised at the session level (task 5).
"""
import ctypes as C
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / 'firmware/esp32/components'

# out_actual / out_error are OUT parameters: POINTER(c_char) (a writable buffer the C code
# writes through), never c_char_p (which ctypes converts to an immutable bytes copy that C
# must not write -- that is exactly the segfault this harness used to hit).
ADAPTER = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.POINTER(C.c_char),
                      C.c_size_t, C.POINTER(C.c_int), C.POINTER(C.c_char), C.c_size_t)


class ConfigAgentTests(unittest.TestCase):
    def setUp(self):
        self._keepalive = []   # ctypes callbacks must outlive the call that passed them

    def library(self):
        source = COMP / 'wyoming_config/wyoming_config.c'
        self.assertTrue(source.exists(), 'config agent missing: %s' % source)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Path(temporary.name) / 'config.so'
        parser = COMP / 'wyoming_json'
        includes = [COMP / 'wyoming_config', COMP / 'wyoming_wire', parser,
                    parser / 'vendor/cjson']
        sources = [source, parser / 'wyoming_json.c', parser / 'vendor/cjson/cJSON.c']
        subprocess.run(
            ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
             '-DCJSON_NESTING_LIMIT=16', *['-I' + str(p) for p in includes],
             *map(str, sources), '-lm', '-o', str(library)], check=True)
        lib = C.CDLL(str(library))
        lib.wy_config_size.restype = C.c_size_t
        lib.wy_config_init.argtypes = [C.c_void_p, C.c_char_p]
        lib.wy_config_init.restype = C.c_int
        lib.wy_config_step.argtypes = [C.c_void_p, C.c_char_p, C.c_char_p, C.c_size_t]
        lib.wy_config_step.restype = C.c_int
        lib.wy_config_set_apply.argtypes = [C.c_void_p, ADAPTER, C.c_void_p]
        return lib

    def _install_adapter(self, lib, session, calls, mode):
        @ADAPTER
        def adapter(user, settings_json, out_actual, actual_cap, out_health,
                    out_error, err_cap):
            calls[0] += 1
            settings = json.loads(settings_json.decode('utf-8'))
            if mode == 'degraded':
                # The device glue's stand-in: the only setting with a live path on this
                # hardware is playback_volume, so that is all that counts as applied; the
                # rest are staged (wake settings) or have no adapter, hence degraded.
                actual = {'playback_volume': settings['playback_volume']} \
                    if 'playback_volume' in settings else {}
                health = 2   # WY_CFG_HEALTH_DEGRADED
            else:   # 'healthy': the adapter applied every setting it was given.
                actual = {k: v for k, v in settings.items()}
                health = 1   # WY_CFG_HEALTH_HEALTHY
            text = json.dumps(actual).encode('utf-8')
            if len(text) + 1 < actual_cap:
                C.memmove(out_actual, text, len(text) + 1)
            else:
                C.memmove(out_actual, b'{}', 2)
            out_health[0] = health
            C.memmove(out_error, b'\x00', 1)
            return 0   # WY_OK
        self._keepalive.append(adapter)
        lib.wy_config_set_apply(session, adapter, None)
        return session

    def new(self, lib, device_id=b'control-fixture', with_adapter=True, calls=None,
            mode='degraded'):
        session = C.create_string_buffer(lib.wy_config_size())
        self.assertEqual(lib.wy_config_init(session, device_id), 0)
        if with_adapter:
            calls = calls if calls is not None else [0]
            self._install_adapter(lib, session, calls, mode)
        return session

    def step(self, lib, session, desired_data):
        """Feed one desired-state payload; return the reported data dict."""
        payload = json.dumps(desired_data).encode('utf-8')
        report = C.create_string_buffer(4096)
        result = lib.wy_config_step(session, payload, report, 4096)
        self.assertEqual(result, 0, 'a well-formed desired always produces a report')
        return json.loads(report.value.decode('utf-8'))

    def desired(self, generation, settings, device_id='control-fixture',
                schema_version=1):
        return {'schema_version': schema_version, 'device_id': device_id,
                'generation': generation, 'settings': settings}

    def default_profile(self, **over):
        """The complete safe-default profile ensure_device seeds for a new device."""
        profile = {'wake_word': 'hey_hermes', 'wake_threshold': 0.35, 'mic_gain': 78,
                   'playback_volume': 55, 'led_brightness': 31, 'locale': 'en-US',
                   'update_channel': 'stable'}
        profile.update(over)
        return profile

    # -- task 1: allowlist validation -----------------------------------------
    def test_first_desired_is_answered_and_applies_volume(self):
        """A new device gets the complete default profile right after registration.
        The adapter applies only playback_volume (the one setting with a live path);
        the rest degrade. actual carries exactly what the adapter reported applied."""
        lib = self.library()
        data = self.step(lib, self.new(lib),
                         self.desired(1, self.default_profile()))
        self.assertEqual(data['generation'], 1)
        self.assertEqual(data['health'], 'degraded')
        self.assertEqual(data['actual'], {'playback_volume': 55})

    def test_unknown_setting_is_an_error_not_applied(self):
        lib = self.library()
        data = self.step(lib, self.new(lib),
                         self.desired(1, {'wake_threshold': 0.35, 'speaker_gain': 50}))
        self.assertEqual(data['health'], 'error')
        self.assertIn('unknown', data['error'])
        # A failed event is not accepted: the device's actual state stays empty.
        self.assertEqual(data['actual'], {})

    def test_out_of_range_and_wrong_type_settings_are_rejected(self):
        lib = self.library()
        for settings in [
            {'playback_volume': 101},      # above the integer maximum
            {'playback_volume': -1},       # below the integer minimum
            {'playback_volume': 50.5},     # a fraction is not an integer
            {'playback_volume': True},     # a bool is not an integer
            {'mic_gain': 'high'},          # a string is not an integer
            {'wake_threshold': 0.01},      # below the number minimum
            {'wake_threshold': 1.5},       # above the number maximum
            {'led_brightness': 0},         # below 1
            {'led_brightness': 32},        # above 31
            {'locale': 'fr-FR'},           # not in the enum
            {'update_channel': 'nightly'}, # not in the enum
            {'wake_word': 'HEY HERMES'},   # uppercase: not the pattern
            {'wake_word': 'hey hermes'},   # space: not the pattern
        ]:
            with self.subTest(settings=settings):
                data = self.step(lib, self.new(lib),
                                 self.desired(1, settings))
                self.assertEqual(data['health'], 'error')
                self.assertEqual(data['actual'], {})

    def test_wrong_schema_version_device_id_and_generation_are_rejected(self):
        lib = self.library()
        for data in [
            self.desired(1, {'playback_volume': 50}, schema_version=2),
            self.desired(1, {'playback_volume': 50}, device_id='another-device'),
            self.desired(0, {'playback_volume': 50}),
            self.desired(True, {'playback_volume': 50}),   # a bool is not a generation
        ]:
            with self.subTest(data=data):
                reported = self.step(lib, self.new(lib), data)
                self.assertEqual(reported['health'], 'error')

    def test_valid_boundary_values_are_accepted(self):
        lib = self.library()
        data = self.step(lib, self.new(lib), self.desired(1, {
            'playback_volume': 0, 'mic_gain': 100, 'wake_threshold': 0.05,
            'led_brightness': 1, 'wake_word': 'a', 'locale': 'en-US',
            'update_channel': 'beta'}))
        self.assertEqual(data['health'], 'degraded')
        self.assertEqual(data['actual'], {'playback_volume': 0})

    # -- task 2: the generation guard (generation-safe) ----------------------
    def test_older_generation_is_never_applied(self):
        """A stale desired (an older generation than the one in force) must not change
        anything: the device re-reports its CURRENT state and does not re-apply."""
        lib = self.library()
        calls = [0]
        session = self.new(lib, calls=calls)
        first = self.step(lib, session, self.desired(2, {'playback_volume': 40}))
        self.assertEqual(first['generation'], 2)
        self.assertEqual(first['actual'], {'playback_volume': 40})
        self.assertEqual(calls[0], 1)
        # An older generation arrives, claiming a different volume.
        stale = self.step(lib, session, self.desired(1, {'playback_volume': 10}))
        self.assertEqual(stale['generation'], 2,
                         'a stale generation re-reports the current one')
        self.assertEqual(stale['actual'], {'playback_volume': 40},
                         'a stale generation must not change the actual state')
        self.assertEqual(calls[0], 1, 'a stale generation must not re-apply')

    def test_same_generation_redelivery_is_idempotent(self):
        """When the adapter fully applied everything (a healthy, fully-applied state),
        redelivering the SAME generation with the SAME settings must not re-apply: it is
        already converged. Proven by the adapter's call count staying put and the state
        staying identical."""
        lib = self.library()
        calls = [0]
        session = self.new(lib, calls=calls, mode='healthy')
        first = self.step(lib, session, self.desired(1, {'playback_volume': 55}))
        self.assertEqual(first['health'], 'healthy')
        self.assertEqual(calls[0], 1)
        again = self.step(lib, session, self.desired(1, {'playback_volume': 55}))
        self.assertEqual(calls[0], 1, 'a converged same-generation redelivery re-applied')
        self.assertEqual(again['generation'], 1)
        self.assertEqual(again['actual'], {'playback_volume': 55})
        self.assertEqual(again['health'], 'healthy')

    def test_same_generation_redelivery_reapplies_volatile(self):
        """The device's immediate setting (the volume) is VOLATILE: it lives in session RAM,
        and the adapter reports only a PARTIAL actual (only the volume applied; the rest are
        staged or have no adapter). With the full 7-key profile, the state is therefore never
        "fully applied", so a same-generation redelivery of that profile MUST re-run the
        adapter (re-setting the same volume, re-staging the same wake settings) -- harmless
        and idempotent in effect, and what makes a power cycle converge. Proven by the call
        count advancing while the reported state stays identical."""
        lib = self.library()
        calls = [0]
        session = self.new(lib, calls=calls, mode='degraded')
        profile = self.default_profile()
        first = self.step(lib, session, self.desired(1, profile))
        self.assertEqual(first['actual'], {'playback_volume': profile['playback_volume']})
        self.assertEqual(calls[0], 1)
        again = self.step(lib, session, self.desired(1, profile))
        self.assertEqual(calls[0], 2, 'the volatile value must be re-applied on redelivery')
        self.assertEqual(again['actual'], {'playback_volume': profile['playback_volume']},
                         're-applying the same value leaves the state identical')
        self.assertEqual(again['generation'], 1)

    def test_same_generation_different_settings_is_rejected(self):
        """Two DIFFERENT settings under one already-spent generation is a replay of a
        different event: it is refused (error) and the stored state is untouched."""
        lib = self.library()
        calls = [0]
        session = self.new(lib, calls=calls)
        self.step(lib, session, self.desired(1, {'playback_volume': 55}))
        bad = self.step(lib, session, self.desired(1, {'playback_volume': 60}))
        self.assertEqual(bad['health'], 'error')
        self.assertIn('different settings', bad['error'])
        self.assertEqual(calls[0], 1, 'a conflicting redelivery must not re-apply')
        # The stored state is still the first event's.
        self.assertEqual(bad['actual'], {'playback_volume': 55})
        # And a fresh, correct generation still applies.
        next_gen = self.step(lib, session, self.desired(2, {'playback_volume': 60}))
        self.assertEqual(next_gen['actual'], {'playback_volume': 60})
        self.assertEqual(next_gen['generation'], 2)


    # -- task 4: durable persistence before acceptance -----------------------
    SAVE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.c_size_t)
    LOAD = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(C.c_char), C.c_size_t,
                       C.POINTER(C.c_size_t))

    def new_storage(self, blob=b'', fail_first=0):
        """An in-memory stand-in for the NVS namespace: one blob, one commit."""
        st = {'blob': bytearray(blob), 'fail': fail_first}

        @self.SAVE
        def save(user, js, length):
            if st['fail']:
                st['fail'] -= 1
                return -1
            st['blob'] = bytearray(C.string_at(js, length))
            return 0

        @self.LOAD
        def load(user, out, cap, outlen):
            if not st['blob'] or len(st['blob']) + 1 >= cap:
                return -1
            data = bytes(st['blob'])
            C.memmove(out, data, len(data))
            outlen[0] = len(data)
            return 0
        self._keepalive += [save, load]
        return st, save, load

    def with_storage(self, lib, session, st, save, load):
        lib.wy_config_set_storage.argtypes = [C.c_void_p, self.SAVE, self.LOAD, C.c_void_p]
        lib.wy_config_set_storage(session, save, load, None)
        return session

    # -- task 4 (real tests) -------------------------------------------------
    def test_persist_writes_the_durable_state_before_the_report(self):
        lib = self.library()
        st, save, load = self.new_storage()
        session = self.new(lib)
        self.with_storage(lib, session, st, save, load)
        profile = self.default_profile()
        report = self.step(lib, session, self.desired(1, profile))
        self.assertEqual(report['generation'], 1)
        # The report was produced; the blob must already be durable.
        self.assertEqual(len(st['blob']) > 0, True, 'no state was persisted')
        blob = json.loads(st['blob'].decode('utf-8'))
        self.assertEqual(blob['schema_version'], 1)
        self.assertEqual(blob['device_id'], 'control-fixture')
        self.assertEqual(blob['generation'], 1)
        self.assertEqual(blob['desired'], profile, 'the guard needs the desired settings')
        self.assertEqual(blob['health'], 2, 'the blob stores the health enum, not the wire string')
        self.assertNotIn('actual', blob,
                         'actual is a live runtime condition, not durable state')

    def test_restart_restores_the_generation_guard(self):
        """A firmware reboot loses the RAM (the volatile volume) but not the NVS. After
        re-init on the same blob: (a) a redelivery of the SAME generation/settings re-applies
        the volatile value (actual was not persisted), (b) a DIFFERENT settings under that
        already-spent generation is refused (proves the desired survived), (c) a new
        generation applies. Together these prove the guard state (generation + desired)
        survived the reboot, not just that the blob existed."""
        lib = self.library()
        st, save, load = self.new_storage()
        session = self.new(lib)
        self.with_storage(lib, session, st, save, load)
        profile = self.default_profile()
        self.step(lib, session, self.desired(1, profile))

        # The reboot: a fresh core (fresh RAM) over the same durable blob.
        calls = [0]
        session2 = self.new(lib, calls=calls)
        self.with_storage(lib, session2, st, save, load)

        # (a) the bridge's reconnect re-dispatch (same generation, same settings) re-applies
        # the volatile volume: actual was empty after restore, so the unapplied check fires.
        redelivered = self.step(lib, session2, self.desired(1, profile))
        self.assertEqual(calls[0], 1, 'the redelivery must re-apply the volatile volume')
        self.assertEqual(redelivered['generation'], 1)
        self.assertEqual(redelivered['actual'], {'playback_volume': profile['playback_volume']})

        # (b) a DIFFERENT payload under the already-spent generation is refused -- this is
        # what proves the DESIRED settings survived (a lost guard would have applied it).
        conflict = self.step(lib, session2,
                             self.desired(1, {'playback_volume': 99}))
        self.assertEqual(conflict['health'], 'error')
        self.assertIn('different settings', conflict['error'])
        self.assertEqual(calls[0], 1, 'a refused redelivery must not re-apply')

        # (c) a new generation still applies.
        fresh = self.step(lib, session2, self.desired(2, {'playback_volume': 99}))
        self.assertEqual(fresh['generation'], 2)
        self.assertEqual(fresh['actual'], {'playback_volume': 99})

    def test_corrupt_blob_is_discarded_to_first_boot(self):
        """A torn or garbage NVS blob must degrade to first-boot state (a fresh guard), not
        crash and not partially restore: schema_version is checked, so anything that fails
        the check is thrown away and the device converges on the bridge's next dispatch."""
        lib = self.library()
        for bad in [b'garbage-not-json', b'{"schema_version":999,"generation":7}',
                    b'{"schema_version":1,  # truncated', b'']:
            with self.subTest(blob=bad):
                st, save, load = self.new_storage(blob=bad)
                calls = [0]
                session = self.new(lib, calls=calls)
                self.with_storage(lib, session, st, save, load)
                # First boot after a corrupt blob: generation 1 must be treated as NEW.
                report = self.step(lib, session,
                                   self.desired(1, {'playback_volume': 50}))
                self.assertEqual(report['generation'], 1)
                self.assertEqual(report['actual'], {'playback_volume': 50})
                self.assertEqual(calls[0], 1, 'a corrupt blob must not block first boot')

    def test_persist_failure_is_an_error_and_retries(self):
        """If the NVS commit fails, the event is NOT accepted as durable: the report says
        error. The bridge will then re-dispatch (or the device retries on redelivery), and
        when storage recovers the same generation converges. The in-memory side effects stay
        (the volume was set); only the durable claim is withheld."""
        lib = self.library()
        st, save, load = self.new_storage(fail_first=1)
        session = self.new(lib)
        self.with_storage(lib, session, st, save, load)
        failed = self.step(lib, session, self.desired(1, {'playback_volume': 55}))
        self.assertEqual(failed['health'], 'error')
        self.assertIn('persist', failed['error'].lower(),
                      'a persist failure must be named as such')
        self.assertEqual(len(st['blob']) > 0, False, 'no state may be durable on failure')
        # Storage recovers; the same generation with the same settings re-applies and lands.
        recovered = self.step(lib, session, self.desired(1, {'playback_volume': 55}))
        self.assertEqual(recovered['generation'], 1)
        self.assertEqual(recovered['actual'], {'playback_volume': 55})
        self.assertEqual(recovered['health'], 'degraded')
        self.assertEqual(len(st['blob']) > 0, True, 'the recovered persist must be durable')

    def test_persist_failure_keeps_the_generation_unspent(self):
        """Distinguishes the commit ordering: persist BEFORE commit. If the persist fails,
        the stored state (generation + desired) must not have advanced, so a payload under
        that generation is still unspent and applies -- it is NOT refused as a conflict.
        (The real bridge only redelivers the identical payload, so this is the input that
        separates 'commit-then-persist' from 'persist-then-commit'.)"""
        lib = self.library()
        st, save, load = self.new_storage(fail_first=1)
        session = self.new(lib)
        self.with_storage(lib, session, st, save, load)
        self.step(lib, session, self.desired(1, {'playback_volume': 55}))
        # Nothing was committed, so generation 1 is still unspent: this payload applies.
        landed = self.step(lib, session, self.desired(1, {'playback_volume': 60}))
        self.assertEqual(landed['generation'], 1)
        self.assertEqual(landed['health'], 'degraded')
        self.assertEqual(landed['actual'], {'playback_volume': 60})

    def test_healthy_restore_redelivery_reapplies(self):
        """A REBOOT (or a gateway restart that re-seeds the volume) loses the live value even
        when the last apply was FULLY APPLIED: the adapter reported every setting, so the
        durable state is healthy -- and actual was deliberately not persisted (it is a live
        runtime condition, not durable state). The bridge's reconnect re-dispatch under the
        same generation must therefore RE-APPLY, not take the healthy no-op fast path: the
        no-op decision has to depend on `actual` (which is empty after restore), never on the
        persisted health alone. Proven by the adapter's call count advancing after restore."""
        lib = self.library()
        st, save, load = self.new_storage()
        session = self.new(lib)
        self.with_storage(lib, session, st, save, load)
        calls = [0]
        session2 = self.new(lib, calls=calls, mode='healthy')
        self.with_storage(lib, session2, st, save, load)
        # First apply: the adapter reports EVERY setting applied -> a fully healthy state.
        first = self.step(lib, session2, self.desired(1, {'playback_volume': 55}))
        self.assertEqual(first['health'], 'healthy')
        self.assertEqual(first['actual'], {'playback_volume': 55})
        self.assertEqual(calls[0], 1)
        # The healthy same-generation no-op still holds WHILE the value is live in RAM ...
        again = self.step(lib, session2, self.desired(1, {'playback_volume': 55}))
        self.assertEqual(calls[0], 1, 'a converged live healthy state must not re-apply')
        # ... but after a reboot the volatile value is GONE (the fresh session defaults it),
        # and the same-generation redelivery must restore it: no-op-on-health alone would
        # leave the speaker at the default level for the rest of the life of the link.
        calls2 = [0]
        session3 = self.new(lib, calls=calls2, mode='healthy')
        self.with_storage(lib, session3, st, save, load)
        redelivered = self.step(lib, session3, self.desired(1, {'playback_volume': 55}))
        self.assertEqual(calls2[0], 1,
                         'the healthy-but-lost volatile value must be re-applied after a reboot')
        self.assertEqual(redelivered['generation'], 1)
        self.assertEqual(redelivered['actual'], {'playback_volume': 55})
        self.assertEqual(redelivered['health'], 'healthy')

    def test_fractional_or_huge_generation_is_rejected(self):
        """The generation must be a POSITIVE INTEGER within the platform's long. A fraction
        (1.5) is not an integer -- the reference requires isinstance(int) -- and a value that
        overflows the device's 32-bit long (above 2^31) must be refused before it is cast,
        not truncated into the stored state (a wrapped 0 would silently land in the
        same-generation no-op path). Both are refused with an error, and the stored state is
        untouched: a correct generation still applies afterwards."""
        lib = self.library()
        for bad in [1.5, 4294967296]:   # a fraction; 2^32 (a 32-bit long wrap to 0)
            with self.subTest(generation=bad):
                calls = [0]
                session = self.new(lib, calls=calls)
                reported = self.step(lib, session, self.desired(bad, {'playback_volume': 55}))
                self.assertEqual(reported['health'], 'error')
                self.assertIn('generation', reported['error'])
                self.assertEqual(reported['actual'], {})
                self.assertEqual(calls[0], 0, 'a refused generation must not apply')
                # The stored state is still empty: the correct generation still applies.
                fresh = self.step(lib, session, self.desired(1, {'playback_volume': 55}))
                self.assertEqual(fresh['generation'], 1)
                self.assertEqual(fresh['actual'], {'playback_volume': 55})

    def test_corrupt_blob_with_fractional_generation_is_discarded(self):
        """The RESTORE path has the same guard: a torn or hand-edited blob whose generation is
        not an integer (or is out of long range) must be treated as no persisted state at
        all -- not a half-restored guard. The device converges on the bridge's next dispatch:
        generation 1 must be treated as NEW, and the redelivery must apply."""
        lib = self.library()
        for bad in [b'{"schema_version":1,"device_id":"control-fixture",'
                    b'"generation":1.5,"desired":{"playback_volume":55}}',
                    b'{"schema_version":1,"device_id":"control-fixture",'
                    b'"generation":4294967296,"desired":{"playback_volume":55}}']:
            with self.subTest(blob=bad):
                st, save, load = self.new_storage(blob=bad)
                calls = [0]
                session = self.new(lib, calls=calls)
                self.with_storage(lib, session, st, save, load)
                report = self.step(lib, session, self.desired(1, {'playback_volume': 55}))
                self.assertEqual(report['generation'], 1,
                                 'a fractional persisted generation must not block first boot')
                self.assertEqual(report['actual'], {'playback_volume': 55})
                self.assertEqual(calls[0], 1, 'a discarded fractional-generation blob must not block apply')

    def test_report_fits_the_session_buffer_even_with_a_poisoned_key(self):
        """The session hands the agent a 1024-byte report buffer, and the validation error
        interpolates the PEER's key into it: a hostile key (control characters) can be JSON-
        escaped to 6x its raw size (\\u00XX), which used to push the report past the buffer --
        the agent then produced no report, the session treated that as a link fault, and the
        bridge re-dispatched the SAME poisoned frame on every registration: a reconnect storm
        the device could never clear. The report must ALWAYS fit the 1024 buffer at any peer
        input: the error may drop the untrusted detail (the key is not the fix) but the event
        is still a clean, parseable error report -- never silence, never a link failure."""
        lib = self.library()
        session = self.new(lib)
        poison = {'\x01' * 255: 55}   # 255 control chars: escapes to ~1530 bytes
        payload = json.dumps(self.desired(1, poison)).encode('utf-8')
        report = C.create_string_buffer(1024)
        result = lib.wy_config_step(session, payload, report, 1024)
        self.assertEqual(result, 0, 'a hostile key must not defeat the 1024-byte report buffer')
        data = json.loads(report.value.decode('utf-8'))
        self.assertEqual(data['health'], 'error', 'the unknown setting is still refused ...')
        self.assertEqual(data['actual'], {}, '... and nothing was applied')
        self.assertIn('unknown', data['error'])


if __name__ == '__main__':
    unittest.main()
