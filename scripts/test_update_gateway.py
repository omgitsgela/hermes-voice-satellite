#!/usr/bin/env python3
"""scripts/update-gateway.sh: the upgrade path for an existing checkout.

The updater is a deployment surface, so the checks are about what it refuses
and what it leaves alone, not just the happy path:

  * --check is read-only and reports the revision and how far behind it is;
  * --dry-run writes nothing and does not move HEAD;
  * a real run fast-forwards a clean tree and never touches the configuration
    files (satellite-gateway.env, state/) that the installer owns;
  * a dirty tree is refused unless --allow-dirty;
  * --no-restart leaves the service alone.

A stub `systemctl` on PATH stands in for the user unit: the script's job is to
call it in the right order and to report honestly, and this checks that without
a real service manager.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/update-gateway.sh'


def git(cwd, *args):
    return subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.com',
                           *args], cwd=cwd, capture_output=True, text=True)


class UpdateGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='update-gateway-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = self.tmp / 'origin'
        self.origin.mkdir()
        self.assertTrue(git(self.origin, 'init', '-q', '-b', 'main').returncode == 0)
        (self.origin / 'requirements.txt').write_text('')
        (self.origin / 'hermes' / 'systemd').mkdir(parents=True)
        (self.origin / 'hermes' / 'systemd' / 'unit.in').write_text('unit v1\n')
        (self.origin / 'satellite-gateway.env').write_text('MINE=1\n')
        git(self.origin, 'add', '.')
        git(self.origin, 'commit', '-qm', 'first')
        self.clone = self.tmp / 'checkout'
        subprocess.run(['git', 'clone', '-q', str(self.origin), str(self.clone)], check=True)

        # A stub systemctl: records calls, reports the unit active.
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        self.calls = self.tmp / 'systemctl.calls'
        (self.bin / 'systemctl').write_text(
            '#!/bin/sh\n'
            f'echo "$@" >> {self.calls}\n'
            'case "$*" in *is-active*) exit 0 ;; esac\n'
            'exit 0\n')
        os.chmod(self.bin / 'systemctl', 0o755)
        # journalctl too: the failure path calls it.
        (self.bin / 'journalctl').write_text('#!/bin/sh\nexit 0\n')
        os.chmod(self.bin / 'journalctl', 0o755)

    def env(self):
        return {**os.environ, 'PATH': f"{self.bin}:{os.environ['PATH']}"}

    def run_script(self, *args):
        return subprocess.run(['bash', str(SCRIPT), '--base', str(self.clone), *args],
                              capture_output=True, text=True, env=self.env())

    def advance_origin(self):
        (self.origin / 'requirements.txt').write_text('# changed\n')
        (self.origin / 'new-file.txt').write_text('new\n')
        (self.origin / 'hermes' / 'systemd' / 'unit.in').write_text('unit v2\n')
        git(self.origin, 'add', '.')
        git(self.origin, 'commit', '-qm', 'second')

    def head(self):
        return git(self.clone, 'rev-parse', 'HEAD').stdout.strip()

    def test_check_is_read_only_and_reports_the_gap(self):
        self.advance_origin()
        before = self.head()
        result = self.run_script('--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('behind', result.stdout)
        self.assertEqual(self.head(), before, '--check must not move HEAD')
        self.assertNotIn('new-file.txt', os.listdir(self.clone))
        self.assertIn('hermes-voice-satellite-gateway.service', result.stdout)

    def test_dry_run_changes_nothing(self):
        self.advance_origin()
        before = self.head()
        result = self.run_script('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('[dry-run]', result.stdout)
        self.assertEqual(self.head(), before, '--dry-run must not move HEAD')
        self.assertFalse((self.clone / 'new-file.txt').exists())

    def test_updates_a_clean_tree_and_leaves_configuration_alone(self):
        self.advance_origin()
        result = self.run_script('--no-restart')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.clone / 'new-file.txt').exists(), 'the update did not apply')
        self.assertEqual((self.clone / 'satellite-gateway.env').read_text(), 'MINE=1\n',
                         'the updater must not rewrite the host configuration')
        self.assertIn('unit template changed', result.stdout,
                      'a changed unit template must be reported, not silently ignored')

    def test_restarts_the_unit_when_asked(self):
        self.advance_origin()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls.read_text()
        self.assertIn('--user daemon-reload', calls)
        self.assertIn('--user restart hermes-voice-satellite-gateway.service', calls)
        self.assertIn('service active', result.stdout)

    def test_refuses_a_dirty_tree(self):
        self.advance_origin()
        (self.clone / 'local-edit.txt').write_text('mine\n')
        before = self.head()
        result = self.run_script()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('uncommitted changes', result.stdout + result.stderr)
        self.assertEqual(self.head(), before)

    def test_refuses_a_directory_that_is_not_a_checkout(self):
        plain = self.tmp / 'plain'
        plain.mkdir()
        result = subprocess.run(['bash', str(SCRIPT), '--base', str(plain)],
                                capture_output=True, text=True, env=self.env())
        self.assertEqual(result.returncode, 2)
        self.assertIn('not a git checkout', result.stderr)

    def test_no_restart_flag_leaves_the_service_alone(self):
        self.advance_origin()
        result = self.run_script('--no-restart')
        self.assertEqual(result.returncode, 0)
        self.assertIn('restart skipped', result.stdout)
        self.assertFalse(self.calls.exists(), 'systemctl was called despite --no-restart')


if __name__ == '__main__':
    unittest.main()
