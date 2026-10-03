#!/usr/bin/env python3
"""Host tests for components/report_tail: the bounded last-line copier.

The gateway task accumulates a bounded, newline-separated report for the life of
the session (item 1). A console STATUS and the soak harness want its LAST line --
"the most recent thing the link did" -- not the whole buffer. The copier is pure
(no RTOS, no device): it bounds, it always NUL-terminates, and it treats "a line
with no newline yet" the same as one that has, so a report cut off mid-line still
yields a complete answer.

The C driver (test_report_tail.c) is the assertion engine: it holds an independent
oracle -- for each (input, capacity) it checks the exact bytes the output buffer
must hold, including the NUL position and that a zero-capacity call leaves the
buffer untouched. This Python test is the harness that proves the right number of
cases actually ran and all passed, so a silently-skipped case cannot read green.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TAIL = ROOT / 'firmware/esp32/components/report_tail'


# (input, capacity, expected_chars) -- expected_chars is what the buffer holds BEFORE
# its NUL, so the NUL sits at index len(expected_chars). capacity==0 means "untouched".
CASES = [
    ('abc', 8, 'abc'),                 # fits with room to spare
    ('abc', 4, 'abc'),                 # exactly chars + NUL
    ('abc', 3, 'ab'),                  # one char short of the NUL
    ('abc', 2, 'a'),
    ('abc', 1, ''),                    # only the NUL fits
    ('abc', 0, None),                  # zero capacity: buffer must be left untouched
    ('first\nsecond', 16, 'second'),   # last line, not the whole report
    ('first\nsecond', 7, 'second'),    # exactly 6 chars + NUL
    ('first\nsecond', 6, 'secon'),     # clipped before the NUL
    ('a\nb', 16, 'b'),
    ('a\n', 16, ''),                   # trailing newline: last line is empty
    ('\n', 16, ''),
    ('', 16, ''),                      # empty report -> empty last line
    ('x' * 30 + '\ntail', 16, 'tail'), # a long line before the one we want
]


def expected_tokens():
    return [f'case{i} cap{cap}' for i, (_inp, cap, _exp) in enumerate(CASES)]


def build(tmp):
    tmp = Path(tmp)
    test = TAIL / 'test_report_tail.c'
    if not test.exists():
        raise AssertionError('missing host test source: ' + str(test))
    program = tmp / 'tail_test'
    result = subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                             '-I' + str(TAIL), str(test), str(TAIL / 'report_tail.c'),
                             '-o', str(program)],
                            capture_output=True, text=True)
    assert result.returncode == 0, 'host compile failed:\n' + result.stderr
    return program


class ReportTailTest(unittest.TestCase):
    def test_last_line_copier(self):
        with tempfile.TemporaryDirectory() as tmp:
            program = build(tmp)
            ran = subprocess.run([program], capture_output=True, text=True)
            self.assertEqual(ran.returncode, 0,
                             'tail test failed:\n' + ran.stdout + '\n' + ran.stderr)
            passed = [line for line in ran.stdout.splitlines() if line.startswith('PASS ')]
            self.assertEqual(len(passed), len(CASES),
                             'expected ' + str(len(CASES)) + ' PASS lines:\n' + ran.stdout)
            for token in expected_tokens():
                self.assertIn('PASS ' + token, ran.stdout, 'missing case ' + token)
            self.assertNotIn('FAIL', ran.stdout, 'a case failed:\n' + ran.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
