#!/usr/bin/env python3
"""The pure core of the soak and telemetry harness.

A soak of a satellite polls one read-only console command -- GATEWAY_STATUS -- for a
fixed window and answers a single question: did the registered link stay alive for the
whole window? The device-side half (the GATEWAY_STATUS command and the supervisor's
accumulated report) is proved by the firmware build and the supervisor's loopback tests;
this module is the decision. It is deliberately pure -- no serial, no timers, no wall
clock -- so the verdict can be driven by scripted samples on a host and unit-tested
without a board. The runner (run-soak.py) owns the clock and the serial port, and hands
its samples to soak_verdict.

The honest definition of healthy, from the supervisor's contract (item 1):
  * the persistent task is active the whole window -- every sample is RUNNING. An IDLE
    sample means the task never ran or has terminated (a refused registration is the
    terminal case, and stopping is the correct thing), and a LOST sample means the console
    stopped answering; either one is a failure.
  * the link actually registered at least once. The persistent task can be active the whole
    window and never get the bridge to accept a registration; that is a failure, reported
    as never_registered rather than read as healthy.
What is NOT a failure, on purpose: a sample whose last line is a backoff or a failed
establish attempt. A dropped link that the supervisor reconnects is exactly the property
item 1 exists to prove, so the soak must treat it as the expected behaviour it is. The
only way to distinguish "reconnected" from "still down" is to watch the reconnect line
arrive, and that is a sample the soak already collects.
"""
import re

#: The one place a sample's kind and its last line are separated from the rest of the line.
#: Everything after "last=" is the supervisor's report line, which may itself contain
#: spaces (for example "reconnect=registered reconnects=2"), so the split is on the first
#: "last=" and the remainder is taken whole.
_RUNNING = re.compile(r'^OK GATEWAY RUNNING last=(.*)$')


def parse_status_line(line):
    """Split one GATEWAY_STATUS reply into (kind, last).

    kind is one of:
      'running'    the task is active; last is its report's last line (possibly empty)
      'idle'       the task is not running
      'lost'       the console did not answer within its deadline
      'unexpected' the line is none of the above (protocol drift, or a different command's
                   reply). It is reported as-is, never guessed at as healthy or as a drop.
    """
    if line is None:
        return ('lost', '')
    if line == 'OK GATEWAY IDLE':
        return ('idle', '')
    match = _RUNNING.match(line)
    if match:
        return ('running', match.group(1))
    return ('unexpected', line)


def _registered_in(last):
    """True when a running sample's last line says the link is (or came back) registered.

    The supervisor appends "session=registered" on the first registration and
    "reconnect=registered reconnects=N" on each reconnect; both are the only lines that
    count. A backoff line, a failed establish, or an empty report are all "not yet".
    """
    return last.startswith('session=registered') or last.startswith('reconnect=registered')


def _max_reconnects(last):
    """The reconnect count carried by a line, or 0 when the line does not carry one."""
    marker = 'reconnects='
    index = last.find(marker)
    if index < 0:
        return 0
    tail = last[index + len(marker):]
    number = 0
    for char in tail:
        if not char.isdigit():
            break
        number = number * 10 + int(char)
    return number


def soak_verdict(samples):
    """Judge a soak. `samples` is a list of (elapsed_seconds, kind, last) in time order.

    Returns a dict with:
      ok             bool  -- the window is healthy
      reason         str   -- 'ok', or the first problem found (see below)
      samples        int   -- how many were collected
      running        int   -- how many were RUNNING
      idle           int   -- how many were IDLE
      lost           int   -- how many were LOST
      unexpected     int   -- how many were UNEXPECTED
      registered     int   -- how many RUNNING samples said the link was registered
      backoffs       int   -- how many RUNNING samples were in backoff
      max_reconnects int   -- the highest reconnect count seen
      first_problem  (elapsed, kind, last) of the first failure-kind sample, or None
      window_seconds float -- the span of the samples, or 0.0 when there are none

    The reason, most specific first:
      'no_samples'                  the window produced nothing
      '<kind>_at_sample_<index>'    the first idle/lost/unexpected sample, by 0-based index
      'never_registered'            the task ran the whole window but never registered
    A problem always outranks never_registered: a dead or silent task is the more direct
    explanation, and reporting the registration gap on top of it would be misleading.
    """
    if not samples:
        return {'ok': False, 'reason': 'no_samples', 'samples': 0, 'running': 0,
                'idle': 0, 'lost': 0, 'unexpected': 0, 'registered': 0, 'backoffs': 0,
                'max_reconnects': 0, 'first_problem': None, 'window_seconds': 0.0}

    running = idle = lost = unexpected = registered = backoffs = 0
    max_reconnects = 0
    first_problem = None
    for index, (_elapsed, kind, last) in enumerate(samples):
        if kind == 'running':
            running += 1
            if _registered_in(last):
                registered += 1
            elif last.startswith('backoff='):
                backoffs += 1
            max_reconnects = max(max_reconnects, _max_reconnects(last))
        elif kind == 'idle':
            idle += 1
        elif kind == 'lost':
            lost += 1
        else:  # 'unexpected' and anything the device ever invents
            unexpected += 1
        if first_problem is None and kind in ('idle', 'lost', 'unexpected'):
            first_problem = (index, samples[index][0], kind, last)

    if first_problem is not None:
        index, elapsed, kind, last = first_problem
        return {'ok': False, 'reason': f'{kind}_at_sample_{index}',
                'samples': len(samples), 'running': running, 'idle': idle, 'lost': lost,
                'unexpected': unexpected, 'registered': registered, 'backoffs': backoffs,
                'max_reconnects': max_reconnects,
                'first_problem': (elapsed, kind, last),
                'window_seconds': samples[-1][0] - samples[0][0]}
    if registered == 0:
        return {'ok': False, 'reason': 'never_registered', 'samples': len(samples),
                'running': running, 'idle': idle, 'lost': lost, 'unexpected': unexpected,
                'registered': 0, 'backoffs': backoffs, 'max_reconnects': max_reconnects,
                'first_problem': None, 'window_seconds': samples[-1][0] - samples[0][0]}
    return {'ok': True, 'reason': 'ok', 'samples': len(samples), 'running': running,
            'idle': idle, 'lost': lost, 'unexpected': unexpected, 'registered': registered,
            'backoffs': backoffs, 'max_reconnects': max_reconnects, 'first_problem': None,
            'window_seconds': samples[-1][0] - samples[0][0]}


def render_report(verdict):
    """One self-describing line for the verdict, in the console's own idiom: field=value
    pairs, no free text, so a log reader can split it on whitespace. The first line of a
    soak's output is always SOAK OK or SOAK FAIL, followed by the same fields either way."""
    fields = ('samples={} running={} idle={} lost={} unexpected={} registered={}'
              ' backoffs={} max_reconnects={} window={:.1f}s'
              ).format(verdict['samples'], verdict['running'], verdict['idle'],
                       verdict['lost'], verdict['unexpected'], verdict['registered'],
                       verdict['backoffs'], verdict['max_reconnects'],
                       verdict['window_seconds'])
    if verdict['ok']:
        return 'SOAK OK ' + fields + ' reason=ok'
    if verdict['first_problem'] is not None:
        return 'SOAK FAIL ' + fields + ' reason={}'.format(verdict['reason'])
    return 'SOAK FAIL ' + fields + ' reason={}'.format(verdict['reason'])
