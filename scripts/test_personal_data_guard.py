#!/usr/bin/env python3
"""Exercise the guard in an isolated repository: the tracked scan, the untracked scan, and the
awkward tracked names. The point of the untracked mode is that a secret in a file the tree has
not staged yet must fail the guard, not read as clean, because the tracked scan cannot see it."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent


class Repo:
    """A throwaway git repository with the guard installed and a few committed, legal files."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.tmp.name)
        (self.root / 'scripts').mkdir()
        shutil.copyfile(ROOT / 'scripts/check-no-personal-data.sh',
                        self.root / 'scripts/check-no-personal-data.sh')
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self._commit_base()

    def _commit_base(self):
        names = ['An' + 'gela', 'Mi' + 'chael']
        (self.root / 'LICENSE').write_text('Copyright ' + ' and '.join(names))
        (self.root / 'THIRD-PARTY.md').write_text(names[0])
        (self.root / 'pi').mkdir(parents=True)
        (self.root / 'pi' / 'vendor').mkdir()
        (self.root / 'reference').mkdir()
        (self.root / 'pi' / 'vendor' / 'upstream.txt').write_text(names[0])
        (self.root / 'reference' / 'upstream.txt').write_text(names[1])
        subprocess.run(['git', 'add', '-A'], cwd=self.root, check=True)
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t',
                        'commit', '-qm', 'base'], cwd=self.root, check=True)

    def guard(self, *args):
        return subprocess.run(['sh', 'scripts/check-no-personal-data.sh', *args],
                              cwd=self.root, capture_output=True, text=True)

    def write(self, name, text, tracked=False, commit=False):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if tracked:
            subprocess.run(['git', 'add', '--', name], cwd=self.root, check=True)
            if commit:
                subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t',
                                'commit', '-qm', 'm', '--', name], cwd=self.root, check=True)

    def gitignore(self, pattern):
        gi = self.root / '.gitignore'
        with open(gi, 'a') as handle:
            handle.write(pattern + '\n')

    def close(self):
        self.tmp.cleanup()


NAMES = ['An' + 'gela', 'Mi' + 'chael']
PLANTS = (NAMES[0], '192.168.' + '0.12', '/home/' + 'Example/file', 'wyoming' + '1')


def test_tracked_scope_is_unchanged():
    repo = Repo()
    try:
        # A clean base: legal names, vendor/reference copies, and the guard itself all pass.
        assert repo.guard().returncode == 0, repo.guard().stdout
        for filename in ('leak.txt', 'a space.txt', 'a\nnewline.txt', "a'quote.txt"):
            for value in (*NAMES, NAMES[0].lower(), *PLANTS):
                repo.write(filename, value, tracked=True)
                result = repo.guard()
                assert result.returncode == 1, ('tracked scan must fail', filename, value)
                assert 'tracked file' in result.stdout, result.stdout
                repo.write(filename, 'safe example', tracked=True)
        assert repo.guard().returncode == 0
    finally:
        repo.close()


def test_untracked_scope_catches_a_planted_value():
    repo = Repo()
    try:
        # A secret in a file that has NOT been staged is invisible to the tracked scan...
        repo.write('notes.txt', 'host is ' + '192.168.' + '0.12')
        assert repo.guard().returncode == 0, 'tracked scope must not see the untracked file'
        # ...but the untracked scope must fail, and say it is untracked and where.
        result = repo.guard('--untracked')
        assert result.returncode == 1, result.stdout + result.stderr
        assert 'untracked' in result.stdout, result.stdout
        assert 'notes.txt' in result.stdout, result.stdout
        # Remove the planted value and the scope is clean again.
        (repo.root / 'notes.txt').write_text('host is 192.0.2.9')
        assert repo.guard('--untracked').returncode == 0
    finally:
        repo.close()


def test_untracked_scope_covers_every_value_kind():
    repo = Repo()
    try:
        for i, value in enumerate(PLANTS):
            repo.write(f'leak{i}.txt', value)
            result = repo.guard('--untracked')
            assert result.returncode == 1, (value, result.stdout)
            assert f'leak{i}.txt' in result.stdout, (value, result.stdout)
            (repo.root / f'leak{i}.txt').write_text('safe')
        assert repo.guard('--untracked').returncode == 0
    finally:
        repo.close()


def test_gitignored_files_are_out_of_scope_for_untracked():
    repo = Repo()
    try:
        repo.gitignore('local.env')
        repo.write('local.env', 'addr=192.168.' + '0.12')
        # Ignored, so the guard does not scan it and does not fail on it.
        assert repo.guard('--untracked').returncode == 0, repo.guard('--untracked').stdout
    finally:
        repo.close()


def test_symlink_to_a_directory_is_skipped_not_followed():
    repo = Repo()
    try:
        # The .venv case: an untracked symlink whose target is a directory. git lists the link as
        # one entry; the guard must not try to grep a directory (which errors) and must not descend
        # through the link, even if the target holds a value the tracked scope would not see.
        target = repo.root / 'ignoredtarget'
        target.mkdir()
        (target / 'secret.txt').write_text('192.168.' + '0.12')
        repo.gitignore('ignoredtarget/')
        (repo.root / 'link').symlink_to(target)
        result = repo.guard('--untracked')
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'scan failed' not in result.stderr, result.stderr
    finally:
        repo.close()


def test_legal_files_are_skipped_in_untracked_scope():
    repo = Repo()
    try:
        # An untracked LICENSE carrying a personal name is legal attribution, not a leak.
        repo.write('LICENSE', NAMES[0])
        assert repo.guard('--untracked').returncode == 0, repo.guard('--untracked').stdout
    finally:
        repo.close()


def test_all_mode_covers_both_scopes():
    repo = Repo()
    try:
        repo.write('tracked.txt', '192.168.' + '0.12', tracked=True)
        result = repo.guard('--all')
        assert result.returncode == 1, result.stdout
        assert 'tracked file' in result.stdout, result.stdout
        (repo.root / 'tracked.txt').write_text('safe')
        repo.write('untracked.txt', '/home/' + 'Example/file')
        result = repo.guard('--all')
        assert result.returncode == 1, result.stdout
        assert 'untracked' in result.stdout and 'untracked.txt' in result.stdout, result.stdout
    finally:
        repo.close()


def test_unknown_option_is_an_error():
    repo = Repo()
    try:
        result = repo.guard('--bogus')
        assert result.returncode == 2, (result.returncode, result.stderr)
    finally:
        repo.close()


if __name__ == '__main__':
    test_tracked_scope_is_unchanged()
    test_untracked_scope_catches_a_planted_value()
    test_untracked_scope_covers_every_value_kind()
    test_gitignored_files_are_out_of_scope_for_untracked()
    test_symlink_to_a_directory_is_skipped_not_followed()
    test_legal_files_are_skipped_in_untracked_scope()
    test_all_mode_covers_both_scopes()
    test_unknown_option_is_an_error()
    print('personal-data guard: tracked and untracked scopes, legal/vendor exclusions, '
          'and sabotage cases passed')
