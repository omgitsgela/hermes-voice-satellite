#!/usr/bin/env python3
"""Offline bridge.env contract: no real config, network or service changes."""
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bridge import config

source = (ROOT / 'bridge/config.py').read_text()

def load(text, env=None):
    namespace = {'__file__': str(ROOT / 'bridge/config.py')}
    with patch.dict(os.environ, env or {}, clear=True), patch.object(Path, 'read_text', return_value=text):
        exec(compile(source, 'bridge/config.py', 'exec'), namespace)
        return namespace

cfg = load('VOICE_REGISTRY_ENABLED=1\nVOICE_REGISTRY_HOST=10.1.2.3\nVOICE_REGISTRY_PORT=12345\nVOICE_ROUTE_ALIAS=\nVOICE_WAKE_WAIT_SECONDS=42\n')
assert cfg['REGISTRY_ENABLED'] and cfg['REGISTRY_HOST'] == '10.1.2.3'
assert cfg['REGISTRY_PORT'] == 12345 and cfg['WAKE_WAIT_SECONDS'] == 42
assert cfg['VOICE_ROUTE_ALIAS'] == '', 'an empty route disables pinning'
cfg = load('VOICE_ROUTE_ALIAS=file\n', {'VOICE_ROUTE_ALIAS': ''})
assert cfg['VOICE_ROUTE_ALIAS'] == '', 'even an empty environment override wins'
cfg = load('VOICE_ROUTE_ALIAS=file\n', {'VOICE_ROUTE_ALIAS': 'environment'})
assert cfg['VOICE_ROUTE_ALIAS'] == 'environment'
text = '''# comment
; systemd comment
PLAIN=some\\ value
QUOTED="a\\"b\\\\c\\$d"
SINGLE='literal\\value'
HASH=literal#hash
INNER=some"quotes"
MULTI="first
second"
CONT=one\\
two
PADDED=" padded "
'''
cfg = load(text)
values = cfg['_LOCAL_ENV_CACHE']
assert values['PLAIN'] == 'some value'
assert values['QUOTED'] == 'a"b\\c$d'
assert values['SINGLE'] == 'literal\\value'
assert values['HASH'] == 'literal#hash'
assert values['INNER'] == 'some"quotes"'
assert values['MULTI'] == 'first\nsecond'
assert values['CONT'] == 'onetwo'
assert values['PADDED'] == ' padded '
with patch.object(Path, 'read_text', side_effect=AssertionError('cache reread')):
    assert cfg['_local_env']() is values
with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'read_text', side_effect=FileNotFoundError):
    ns = {'__file__': str(ROOT / 'bridge/config.py')}
    exec(compile(source, 'bridge/config.py', 'exec'), ns)
    assert ns['SATELLITE_HOST'] == '127.0.0.1'
    assert ns['VOICE_SESSION_ID'] == 'voice-satellite'
    assert ns['VOICE_ROUTE_ALIAS'] == 'voice-fast'
    assert ns['PIPER_VOICE_NAME'] == 'en_US-hfc_male-medium'
    assert ns['REGISTRY_HOST'] == '127.0.0.1'
from bridge.announce import Announcer
with patch.dict(os.environ, {}, clear=True), patch.object(config, '_LOCAL_ENV_CACHE', {'VOICE_ANNOUNCE_TOKEN': 'test-token'}):
    with patch.object(Path, 'read_text', side_effect=AssertionError('token file fallback')):
        assert Announcer._load_token(None) == 'test-token'
print('bridge.env precedence, registry, empty values, quoting, cache, token and defaults: passed')
