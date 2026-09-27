#!/usr/bin/env python3
"""Exercise the guard in an isolated repository, including awkward tracked names."""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
    repo = Path(tmp)
    (repo / 'scripts').mkdir()
    shutil.copyfile(ROOT / 'scripts/check-no-personal-data.sh', repo / 'scripts/check-no-personal-data.sh')
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    def run():
        return subprocess.run(['sh', 'scripts/check-no-personal-data.sh'], cwd=repo, capture_output=True, text=True)
    def stage(name, text):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
        subprocess.run(['git', 'add', '--', name], cwd=repo, check=True)
    names = ['An' + 'gela', 'Mi' + 'chael']
    for legal in ('LICENSE', 'THIRD-PARTY.md'):
        stage(legal, 'Copyright ' + ' and '.join(names))
    stage('pi/vendor/upstream.txt', names[0])
    stage('reference/upstream.txt', names[1])
    subprocess.run(['git', 'add', 'scripts'], cwd=repo, check=True)
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    for filename in ('leak.txt', 'a space.txt', 'a\nnewline.txt', "a'quote.txt"):
        for value in (*names, names[0].lower(), '192.168.' + '0.12', '/home/' + 'Example/file', 'wyoming' + '1'):
            stage(filename, value)
            assert run().returncode == 1, (filename, value)
            stage(filename, 'safe example')
    assert run().returncode == 0
print('personal-data guard: clean tree, legal/vendor exclusions and sabotage cases passed')
