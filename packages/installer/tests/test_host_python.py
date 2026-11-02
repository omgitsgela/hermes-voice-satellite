import tempfile
import unittest
from pathlib import Path
from hermes_installer.environment import host_python


class HostPythonTests(unittest.TestCase):
    def test_hermes_runtime_is_preferred_and_never_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            runtime = home / '.hermes/hermes-agent/venv/bin/python'
            runtime.parent.mkdir(parents=True)
            runtime.write_text('#!/bin/sh\nexit 0\n')
            runtime.chmod(0o700)
            self.assertEqual(host_python(home=home, env={}), str(runtime))
            self.assertEqual(host_python(home=home, env={'HERMES_HOST_PYTHON':'/opt/hermes/python'}), '/opt/hermes/python')
            self.assertEqual(host_python(home=home, env={'HERMES_PYTHON':'/opt/other/python'}), '/opt/other/python')


if __name__ == '__main__':
    unittest.main()
