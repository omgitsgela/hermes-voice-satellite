"""Offline regression tests. No hub requests are made."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hubitat

class ColorRegression(unittest.TestCase):
    def run_color(self, saturation=100, rgb=255, requested=0):
        detail = {'id': '601', 'label': 'Fairy Lights', 'attributes': [
            {'name': 'saturation', 'currentValue': saturation},
            {'name': 'colorRGBNum', 'currentValue': rgb}]}
        with patch.object(hubitat, 'load_config', return_value={}), \
             patch.object(hubitat, '_resolve', return_value=detail), \
             patch.object(hubitat, 'device_detail', return_value=detail), \
             patch.object(hubitat, 'check_command_allowed'), \
             patch.object(hubitat, '_send') as send:
            result = json.loads(hubitat.handle({'action':'set_color','device':'601',
                                                'hue':requested,'saturation':100}))
            return result, [(c.args[2], c.args[3]) for c in send.call_args_list]

    def test_unchanged_saturation_sends_only_hue(self):
        result, calls = self.run_color()
        self.assertTrue(result['ok'])
        self.assertEqual(calls, [('setHue', ['0'])])

    def test_cached_target_does_not_suppress_explicit_request(self):
        result, calls = self.run_color(rgb=16711680)
        self.assertEqual(calls, [('setHue', ['0'])])

if __name__ == '__main__':
    unittest.main()
