"""Native Windows ESP-IDF invocation, isolated to this workspace."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path('C:/Users/<operator>/esp')
IDF = ROOT / 'esp-idf'
ENV = dict(os.environ)
for name in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
    ENV.pop(name, None)
ENV['PYTHONNOUSERSITE'] = '1'
for name in list(ENV):
    if name.startswith(('MSYSTEM', 'MINGW')):
        ENV.pop(name)
ENV['IDF_PATH'] = str(IDF)
ENV['IDF_TOOLS_PATH'] = str(ROOT / 'tools')
mode = sys.argv[1]
if mode in ('install', 'install-python-env'):
    args = [sys.executable, str(IDF / 'tools/idf_tools.py'), mode]
    if mode == 'install':
        args += ['--targets', 'esp32s3']
    raise SystemExit(subprocess.call(args, env=ENV))
venvs = list((ROOT / 'tools/python_env').glob('idf6.1*_env/Scripts/python.exe'))
if len(venvs) != 1:
    raise SystemExit(f'Expected one IDF venv, found {venvs}')
python = venvs[0]
ENV['IDF_PYTHON_ENV_PATH'] = str(python.parent.parent)
ENV['ESP_IDF_VERSION'] = '6.1'
# Derive tool PATH from the SDK installer, not host/global installations.
import json
result = subprocess.check_output([str(python), str(IDF / 'tools/idf_tools.py'), 'export', '--format', 'key-value'], env=ENV, text=True)
for line in result.splitlines():
    if '=' not in line:
        continue
    key, value = line.split('=', 1)
    value = value.replace('%PATH%', ENV.get('PATH', '')).replace('$PATH', ENV.get('PATH', ''))
    ENV[key] = value
ENV['PATH'] = str(python.parent) + os.pathsep + ENV['PATH']
if mode == 'env':
    print(json.dumps(ENV, indent=2))
else:
    raise SystemExit(subprocess.call([str(python), str(IDF / 'tools/idf.py')] + sys.argv[1:], cwd=ROOT / 'firmware', env=ENV))
