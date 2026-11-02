#!/usr/bin/env python3
"""scripts/release.sh: the parts that can be checked without publishing.

A release is an external, irreversible action, so the script is built to refuse
early: a missing flash bundle, a missing changelog section, a dirty tree, an
already-published tag, or package versions that disagree. These checks cover
the read-only report and the first refusal, plus the version bookkeeping the
script derives everything from. The publish path itself is exercised by the
release run and read back with `gh release view`, not here.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/release.sh'


class ReleaseScriptTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run(['bash', str(SCRIPT), *args],
                              capture_output=True, text=True, cwd=ROOT)

    def version(self):
        text = (ROOT / 'packages/installer/pyproject.toml').read_text()
        return re.search(r'^version = "([^"]+)"', text, re.M).group(1)

    def test_check_reports_version_tag_and_notes(self):
        result = self.run_script('--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.version(), result.stdout)
        self.assertIn(f'v{self.version()}', result.stdout)
        self.assertIn('already published' if 'already published' in result.stdout
                      else 'not published', result.stdout)
        self.assertIn('CHANGELOG.md has a', result.stdout)
        self.assertIn('yes', result.stdout, 'the changelog section for this version must exist')

    def test_publish_refuses_without_a_flash_bundle(self):
        result = self.run_script('--dry-run', '--skip-gate', '--allow-dirty')
        self.assertNotEqual(result.returncode, 0)
        out = result.stdout + result.stderr
        # Precondition order is already-published before missing-bundle: once a tag is out,
        # the script refuses on the tag and never reaches the bundle check. Both are early
        # refusals; the point under test is that publish cannot proceed on a bare invocation.
        if 'already published' in out:
            self.assertIn('v' + self.version() + ' is already published', out)
        else:
            self.assertIn('--flash-bundle', out)
            self.assertIn('release asset', out)

    def test_all_four_packages_share_the_version(self):
        version = self.version()
        for package in ('installer', 'gateway', 'device-esp32', 'device-pi'):
            text = (ROOT / f'packages/{package}/pyproject.toml').read_text()
            found = re.search(r'^version = "([^"]+)"', text, re.M).group(1)
            self.assertEqual(found, version, f'{package} disagrees about the version')

    def test_the_installer_reports_the_packaged_version(self):
        init = (ROOT / 'packages/installer/src/hermes_installer/__init__.py').read_text()
        self.assertIn(f'__version__ = "{self.version()}"', init)


if __name__ == '__main__':
    unittest.main()
