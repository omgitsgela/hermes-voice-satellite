#!/usr/bin/env python3
"""Diagnostic placement and standalone supervisor-build regression checks."""
import ast
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DiagnosticTests(unittest.TestCase):
    def test_unexpected_turn_errors_are_logged_in_one_turn(self):
        tree = ast.parse((ROOT / 'gateway/loop.py').read_text())
        function = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.AsyncFunctionDef) and n.name == 'one_turn')
        handlers = [n for n in ast.walk(function) if isinstance(n, ast.ExceptHandler)
                    and isinstance(n.type, ast.Name) and n.type.id == 'Exception']
        self.assertTrue(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and n.func.attr == 'exception'
                            for handler in handlers for n in ast.walk(handler)),
                        'one_turn needs its own unexpected-failure traceback')

    def test_followup_retry_does_not_have_shadowed_handlers(self):
        tree = ast.parse((ROOT / 'gateway/loop.py').read_text())
        function = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.AsyncFunctionDef) and n.name == '_reinforce_wake_now')
        for statement in ast.walk(function):
            if isinstance(statement, ast.Try):
                names = [h.type.id for h in statement.handlers if isinstance(h.type, ast.Name)]
                self.assertEqual(len(names), len(set(names)), 'shadowed exception handler')

    def test_portable_supervisor_still_builds(self):
        run = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/test_esp32_supervisor.py')],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, (run.stdout + run.stderr)[-5000:])


if __name__ == '__main__':
    unittest.main()
