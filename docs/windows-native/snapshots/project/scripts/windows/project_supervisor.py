"""Local Windows supervisor for satellite, pairing and Hermes API; stdlib only."""
from dataclasses import dataclass
from pathlib import Path
import json
import re
import os
import subprocess
import msvcrt
import logging
from logging.handlers import RotatingFileHandler
import time
from windows_job import WindowsJob


def main(root=None, *, sleep=time.sleep):
    root = Path(root or Path(__file__).resolve().parents[2])
    lock = acquire_lock(root / 'state/windows-supervisor.lock')
    if lock is None:
        return 0
    runners = []
    job = None
    handler = None
    log = logging.getLogger('project-windows-supervisor')
    try:
        directory = root / 'logs/windows'
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(directory / 'supervisor.log',
                                      maxBytes=2_000_000, backupCount=2, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
        log.info('Supervisor starting pid=%s root=%s', os.getpid(), root)
        job = WindowsJob()
        runners = [Runner(spec, lambda spec: launch_child(root, spec, job), inspect_windows, log)
                   for spec in service_specs(root)]
        stop = root / 'state/windows-supervisor.stop'
        while not stop.exists():
            for runner in runners:
                runner.tick(time.monotonic())
            sleep(1)
        log.info('Stop sentinel present; exiting (only owned children will stop)')
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        log.exception('Supervisor failed')
        return 1
    finally:
        # Closing this private job kills whole owned trees, including venv
        # redirectors' base interpreters. Foreign processes were never assigned.
        if job is not None:
            job.close()
        if handler is not None:
            log.removeHandler(handler)
            handler.close()
        lock.close()


def acquire_lock(path):
    """Lifetime OS byte lock; auto-released even if pythonw crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def inspect_windows():
    """Read-only inventory. No probes that could speak or affect a satellite."""
    query = ("$ErrorActionPreference='Stop'; "
             "ConvertTo-Json -Compress -InputObject @(Get-CimInstance Win32_Process | "
             "Where-Object {$_.Name -match '^python(w)?(\\.exe)?$'} | "
             "Select-Object CommandLine)")
    options = dict(capture_output=True, text=True, errors='replace', check=True,
                   timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        processes = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile',
                                    '-NonInteractive', '-Command', query], **options)
        listeners = subprocess.run(['netstat.exe', '-ano', '-p', 'tcp'], **options)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError('Windows process/listener inspection failed') from exc
    return parse_inventory(processes.stdout, listeners.stdout)


def launch_child(root, spec, job):
    directory = root / 'logs' / 'windows'
    directory.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for name in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
        env.pop(name, None)
    env['PYTHONNOUSERSITE'] = '1'
    env.setdefault('HERMES_HOME', str(Path.home() / 'AppData/Local/hermes'))
    env['PYTHONUNBUFFERED'] = '1'
    with (directory / (spec.module + '.log')).open('ab', buffering=0) as output:
        return job.spawn(spec.command, cwd=str(spec.cwd or root), env=env,
                                stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.STDOUT, close_fds=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)


@dataclass(frozen=True)
class Inventory:
    commands: tuple[str, ...]
    ports: frozenset[int]

    def occupied(self, spec):
        pattern = r'(?:^|\s)-m\s+' + re.escape(spec.module)
        if spec.module == 'hermes_cli.main':
            pattern += r'\s+gateway\s+run'
        pattern += r'(?=\s|$)'
        return bool(set(spec.ports) & self.ports) or any(
            re.search(pattern, command) for command in self.commands)


def parse_inventory(process_json, netstat):
    rows = json.loads(process_json)
    if not isinstance(rows, list):
        raise ValueError('Expected process array')
    commands = []
    for row in rows:
        command = row.get('CommandLine')
        if not isinstance(command, str) or not command.strip():
            raise ValueError('Cannot inspect a Python command line; refusing duplicate risk')
        commands.append(command)
    ports = set()
    for line in netstat.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[0] == 'TCP' and fields[3] == 'LISTENING':
            ports.add(int(fields[1].rsplit(':', 1)[1]))
    return Inventory(tuple(commands), frozenset(ports))


class Runner:
    """Own only children we launch; foreign listeners/processes are never touched."""

    def __init__(self, spec, launch, inspect, log):
        self.spec, self.launch, self.inspect, self.log = spec, launch, inspect, log
        self.child = None
        self.next_attempt = 0
        self.failures = 0
        self.started_at = 0

    def failed(self, now, reason):
        self.failures = min(self.failures + 1, 6)
        delay = min(10 * 2 ** (self.failures - 1), 300)
        self.next_attempt = now + delay
        self.log.warning('%s: %s; retry in %ss', self.spec.module, reason, delay)

    def tick(self, now):
        if self.child is not None:
            code = self.child.poll()
            if code is None:
                return
            self.child = None
            if now - self.started_at >= 300:
                self.failures = 0
            self.failed(now, f'exited {code}')
            return
        if now < self.next_attempt:
            return
        try:
            if self.inspect().occupied(self.spec):
                self.log.info('%s: external process or listener; leaving it alone', self.spec.module)
                self.next_attempt = now + 30
                return
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.log.warning('%s: inspection unavailable (%s); defer 30s', self.spec.module, exc)
            self.next_attempt = now + 30
            return
        try:
            self.child = self.launch(self.spec)
        except OSError as exc:
            self.failed(now, str(exc))
            return
        self.started_at = now
        self.log.info('%s: launched pid=%s', self.spec.module, self.child.pid)


@dataclass(frozen=True)
class Service:
    module: str
    command: list[str]
    ports: tuple[int, ...]
    cwd: Path | None = None


def service_specs(root):
    root = Path(root)
    home = Path(os.environ.get('HERMES_HOME', str(Path.home() / 'AppData/Local/hermes')))
    core = home / 'hermes-agent'
    return [
        Service('satellite_gateway.loop', [str(root / '.venv/Scripts/python.exe'),
                '-u', '-m', 'satellite_gateway.loop'], (8765, 10702)),
        Service('hermes_gateway.pairing_server', [str(root / '.tools-venv/Scripts/python.exe'),
                '-u', '-m', 'hermes_gateway.pairing_server', '--state', 'state',
                '--dial', 'tls://<gateway-LAN-IP>:10702', '--bind', '<gateway-LAN-IP>',
                '--port', '10704'], (10704,)),
        Service('hermes_cli.main', [str(core / 'venv/Scripts/python.exe'),
                '-u', '-m', 'hermes_cli.main', 'gateway', 'run'], (8642,), core),
    ]
