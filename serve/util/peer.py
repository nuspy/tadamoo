"""serve/util/peer.py - which process is on the other end of a connection to this PC, and whether it is the agent's own code.

The agent runs commands and code as the same user as the server, so nothing in a request tells a `curl` the agent started from the
person's browser. The server knows which processes it started as agent sessions; a connection whose client process is a descendant of
one of them is agent-run code and is not given the secret of the control routes. The agent session process itself, which asks for
approvals through those routes, is not a descendant of itself and is not affected.

Roots come from providers (functions returning process ids). When the peer or the process tree cannot be read the answer is
"unknown" (False), so a platform without the lookup keeps the other checks of the handler.
"""
from __future__ import annotations

import os
import socket
import threading
import weakref
from typing import Callable, Dict, Iterable, List, Optional, Tuple

_providers: List[Callable[[], Optional[Callable[[], Iterable[int]]]]] = []      # each returns the provider, or None once its owner is gone
_lock = threading.Lock()
MAX_DEPTH = 64


def add_provider(provider: Callable[[], Iterable[int]]) -> None:
    """`provider()` returns the pids of the agent processes that are running now. A method is held weakly: it lasts as long as its owner."""
    ref = weakref.WeakMethod(provider) if hasattr(provider, "__self__") else (lambda: provider)
    with _lock:
        _providers.append(ref)


def remove_provider(provider: Callable[[], Iterable[int]]) -> None:
    with _lock:
        _providers[:] = [r for r in _providers if r() is not None and r() != provider]


def roots() -> set:
    with _lock:
        _providers[:] = [r for r in _providers if r() is not None]
        providers = [r() for r in _providers]
    found: set = set()
    for provider in providers:
        try:
            found |= {int(p) for p in provider() if p}
        except Exception:
            continue
    return found


def peer_pid(client_host: str, client_port: int, server_port: int) -> Optional[int]:
    """The process that owns the client end of a loopback connection, or None when it cannot be told."""
    try:
        return _peer_pid(client_host, client_port, server_port)
    except Exception:
        return None


def ancestors(pid: int) -> List[int]:
    """The parent chain of a process, nearest first, ending at the top of the tree or at MAX_DEPTH."""
    try:
        parents = _parent_map() if os.name == "nt" else None
    except Exception:
        return []
    chain: List[int] = []
    seen = {pid}
    cur = pid
    for _ in range(MAX_DEPTH):
        nxt = parents.get(cur) if parents is not None else _linux_parent(cur)
        if not nxt or nxt in seen:
            break
        chain.append(nxt)
        seen.add(nxt)
        cur = nxt
    return chain


def is_agent_code(client_host: str, client_port: int, server_port: int) -> bool:
    """True when the client of this connection is a strict descendant of a running agent session."""
    found = roots()
    if not found:
        return False
    pid = peer_pid(client_host, client_port, server_port)
    if pid is None:
        return False
    return bool(found.intersection(ancestors(pid)))


# -- Windows ---------------------------------------------------------------------------------------------------------

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _TCP_TABLE_OWNER_PID_ALL = 5
    _AF_INET, _AF_INET6 = 2, 23

    class _Entry32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    _kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Entry32))
    _kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Entry32))
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    def _tcp_rows(family: int) -> List[Tuple[bytes, int, int, int]]:
        """(local address, local port, remote port, pid) of each TCP connection of one address family."""
        size = wintypes.DWORD(0)
        for _ in range(8):
            buf = ctypes.create_string_buffer(size.value)
            rc = _iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, family, _TCP_TABLE_OWNER_PID_ALL, 0)
            if rc == 0:
                break
            if rc != 122:                                  # ERROR_INSUFFICIENT_BUFFER: the table grew, ask again with the new size
                return []
        else:
            return []
        data = buf.raw
        count = int.from_bytes(data[:4], "little")
        rows = []
        if family == _AF_INET:
            width, base = 24, 4                            # state, local addr, local port, remote addr, remote port, pid
            for i in range(count):
                row = data[base + i * width: base + (i + 1) * width]
                local, lport, rport, pid = row[4:8], row[8:12], row[16:20], row[20:24]
                rows.append((bytes(local), socket.ntohs(int.from_bytes(lport[:2], "little")),
                             socket.ntohs(int.from_bytes(rport[:2], "little")), int.from_bytes(pid, "little")))
        else:
            width, base = 56, 4                            # local addr[16], scope, port, remote addr[16], scope, port, state, pid
            for i in range(count):
                row = data[base + i * width: base + (i + 1) * width]
                rows.append((bytes(row[0:16]), socket.ntohs(int.from_bytes(row[20:22], "little")),
                             socket.ntohs(int.from_bytes(row[40:42], "little")), int.from_bytes(row[52:56], "little")))
        return rows

    def _peer_pid(client_host: str, client_port: int, server_port: int) -> Optional[int]:
        family = _AF_INET6 if ":" in client_host and not client_host.startswith("::ffff:") else _AF_INET
        for local, lport, rport, pid in _tcp_rows(family):
            if lport == client_port and rport == server_port:
                return pid
        return None

    def _parent_map() -> Dict[int, int]:
        snap = _kernel32.CreateToolhelp32Snapshot(2, 0)     # TH32CS_SNAPPROCESS
        if not snap or snap == wintypes.HANDLE(-1).value:
            raise OSError("process list unavailable")
        parents: Dict[int, int] = {}
        try:
            entry = _Entry32()
            entry.dwSize = ctypes.sizeof(_Entry32)
            ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                parents[entry.th32ProcessID] = entry.th32ParentProcessID
                ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snap)
        return parents

    def _linux_parent(pid: int) -> Optional[int]:
        return None

else:
    def _parent_map() -> Dict[int, int]:
        return {}

    def _linux_parent(pid: int) -> Optional[int]:
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                text = f.read().decode("utf-8", errors="replace")
            return int(text.rsplit(")", 1)[1].split()[1])          # the field after the state: the parent
        except (OSError, ValueError, IndexError):
            return None

    def _peer_pid(client_host: str, client_port: int, server_port: int) -> Optional[int]:
        inode = None
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(table, "r", encoding="ascii") as f:
                    next(f, None)
                    for line in f:
                        cols = line.split()
                        if len(cols) < 10:
                            continue
                        if int(cols[1].rsplit(":", 1)[1], 16) == client_port and int(cols[2].rsplit(":", 1)[1], 16) == server_port:
                            inode = cols[9]
                            break
            except OSError:
                continue
            if inode:
                break
        if not inode:
            return None
        target = f"socket:[{inode}]"
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                for fd in os.listdir(f"/proc/{name}/fd"):
                    if os.readlink(f"/proc/{name}/fd/{fd}") == target:
                        return int(name)
            except OSError:
                continue
        return None
