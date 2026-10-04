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
# Cases 0-13 drive report_tail_last_line; 14-24 drive report_tail_last_complete_line
# (the gateway report's lines are line-terminated, so the last line's TEXT is what a
# status query must return -- not the empty string after its terminator).
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
    # --- report_tail_last_complete_line ---
    ('first\nsecond\n', 16, 'second'), # line-terminated: the last line's TEXT
    ('first\nsecond\n', 6, 'secon'),   # clipped to 5 + NUL
    ('second', 16, ''),                # no newline: no complete line
    ('first\npartial', 16, 'first'),   # a trailing partial line is ignored
    ('a\nb\nc', 16, 'b'),              # the newest complete line wins
    ('a\nb\nc', 3, 'b'),               # same, clipped to 1 + NUL
    ('', 16, ''),                      # empty report
    ('\n', 16, ''),                    # one empty complete line
    ('x\ny\nz\n', 16, 'z'),            # the newest complete line, not the empty text after \n
    ('x\ny\nz\n', 2, 'z'),             # same, clipped to 1 + NUL
    ('abc', 0, None),                  # zero capacity: untouched
]

# The C driver's report_tail_append sequence cases, in the same order (their capacities):
#   0: a,b into 8            -> a\nb\n
#   1: a,b,c into 8          -> a\nb\nc\n (exactly full, no eviction needed)
#   2: abcd,efgh into 8      -> efgh\n (abcd evicted, efgh fits)
#   3: a,b,c,d into 8        -> b\nc\nd\n (evicts keep rolling as long as lines keep coming)
#   4: abcdef into 6         -> abcd\n (a line longer than the buffer is truncated)
#   5: abcdef,gh into 6      -> gh\n (truncate, then roll for the next)
#   6: ab into 4             -> ab\n (fits exactly)
#   7: a,b into 4            -> b\n (evict a, keep b)
#   8: "" into 8             -> \n (a bare newline is a line)
#   9: a into 2              -> "" (not even a bare newline fits: the buffer stays empty)
APPEND_CAPS = [8, 8, 8, 8, 6, 6, 4, 4, 8, 2]


def expected_tokens():
    tokens = [f'case{i} cap{cap}' for i, (_inp, cap, _exp) in enumerate(CASES)]
    # The C driver then runs the stateful report_tail_append cases (app0..app9): scripted
    # sequences into a small buffer, exact-bytes oracle -- the rolling contract that keeps the
    # gateway report's last line live for the life of the session.
    tokens += [f'app{i} cap{cap}' for i, cap in enumerate(APPEND_CAPS)]
    return tokens


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
            self.assertEqual(len(passed), len(CASES) + len(APPEND_CAPS),
                             'expected ' + str(len(CASES) + len(APPEND_CAPS)) + ' PASS lines:\n'
                             + ran.stdout)
            for token in expected_tokens():
                self.assertIn('PASS ' + token, ran.stdout, 'missing case ' + token)
            self.assertNotIn('FAIL', ran.stdout, 'a case failed:\n' + ran.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
