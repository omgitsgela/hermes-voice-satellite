"""Native Windows gateway regressions. No listeners, devices, or real credentials."""
import json
from satellite_gateway.config_plane import ConfigPlane


def test_config_plane_can_persist_without_unix_fchmod(tmp_path, monkeypatch):
    import satellite_gateway.config_plane as module
    monkeypatch.delattr(module.os, 'fchmod', raising=False)
    path = tmp_path / 'fleet.json'
    plane = ConfigPlane(path)
    plane.ensure_device('windows-test')
    assert 'windows-test' in json.loads(path.read_text())['devices']


def test_gateway_env_file_uses_explicit_hermes_home(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    env = dict(os.environ, HERMES_HOME=str(tmp_path / 'isolated-profile'))
    actual = subprocess.check_output(
        [sys.executable, '-c', 'from satellite_gateway.config import GATEWAY_ENV_FILE; print(GATEWAY_ENV_FILE)'],
        env=env, text=True).strip()
    assert Path(actual) == tmp_path / 'isolated-profile' / '.env'


def test_gateway_env_file_uses_native_default_without_override(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    env = dict(os.environ)
    env.pop('HERMES_HOME', None)
    env['LOCALAPPDATA'] = str(tmp_path / 'local-appdata')
    actual = subprocess.check_output(
        [sys.executable, '-c', 'from satellite_gateway.config import GATEWAY_ENV_FILE; print(GATEWAY_ENV_FILE)'],
        env=env, text=True).strip()
    expected = (tmp_path / 'local-appdata' / 'hermes' if sys.platform == 'win32'
                else Path.home() / '.hermes') / '.env'
    assert Path(actual) == expected
