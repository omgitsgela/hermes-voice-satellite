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
import base64
import ctypes as C
import json
import subprocess
import sys
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
        lib.wy_config_step.argtypes = [C.c_void_p, C.c_char_p, C.POINTER(C.c_char), C.c_size_t]
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
        profile = {'wake_word': 'hey_hermes', 'wake_threshold': 0.35, 'mic_volume': 78,
                   'playback_volume': 55, 'led_brightness': 31}
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
            {'mic_volume': 'high'},          # a string is not an integer
            {'wake_threshold': 0.01},      # below the number minimum
            {'wake_threshold': 1.5},       # above the number maximum
            {'led_brightness': 0},         # below 1
            {'led_brightness': 32},        # above 31
            {'playback_volume': 101},      # above the maximum
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
            'playback_volume': 0, 'mic_volume': 100, 'wake_threshold': 0.05,
            'led_brightness': 1, 'wake_word': 'a'}))
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

        # (a) the Gateway's reconnect re-dispatch (same generation, same settings) re-applies
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
        the check is thrown away and the device converges on the Gateway's next dispatch."""
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
        error. The Gateway will then re-dispatch (or the device retries on redelivery), and
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
        (The real Gateway only redelivers the identical payload, so this is the input that
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
        runtime condition, not durable state). The Gateway's reconnect re-dispatch under the
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

    def test_persist_failure_leaves_stored_state_untouched(self):
        """A failed persist must leave the STORED state untouched -- every field that is
        durable, including c->error (it is persisted in the blob and restored on reboot).
        The apply section used to write c->error in RAM before the commit (it is cleared at
        the top of the apply and set from the adapter result), so a persist failure left the
        RAM a mix: the NEW/cleared error with the OLD generation/health/desired. The RAM
        then DIVERGED from the durable blob: a current-state read on the same session
        (e.g. an older-generation event, which re-reports the stored state) returned the
        mutated RAM error while the blob still carried the durable one, and a reboot would
        restore yet another value. The invariant: after a failed step, the RAM still equals
        the durable state, so a current-state read agrees with what restore will bring back.
        Proven on the SAME session: the durable error must survive the failed persist into
        the next current-state read."""
        lib = self.library()
        # Phase 1 (durable baseline, gen 2): no adapter -> degraded, error "staged; no
        # hardware adapter installed", committed twice so the stored state is gen 2.
        st1, save1, load1 = self.new_storage()
        session = self.new(lib, with_adapter=False)
        self.with_storage(lib, session, st1, save1, load1)
        self.step(lib, session, self.desired(1, {'playback_volume': 55}))
        second = self.step(lib, session, self.desired(2, {'playback_volume': 55}))
        self.assertEqual(second['health'], 'degraded')
        self.assertIn('staged', second['error'])
        self.assertEqual(second['generation'], 2, 'the baseline must reach generation 2')
        # Phase 2 (the failed commit): a fresh session restores the durable gen-2 state
        # (actual is NULL, not persisted); storage now FAILS once. A healthy-mode adapter
        # applies the value, so the apply SUCCEEDS and clears the staged error in RAM -- but
        # the persist fails, so the commit is refused and the generation does not advance.
        st2, save2, load2 = self.new_storage(blob=bytes(st1['blob']), fail_first=1)
        session = self.new(lib, calls=[0], mode='healthy')
        self.with_storage(lib, session, st2, save2, load2)
        failed = self.step(lib, session, self.desired(2, {'playback_volume': 55}))
        self.assertEqual(failed['health'], 'error')
        self.assertIn('persist', failed['error'].lower())
        # Phase 3 (the probe, SAME session): an older-generation event re-reports the stored
        # state and reads c->error from RAM. The durable (staged) error must still be there --
        # the failed persist must not have let the cleared RAM error become the reported state.
        current = self.step(lib, session, self.desired(1, {'playback_volume': 55}))
        self.assertEqual(current['generation'], 2,
                         'the stored generation must not have advanced (persist failed)')
        self.assertIsNotNone(current['error'],
                             'the stored (durable) error must survive the failed persist in '
                             'RAM; a cleared/None error means the apply section mutated '
                             'c->error before the commit')
        self.assertIn('staged', current['error'],
                      'a failed persist must leave the stored error in RAM untouched; the '
                      'apply section clearing c->error before the commit leaks the mutated '
                      'value into the current-state report')
        # And the unspent generation still converges once storage recovers (a fresh session
        # re-dispatching the same gen-2 payload applies and persists it).
        st3, save3, load3 = self.new_storage(blob=bytes(st1['blob']))
        session3 = self.new(lib, calls=[0], mode='healthy')
        self.with_storage(lib, session3, st3, save3, load3)
        landed = self.step(lib, session3, self.desired(2, {'playback_volume': 55}))
        self.assertEqual(landed['generation'], 2)
        self.assertEqual(landed['actual'], {'playback_volume': 55})
        self.assertEqual(landed['health'], 'healthy')

    def test_rejected_frame_does_not_bind_the_device_identity(self):
        """Identity binding follows the reference (adopt-on-commit): a device that starts
        without a stored identity (wy_config_init with no valid id) adopts the event's
        device_id only when the event is ACCEPTED and committed. The C agent adopted it as
        soon as the guard passed -- BEFORE validate_settings could reject -- so a REJECTED
        frame still pinned the identity, and every later well-formed frame addressed to
        the real device was then refused as "belongs to another device": one bad frame
        wedged the device's whole life (the guard re-reads the adopted id on every step).
        The reference (pi/config_agent.py) binds the identity in the committing
        state.update only, so a rejected frame never binds. Invariant: a frame that is
        rejected must not bind the identity; the next well-formed frame still lands."""
        lib = self.library()
        # A device with no stored identity (the embedding seeds it from the event).
        session = self.new(lib, device_id=b'')
        calls = [0]
        self._install_adapter(lib, session, calls, 'degraded')
        # A REJECTED frame (an unknown setting) addressed to some id.
        bad = self.step(lib, session,
                        self.desired(1, {'no_such_setting': 5}, device_id='evil-pin'))
        self.assertEqual(bad['health'], 'error')
        self.assertIn('unknown', bad['error'])
        # The identity must NOT have been bound by the rejected frame: a later well-formed
        # frame addressed to the real device must apply, not be refused as another device's.
        good = self.step(lib, session,
                         self.desired(2, {'playback_volume': 55}, device_id='real-device'))
        self.assertEqual(good['generation'], 2,
                         'a rejected frame must not bind the identity; the next well-formed '
                         'frame must not be refused as another device\'s')
        self.assertEqual(good['health'], 'degraded')
        self.assertEqual(good['actual'], {'playback_volume': 55})

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
        all -- not a half-restored guard. The device converges on the Gateway's next dispatch:
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

    def test_discarded_blob_does_not_clobber_the_identity(self):
        """A discarded blob must not leave ANY partial state behind -- including its
        device_id. The guard compares every desired's device_id against the agent's, and
        the true identity is seeded by wy_config_init (from device_identity on the device):
        if a torn blob's identity survived its own discard, every well-formed dispatch is
        refused ('belongs to another device') forever, because the blob re-reads on every
        boot -- the plane is wedged for the whole life of the device. A discarded blob is
        NO state: the real identity applies and generation 1 is treated as new."""
        lib = self.library()
        st, save, load = self.new_storage(
            blob=b'{"schema_version":1,"device_id":"evil-id","generation":1.5,'
                 b'"desired":{"playback_volume":55}}')
        calls = [0]
        session = self.new(lib, calls=calls)
        self.with_storage(lib, session, st, save, load)
        report = self.step(lib, session, self.desired(1, {'playback_volume': 55}))
        self.assertEqual(report['generation'], 1,
                         'a discarded blob must not clobber the identity: the real ID applies')
        self.assertEqual(report['actual'], {'playback_volume': 55})
        self.assertEqual(calls[0], 1, 'a discarded blob must not wedge the plane')

    def test_restore_requires_a_usable_desired(self):
        """A blob with a valid schema and generation but no usable `desired` is incomplete,
        not restorable: the guard's conflict check and the unapplied check both depend on
        c->desired, and with it NULL the same-generation path no-ops on EVERYTHING -- the
        lost volatile value is never re-applied and a conflicting payload under the spent
        generation is silently swallowed instead of refused (both pass-1 guarantees,
        reached through a corrupt-blob door, and through a duplicate OOM). Discard it:
        generation 5 applies, the conflicting payload is refused, and the converged no-op
        then holds."""
        lib = self.library()
        st, save, load = self.new_storage(
            blob=b'{"schema_version":1,"device_id":"control-fixture","generation":5,'
                 b'"health":2}')
        calls = [0]
        session = self.new(lib, calls=calls)
        self.with_storage(lib, session, st, save, load)
        # (a) the redelivery under the blob's generation APPLIES (discarded, not half-restored):
        # the volatile value is back.
        applied = self.step(lib, session, self.desired(5, {'playback_volume': 30}))
        self.assertEqual(calls[0], 1, 'a blob with no usable desired must be discarded, not restored')
        self.assertEqual(applied['generation'], 5)
        self.assertEqual(applied['actual'], {'playback_volume': 30})
        # (b) a conflicting payload under the now-spent generation is refused, not swallowed.
        conflict = self.step(lib, session, self.desired(5, {'playback_volume': 99}))
        self.assertEqual(conflict['health'], 'error')
        self.assertIn('different settings', conflict['error'])
        self.assertEqual(calls[0], 1, 'the refused payload must not apply')
        # (c) the converged same-generation no-op still holds.
        again = self.step(lib, session, self.desired(5, {'playback_volume': 30}))
        self.assertEqual(calls[0], 1, 'a converged redelivery must not re-apply')
        self.assertEqual(again['actual'], {'playback_volume': 30})

    def test_valid_mismatched_device_blob_wedges_the_plane(self):
        """Documented design (faithful to pi/config_agent.py, which does state.update(data)):
        a VALID blob (schema and generation intact, usable desired) whose device_id is not
        this device is adopted, and the guard then refuses every desired addressed to the
        real device -- an explicit 'this state is not this device's; an operator must erase
        the NVS' wedge, NOT a silent adoption of another device's settings. The wedge is
        permanent (the blob re-reads on every boot). This pins the design so the discard
        fix for torn blobs does not quietly change what a valid foreign blob means."""
        lib = self.library()
        st, save, load = self.new_storage(
            blob=b'{"schema_version":1,"device_id":"other-device","generation":5,'
                 b'"desired":{"playback_volume":30},"health":2}')
        calls = [0]
        session = self.new(lib, calls=calls)
        self.with_storage(lib, session, st, save, load)
        refused = self.step(lib, session, self.desired(1, {'playback_volume': 55}))
        self.assertEqual(refused['health'], 'error')
        self.assertIn('another device', refused['error'])
        self.assertEqual(calls[0], 0, 'the wedge refuses the dispatch, it does not apply')

    def test_report_fits_the_session_buffer_even_with_a_poisoned_key(self):
        """The session hands the agent a 1024-byte report buffer, and the validation error
        interpolates the PEER's key into it: a hostile key (control characters) can be JSON-
        escaped to 6x its raw size (\\u00XX), which used to push the report past the buffer --
        the agent then produced no report, the session treated that as a link fault, and the
        Gateway re-dispatched the SAME poisoned frame on every registration: a reconnect storm
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

    def test_duplicate_setting_keys_are_rejected(self):
        """cJSON preserves duplicate object keys, and the guard's equality (json_objects_equal)
        matches a key against its FIRST occurrence while cJSON_GetArraySize counts ALL of them
        -- so a desired with a duplicate key could be applied once and then refused forever as
        'same generation has different settings' on every byte-identical redelivery, and after
        a reboot the volatile value would never re-apply (the durable blob carries the dups).
        The wire is peer-controlled, so a setting that appears twice is malformed input and is
        refused at the validation layer, where malformed input is already refused: never applied,
        never stored, never reaching the guard. (The reference collapses duplicates via dict
        semantics; on the wire we reject rather than silently adopt last-wins.) The payload
        must be hand-built: the harness's desired() goes through a Python dict + json.dumps,
        which cannot carry duplicate keys."""
        lib = self.library()
        calls = [0]
        session = self.new(lib, calls=calls)
        payload = ('{"schema_version":1,"device_id":"control-fixture","generation":1,'
                   '"settings":{"playback_volume":50,"playback_volume":60}}')
        report = C.create_string_buffer(4096)
        result = lib.wy_config_step(session, payload.encode('utf-8'), report, 4096)
        self.assertEqual(result, 0, 'a rejection still produces a report')
        data = json.loads(report.value.decode('utf-8'))
        self.assertEqual(data['health'], 'error', 'a duplicate key is malformed and refused')
        self.assertIn('duplicate', data['error'])
        self.assertEqual(data['actual'], {}, 'nothing was applied')
        self.assertEqual(calls[0], 0, 'a duplicate payload must not apply')
        # And a clean, duplicate-free payload under the same generation still applies: the
        # refusal did not spend or wedge the guard.
        clean = self.step(lib, session, self.desired(1, {'playback_volume': 55}))
        self.assertEqual(clean['generation'], 1)
        self.assertEqual(clean['actual'], {'playback_volume': 55})

    def test_persist_never_stores_an_empty_desired_under_oom(self):
        """persist() must never claim durability for a state that restore() must reject. The
        desired is a REQUIRED field of the blob (restore discards an empty/missing one); if it
        cannot be duplicated (an allocation failure) persist must FAIL -- the caller then
        reports 'could not persist configuration state' and leaves the generation unspent --
        not substitute an empty object and claim success. A success-claim over an empty-desired
        blob is a lie across the reboot: the guard the report says is durable is the one
        restore must throw away. Proven by fault injection: a cJSON allocator that fails its
        Nth allocation is swept across N, and for EVERY N the invariant holds -- any blob that
        was written has a non-empty, correct desired. (The well-formed no-OOM success path is
        covered by the persist tests; this is the allocation-failure window they cannot reach.)"""
        lib = self.library()
        libc = C.CDLL(None)
        libc.malloc.argtypes = [C.c_size_t]
        libc.malloc.restype = C.c_void_p
        libc.free.argtypes = [C.c_void_p]
        libc.free.restype = None
        FREEFN = C.CFUNCTYPE(None, C.c_void_p)
        real_free = FREEFN(libc.free)   # the real free, as a CFunctionType (kept alive below)
        class Hooks(C.Structure):
            _fields_ = [('malloc_fn', C.CFUNCTYPE(C.c_void_p, C.c_size_t)),
                        ('free_fn', FREEFN)]
        lib.cJSON_InitHooks.argtypes = [C.POINTER(Hooks)]
        # Always reset the (process-global, per-library) allocator to the real malloc/free,
        # even if a subtest hits an unexpected C error -- never leave a failing allocator live.
        self.addCleanup(lambda: lib.cJSON_InitHooks(None))
        self._keepalive.append(real_free)
        SAVE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.c_size_t)
        LOAD = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(C.c_char), C.c_size_t,
                           C.POINTER(C.c_size_t))
        payload = json.dumps(self.desired(1, {'playback_volume': 55})).encode('utf-8')
        for n in range(1, 97):
            with self.subTest(n=n):
                counter = [0]
                @C.CFUNCTYPE(C.c_void_p, C.c_size_t)
                def alloc(size, _c=counter):
                    _c[0] += 1
                    return None if _c[0] == n else libc.malloc(size)
                keep = [alloc]
                # free_fn is the real free (matches malloc's origin, so cJSON also enables its
                # realloc path; the manual path degrades cleanly on a failed allocation).
                lib.cJSON_InitHooks(Hooks(alloc, real_free))
                st = {'blob': bytearray()}
                try:
                    @SAVE
                    def save(user, js, length):
                        st['blob'] = bytearray(C.string_at(js, length))
                        return 0
                    @LOAD
                    def load(user, out, cap, outlen):
                        return -1
                    keep += [save, load]
                    session = C.create_string_buffer(lib.wy_config_size())
                    self.assertEqual(lib.wy_config_init(session, b'control-fixture'), 0)
                    lib.wy_config_set_storage.argtypes = [C.c_void_p, SAVE, LOAD, C.c_void_p]
                    lib.wy_config_set_storage(session, save, load, None)
                    report = C.create_string_buffer(4096)
                    lib.wy_config_step(session, payload, report, 4096)
                finally:
                    lib.cJSON_InitHooks(None)
                # THE INVARIANT (N-independent): any blob that was written carries a
                # non-empty, correct desired -- never an empty-desired success-claim.
                if st['blob']:
                    blob = json.loads(bytes(st['blob']).decode('utf-8'))
                    self.assertTrue(blob.get('desired'),
                                    'a written blob must carry a non-empty desired (n=%d)' % n)
                    self.assertEqual(blob['desired'], {'playback_volume': 55},
                                     'a written blob must carry the real desired (n=%d)' % n)

    _OOM_SWEEP_CHILD = r'''
import base64, ctypes as C, json, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(sys.argv[1]).resolve()
COMP = ROOT / 'firmware/esp32/components'
payload = base64.b64decode(sys.argv[2])
nmax = int(sys.argv[3])
parser = COMP / 'wyoming_json'
includes = [COMP / 'wyoming_config', COMP / 'wyoming_wire', parser, parser / 'vendor/cjson']
sources = [COMP / 'wyoming_config' / 'wyoming_config.c', parser / 'wyoming_json.c',
           parser / 'vendor/cjson' / 'cJSON.c']
tmp = tempfile.TemporaryDirectory()
libpath = Path(tmp.name) / 'config.so'
subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                '-DCJSON_NESTING_LIMIT=16', *['-I' + str(p) for p in includes],
                *map(str, sources), '-lm', '-o', str(libpath)], check=True)
lib = C.CDLL(str(libpath))
lib.wy_config_size.restype = C.c_size_t
lib.wy_config_init.argtypes = [C.c_void_p, C.c_char_p]; lib.wy_config_init.restype = C.c_int
lib.wy_config_step.argtypes = [C.c_void_p, C.c_char_p, C.POINTER(C.c_char), C.c_size_t]
lib.wy_config_step.restype = C.c_int
SAVE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.c_size_t)
LOAD = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(C.c_char), C.c_size_t, C.POINTER(C.c_size_t))
lib.wy_config_set_storage.argtypes = [C.c_void_p, SAVE, LOAD, C.c_void_p]
libc = C.CDLL(None); libc.malloc.argtypes = [C.c_size_t]; libc.malloc.restype = C.c_void_p
libc.free.argtypes = [C.c_void_p]; libc.free.restype = None
FREEFN = C.CFUNCTYPE(None, C.c_void_p); real_free = FREEFN(libc.free)
class Hooks(C.Structure):
    _fields_ = [('malloc_fn', C.CFUNCTYPE(C.c_void_p, C.c_size_t)), ('free_fn', FREEFN)]
lib.cJSON_InitHooks.argtypes = [C.POINTER(Hooks)]
for n in range(1, nmax + 1):
    counter = [0]
    @C.CFUNCTYPE(C.c_void_p, C.c_size_t)
    def alloc(size, _c=counter):
        _c[0] += 1
        return None if _c[0] == n else libc.malloc(size)
    st = {'blob': bytearray()}
    @SAVE
    def save(user, js, length):
        st['blob'] = bytearray(C.string_at(js, length))
        return 0
    @LOAD
    def load(user, out, cap, outlen):
        return -1
    lib.cJSON_InitHooks(Hooks(alloc, real_free))
    session = C.create_string_buffer(lib.wy_config_size())
    lib.wy_config_init(session, b'control-fixture')
    lib.wy_config_set_storage(session, save, load, None)
    report = C.create_string_buffer(4096)
    rc = lib.wy_config_step(session, payload, report, 4096)
    lib.cJSON_InitHooks(None)
    print(json.dumps({'n': n, 'rc': rc,
                      'report': base64.b64encode(bytes(report.value)).decode('ascii'),
                      'blob': base64.b64encode(bytes(st['blob'])).decode('ascii')}))
'''

    def _oom_sweep(self, lib, payload, nmax):
        """Shared fault-injection driver for the OOM-window tests: a cJSON allocator that fails
        its Nth allocation, swept across N; each subtest yields (n, rc, report_bytes, blob).

        The sweep runs in a FRESH child interpreter, not in this process. The window that a
        failing key-strdup allocation lands on is a property of the glibc malloc arena: the
        earlier tests in this suite each dlopen a freshly-compiled .so and drive wy_config_step,
        which warms the parent's arena and shifts (or masks) which N lands on the "pending"/
        "actual" key allocation. A clean child process -- one .so load, no prior steps -- starts
        from the same cold arena every run, so the window is deterministic and the invariant the
        tests check is actually exercised (an in-process sweep here was a false green: it
        passed on the unfixed code because the warmed heap moved the window out of the sweep).
        `lib` is accepted for signature stability but unused: the child compiles its own."""
        child_dir = tempfile.mkdtemp(prefix='oom-sweep-')
        self.addCleanup(lambda: __import__('shutil').rmtree(child_dir, ignore_errors=True))
        child_path = Path(child_dir) / 'oom_sweep_child.py'
        child_path.write_text(self._OOM_SWEEP_CHILD)
        proc = subprocess.run(
            [sys.executable, '-B', str(child_path), str(ROOT),
             base64.b64encode(payload).decode('ascii'), str(nmax)],
            capture_output=True, text=True, check=True, timeout=300)
        results = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            results.append((d['n'], d['rc'],
                            base64.b64decode(d['report']),
                            base64.b64decode(d['blob'])))
        self.assertEqual(len(results), nmax, 'the OOM-sweep child must yield one row per N')
        return results

    def test_written_blob_is_complete_on_any_successful_persist(self):
        """persist() writes the blob through unchecked cJSON_AddItemToObject calls: on a
        key-strdup allocation failure the vendored add_item_to_object returns false with the
        item UNATTACHED and caller-owned, so a failed 'pending' add leaks the array AND saves
        a blob silently missing the 'pending' key while persist still claims success (the
        durability claim stays honest because restore treats pending as optional, but the
        advisory list is lost and the tree leaks). Invariant (N-independent): any blob that
        was written is COMPLETE -- it carries every field persist is supposed to write,
        including the 'pending' array (present even when empty). The RED window on the
        unfixed code is the 'pending' add (an empty-object substitution there, or a missing
        key with a claimed success)."""
        lib = self.library()
        payload = json.dumps(self.desired(1, {'playback_volume': 55})).encode('utf-8')
        for n, rc, report, blob in self._oom_sweep(lib, payload, 120):
            if not blob:
                continue   # persist failed (or was skipped): nothing written to check
            parsed = json.loads(blob.decode('utf-8'))
            self.assertIn('pending', parsed,
                          'a written blob must carry the pending array (n=%d); a failed '
                          'AddItemToObject leaves it out while persist claims success' % n)
            self.assertTrue(isinstance(parsed['pending'], list),
                            'a written blob must carry a real pending array (n=%d)' % n)

    def test_successful_report_always_has_the_actual_field(self):
        """report_json() builds the report through unchecked cJSON_AddItemToObject calls: on
        a key-strdup allocation failure the item is unattached and caller-owned, so the
        printed report drops that field -- the contract's exact shape (item 3: always exactly
        {generation, actual, health, error}) is broken ON THE WIRE -- while report_json
        returns success and wy_config_step returns WY_OK, so the session sends the malformed
        report (the host logs and drops a report missing a required field). Invariant
        (N-independent): every successful (WY_OK) report is EXACTLY the four contract fields
        -- generation, actual, health, error, and nothing else. The RED window on the
        unfixed code is any of the field adds (a report missing that field, exit ok); the
        exact-set check also pins the shape against future fields sneaking in."""
        lib = self.library()
        payload = json.dumps(self.desired(1, {'playback_volume': 55})).encode('utf-8')
        saw_success = False
        for n, rc, report, blob in self._oom_sweep(lib, payload, 120):
            if rc != 0:
                continue   # a failed step is allowed to produce no report
            saw_success = True
            data = json.loads(report.decode('utf-8'))
            self.assertIn('actual', data,
                          'a WY_OK report must carry the actual field (n=%d); a failed '
                          'AddItemToObject emits the report without it while claiming '
                          'success' % n)
            self.assertIn('generation', data, 'a WY_OK report must carry generation (n=%d)' % n)
            self.assertIn('health', data, 'a WY_OK report must carry health (n=%d)' % n)
            self.assertIn('error', data, 'a WY_OK report must carry the error field (n=%d)' % n)
            self.assertEqual(set(data), {'generation', 'actual', 'health', 'error'},
                             'a WY_OK report must be exactly the contract shape, '
                             'no extra keys (n=%d): %s' % (n, sorted(data)))
        self.assertTrue(saw_success, 'the sweep must include at least one successful report')


if __name__ == '__main__':
    unittest.main()
