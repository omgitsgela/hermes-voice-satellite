"""Regression: pairing must use its running interpreter on Windows, not absent python3."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packages/gateway/src'))
from hermes_gateway.pairing_server import ScriptEnroller

class PairingInterpreterTest(unittest.TestCase):
    def test_enroller_uses_running_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            tls = state / 'registry-tls'
            tls.mkdir()
            for suffix in ('crt', 'key'):
                (tls / ('test-board.' + suffix)).write_text('test-only')
            with patch('hermes_gateway.pairing_server.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
                ScriptEnroller(ROOT / 'scripts/issue-registry-tls.py', state).enroll('test-board')
            self.assertEqual(run.call_args.args[0][0], sys.executable)

if __name__ == '__main__':
    unittest.main()
