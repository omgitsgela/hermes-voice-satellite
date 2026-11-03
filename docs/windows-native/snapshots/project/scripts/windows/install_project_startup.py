"""Preview by default; --install writes only this project's per-user Startup VBS."""
import argparse
import os
from pathlib import Path
import subprocess

MARKER = "' Hermes voice satellite project startup - managed locally"
NAME = 'HermesVoiceSatelliteProject.vbs'


def install(root, startup, *, apply=False):
    root, startup = Path(root).resolve(), Path(startup)
    pythonw = root / '.tools-venv/Scripts/pythonw.exe'
    entry = root / 'scripts/windows/supervise.pyw'
    command = subprocess.list2cmdline([str(pythonw), str(entry)])
    quote = lambda text: '"' + str(text).replace('"', '""') + '"'
    text = (MARKER + '\nOption Explicit\nDim shell\n'
            'Set shell = CreateObject("WScript.Shell")\n'
            'shell.CurrentDirectory = ' + quote(root) + '\n'
            'shell.Run ' + quote(command) + ', 0, False\n')
    target = startup / NAME
    if apply:
        for path in [pythonw, entry, root / '.tools-venv/Scripts/python.exe',
                     root / '.venv/Scripts/python.exe']:
            if not path.is_file():
                raise FileNotFoundError(path)
        if target.exists():
            try:
                managed = target.read_text(encoding='utf-16').startswith(MARKER + '\n')
            except UnicodeError:
                managed = False
            if not managed:
                raise FileExistsError('Refusing to replace non-project startup file: ' + str(target))
        startup.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-16')
    return target, text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', help='Create project VBS; does not launch it')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    startup = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Startup'
    target, text = install(root, startup, apply=args.install)
    print(('Installed: ' if args.install else 'PREVIEW ONLY: ') + str(target))
    print(text)


if __name__ == '__main__':
    main()
