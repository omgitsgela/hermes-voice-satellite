"""Native isolated smoke: only temporary-root harmless sleepers are terminated."""
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/windows'))

class JobTests(unittest.TestCase):
    def test_native_assignment_failure_cannot_execute_child_code(self):
        from windows_job import WindowsJob
        with tempfile.TemporaryDirectory() as tmp, WindowsJob() as job:
            marker = Path(tmp) / 'must-not-exist'
            children = []
            real_popen = subprocess.Popen
            def capture(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                children.append(child)
                self.assertTrue(kwargs['creationflags'] & 0x4)
                self.assertFalse(kwargs['creationflags'] & 0x01000000)  # no breakaway
                return child
            with patch('windows_job.subprocess.Popen', side_effect=capture), \
                    patch.object(job, 'assign', side_effect=OSError('injected assignment failure')):
                with self.assertRaises(OSError):
                    job.spawn([sys.executable, '-c',
                               'from pathlib import Path; import sys; Path(sys.argv[1]).touch()', str(marker)])
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            self.assertFalse(marker.exists(), 'unassigned suspended child executed')

    def test_assignment_failure_kills_suspended_child_before_it_can_spawn(self):
        from windows_job import WindowsJob
        child = Mock()
        with WindowsJob() as job, patch('windows_job.subprocess.Popen', return_value=child), \
                patch.object(job, 'assign', side_effect=OSError('denied')):
            with self.assertRaises(OSError):
                job.spawn(['unused'])
        child.kill.assert_called_once()
        child.wait.assert_called_once()

    def test_crashed_supervisor_kills_venv_redirector_and_descendants(self):
        # Helper imports production code but never calls main/service_specs/inspection.
        helper = """import sys,time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from windows_job import WindowsJob
job=WindowsJob()
child=job.spawn([sys.argv[2], '-c', sys.argv[3], sys.argv[4]], cwd=sys.argv[4])
Path(sys.argv[4], 'redirector').write_text(str(child.pid))
time.sleep(120)
"""
        sleeper = """import os,subprocess,sys,time
from pathlib import Path
p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])
Path(sys.argv[1], 'pids').write_text(str(os.getpid())+' '+str(p.pid))
time.sleep(120)
"""
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.argtypes = [w.DWORD,w.BOOL,w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.WaitForSingleObject.argtypes = [w.HANDLE,w.DWORD]
        k.WaitForSingleObject.restype = w.DWORD
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        handles = []
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = subprocess.Popen([sys.executable, '-c', helper,
                str(ROOT / 'scripts/windows'), str(ROOT / '.tools-venv/Scripts/python.exe'),
                sleeper, tmp], creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic()+15
                while not ((Path(tmp)/'pids').exists() and (Path(tmp)/'redirector').exists()):
                    self.assertIsNone(supervisor.poll(), 'test supervisor exited before children ready')
                    if time.monotonic()>deadline:
                        self.fail('temporary children did not become ready')
                    time.sleep(.05)
                pids = [int((Path(tmp)/'redirector').read_text()),
                        *map(int,(Path(tmp)/'pids').read_text().split())]
                for pid in pids:
                    handle = k.OpenProcess(0x100000, False, pid)
                    self.assertTrue(handle, 'cannot obtain synchronization handle')
                    handles.append(handle)
                supervisor.kill()  # abrupt death; no Python finally/atexit
                supervisor.wait(timeout=5)
                for handle in handles:
                    self.assertEqual(k.WaitForSingleObject(handle, 10000), 0,
                                     'owned redirector/interpreter/descendant survived supervisor crash')
                print('NATIVE JOB SMOKE: abrupt owner death killed redirector, interpreter and descendant', flush=True)
            finally:
                if supervisor.poll() is None:
                    supervisor.kill()
                    supervisor.wait(timeout=5)
                for handle in handles:
                    k.CloseHandle(handle)

if __name__ == '__main__':
    unittest.main()
