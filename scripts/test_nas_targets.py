#!/usr/bin/env python3
"""NAS target selection is explicit, consistent, and never guesses another share."""
import importlib.util
import os
from pathlib import Path
import sys
from unittest.mock import patch

HERE = Path(__file__).resolve().parent

def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, 'argv', [name]), patch.dict(os.environ, {}, clear=True):
        spec.loader.exec_module(module)
    return module

mirror = load('mirror-to-nas')
prune = load('prune-nas-mirror')
for mod in (mirror, prune):
    with patch.dict(os.environ, {'VOICE_NAS_MOUNT': '/mnt/from-env'}, clear=True):
        with patch.object(sys, 'argv', ['test', '/mnt/explicit']):
            assert mod.mirror_root() == Path('/mnt/explicit/hermes-voice-satellite')
        with patch.object(sys, 'argv', ['test']):
            assert mod.mirror_root() == Path('/mnt/from-env/hermes-voice-satellite')
    with patch.dict(os.environ, {'VOICE_NAS_MIRROR_ROOT': '/mnt/legacy'}, clear=True):
        with patch.object(sys, 'argv', ['test', '/mnt/explicit']):
            assert mod.mirror_root() == Path('/mnt/explicit/hermes-voice-satellite')
    with patch.dict(os.environ, {}, clear=True), patch.object(sys, 'argv', ['test']):
        assert mod.mirror_root() is None
        assert mod.main() != 0, 'missing target must fail before writes'
with patch.dict(os.environ, {}, clear=True), patch.object(sys, 'argv', ['test', '/mnt/explicit', '--apply']):
    assert prune.mirror_root() == Path('/mnt/explicit/hermes-voice-satellite')
print('NAS mirror/prune explicit target, environment, precedence and missing target: passed')
