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

# The saved-profile store, as the core sees it: a bounded list of networks the device may
# join, tried in the order it holds them.
PROFILE_SAVE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.c_char_p)
PROFILE_REMOVE = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p)
PROFILE_CLEAR = C.CFUNCTYPE(C.c_int, C.c_void_p)
PROFILE_COUNT = C.CFUNCTYPE(C.c_int, C.c_void_p)
PROFILE_GET = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_int, C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t)


class Profiles(C.Structure):
    _fields_ = [('save', PROFILE_SAVE), ('remove', PROFILE_REMOVE), ('clear', PROFILE_CLEAR),
                ('count', PROFILE_COUNT), ('get', PROFILE_GET)]


#: A real-shaped secret, used only to prove it never appears in a response.
SECRET = b'SuperSecret123'
OTHER_SECRET = b'AnotherSecret456'


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
                 'lie_on_save': False, 'lie_on_clear': False,
                 'profiles': [], 'profile_calls': []}

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

        @PROFILE_SAVE
        def profile_save(ctx, ssid, psk):
            ssid = ssid or b''
            psk = psk or b''
            if state['fail']:
                return -1
            if state['lie_on_save']:
                state['profile_calls'].append(('save-lie', ssid))
                return 0  # reports success without keeping the pair
            for entry in state['profiles']:
                if entry[0] == ssid:
                    entry[1] = psk
                    state['profile_calls'].append(('replace', ssid))
                    return 0
            state['profiles'].append([ssid, psk])
            state['profile_calls'].append(('add', ssid))
            return 0

        @PROFILE_REMOVE
        def profile_remove(ctx, ssid):
            if state['fail']:
                return -1
            ssid = ssid or b''
            for index, entry in enumerate(state['profiles']):
                if entry[0] == ssid:
                    state['profiles'].pop(index)
                    state['profile_calls'].append(('remove', ssid))
                    return 0
            return -1

        @PROFILE_CLEAR
        def profile_clear(ctx):
            if state['fail']:
                return -1
            state['profiles'].clear()
            state['profile_calls'].append(('clear', None))
            return 0

        @PROFILE_COUNT
        def profile_count(ctx):
            return len(state['profiles'])

        @PROFILE_GET
        def profile_get(ctx, index, ssid_out, ssid_capacity, psk_out, psk_capacity):
            if index < 0 or index >= len(state['profiles']):
                return -1
            ssid, psk = state['profiles'][index]
            for value, target, capacity in ((ssid, ssid_out, ssid_capacity), (psk, psk_out, psk_capacity)):
                data = value + b'\0'
                if len(data) > capacity:
                    return -1
                C.memmove(target, data, len(data))
            return 0

        store = Profiles(profile_save, profile_remove, profile_clear, profile_count, profile_get)

        lib.provision_wifi_handle.argtypes = [C.c_char_p, SAVE, SAVE, CLEAR, LOAD,
                                              C.POINTER(Profiles), C.c_void_p, C.c_void_p, C.c_size_t]

        def request(command, with_store=True):
            output = C.create_string_buffer(256)
            lib.provision_wifi_handle(command, save_ssid, save_psk, clear, load,
                                      C.byref(store) if with_store else None, None,
                                      output, len(output))
            return output.value

        lib.provision_wifi_candidates.argtypes = [C.POINTER(Profiles), LOAD, C.c_void_p, C.c_void_p,
                                                  C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]

        def candidates(capacity=6):
            ssids = C.create_string_buffer(33 * capacity)
            psks = C.create_string_buffer(65 * capacity)
            count = C.c_size_t(0)
            code = lib.provision_wifi_candidates(C.byref(store), load, None, ssids, psks, capacity,
                                                 C.byref(count))
            found = [(ssids.raw[index * 33:index * 33 + 33].split(b'\0')[0],
                      psks.raw[index * 65:index * 65 + 65].split(b'\0')[0])
                     for index in range(count.value)]
            return code, found

        state['store'] = store
        return state, request, candidates

    def test_ssid_validation(self):
        lib = self.library()
        # A 32-char SSID is legal 802.11, but the station join cannot stage one: the radio
        # config has no length field, and a full-field SSID cannot carry its terminator, so the
        # validator must refuse it rather than let it be staged and then fail at every join.
        for value, expected in [(b'GL-Test-AP', True), (b'a', True), (b'a' * 31, True),
                                (b'a' * 32, False), (b'a' * 33, False), (b'', False),
                                (b'has space', True),
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
        state, request, _ = self.harness(lib)
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
        state, request, _ = self.harness(lib)
        self.assertEqual(request(b'SET_WIFI_SSID ' + b'a' * 33), b'ERR WIFI SSID')
        self.assertEqual(request(b'SET_WIFI_SSID ' + b'a' * 32), b'ERR WIFI SSID',
                         'a 32-char SSID cannot be joined, so it must not be stageable')
        self.assertEqual(request(b'SET_WIFI_SSID'), b'ERR WIFI SSID')
        self.assertEqual(request(b'SET_WIFI_PSK 1234567'), b'ERR WIFI PSK')
        self.assertEqual(request(b'SET_WIFI_PSK'), b'ERR WIFI PSK')
        self.assertEqual(request(b'SET_WIFI_PSK ' + b'z' * 64), b'ERR WIFI PSK')
        self.assertEqual(state['calls'], [], 'nothing invalid may be written')

    def test_storage_failure_is_reported_and_not_hidden(self):
        lib = self.library()
        state, request, _ = self.harness(lib)
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
        state, request, _ = self.harness(lib)
        request(b'SET_WIFI_SSID GL-Test-AP')
        request(b'SET_WIFI_PSK ' + SECRET)
        self.assertEqual(request(b'CLEAR_WIFI'), b'OK WIFI CLEARED')
        self.assertEqual(state['ssid'], b'')
        self.assertEqual(state['psk'], b'')
        self.assertEqual(request(b'WIFI_STATUS'), b'OK WIFI UNCONFIGURED')

    def test_a_store_that_lies_is_caught_by_the_readback(self):
        """A save that reports success without storing must not be acknowledged."""
        lib = self.library()
        state, request, _ = self.harness(lib)
        state['lie_on_save'] = True
        self.assertEqual(request(b'SET_WIFI_SSID GL-Test-AP'), b'ERR STORAGE')
        self.assertEqual(request(b'SET_WIFI_PSK ' + SECRET), b'ERR STORAGE')
        self.assertNotIn(SECRET, request(b'WIFI_STATUS'))
        self.assertEqual(state['ssid'], b'', 'nothing was actually stored')

    def test_a_clear_that_lies_is_caught_by_the_readback(self):
        """A clear that reports success without erasing must not be acknowledged."""
        lib = self.library()
        state, request, _ = self.harness(lib)
        request(b'SET_WIFI_SSID GL-Test-AP')
        request(b'SET_WIFI_PSK ' + SECRET)
        state['lie_on_clear'] = True
        self.assertEqual(request(b'CLEAR_WIFI'), b'ERR STORAGE')
        self.assertEqual(request(b'WIFI_STATUS'), b'OK WIFI ssid=GL-Test-AP psk=staged',
                         'a failed clear must still report the credentials that are really there')

    def test_unknown_commands_are_refused_not_guessed(self):
        lib = self.library()
        state, request, _ = self.harness(lib)
        for command in (b'SET_WIFI', b'SET_WIFI_SSIDX rawr', b'WIFI', b'', b'SET_WIFI_PSKX abc'):
            with self.subTest(command=command):
                self.assertEqual(request(command), b'ERR COMMAND')
        self.assertEqual(state['calls'], [])


class WifiProfileTests(unittest.TestCase):
    """A device holds several networks, so it can be carried between them without a laptop."""
    library = WifiProvisioningTests.library
    harness = WifiProvisioningTests.harness

    def stage(self, request, ssid=b'GL-Test-AP', psk=SECRET):
        self.assertEqual(request(b'SET_WIFI_SSID ' + ssid), b'OK WIFI SSID STAGED')
        self.assertEqual(request(b'SET_WIFI_PSK ' + psk), b'OK WIFI PSK STAGED')

    def test_a_profile_is_saved_from_the_staged_pair_and_listed(self):
        lib = self.library()
        state, request, _ = self.harness(lib)
        self.assertEqual(request(b'WIFI_PROFILE_SAVE'), b'ERR WIFI NOT_STAGED',
                         'a profile needs a whole pair, not half of one')
        self.stage(request)
        self.assertEqual(request(b'WIFI_PROFILE_SAVE'), b'OK WIFI PROFILE SAVED n=1')
        self.assertEqual(state['profiles'], [[b'GL-Test-AP', SECRET]])
        self.assertEqual(request(b'WIFI_PROFILES'),
                         b'WIFI PROFILE index=0 ssid=GL-Test-AP psk=present\n')

    def test_saving_an_existing_network_replaces_it_in_place(self):
        """Re-configuring a network must not reorder the list or add a duplicate."""
        lib = self.library()
        state, request, _ = self.harness(lib)
        self.stage(request)
        request(b'WIFI_PROFILE_SAVE')
        self.stage(request, ssid=b'Other-AP', psk=OTHER_SECRET)
        self.assertEqual(request(b'WIFI_PROFILE_SAVE'), b'OK WIFI PROFILE SAVED n=2')
        self.stage(request, ssid=b'GL-Test-AP', psk=OTHER_SECRET)
        self.assertEqual(request(b'WIFI_PROFILE_SAVE'), b'OK WIFI PROFILE SAVED n=2')
        self.assertEqual(state['profiles'], [[b'GL-Test-AP', OTHER_SECRET], [b'Other-AP', OTHER_SECRET]])
        self.assertEqual(request(b'WIFI_PROFILES'),
                         b'WIFI PROFILE index=0 ssid=GL-Test-AP psk=present\n'
                         b'WIFI PROFILE index=1 ssid=Other-AP psk=present\n')

    def test_the_list_is_bounded_and_full_is_refused_rather_than_dropped(self):
        lib = self.library()
        state, request, _ = self.harness(lib)
        for index in range(4):
            self.stage(request, ssid=b'AP-%d' % index)
            self.assertEqual(request(b'WIFI_PROFILE_SAVE'), b'OK WIFI PROFILE SAVED n=%d' % (index + 1))
        self.stage(request, ssid=b'AP-4')
        self.assertEqual(request(b'WIFI_PROFILE_SAVE'), b'ERR WIFI PROFILE FULL')
        self.assertEqual(len(state['profiles']), 4, 'a refused save must not evict a working network')
        self.stage(request, ssid=b'AP-0', psk=OTHER_SECRET)
        self.assertEqual(request(b'WIFI_PROFILE_SAVE'), b'OK WIFI PROFILE SAVED n=4',
                         'a full list still accepts a replacement for a network it already holds')

    def test_a_profile_can_be_removed_and_the_rest_keep_their_order(self):
        lib = self.library()
        state, request, _ = self.harness(lib)
        for index in range(3):
            self.stage(request, ssid=b'AP-%d' % index)
            request(b'WIFI_PROFILE_SAVE')
        self.assertEqual(request(b'WIFI_PROFILE_REMOVE unknown-AP'), b'ERR WIFI PROFILE UNKNOWN')
        self.assertEqual(request(b'WIFI_PROFILE_REMOVE AP-1'), b'OK WIFI PROFILE REMOVED n=2')
        self.assertEqual([entry[0] for entry in state['profiles']], [b'AP-0', b'AP-2'])

    def test_clearing_profiles_leaves_the_staged_pair_alone(self):
        lib = self.library()
        state, request, _ = self.harness(lib)
        self.stage(request)
        request(b'WIFI_PROFILE_SAVE')
        self.assertEqual(request(b'WIFI_PROFILE_CLEAR'), b'OK WIFI PROFILES CLEARED n=0')
        self.assertEqual(state['profiles'], [])
        self.assertEqual(state['ssid'], b'GL-Test-AP', 'clearing the list is not clearing the staging')
        self.assertEqual(request(b'WIFI_STATUS'), b'OK WIFI ssid=GL-Test-AP psk=staged')

    def test_the_stored_passphrase_is_never_echoed(self):
        lib = self.library()
        _, request, candidates = self.harness(lib)
        self.stage(request)
        for reply in (request(b'WIFI_PROFILE_SAVE'), request(b'WIFI_PROFILES'),
                      request(b'WIFI_PROFILE_REMOVE GL-Test-AP'), request(b'WIFI_PROFILE_CLEAR'),
                      request(b'WIFI_PROFILE_SAVE')):
            self.assertNotIn(SECRET, reply, 'a reply may never carry the passphrase')
        code, found = candidates()
        self.assertEqual(code, 0)
        self.assertEqual([ssid for ssid, _psk in found], [b'GL-Test-AP'])

    def test_a_profile_store_that_lies_is_caught_by_the_readback(self):
        lib = self.library()
        state, request, _ = self.harness(lib)
        self.stage(request)
        state['lie_on_save'] = True
        self.assertEqual(request(b'WIFI_PROFILE_SAVE'), b'ERR STORAGE')
        self.assertEqual(state['profiles'], [], 'nothing was actually kept')

    def test_a_storage_failure_during_a_profile_change_is_reported(self):
        lib = self.library()
        state, request, _ = self.harness(lib)
        self.stage(request)
        request(b'WIFI_PROFILE_SAVE')
        state['fail'] = True
        self.assertEqual(request(b'WIFI_PROFILE_REMOVE GL-Test-AP'), b'ERR STORAGE')
        self.assertEqual(request(b'WIFI_PROFILE_CLEAR'), b'ERR STORAGE')
        self.assertEqual(state['profiles'], [[b'GL-Test-AP', SECRET]],
                         'a failed removal must not pretend the network is gone')

    def test_profile_commands_are_refused_when_there_is_no_profile_store(self):
        lib = self.library()
        _, request, _ = self.harness(lib)
        for command in (b'WIFI_PROFILE_SAVE', b'WIFI_PROFILES', b'WIFI_PROFILE_REMOVE x',
                        b'WIFI_PROFILE_CLEAR'):
            with self.subTest(command=command):
                self.assertEqual(request(command, with_store=False), b'ERR WIFI PROFILE UNSUPPORTED')

    def test_candidates_are_the_profiles_then_the_staged_pair_when_it_is_new(self):
        """The staged pair is how the single-network flow keeps working: it joins the end of the
        list, and it does not appear twice when it is already a saved network."""
        lib = self.library()
        _, request, candidates = self.harness(lib)
        self.assertEqual(candidates(), (0, []), 'nothing staged and nothing saved is no candidate')
        self.stage(request, ssid=b'AP-0', psk=SECRET)
        self.assertEqual(candidates(), (0, [(b'AP-0', SECRET)]),
                         'an unsaved staged pair is still something to try')
        request(b'WIFI_PROFILE_SAVE')
        # The staged slot holds one network, so AP-1 arrives as the staged candidate and AP-0
        # stays the saved one: saved networks come first, then the staged pair.
        self.stage(request, ssid=b'AP-1', psk=OTHER_SECRET)
        self.assertEqual(candidates(), (0, [(b'AP-0', SECRET), (b'AP-1', OTHER_SECRET)]))
        # Re-staging a network that is already saved neither duplicates it nor quietly rewrites
        # what is stored: only WIFI_PROFILE_SAVE changes a saved network.
        self.stage(request, ssid=b'AP-0', psk=OTHER_SECRET)
        self.assertEqual(candidates(), (0, [(b'AP-0', SECRET)]),
                         'the staged pair must not duplicate a saved network')
        self.assertEqual(request(b'WIFI_PROFILE_SAVE'), b'OK WIFI PROFILE SAVED n=1',
                         'saving a re-staged network updates it in place rather than adding one')
        self.assertEqual(candidates(), (0, [(b'AP-0', OTHER_SECRET)]),
                         'once saved, the new passphrase is the one a join would use')

    def test_candidates_skip_an_entry_that_could_not_be_joined(self):
        lib = self.library()
        state, request, candidates = self.harness(lib)
        self.stage(request, ssid=b'AP-0')
        request(b'WIFI_PROFILE_SAVE')
        state['profiles'].append([b'AP-Broken', b''])  # a half-written entry, however it got there
        code, found = candidates()
        self.assertEqual(code, 0)
        self.assertEqual([ssid for ssid, _psk in found], [b'AP-0'],
                         'an unjoinable entry is not offered as a candidate')


if __name__ == '__main__':
    unittest.main()
