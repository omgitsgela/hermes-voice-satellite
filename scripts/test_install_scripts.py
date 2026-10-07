#!/usr/bin/env python3
"""Offline checks for the install scripts (scripts/install-esp32.sh, scripts/install-pi.sh).

Run with the project's interpreter (no pytest here):

    ./.venv/bin/python -B scripts/test_install_scripts.py

An install script is the one thing a user runs on a bare device, so its DRY-RUN path is what
gets pinned here: the exact commands it WOULD run, that a board write is never implied, that the
credentials move as file paths rather than argv, and that the required-argument guards actually
fire. Nothing touches a real board or a real /etc -- every run is --dry-run or a bad-argument
invocation, so the suite is offline and repeatable.

What is pinned:

  1  --help exits 0 and prints usage
  2  --dry-run --build prints the build + bundle commands and touches nothing
  3  --dry-run --flash prints the board write (0x0, the bundle) and nothing more
  4  --flash without --port fails with a clear message (a board write is never implied)
  5  --dry-run --provision passes credential/model material as FILE PATHS, never inline
  6  the Pi --dry-run renders the config (VS_BASE / device name / wake word) and names the
     coupled-service group
  7  the Pi script refuses to proceed without --base / --device-name
"""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ESP32 = ROOT / 'scripts' / 'install-esp32.sh'
PI = ROOT / 'scripts' / 'install-pi.sh'


def run(script, *args):
    return subprocess.run(['bash', str(script), *args], capture_output=True, text=True)


class InstallScriptTests(unittest.TestCase):
    def test_scripts_exist_and_are_bash(self):
        for script in (ESP32, PI):
            self.assertTrue(script.exists(), f'missing {script}')
            # A shebang that names bash: these use ${BASH_SOURCE} and pipefail.
            self.assertEqual(script.read_text().splitlines()[0], '#!/usr/bin/env bash')

    def test_help_exits_cleanly(self):
        for script in (ESP32, PI):
            with self.subTest(script=script.name):
                result = run(script, '--help')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Usage:', result.stdout)

    def test_esp32_dry_run_build_prints_the_commands_and_touches_nothing(self):
        result = run(ESP32, '--dry-run', '--build')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('DRY-RUN', result.stdout)
        self.assertIn('idf.py build', result.stdout)
        self.assertIn('bundle-esp32-flash.py', result.stdout)
        self.assertIn('flash.manifest.txt', result.stdout)
        # Nothing is written to a board without --flash.
        self.assertNotIn('write_flash', result.stdout)

    def test_esp32_dry_run_flash_names_the_board_write(self):
        result = run(ESP32, '--dry-run', '--flash', '--port', '/dev/ttyACM0')
        self.assertEqual(result.returncode, 0, result.stderr)
        # The bundle is written straight to 0x0: bootloader + partition table + app.
        self.assertIn('write_flash', result.stdout)
        self.assertIn('0x0', result.stdout)
        self.assertIn('/dev/ttyACM0', result.stdout)
        self.assertIn('flash.bundle.bin', result.stdout)

    def test_esp32_flash_without_a_port_is_refused(self):
        """A board write must be explicit: --flash with no --port fails, it never guesses a port."""
        result = run(ESP32, '--flash')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--port', result.stderr)

    def test_esp32_provision_moves_credentials_as_paths_not_argv(self):
        result = run(ESP32, '--dry-run', '--provision', '--port', '/dev/ttyACM0',
                     '--tls-manifest', '/tmp/manifest.json',
                     '--model', '/tmp/hey_hermes.tflite', '--model-name', 'hey_hermes')
        self.assertEqual(result.returncode, 0, result.stderr)
        # The provisioning helpers take the FILE PATHS; no key material is inline in argv.
        self.assertIn('provision-device-tls.py', result.stdout)
        self.assertIn('/tmp/manifest.json', result.stdout)
        self.assertIn('provision-device-model.py', result.stdout)
        self.assertIn('/tmp/hey_hermes.tflite', result.stdout)

    def test_pi_dry_run_renders_config_and_names_the_coupled_group(self):
        result = run(PI, '--dry-run', '--base', '/opt/wyoming-satellite',
                     '--device-name', 'kitchen', '--wake-word', 'hey_hermes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('DRY-RUN', result.stdout)
        self.assertIn('/opt/wyoming-satellite', result.stdout)
        self.assertIn('kitchen', result.stdout)
        self.assertIn('hey_hermes', result.stdout)
        # The four audio units are a coupled group; the script must say so (they Require= each
        # other in a cycle, so restarting one alone leaves it dead).
        self.assertIn('coupled', result.stdout.lower())

    def test_pi_requires_base_and_device_name(self):
        missing_base = run(PI, '--dry-run', '--device-name', 'kitchen')
        self.assertNotEqual(missing_base.returncode, 0)
        self.assertIn('--base', missing_base.stderr)
        missing_name = run(PI, '--dry-run', '--base', '/opt/wyoming-satellite')
        self.assertNotEqual(missing_name.returncode, 0)
        self.assertIn('--device-name', missing_name.stderr)


if __name__ == '__main__':
    unittest.main()