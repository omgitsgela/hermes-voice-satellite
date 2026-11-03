"""Private kill-on-close Windows job; children cannot run before assignment.

No process discovery or PID-based termination. Handles are non-inheritable.
Popen closes its primary thread handle, so NtResumeProcess resumes the suspended
process using its retained process handle after successful job assignment.
"""
import ctypes
from ctypes import wintypes as w
import subprocess

SIZE_T = ctypes.c_size_t

class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
        'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

class BASIC_LIMIT(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                ('PerJobUserTimeLimit', ctypes.c_longlong), ('LimitFlags', w.DWORD),
                ('MinimumWorkingSetSize', SIZE_T), ('MaximumWorkingSetSize', SIZE_T),
                ('ActiveProcessLimit', w.DWORD), ('Affinity', SIZE_T),
                ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]

class EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', BASIC_LIMIT), ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', SIZE_T), ('JobMemoryLimit', SIZE_T),
                ('PeakProcessMemoryUsed', SIZE_T), ('PeakJobMemoryUsed', SIZE_T)]

class WindowsJob:
    def __init__(self):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.nt = ctypes.WinDLL('ntdll', use_last_error=True)
        signatures = {
            'CreateJobObjectW': ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            'SetInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
            'TerminateJobObject': ([w.HANDLE, w.UINT], w.BOOL),
            'CloseHandle': ([w.HANDLE], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.nt.NtResumeProcess.argtypes = [w.HANDLE]
        self.nt.NtResumeProcess.restype = w.LONG  # NTSTATUS, signed 32-bit
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, child):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(child._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def spawn(self, command, **kwargs):
        if not self.handle:
            raise OSError('Job is closed; refusing unmanaged launch')
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | 0x4 | subprocess.CREATE_NO_WINDOW
        child = subprocess.Popen(command, **kwargs)  # CREATE_SUSPENDED; no breakaway
        try:
            self.assign(child)
            status = self.nt.NtResumeProcess(int(child._handle))
            if status < 0:
                raise OSError(f'NtResumeProcess failed: NTSTATUS {status:#x}')
        except BaseException:
            # Still suspended on assignment failure, so it cannot have descendants.
            child.kill()
            child.wait(timeout=10)
            raise
        return child

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
