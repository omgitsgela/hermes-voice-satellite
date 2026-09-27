#!/usr/bin/env python3
"""Legacy chime installer must refuse missing audio before touching a unit."""
import os
from pathlib import Path
import runpy
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parent / 'enable_chimes_on_satellite.py'
# Compile before mocking reads so the real script, not a duplicate helper, is tested.
code = compile(SCRIPT.read_text(), str(SCRIPT), 'exec')
with patch.dict(os.environ, {'SAT_SOUNDS_DIR': '/nonexistent-sounds'}):
    with patch.object(Path, 'is_file', return_value=False), patch.object(Path, 'read_text', side_effect=AssertionError('touched unit before checking WAVs')):
        try:
            exec(code, {'__name__': '__main__'})
        except SystemExit as exc:
            assert 'missing sound files' in str(exc)
        else:
            raise AssertionError('missing WAVs accepted')
print('chime installer fails closed before any unit read/write: passed')
