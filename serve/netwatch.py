"""The security monitor: watches the network and disk behaviour of the processes that run the model and flags what looks wrong.

Nothing is blocked. Each poll reads the connection table and the open files of the watched processes; a connection or a file
seen for the first time becomes an event. Tags describe what is unusual about it. A suspicious event is an alert until the person
resolves it (a dialog offers exceptions). Every event, suppressed ones included, goes to a hash-chained log.

A behaviour string, matched by the behaviour exceptions, is
    f"{process} {proto} outbound port {remote_port} tags={','.join(sorted(tags))}"
and a resource string, matched by the resource exceptions, is
    f"{remote_ip} {host}"        (for a file or a persistence entry: remote_ip is empty and host holds the path or name).
Exceptions are Python regular expressions applied with re.search.

The connection table can miss a connection that lives for milliseconds; there is no packet capture."""
from __future__ import annotations

import bisect
import collections
import hashlib
import ipaddress
import json
import os
import queue
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

ENGINE_NAMES = ("basalt", "llama-server", "strata")
ALIBABA_MARKERS = ("aliyun", "alicdn", "dashscope", "modelscope", "qwen", "alibaba", "taobao", "aliyuncs", "tbcdn")
PROXY_PORTS = frozenset({1080, 8080, 3128, 8118, 9050, 9150, 7890, 7897, 10808, 10809})
PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
DOH_HOSTS = ("dns.google", "cloudflare-dns.com", "dns.quad9.net", "mozilla.cloudflare-dns.com")
DOH_PORTS = frozenset({853, 9001, 9030})
ARCHIVE_EXT = frozenset({"zip", "7z", "rar", "tar", "gz", "tgz", "xz", "zst", "enc", "bin", "dat"})
ALL_TAGS = frozenset({"alibaba_cn_domain", "proxy_env", "system_proxy", "local_proxy_listener", "doh_or_tor", "no_agent_call",
                      "disk_outside_expected", "archive_or_staging", "persistence"})
# any tag makes an event suspicious; so does a public connection of a process whose role is in this set
ALERT_TAGS = ALL_TAGS
ALERTING_ROLES = frozenset({"engine"})
TOOL_CALL_WINDOW_S = 30.0
RESOLVE_GRACE_S = 3.0
SEEN_FORGET_S = 600.0
SLOW_CHECK_S = 30.0
GENESIS = "0" * 64
DEFAULTS: Dict[str, Any] = {
    "enabled": False, "interval_s": 2.0, "watch_extra_names": [], "expected_folders": [],
    "geo_db_path": "", "alert_on_system_wide": False, "max_log_mb": 0,
}
COVERAGE_NOTE = ("The monitor reads the operating system's connection table at an interval: a connection that lasts only milliseconds "
                 "can be missed. Full packet capture (ETW) is not implemented. A file with the weights does not execute code, and "
                 "the monitor cannot prove that nothing was sent.")
RUN_KEYS = ((r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU"), (r"Software\Microsoft\Windows\CurrentVersion\Run", "HKLM"))


class NetwatchError(ValueError):
    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.code = code


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except Exception:                                   # a check never raises into the poll
        return default


def is_admin() -> bool:
    try:
        if os.name == "nt":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


def stem(name: str) -> str:
    base = os.path.basename(name or "").lower()
    return base[:-4] if base.endswith(".exe") else base


def behavior_string(process: str, proto: str, port: Any, tags: Iterable[str]) -> str:
    return f"{process} {proto} outbound port {port} tags={','.join(sorted(tags))}"


def resource_string(remote_ip: str, host: str) -> str:
    return f"{remote_ip} {host}"


def classify_remote(ip: str) -> str:
    """'ignore' for loopback, link-local and other non-routable addresses, 'lan' for private ranges, else 'public'."""
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return "ignore"
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast or addr.is_reserved:
        return "ignore"
    if addr.is_private or (addr.version == 4 and addr in ipaddress.ip_network("100.64.0.0/10")):
        return "lan"
    return "public"


def _within(path: str, folders: Iterable[str]) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    for f in folders:
        if not f:
            continue
        base = os.path.normcase(os.path.abspath(f))
        if p == base or p.startswith(base.rstrip("\\/") + os.sep):
            return True
    return False


def default_expected_folders() -> List[str]:
    """The folders a model process may write to: the user data folder, the temp folder, the engine cache, the models folders and the agent's folders."""
    found: List[str] = [tempfile.gettempdir()]

    def add_all() -> None:
        from serve import userdata
        found.append(str(userdata.user_data_dir()))
        from serve.engine.modelfile import cache_root
        found.append(str(cache_root()))
        from serve import models
        found.extend(models.dirs())
        found.append(str(userdata.path("projects.json").parent))
    _safe(add_all, None)
    return [f for f in dict.fromkeys(found) if f]


# ---------------------------------------------------------------- sources of facts

class PsutilSource:
    """The processes, connections, open files and environments as psutil reports them. Every call returns empty on failure."""

    def __init__(self, extra_names: Callable[[], List[str]], system_wide: Callable[[], bool], server_pid: Optional[int] = None) -> None:
        try:
            import psutil
            self.ps = psutil
        except ImportError:
            self.ps = None
        self.extra_names, self.system_wide = extra_names, system_wide
        self.server_pid = server_pid or os.getpid()

    @property
    def available(self) -> bool:
        return self.ps is not None

    def processes(self) -> List[Dict[str, Any]]:
        if not self.ps:
            return []
        names = {stem(n) for n in ENGINE_NAMES} | {stem(n) for n in self.extra_names() if n}
        table: Dict[int, Tuple[str, int]] = {}
        for p in self.ps.process_iter(["pid", "name", "ppid"]):
            info = p.info
            table[info["pid"]] = (info.get("name") or "", info.get("ppid") or 0)
        children: Dict[int, List[int]] = collections.defaultdict(list)
        for pid, (_, ppid) in table.items():
            children[ppid].append(pid)
        below: set = set()
        todo = list(children.get(self.server_pid, []))
        while todo:
            pid = todo.pop()
            if pid not in below:
                below.add(pid)
                todo.extend(children.get(pid, []))
        wide, out = self.system_wide(), []
        for pid, (name, _) in table.items():
            if pid == self.server_pid:
                role = "server"
            elif stem(name) in names:
                role = "engine"
            elif pid in below:
                role = "tool"
            elif wide and pid > 4:
                role = "other"
            else:
                continue
            out.append({"pid": pid, "name": name, "role": role})
        return out

    def _proc(self, pid: int):
        return self.ps.Process(pid)

    def connections(self, pid: int) -> List[Dict[str, Any]]:
        if not self.ps:
            return []
        proc = self._proc(pid)
        fn = getattr(proc, "net_connections", None) or proc.connections
        out = []
        for c in fn(kind="inet"):
            if not c.raddr:
                continue
            proto = "tcp" if c.type == socket.SOCK_STREAM else "udp"
            out.append({"proto": proto + ("6" if c.family == socket.AF_INET6 else ""), "laddr": tuple(c.laddr) if c.laddr else ("", 0),
                        "raddr": (c.raddr[0], c.raddr[1]), "status": c.status})
        return out

    def open_files(self, pid: int) -> List[Dict[str, Any]]:
        if not self.ps:
            return []
        return [{"path": f.path, "mode": getattr(f, "mode", "") or ""} for f in self._proc(pid).open_files()]

    def environ(self, pid: int) -> Dict[str, str]:
        return dict(self._proc(pid).environ()) if self.ps else {}

    def listeners(self) -> List[Dict[str, Any]]:
        if not self.ps:
            return []
        out = []
        for c in self.ps.net_connections(kind="inet"):
            if c.status == "LISTEN" and c.laddr:
                out.append({"ip": c.laddr[0], "port": c.laddr[1], "pid": c.pid or 0})
        return out


# ---------------------------------------------------------------- host names and geography

class HostResolver:
    """Reverse DNS in a background thread. Calling it never blocks: the name when known, '' when there is none, None while pending."""

    def __init__(self) -> None:
        self.cache: Dict[str, str] = {}
        self.todo: "queue.Queue[str]" = queue.Queue()
        self.asked: set = set()
        self.thread: Optional[threading.Thread] = None

    def __call__(self, ip: str) -> Optional[str]:
        if ip in self.cache:
            return self.cache[ip]
        if ip not in self.asked:
            self.asked.add(ip)
            self.todo.put(ip)
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, daemon=True, name="netwatch-dns")
                self.thread.start()
        return None

    def _run(self) -> None:
        while True:
            try:
                ip = self.todo.get(timeout=30)
            except queue.Empty:
                return
            try:
                self.cache[ip] = socket.gethostbyaddr(ip)[0].lower()
            except (OSError, UnicodeError):
                self.cache[ip] = ""


class GeoDb:
    """An offline table of address ranges, read from a CSV file: cidr,asn,country[,organisation]. Nothing is downloaded."""

    def __init__(self, path: str) -> None:
        self.rows: Dict[int, List[Tuple[int, int, str, str]]] = {4: [], 6: []}
        self.count, self.error = 0, ""
        if not path:
            self.error = "no geo database"
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    cells = [c.strip() for c in line.split(",")]
                    if len(cells) < 3 or cells[0].startswith("#"):
                        continue
                    try:
                        net = ipaddress.ip_network(cells[0], strict=False)
                    except ValueError:
                        continue
                    self.rows[net.version].append((int(net.network_address), int(net.broadcast_address), cells[1], cells[2]))
                    self.count += 1
            for rows in self.rows.values():
                rows.sort()
            if not self.count:
                self.error = "the geo database has no usable rows"
        except OSError as exc:
            self.error = f"geo database not readable: {exc.strerror or exc}"

    def lookup(self, ip: str) -> Tuple[str, str]:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return "", ""
        rows = self.rows[addr.version]
        i = bisect.bisect_right(rows, (int(addr), 1 << 130, "", "")) - 1
        if i >= 0 and rows[i][0] <= int(addr) <= rows[i][1]:
            return rows[i][2], rows[i][3]
        return "", ""


# ---------------------------------------------------------------- the hash-chained log

def _hash(prev: str, body: Dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((prev + canonical).encode("utf-8")).hexdigest()


class ChainLog:
    """A JSON Lines file in which each line carries `prev` and `hash` = sha256(prev + canonical json of seq, ts, type, data).
    Deleting entries appends a 'deleted' record, then rewrites the file without them, re-chained, ending with a 'compacted' record
    that holds the head hash the file had before. The 'deleted' records stay, so the file shows that deletions happened."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()
        self.records: List[Dict[str, Any]] = []
        self.corrupt = 0
        self.load()

    def load(self) -> None:
        self.records, self.corrupt = [], 0
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        rec = None
                    if isinstance(rec, dict) and {"seq", "ts", "type", "data", "prev", "hash"} <= rec.keys():
                        self.records.append(rec)
                    else:
                        self.corrupt += 1
        except OSError:
            pass

    @property
    def head(self) -> str:
        return self.records[-1]["hash"] if self.records else GENESIS

    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def append(self, kind: str, data: Dict[str, Any], ts: float) -> Dict[str, Any]:
        with self.lock:
            body = {"seq": self.records[-1]["seq"] + 1 if self.records else 0, "ts": ts, "type": kind, "data": data}
            rec = {**body, "prev": self.head}
            rec["hash"] = _hash(rec["prev"], body)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.records.append(rec)
            return rec

    def compact(self, drop: Callable[[Dict[str, Any]], bool], marker: Dict[str, Any], ts: float) -> int:
        """Rewrites the file without the records `drop` selects; returns how many went."""
        with self.lock:
            kept = [r for r in self.records if not drop(r)]
            removed = len(self.records) - len(kept)
            marker = {**marker, "previous_head": self.head, "previous_records": len(self.records), "removed_records": removed}
            kept_bodies = [{"type": r["type"], "ts": r["ts"], "data": r["data"]} for r in kept]
            kept_bodies.append({"type": "compacted", "ts": ts, "data": marker})
            rebuilt, prev = [], GENESIS
            for i, b in enumerate(kept_bodies):
                body = {"seq": i, "ts": b["ts"], "type": b["type"], "data": b["data"]}
                rec = {**body, "prev": prev}
                rec["hash"] = prev = _hash(prev, body)
                rebuilt.append(rec)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                for rec in rebuilt:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            self.records = rebuilt
            return removed

    def verify(self) -> Dict[str, Any]:
        """Checks the file as it is on disk now."""
        problems: List[str] = []
        prev, count, lines = GENESIS, 0, 0
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                raw = [ln.strip() for ln in f if ln.strip()]
        except OSError:
            raw = []
        for i, line in enumerate(raw):
            lines += 1
            try:
                rec = json.loads(line)
                body = {k: rec[k] for k in ("seq", "ts", "type", "data")}
                if rec["seq"] != i:
                    problems.append(f"line {i + 1}: sequence number {rec['seq']}, expected {i}")
                if rec["prev"] != prev:
                    problems.append(f"line {i + 1}: the link to the previous line is broken")
                if _hash(rec["prev"], body) != rec["hash"]:
                    problems.append(f"line {i + 1}: the content does not match its hash (edited)")
                prev = rec["hash"] if isinstance(rec["hash"], str) else prev
            except (ValueError, KeyError, TypeError):
                problems.append(f"line {i + 1}: unreadable or incomplete")
            else:
                count += 1
        compactions = sum(1 for r in self.records if r["type"] == "compacted")
        return {"ok": not problems, "records": lines, "valid_records": count, "head": prev, "problems": problems[:50],
                "problem_count": len(problems), "compactions": compactions}


# ---------------------------------------------------------------- settings and exceptions

class Store:
    """netwatch.json: the settings and the exceptions, written atomically."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()
        self.settings: Dict[str, Any] = dict(DEFAULTS)
        self.exceptions: List[Dict[str, Any]] = []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.settings.update(self.check_settings(data.get("settings") or {}, strict=False))
                self.exceptions = [e for e in data.get("exceptions", []) if isinstance(e, dict) and "id" in e]
        except (OSError, ValueError):
            pass

    @staticmethod
    def check_settings(patch: Dict[str, Any], strict: bool = True) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, value in patch.items():
            if key not in DEFAULTS:
                if strict:
                    raise NetwatchError(f"Unknown setting: {key}")
                continue
            try:
                if key in ("enabled", "alert_on_system_wide"):
                    if not isinstance(value, bool):
                        raise ValueError("must be true or false")
                elif key in ("interval_s", "max_log_mb"):
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise ValueError("must be a number")
                    if key == "interval_s" and not 0.5 <= value <= 3600:
                        raise ValueError("must be between 0.5 and 3600 seconds")
                    if key == "max_log_mb" and value < 0:
                        raise ValueError("must be 0 (no limit) or more")
                elif key in ("watch_extra_names", "expected_folders"):
                    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                        raise ValueError("must be a list of text")
                    value = [v.strip() for v in value if v.strip()]
                elif not isinstance(value, str):
                    raise ValueError("must be text")
            except ValueError as exc:
                if strict:
                    raise NetwatchError(f"{key} {exc}") from None
                continue
            out[key] = value
        return out

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps({"settings": self.settings, "exceptions": self.exceptions}, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)


def check_exception(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise NetwatchError("Send an exception as an object.")
    kind = raw.get("kind")
    if kind not in ("behavior", "resource"):
        raise NetwatchError('The kind of an exception is "behavior" or "resource".')
    pattern = raw.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise NetwatchError("The pattern of an exception is a non-empty regular expression.", "bad_pattern")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise NetwatchError(f"Invalid regular expression: {exc}", "bad_pattern") from None
    note = raw.get("note", "")
    if not isinstance(note, str):
        raise NetwatchError("The note is text.")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise NetwatchError("enabled is true or false.")
    return {"kind": kind, "pattern": pattern, "note": note, "enabled": enabled}


# ---------------------------------------------------------------- the monitor

class NetWatch:
    def __init__(self, data_dir: Optional[Path] = None, source: Any = None, resolver: Optional[Callable[[str], Optional[str]]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        if data_dir is None:
            from serve import userdata
            data_dir = userdata.user_data_dir()
        self.dir = Path(data_dir)
        self.clock = clock
        self.store = Store(self.dir / "netwatch.json")
        self.log = ChainLog(self.dir / "netwatch.log.jsonl")
        self.lock = threading.RLock()
        self.source = source if source is not None else PsutilSource(lambda: self.store.settings["watch_extra_names"],
                                                                    lambda: self.store.settings["alert_on_system_wide"])
        self.resolver = resolver if resolver is not None else HostResolver()
        self.started = clock()
        self.events: Dict[str, Dict[str, Any]] = {}
        self.resolved: Dict[str, Dict[str, Any]] = {}
        self._replay()
        self.thread: Optional[threading.Thread] = None
        self.stop_flag = threading.Event()
        self.seen: Dict[Any, float] = {}
        self.pending: Dict[Any, Tuple[float, Dict[str, Any]]] = {}
        self.sizes: Dict[Tuple[int, str], int] = {}
        self.disk_seen: set = set()
        self.env_cache: Dict[int, Dict[str, str]] = {}
        self.tool_calls: collections.deque = collections.deque(maxlen=200)
        self.tool_calls_seen = False
        self.slow_at = 0.0
        self.baseline: Optional[Dict[str, set]] = None
        self.persist_seen: set = set()
        self.listeners: List[Dict[str, Any]] = []
        self.system_proxy = False
        self.counters = {"polls": 0, "events": 0, "suspicious": 0, "suppressed": 0, "errors": 0}
        self.last_poll = 0.0
        self.last_error = ""
        self.watched: List[Dict[str, Any]] = []
        self.geo = GeoDb("")
        self._geo_path: Optional[str] = None
        self.expected_cache: Optional[List[str]] = None

    # -- state from the log
    def _replay(self) -> None:
        for rec in self.log.records:
            data = rec["data"]
            if rec["type"] == "event" and isinstance(data, dict) and data.get("id"):
                self.events[data["id"]] = data
            elif rec["type"] == "resolved" and isinstance(data, dict):
                self.resolved[data.get("id", "")] = data

    # -- thread
    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        with self.lock:
            if self.running:
                return
            self.stop_flag.clear()
            self.started = self.clock()
            self.baseline = None
            self.thread = threading.Thread(target=self._run, daemon=True, name="netwatch")
            self.thread.start()

    def stop(self) -> None:
        self.stop_flag.set()
        t = self.thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)
        self.thread = None

    def _run(self) -> None:
        while not self.stop_flag.is_set():
            self.poll_once()
            self.stop_flag.wait(float(self.store.settings["interval_s"]))

    def note_tool_call(self, ts: float, tool: str, target: str) -> None:
        """The server tells the monitor about each outward tool call, so a connection of the engine with no such call can be tagged."""
        self.tool_calls_seen = True
        self.tool_calls.append((float(ts), str(tool), str(target or "")))

    def note_tool_calls(self, req: Any) -> Dict[str, Any]:
        """The same, as sent by an agent process over HTTP: {ts, tool, target}. A time in the future is taken as now."""
        if not isinstance(req, dict) or not isinstance(req.get("tool"), str) or not req["tool"]:
            raise NetwatchError("Send {ts, tool, target}.")
        now = self.clock()
        try:
            ts = min(float(req.get("ts") or now), now)
        except (TypeError, ValueError):
            raise NetwatchError("ts is a number.") from None
        target = req.get("target")
        self.note_tool_call(ts, req["tool"][:80], target[:2000] if isinstance(target, str) else "")
        return {"noted": True}

    # -- settings
    def settings(self) -> Dict[str, Any]:
        return dict(self.store.settings)

    def update_settings(self, patch: Any) -> Dict[str, Any]:
        if not isinstance(patch, dict):
            raise NetwatchError("Send the settings as an object.")
        clean = Store.check_settings(patch)
        with self.lock:
            was = self.store.settings["enabled"]
            self.store.settings.update(clean)
            self.store.save()
            self.expected_cache = None
        if self.store.settings["enabled"] and not was:
            self.start()
        elif was and not self.store.settings["enabled"]:
            self.stop()
        return self.settings()

    def apply(self) -> None:
        """Starts the thread when the saved settings say so."""
        if self.store.settings["enabled"]:
            self.start()

    def expected_folders(self) -> List[str]:
        if self.expected_cache is None:
            self.expected_cache = default_expected_folders() + list(self.store.settings["expected_folders"])
        return self.expected_cache

    # -- one poll
    def poll_once(self) -> List[Dict[str, Any]]:
        """One pass over the watched processes; returns the events it made. Never raises."""
        made: List[Dict[str, Any]] = []
        now = self.clock()
        try:
            self.counters["polls"] += 1
            self.last_poll = now
            self._load_geo()
            if now - self.slow_at >= SLOW_CHECK_S or self.slow_at == 0:
                self.slow_at = now
                self.listeners = _safe(self.source.listeners, []) if hasattr(self.source, "listeners") else []
                self.system_proxy = self._system_proxy()
                made += self._persistence(now)
            procs = _safe(self.source.processes, [])
            self.watched = procs
            alive = {p["pid"] for p in procs}
            for pid in list(self.env_cache):
                if pid not in alive:
                    del self.env_cache[pid]
            for proc in procs:
                made += self._network(proc, now)
                made += self._disk(proc, now)
            made += self._release_pending(now)
            for key in [k for k, t in self.seen.items() if now - t > SEEN_FORGET_S]:
                del self.seen[key]
        except Exception as exc:                        # the monitor must not stop on a bad poll
            self.counters["errors"] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
        return made

    def _load_geo(self) -> None:
        path = self.store.settings["geo_db_path"]
        if path != self._geo_path:
            self._geo_path = path
            self.geo = GeoDb(path)

    def _system_proxy(self) -> bool:
        if os.name != "nt":
            return False

        def read() -> bool:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
                return bool(winreg.QueryValueEx(k, "ProxyEnable")[0])
        return bool(_safe(read, False))

    def _proxy_env(self, pid: int) -> bool:
        if pid not in self.env_cache:
            self.env_cache[pid] = _safe(lambda: self.source.environ(pid), {})
        env = {k.upper(): v for k, v in self.env_cache[pid].items()}
        return any(env.get(k) for k in PROXY_ENV)

    def _local_proxy(self) -> bool:
        return any(l["port"] in PROXY_PORTS and classify_remote(l["ip"]) == "ignore" for l in self.listeners)

    def _tool_call_covers(self, now: float, ip: str, host: str) -> bool:
        for ts, _tool, target in self.tool_calls:
            if now - ts <= TOOL_CALL_WINDOW_S and ((ip and ip in target) or (host and host in target)):
                return True
        return False

    def _network(self, proc: Dict[str, Any], now: float) -> List[Dict[str, Any]]:
        pid = proc["pid"]
        out: List[Dict[str, Any]] = []
        for c in _safe(lambda: self.source.connections(pid), []):
            ip, port = c["raddr"]
            if classify_remote(ip) == "ignore":
                continue
            key = (pid, c["proto"], tuple(c["laddr"]), (ip, port), c["status"])
            if key in self.seen or key in self.pending:
                self.seen[key] = now
                continue
            host = self.resolver(ip)
            conn = {"proc": proc, "conn": c, "ip": ip, "port": port}
            if host is None:
                self.pending[key] = (now, conn)
                continue
            self.seen[key] = now
            out.append(self._network_event(conn, host, now))
        return [e for e in out if e]

    def _release_pending(self, now: float) -> List[Dict[str, Any]]:
        out = []
        for key, (since, conn) in list(self.pending.items()):
            host = self.resolver(conn["ip"])
            if host is None and now - since < RESOLVE_GRACE_S:
                continue
            del self.pending[key]
            self.seen[key] = now
            ev = self._network_event(conn, host or "", now)
            if ev:
                out.append(ev)
        return out

    def _network_event(self, conn: Dict[str, Any], host: str, now: float) -> Optional[Dict[str, Any]]:
        proc, c, ip, port = conn["proc"], conn["conn"], conn["ip"], conn["port"]
        tags = set()
        lowered = (host or "").lower()
        if any(m in lowered for m in ALIBABA_MARKERS):
            tags.add("alibaba_cn_domain")
        if self._proxy_env(proc["pid"]):
            tags.add("proxy_env")
        if self.system_proxy:
            tags.add("system_proxy")
        if self._local_proxy():
            tags.add("local_proxy_listener")
        if port in DOH_PORTS or lowered in DOH_HOSTS or any(lowered.endswith("." + h) for h in DOH_HOSTS):
            tags.add("doh_or_tor")
        if proc["role"] == "engine" and self.tool_calls_seen and not self._tool_call_covers(now, ip, lowered):
            tags.add("no_agent_call")
        asn, country = self.geo.lookup(ip) if self.geo.count else ("", "")
        public = classify_remote(ip) == "public"
        suspicious = bool(tags & ALERT_TAGS) or (proc["role"] in ALERTING_ROLES and public)
        return self._emit({"kind": "network", "pid": proc["pid"], "process": stem(proc["name"]) or proc["name"], "role": proc["role"],
                           "proto": c["proto"], "remote_ip": ip, "remote_port": port, "host": host or "", "asn": asn, "country": country,
                           "local": f"{c['laddr'][0]}:{c['laddr'][1]}", "status": c["status"], "tags": sorted(tags)}, suspicious, now)

    def _write_evidence(self, pid: int, path: str, mode: str) -> Tuple[bool, Optional[os.stat_result]]:
        try:
            st = os.stat(path)
        except OSError:
            return False, None
        if not os.path.isfile(path):
            return False, None
        before = self.sizes.get((pid, path))
        self.sizes[(pid, path)] = st.st_size
        if any(ch in (mode or "") for ch in "wa+"):
            return True, st
        if before is not None:
            return st.st_size > before, st
        return st.st_mtime >= self.started - 1, st

    def _hidden(self, path: str, st: os.stat_result) -> bool:
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & 2) if os.name == "nt" else os.path.basename(path).startswith(".")

    def _disk(self, proc: Dict[str, Any], now: float) -> List[Dict[str, Any]]:
        pid, out = proc["pid"], []
        files = _safe(lambda: self.source.open_files(pid), [])
        folders = self.expected_folders()
        for f in files:
            path = f["path"]
            tail = path[2:] if len(path) > 2 and path[1] == ":" else path
            stream = ":" in tail
            base = path.split(":", 1)[0] if stream and len(path) > 2 and path[1] != ":" else path
            wrote, st = self._write_evidence(pid, base if stream else path, f.get("mode", ""))
            if st is None and not stream:
                continue
            outside = not _within(path, folders)
            tags = set()
            ext = path.rsplit(".", 1)[-1].lower() if "." in os.path.basename(path) else ""
            if outside and wrote:
                tags.add("disk_outside_expected")
                if ext in ARCHIVE_EXT:
                    tags.add("archive_or_staging")
            if outside and (stream or (st is not None and self._hidden(path, st) and st.st_mtime >= self.started - 1)):
                tags.add("archive_or_staging")
            if not tags:
                continue
            key = (pid, path, tuple(sorted(tags)))
            if key in self.disk_seen:
                continue
            self.disk_seen.add(key)
            out.append(self._emit({"kind": "disk", "pid": pid, "process": stem(proc["name"]) or proc["name"], "role": proc["role"],
                                   "proto": "file", "remote_ip": "", "remote_port": 0, "host": path, "asn": "", "country": "",
                                   "tags": sorted(tags)}, True, now))
        return [e for e in out if e]

    def _persistence(self, now: float) -> List[Dict[str, Any]]:
        if os.name != "nt":
            return []
        current = {"run": _safe(self._run_values, set()), "tasks": _safe(self._task_names, set())}
        if self.baseline is None:
            self.baseline = current
            return []
        out = []
        for what, label in (("run", "run_key"), ("tasks", "scheduled_task")):
            for name in sorted(current[what] - self.baseline[what]):
                if (what, name) in self.persist_seen:
                    continue
                self.persist_seen.add((what, name))
                out.append(self._emit({"kind": "persistence", "pid": 0, "process": "(system)", "role": "other", "proto": label,
                                       "remote_ip": "", "remote_port": 0, "host": name, "asn": "", "country": "",
                                       "tags": ["persistence"]}, True, now))
        self.baseline = {k: self.baseline[k] | current[k] for k in current}
        return [e for e in out if e]

    @staticmethod
    def _run_values() -> set:
        import winreg
        found = set()
        for sub, hive in RUN_KEYS:
            root = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
            try:
                with winreg.OpenKey(root, sub) as k:
                    i = 0
                    while True:
                        name, value, _ = winreg.EnumValue(k, i)
                        found.add(f"{hive}\\{sub}\\{name} = {value}")
                        i += 1
            except OSError:
                continue
        return found

    @staticmethod
    def _task_names() -> set:
        base = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "Tasks"
        found = set()
        for dirpath, _dirs, files in os.walk(base, onerror=lambda e: None):
            for name in files:
                found.add(os.path.relpath(os.path.join(dirpath, name), base))
        return found

    # -- events, exceptions, alerts
    def _suppressor(self, behavior: str, resource: str) -> Optional[str]:
        for exc in self.store.exceptions:
            if not exc.get("enabled", True):
                continue
            text = behavior if exc["kind"] == "behavior" else resource
            if _safe(lambda: re.search(exc["pattern"], text) is not None, False):
                return exc["id"]
        return None

    def _emit(self, ev: Dict[str, Any], suspicious: bool, now: float) -> Dict[str, Any]:
        with self.lock:
            behavior = behavior_string(ev["process"], ev["proto"], ev["remote_port"], ev["tags"])
            resource = resource_string(ev["remote_ip"], ev["host"])
            by = self._suppressor(behavior, resource)
            ev.update({"id": uuid.uuid4().hex[:12], "ts": now, "behavior": behavior, "resource": resource, "suspicious": suspicious,
                       "suppressed": by is not None, "manual": False})
            if by:
                ev["suppressed_by"] = by
            self._store_event(ev)
            self.counters["events"] += 1
            self.counters["suspicious"] += suspicious and not by
            self.counters["suppressed"] += by is not None
            return ev

    def _store_event(self, ev: Dict[str, Any]) -> None:
        self.log.append("event", ev, ev["ts"])
        self.events[ev["id"]] = ev
        limit = self.store.settings["max_log_mb"]
        if limit and self.log.size() > limit * 1048576:
            self._trim(int(limit * 1048576 * 0.9))

    def _trim(self, target: int) -> None:
        while self.log.size() > target and self.events:
            oldest = sorted(self.events.values(), key=lambda e: e["ts"])[:max(1, len(self.events) // 10)]
            self._delete([e["id"] for e in oldest], "max_log_mb")

    def alert_state(self, ev: Dict[str, Any]) -> str:
        if ev.get("manual") or not ev.get("suspicious") or ev.get("suppressed"):
            return ""
        return "resolved" if ev["id"] in self.resolved else "open"

    def view(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        out = {**ev, "alert_state": self.alert_state(ev), "suggest": {"behavior_pattern": re.escape(ev["behavior"]), "resource_pattern": re.escape(ev["resource"])}}
        if ev["id"] in self.resolved:
            out["resolution"] = self.resolved[ev["id"]]
        return out

    def events_list(self, since: float = 0.0, limit: int = 200, only: str = "all") -> List[Dict[str, Any]]:
        with self.lock:
            rows = [e for e in self.events.values() if e["ts"] > since and (only != "suspicious" or e.get("suspicious"))]
            rows.sort(key=lambda e: (e["ts"], e["id"]), reverse=True)
            return [self.view(e) for e in rows[:max(1, limit)]]

    def alerts(self) -> List[Dict[str, Any]]:
        with self.lock:
            rows = sorted((e for e in self.events.values() if self.alert_state(e) == "open"), key=lambda e: e["ts"])
            return [self.view(e) for e in rows]

    def suggestions(self, event_id: str) -> Dict[str, str]:
        ev = self.events.get(event_id)
        if ev is None:
            raise NetwatchError("That report is not there any more.", "gone")
        return {"behavior_pattern": re.escape(ev["behavior"]), "resource_pattern": re.escape(ev["resource"])}

    def resolve(self, event_id: str, req: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            ev = self.events.get(event_id)
            if ev is None:
                raise NetwatchError("That alert is not there any more.", "gone")
            note = req.get("note", "")
            if not isinstance(note, str):
                raise NetwatchError("The note is text.")
            wanted = []
            for flag, pattern, kind in (("add_behavior", "behavior_pattern", "behavior"), ("add_resource", "resource_pattern", "resource")):
                if req.get(flag) is True:
                    wanted.append(check_exception({"kind": kind, "pattern": req.get(pattern), "note": note}))
            added = [self._add_exception(w, log=False) for w in wanted]
            data = {"id": event_id, "action": "exceptions" if added else "dismissed", "exception_ids": [a["id"] for a in added], "note": note}
            self.log.append("resolved", data, self.clock())
            self.resolved[event_id] = data
            return {"resolved": data, "exceptions": added}

    def exceptions_list(self) -> List[Dict[str, Any]]:
        return [dict(e) for e in self.store.exceptions]

    def _add_exception(self, fields: Dict[str, Any], log: bool = True) -> Dict[str, Any]:
        exc = {"id": uuid.uuid4().hex[:10], **fields, "created_ts": self.clock()}
        self.store.exceptions.append(exc)
        self.store.save()
        if log:
            self.log.append("exception", {"action": "add", "id": exc["id"], "kind": exc["kind"], "pattern": exc["pattern"]}, self.clock())
        return exc

    def save_exception(self, raw: Any) -> Dict[str, Any]:
        fields = check_exception(raw)
        with self.lock:
            eid = raw.get("id")
            if eid:
                for exc in self.store.exceptions:
                    if exc["id"] == eid:
                        exc.update(fields)
                        self.store.save()
                        self.log.append("exception", {"action": "update", "id": eid, "kind": exc["kind"], "pattern": exc["pattern"]}, self.clock())
                        return dict(exc)
                raise NetwatchError("That exception is not there any more.", "gone")
            return dict(self._add_exception(fields))

    def delete_exception(self, eid: Any) -> None:
        with self.lock:
            before = len(self.store.exceptions)
            self.store.exceptions = [e for e in self.store.exceptions if e["id"] != eid]
            if len(self.store.exceptions) == before:
                raise NetwatchError("That exception is not there any more.", "gone")
            self.store.save()
            self.log.append("exception", {"action": "delete", "id": eid}, self.clock())

    # -- manual entries and deletion
    def add_manual(self, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise NetwatchError("Send the entry as an object.")
        text = lambda k: str(raw.get(k) or "").strip()
        process, proto, ip, host = text("process"), text("proto") or "tcp", text("remote_ip"), text("host")
        if not (process or ip or host):
            raise NetwatchError("Name at least the process, the address or the host.")
        try:
            port = int(raw.get("remote_port") or 0)
        except (TypeError, ValueError):
            raise NetwatchError("The port is a number.") from None
        tags = raw.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise NetwatchError("The tags are a list of text.")
        ev = {"kind": "manual", "pid": 0, "process": process or "(unknown)", "role": "other", "proto": proto, "remote_ip": ip,
              "remote_port": port, "host": host, "asn": "", "country": "", "tags": sorted(set(tags)), "note": text("note")[:2000]}
        now = self.clock()
        with self.lock:
            ev.update({"id": uuid.uuid4().hex[:12], "ts": now, "behavior": behavior_string(ev["process"], proto, port, ev["tags"]),
                       "resource": resource_string(ip, host), "suspicious": True, "suppressed": False, "manual": True})
            self._store_event(ev)
            return self.view(ev)

    def delete_events(self, req: Any) -> Dict[str, Any]:
        if not isinstance(req, dict):
            raise NetwatchError("Send an object.")
        with self.lock:
            if req.get("all") is True:
                ids = list(self.events)
            elif isinstance(req.get("ids"), list):
                ids = [i for i in req["ids"] if isinstance(i, str)]
            elif req.get("older_than_days") is not None:
                days = req["older_than_days"]
                if isinstance(days, bool) or not isinstance(days, (int, float)) or days < 0:
                    raise NetwatchError("older_than_days is a number of days.")
                cut = self.clock() - days * 86400
                ids = [i for i, e in self.events.items() if e["ts"] < cut]
            else:
                raise NetwatchError("Give ids, all or older_than_days.")
            return {"deleted": self._delete(ids, "requested")}

    def _delete(self, ids: Iterable[str], reason: str) -> int:
        with self.lock:
            gone = sorted(i for i in set(ids) if i in self.events)
            if not gone:
                return 0
            gone_set = set(gone)
            now = self.clock()
            self.log.append("deleted", {"ids": gone, "count": len(gone), "reason": reason}, now)
            self.log.compact(lambda r: r["type"] in ("event", "resolved") and isinstance(r["data"], dict) and r["data"].get("id") in gone_set,
                             {"reason": reason, "removed_ids": gone}, now)
            for i in gone:
                self.events.pop(i, None)
                self.resolved.pop(i, None)
            return len(gone)

    # -- status and export
    def verify(self) -> Dict[str, Any]:
        with self.lock:
            return self.log.verify()

    def export(self) -> Dict[str, Any]:
        with self.lock:
            return {"exported_ts": self.clock(), "events": self.events_list(0, 10 ** 9, "all"), "exceptions": self.exceptions_list(),
                    "verify": self.log.verify(), "head": self.log.head}

    def status(self) -> Dict[str, Any]:
        psutil_ok = bool(getattr(self.source, "available", True))
        with self.lock:
            open_alerts = sum(1 for e in self.events.values() if self.alert_state(e) == "open")
        geo = f"{self.geo.count} ranges" if self.geo.count else (self.geo.error or "no geo database")
        return {
            "enabled": bool(self.store.settings["enabled"]), "running": self.running, "settings": self.settings(),
            "psutil": psutil_ok,
            "unavailable": "" if psutil_ok else "psutil is not installed, so processes and connections cannot be read.",
            "coverage": {"mode": "connection_table", "admin": is_admin(), "note": COVERAGE_NOTE},
            "watched": [{"pid": p["pid"], "name": p["name"], "role": p["role"]} for p in self.watched],
            "open_alerts": open_alerts, "counters": dict(self.counters), "geo": geo, "last_error": self.last_error,
            "last_poll": self.last_poll, "tool_calls_reported": self.tool_calls_seen,
            "expected_folders": self.expected_folders(), "engine_names": list(ENGINE_NAMES),
            "log": {"records": len(self.log.records), "bytes": self.log.size(), "head": self.log.head},
        }
