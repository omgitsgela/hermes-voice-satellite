"""Isolated tests: no project services, Startup changes or live process mutations."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'windows'))


class SpecificationTests(unittest.TestCase):
    def test_three_project_modules_and_expected_interpreters(self):
        import project_supervisor as s
        specs = s.service_specs(ROOT)
        self.assertEqual([x.module for x in specs], [
            'satellite_gateway.loop', 'hermes_gateway.pairing_server', 'hermes_cli.main'])
        import os
        home = Path(os.environ.get('HERMES_HOME', str(Path.home() / 'AppData/Local/hermes')))
        self.assertEqual(specs[2].command, [str(home / 'hermes-agent/venv/Scripts/python.exe'),
                         '-u', '-m', 'hermes_cli.main', 'gateway', 'run'])
        self.assertEqual(specs[2].cwd, home / 'hermes-agent')
        self.assertEqual(specs[2].ports, (8642,))
        for command in ['python -m hermes_cli.main desktop serve',
                        'python -m hermes_cli.main chat',
                        'python -m hermes_cli.main gateway runner']:
            self.assertFalse(s.Inventory((command,), frozenset()).occupied(specs[2]))
        self.assertTrue(s.Inventory(('python -u -m hermes_cli.main gateway run',), frozenset()).occupied(specs[2]))
        self.assertTrue(s.Inventory((), frozenset({8642})).occupied(specs[2]))
        self.assertEqual(specs[0].command[:4], [str(ROOT / '.venv/Scripts/python.exe'), '-u', '-m', 'satellite_gateway.loop'])
        self.assertEqual(specs[1].command, [str(ROOT / '.tools-venv/Scripts/python.exe'), '-u', '-m',
            'hermes_gateway.pairing_server', '--state', 'state', '--dial',
            'tls://<gateway-LAN-IP>:10702', '--bind', '<gateway-LAN-IP>', '--port', '10704'])
        self.assertEqual(specs[0].ports, (8765, 10702))
        self.assertEqual(specs[1].ports, (10704,))


class DuplicateDetectionTests(unittest.TestCase):
    def test_unready_python_module_or_either_listener_blocks_duplicate(self):
        import project_supervisor as s
        spec = s.service_specs(ROOT)[0]
        inv = s.parse_inventory('[{"CommandLine":"python.exe -u -m satellite_gateway.loop"}]', '')
        self.assertTrue(inv.occupied(spec))
        for address in ['127.0.0.1:8765', '<gateway-LAN-IP>:10702', '[::]:10702']:
            inv = s.parse_inventory('[]', f'  TCP    {address}   0.0.0.0:0   LISTENING   123\n')
            self.assertTrue(inv.occupied(spec))
        inv = s.parse_inventory('[{"CommandLine":"python.exe -m satellite_gateway.loop_extra"}]',
                                ' TCP 127.0.0.1:8642 0.0.0.0:0 LISTENING 5\n')
        self.assertFalse(inv.occupied(spec))

    def test_inaccessible_or_malformed_inventory_fails_closed(self):
        import project_supervisor as s
        for value in ['not json', '[{"CommandLine":null}]']:
            with self.assertRaises((ValueError, TypeError)):
                s.parse_inventory(value, '')


class RestartTests(unittest.TestCase):
    def make_runner(self):
        import project_supervisor as s
        from unittest.mock import Mock
        self.child = Mock()
        self.child.poll.return_value = None
        self.launch = Mock(return_value=self.child)
        self.inspect = Mock(return_value=s.Inventory((), frozenset()))
        self.log = Mock()
        self.runner = s.Runner(s.service_specs(ROOT)[0], self.launch, self.inspect, self.log)

    def test_crash_backoff_is_exponential_capped_and_resets_after_stable_run(self):
        self.make_runner()
        self.runner.tick(0)
        self.assertEqual(self.launch.call_count, 1)
        now = 1
        for delay in [10, 20, 40, 80, 160, 300, 300]:
            self.child.poll.return_value = 1
            self.runner.tick(now)
            self.runner.tick(now + delay - 0.1)
            before = self.launch.call_count
            self.child.poll.return_value = None
            self.runner.tick(now + delay)
            self.assertEqual(self.launch.call_count, before + 1)
            now += delay + 1
        self.child.poll.return_value = 2
        self.runner.tick(now + 301)
        before = self.launch.call_count
        self.child.poll.return_value = None
        self.runner.tick(now + 310)
        self.assertEqual(self.launch.call_count, before)
        self.runner.tick(now + 311)
        self.assertEqual(self.launch.call_count, before + 1)

    def test_external_service_is_not_launched_or_owned_and_rechecked_later(self):
        import project_supervisor as s
        self.make_runner()
        self.inspect.return_value = s.Inventory(('python -m satellite_gateway.loop',), frozenset())
        self.runner.tick(0)
        self.launch.assert_not_called()
        self.assertIsNone(self.runner.child)
        self.inspect.return_value = s.Inventory((), frozenset())
        self.runner.tick(29)
        self.launch.assert_not_called()
        self.runner.tick(30)
        self.launch.assert_called_once()

    def test_failed_inspection_or_spawn_defers_without_busy_retry(self):
        self.make_runner()
        self.inspect.side_effect = OSError('access denied')
        self.runner.tick(0)
        self.runner.tick(1)
        self.launch.assert_not_called()
        self.assertEqual(self.inspect.call_count, 1)
        self.inspect.side_effect = None
        self.launch.side_effect = OSError('missing interpreter')
        self.runner.tick(30)
        self.runner.tick(31)
        self.assertEqual(self.launch.call_count, 1)
        self.runner.tick(40)
        self.assertEqual(self.launch.call_count, 2)


class WindowsBoundaryTests(unittest.TestCase):
    def test_child_launch_is_hidden_redirected_and_uses_project_environment(self):
        import project_supervisor as s
        import tempfile
        from unittest.mock import patch, Mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = s.service_specs(root)[0]
            def fake_popen(command, **kwargs):
                self.assertEqual(command, spec.command)
                self.assertEqual(kwargs['cwd'], str(root))
                self.assertTrue(kwargs['creationflags'] & s.subprocess.CREATE_NO_WINDOW)
                self.assertEqual(kwargs['stdin'], s.subprocess.DEVNULL)
                self.assertEqual(kwargs['stderr'], s.subprocess.STDOUT)
                self.assertTrue(kwargs['env']['HERMES_HOME'].endswith('hermes'))
                self.assertEqual(kwargs['env']['PYTHONUNBUFFERED'], '1')
                kwargs['stdout'].write(b'test child stdout and merged stderr\n')
                return Mock(pid=42)
            job = Mock()
            job.spawn.side_effect = fake_popen
            s.launch_child(root, spec, job)
            job.spawn.assert_called_once()
            self.assertEqual((root / 'logs/windows/satellite_gateway.loop.log').read_text(),
                             'test child stdout and merged stderr\n')

    def test_inventory_commands_are_hidden_bounded_and_failure_is_closed(self):
        import project_supervisor as s
        from unittest.mock import patch, Mock
        with patch.object(s.subprocess, 'run', side_effect=[
                Mock(stdout='[]'), Mock(stdout=' TCP 0.0.0.0:10704 0.0.0.0:0 LISTENING 99')]) as run:
            inv = s.inspect_windows()
            self.assertTrue(inv.occupied(s.service_specs(ROOT)[1]))
            for call in run.call_args_list:
                self.assertTrue(call.kwargs['creationflags'] & s.subprocess.CREATE_NO_WINDOW)
                self.assertLessEqual(call.kwargs['timeout'], 15)
                self.assertTrue(call.kwargs['check'])
        with patch.object(s.subprocess, 'run', side_effect=s.subprocess.TimeoutExpired('probe', 15)):
            with self.assertRaises(OSError):
                s.inspect_windows()

    def test_singleton_refuses_second_lock_and_releases_without_stale_pid_logic(self):
        import project_supervisor as s
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'supervisor.lock'
            first = s.acquire_lock(path)
            self.assertIsNotNone(first)
            self.assertIsNone(s.acquire_lock(path))
            first.close()
            second = s.acquire_lock(path)
            self.assertIsNotNone(second)
            second.close()


class MainLifecycleTests(unittest.TestCase):
    def test_stop_sentinel_stops_only_owned_children_and_lock_releases(self):
        import project_supervisor as s
        import tempfile
        from unittest.mock import patch, Mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owned = Mock(pid=42)
            owned.poll.return_value = None
            def sleep(_):
                (root / 'state/windows-supervisor.stop').touch()
            # Pairing is external, runtime is absent. No real inventory or child launch.
            inventory = s.Inventory(('python -m hermes_gateway.pairing_server',
                                     'python -m hermes_cli.main gateway run'), frozenset())
            with patch.object(s, 'inspect_windows', return_value=inventory), \
                 patch.object(s, 'launch_child', return_value=owned) as launch, \
                 patch.object(s, 'WindowsJob') as job_type:
                self.assertEqual(s.main(root, sleep=sleep), 0)
                self.assertEqual(launch.call_count, 1)
                self.assertEqual(launch.call_args.args[1].module, 'satellite_gateway.loop')
                job_type.return_value.close.assert_called_once()
                owned.terminate.assert_not_called()
                self.assertIs(launch.call_args.args[2], job_type.return_value)
            lock = s.acquire_lock(root / 'state/windows-supervisor.lock')
            self.assertIsNotNone(lock)
            lock.close()

    def test_second_supervisor_or_existing_stop_file_never_launches(self):
        import project_supervisor as s
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = s.acquire_lock(root / 'state/windows-supervisor.lock')
            with patch.object(s, 'launch_child') as launch:
                self.assertEqual(s.main(root), 0)
                launch.assert_not_called()
                lock.close()
                (root / 'state/windows-supervisor.stop').touch()
                self.assertEqual(s.main(root), 0)
                launch.assert_not_called()


class StartupTests(unittest.TestCase):
    def test_entrypoint_calls_main_only_when_executed(self):
        from unittest.mock import patch
        import project_supervisor as s
        import runpy
        entry = ROOT / 'scripts/windows/supervise.pyw'
        with patch.object(s, 'main', return_value=0) as main:
            runpy.run_path(str(entry), run_name='not_main')
            main.assert_not_called()
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(entry), run_name='__main__')
            self.assertEqual(result.exception.code, 0)
            main.assert_called_once_with()

    def test_startup_preview_does_not_write_and_install_creates_only_project_vbs(self):
        import install_project_startup as install
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project with spaces'
            startup = Path(tmp) / 'Startup'
            target, text = install.install(root, startup, apply=False)
            self.assertFalse(startup.exists())
            self.assertEqual(target.name, 'HermesVoiceSatelliteProject.vbs')
            command = install.subprocess.list2cmdline([
                str(root / '.tools-venv/Scripts/pythonw.exe'), str(root / 'scripts/windows/supervise.pyw')])
            self.assertIn('shell.Run "' + command.replace('"', '""') + '", 0, False', text)
            for relative in ['.tools-venv/Scripts/pythonw.exe', '.venv/Scripts/python.exe',
                             '.tools-venv/Scripts/python.exe', 'scripts/windows/supervise.pyw']:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            install.install(root, startup, apply=True)
            self.assertEqual(list(startup.iterdir()), [target])
            self.assertEqual(target.read_text(encoding='utf-16'), text)
            target.write_text('foreign startup', encoding='utf-16')
            with self.assertRaises(FileExistsError):
                install.install(root, startup, apply=True)
            self.assertEqual(target.read_text(encoding='utf-16'), 'foreign startup')

    def test_install_missing_interpreter_fails_without_writing_startup(self):
        import install_project_startup as install
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            startup = Path(tmp) / 'Startup'
            with self.assertRaises(FileNotFoundError):
                install.install(Path(tmp) / 'missing', startup, apply=True)
            self.assertFalse(startup.exists())


if __name__ == '__main__':
    unittest.main()
