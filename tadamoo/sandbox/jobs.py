"""A Windows job object for one command: its memory and process count are capped, and everything it started dies with it.

The job is made before the command runs a single instruction: the process is created suspended, put in the job,
then resumed, so nothing it starts can escape. Closing the job (or the harness ending, however it ends) kills
every process in it. Elsewhere this is a no-op and the caller falls back to killing the process group.
"""
from __future__ import annotations

import os
from typing import Optional

AVAILABLE = os.name == "nt"

if AVAILABLE:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _nt = ctypes.WinDLL("ntdll")

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                       "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _Extended(ctypes.Structure):
        _fields_ = [("Basic", _Basic), ("Io", _IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _ACTIVE_PROCESS = 0x8
    _JOB_MEMORY = 0x200
    _KILL_ON_CLOSE = 0x2000
    _EXTENDED_INFO = 9
    CREATE_SUSPENDED = 0x4

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _nt.NtResumeProcess.argtypes = (wintypes.HANDLE,)
else:
    CREATE_SUSPENDED = 0


class CommandJob:
    """Made before the process starts; `attach` puts the suspended process in and lets it run."""

    def __init__(self, max_memory_bytes: Optional[int] = None, max_processes: Optional[int] = None) -> None:
        self.handle = None
        if not AVAILABLE:
            return
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return
        info = _Extended()
        info.Basic.LimitFlags = _KILL_ON_CLOSE
        if max_memory_bytes:
            info.Basic.LimitFlags |= _JOB_MEMORY
            info.JobMemoryLimit = int(max_memory_bytes)
        if max_processes:
            info.Basic.LimitFlags |= _ACTIVE_PROCESS
            info.Basic.ActiveProcessLimit = int(max_processes)
        if not _k32.SetInformationJobObject(job, _EXTENDED_INFO, ctypes.byref(info), ctypes.sizeof(info)):
            _k32.CloseHandle(job)
            return
        self.handle = job

    @property
    def active(self) -> bool:
        return bool(self.handle)

    def attach(self, proc) -> bool:
        """True if the process is now contained. The process is resumed either way: a failed attach must not hang it."""
        contained = False
        try:
            if self.handle:
                contained = bool(_k32.AssignProcessToJobObject(self.handle, int(proc._handle)))
        finally:
            if AVAILABLE:
                _nt.NtResumeProcess(int(proc._handle))
        return contained

    def kill(self) -> None:
        if self.handle:
            _k32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None
