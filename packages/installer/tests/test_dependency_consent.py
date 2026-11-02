import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from hermes_installer.session import Session


class ConsentTests(unittest.TestCase):
    def test_inventory_and_explicit_consent_guard_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'requirements.txt').write_text('piper-tts==1.8.0\nwyoming==1.10.2\n')
            session = Session(base, log_dir=base / "logs")
            try:
                inventory = session.dependency_inventory()
                self.assertEqual({d['class'] for d in inventory['dependencies']}, {1, 2, 3})
                self.assertIn('piper-tts==1.8.0', str(inventory))
                with patch('hermes_installer.session.gateway_module.run_commands', return_value=(True, 'done')) as run:
                    denied = session.install_gateway('gateway.example', dry_run=False)
                    self.assertFalse(denied['ok'])
                    run.assert_not_called()
                    # Record consent against the exact currently displayed inventory, not a generic yes.
                    self.assertTrue(session.consent_dependencies(inventory['id'], True)['ok'])
                    self.assertIn('piper-tts==1.8.0', '\n'.join(session.state.log))
                    log = session.run_log_path.read_text()
                    self.assertIn(inventory['id'], log)
                    self.assertIn('piper-tts==1.8.0', log)
                    self.assertEqual(session.run_log_path.stat().st_mode & 0o777, 0o600)
                    session.install_gateway('gateway.example', dry_run=False)
                    run.assert_called_once()
                    (base / 'requirements.txt').write_text('different-package==1\n')
                    denied = session.install_gateway('gateway.example', dry_run=False)
                    self.assertFalse(denied['ok'])
                    self.assertEqual(run.call_count, 1)
            finally:
                session.close()


if __name__ == '__main__':
    unittest.main()
