"""Billion Context in the chat: a context-compression proxy that Basalt starts, owns and stops.

The proxy is the Node.js add-on shipped with the app. Streaming chat requests that opt in (the web chat and the agent's
main loop send the header X-Basalt-Compress) are handed to it as
    http://127.0.0.1:<port>/bili/http://127.0.0.1:<server port>/v1/chat/completions
and it relays them back to this same server, compressing a long conversation on the way. Whatever goes wrong (add-on
missing, process dead, a refusal before the first byte) the request is answered directly instead: the proxy never
blocks or fails a chat. Requests for cloud providers, with their keys, never pass through it.

The setting is billion-context.json in the user data folder:  {"enabled": true, "port": 18788}
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

FILE = "billion-context.json"
CONFIG_NAME = "proxy-config.json"                 # the proxy's own configuration, written by Basalt at each start
DEFAULT_PORT = 18788
OPT_HEADER = "X-Basalt-Compress"                 # a client that wants its streamed chat compressed
HOP_HEADER = "X-Basalt-Bili-Hop"                 # set on the request the proxy relays back, so it is not sent round again
PROXY_HOP = "x-bili-hop"                         # the proxy's own mark on what it relays
HEALTH_WAIT_S = 25.0
POLL_S = 2.0
BACKOFF_FIRST_S = 2.0
BACKOFF_MAX_S = 300.0
STABLE_S = 60.0                                  # a proxy that ran this long starts the waits over
STOP_WAIT_S = 4.0
# Basalt's own compaction (Chat settings > When the context is full) acts at 80% of the context by default, so the proxy's
# nudge to compress starts above that: it is the safety net for conversations that reach it, the agent's among them.
CONFIG = {"compress": {"maxContextLimit": "90%", "emergencyThresholdPercent": "97%"}}
CONTEXT_CHANGE = 0.1                             # a new model context this much different restarts the proxy when idle
_lock = threading.Lock()


def file_path() -> Path:
    from serve import userdata
    return userdata.user_data_dir() / FILE


def state_dir() -> Path:
    from serve import userdata
    return userdata.user_data_dir() / "billion-context"


def load() -> Dict[str, Any]:
    """The saved setting; a missing or damaged file gives the defaults (on, port 18788)."""
    out = {"enabled": True, "port": DEFAULT_PORT}
    try:
        data = json.loads(file_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return out
    if isinstance(data, dict):
        if isinstance(data.get("enabled"), bool):
            out["enabled"] = data["enabled"]
        try:
            out["port"] = check_port(data.get("port", DEFAULT_PORT))
        except ValueError:
            pass
    return out


def check_port(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("the port is a whole number from 1024 to 65535")
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("the port is a whole number from 1024 to 65535") from None
    if not 1024 <= port <= 65535:
        raise ValueError("the port is a whole number from 1024 to 65535")
    return port


def save(values: Dict[str, Any]) -> Dict[str, Any]:
    """Merge `enabled` and `port` into the saved setting (atomic write); ValueError for a bad value."""
    with _lock:
        cur = load()
        if "enabled" in values:
            if not isinstance(values["enabled"], bool):
                raise ValueError("enabled is true or false")
            cur["enabled"] = values["enabled"]
        if "port" in values:
            cur["port"] = check_port(values["port"])
        p = file_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return cur


def _addon_command() -> Optional[List[str]]:
    from serve import addons
    node, entry = addons.node_exe(), addons.npm_entry("billion-context", "dist/index.js")
    return [str(node), str(entry)] if node and entry else None


def _missing() -> str:
    from serve import addons
    if addons.addons_dir() is None:
        return "the add-ons are not installed"
    if addons.node_exe() is None:
        return "Node.js is missing from the add-ons"
    return "Billion Context is missing from the add-ons"


def _http_json(port: int, path: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """GET a JSON document from the proxy; None when it does not answer or answers something else."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            data = resp.read(1 << 22)
        finally:
            conn.close()
        if resp.status != 200:
            return None
        out = json.loads(data.decode("utf-8", "replace"))
        return out if isinstance(out, dict) else None
    except (OSError, ValueError, http.client.HTTPException):
        return None


def _start_process(argv: Sequence[str], env: Dict[str, str], log: Any) -> "subprocess.Popen[bytes]":
    kw: Dict[str, Any] = {}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(list(argv), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kw)


class Manager:
    """Starts the proxy when it is on and the add-on is there, keeps it running, stops it with the server.

    `command` returns the argv to run (None when the add-on is missing), `launch(argv, env, log)` starts it, `probe(port,
    path)` asks it for a JSON document, `context` returns the model's context in tokens (0 when not known): the tests
    replace them."""

    def __init__(self, command: Callable[[], Optional[List[str]]] = _addon_command,
                 launch: Callable[..., Any] = _start_process,
                 probe: Callable[[int, str], Optional[Dict[str, Any]]] = _http_json,
                 context: Callable[[], int] = lambda: 0,
                 server_port: int = 0, poll_s: float = POLL_S, backoff_first_s: float = BACKOFF_FIRST_S) -> None:
        self.command, self.launch, self.probe, self.context = command, launch, probe, context
        self.server_port = server_port
        self.poll_s, self.backoff_first_s = poll_s, backoff_first_s
        self._cv = threading.Condition()
        self._proc: Any = None
        self._thread: Optional[threading.Thread] = None
        self._quit = False
        self._wake = False
        self._port = DEFAULT_PORT
        self._enabled = False
        self._healthy = False
        self._misses = 0
        self._status = "stopped"
        self._detail = ""
        self._since = 0.0
        self._started_ctx = 0
        self._restarts = 0
        self._active = 0
        self._log: Any = None

    # -- the supervisor ------------------------------------------------------------------------------------------
    def apply(self) -> None:
        """Read the saved setting and make the process match it (start, stop or move to another port)."""
        cfg = load()
        with self._cv:
            self._enabled, self._port = cfg["enabled"], cfg["port"]
            self._wake = True
            if self._enabled and (self._thread is None or not self._thread.is_alive()):
                self._quit = False
                self._thread = threading.Thread(target=self._run, daemon=True, name="billion-context")
                self._thread.start()
            elif not self._enabled:
                self._status, self._detail = "disabled", ""
            self._cv.notify_all()

    def restart(self) -> None:
        with self._cv:
            self._restarts = 0
        self._terminate()
        self.apply()

    def stop(self) -> None:
        with self._cv:
            self._quit = True
            self._cv.notify_all()
        self._terminate()
        self._set("stopped")
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(STOP_WAIT_S + 1)

    def _set(self, status: str, detail: str = "", healthy: bool = False) -> None:
        with self._cv:
            self._status, self._detail, self._healthy = status, detail, healthy

    def _wait(self, seconds: float) -> bool:
        """Sleep until the time is up or something changes; True when the supervisor should look again."""
        with self._cv:
            if not (self._wake or self._quit):
                self._cv.wait(seconds)
            woke, self._wake = self._wake, False
            return woke or self._quit

    def _env(self, port: int) -> Dict[str, str]:
        base = state_dir()
        env = dict(os.environ)
        for k in [k for k in env if k.startswith(("ACP_", "BILI_"))]:
            del env[k]
        env.update({
            "ACP_PORT": str(port), "ACP_HOST": "127.0.0.1", "BILI_STRICT_PORT": "1",
            "BILI_CONFIG_FILE": str(base / CONFIG_NAME), "ACP_AUTO_UPDATE": "0", "BILI_ADVISORY_CHECK": "0", "BILI_RELEASE_NOTES_CHECK": "0",
            "XDG_CONFIG_HOME": str(base / "config"), "XDG_DATA_HOME": str(base / "data"), "XDG_STATE_HOME": str(base / "state"),
        })
        ctx = self._started_ctx
        if ctx > 0:
            env["ACP_MODEL_CONTEXT_LIMIT"] = str(ctx)
        return env

    def _spawn(self) -> str:
        """Start the process: "ok" when it answers, "now" to look again at once, "later" after a failure (the status says why)."""
        argv = self.command()
        if not argv:
            self._set("not_installed", _missing())
            return "absent"
        port = self._port
        if self.server_port and port == self.server_port:
            self._set("error", "the port is the one of this server: choose another")
            return "later"
        for sub in ("config", "data", "state"):
            (state_dir() / sub).mkdir(parents=True, exist_ok=True)
        self._started_ctx = max(0, int(self.context() or 0))
        try:
            (state_dir() / CONFIG_NAME).write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
            self._log = open(state_dir() / "proxy.log", "ab")
            proc = self.launch(argv, self._env(port), self._log)
        except OSError as exc:
            self._set("error", f"could not start: {exc}")
            return "later"
        self._proc, self._since = proc, time.monotonic()
        self._set("starting")
        deadline = time.monotonic() + HEALTH_WAIT_S
        while time.monotonic() < deadline:
            if self._proc is not proc or self._quit or not self._enabled:
                return "now"                               # stopped or moved by the person while starting
            if proc.poll() is not None:
                self._proc = None
                self._set("error", f"stopped at start (code {proc.poll()}); is port {port} free?")
                return "later"
            h = self.probe(port, "/__bili/health")
            if h and h.get("ok") and (not h.get("pid") or h.get("pid") == getattr(proc, "pid", None)):
                self._set("running", healthy=True)
                return "ok"
            self._wait(0.2)
        self._set("error", "did not answer in time")
        self._terminate()
        return "later"

    def _run(self) -> None:
        wait = self.backoff_first_s
        while True:
            with self._cv:
                quit_, enabled = self._quit, self._enabled
            if quit_:
                return
            if not enabled:
                self._terminate()
                self._set("disabled")
                self._wait(self.poll_s * 5)
                continue
            if self._proc is not None and self._proc.poll() is not None:
                code, ran = self._proc.poll(), time.monotonic() - self._since
                self._proc = None
                if ran >= STABLE_S:
                    wait = self.backoff_first_s
                self._restarts += 1
                self._set("error", f"stopped (code {code}); starting again in {wait:.0f} s")
                if not self._wait(wait):
                    wait = min(wait * 2, BACKOFF_MAX_S)
                continue
            if self._proc is None:
                result = self._spawn()
                if result == "ok":
                    continue
                if result == "absent":
                    self._wait(30.0)
                elif result == "later":
                    self._restarts += 1
                    if not self._wait(wait):
                        wait = min(wait * 2, BACKOFF_MAX_S)
                continue
            self._check()
            self._wait(self.poll_s)

    def _check(self) -> None:
        ok = bool(self.probe(self._port, "/__bili/health"))
        self._misses = 0 if ok else self._misses + 1
        with self._cv:
            if ok:
                self._healthy = True
                if self._status != "running":
                    self._status, self._detail = "running", ""
            elif self._misses >= 2:                     # one slow answer on a busy PC is not a dead proxy
                self._healthy = False
                self._status, self._detail = "error", "does not answer"
        ctx = max(0, int(self.context() or 0))
        if ok and ctx and self._started_ctx and abs(ctx - self._started_ctx) / self._started_ctx > CONTEXT_CHANGE:
            with self._cv:
                idle = self._active == 0
            if idle:
                self._terminate()                      # started again at the next look with the new context

    def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        with self._cv:
            self._healthy = False
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(STOP_WAIT_S)
                except Exception:
                    proc.kill()
                    proc.wait(STOP_WAIT_S)
        except Exception:
            pass
        finally:
            if self._log is not None:
                try:
                    self._log.close()
                except OSError:
                    pass
                self._log = None

    # -- the page and the routing --------------------------------------------------------------------------------
    def target(self) -> Optional[int]:
        """The proxy's port when requests may be sent through it now, else None."""
        with self._cv:
            return self._port if self._enabled and self._healthy and self._proc is not None else None

    def enter(self) -> None:
        with self._cv:
            self._active += 1

    def leave(self) -> None:
        with self._cv:
            self._active = max(0, self._active - 1)

    def failed(self) -> None:
        """A request could not reach the proxy: the supervisor looks at it again at once."""
        with self._cv:
            self._healthy = False
            self._wake = True
            self._cv.notify_all()

    def view(self, with_stats: bool = True) -> Dict[str, Any]:
        cfg = load()
        with self._cv:
            out: Dict[str, Any] = {"enabled": cfg["enabled"], "port": cfg["port"], "status": self._status,
                                   "detail": self._detail, "running": self._healthy, "restarts": self._restarts,
                                   "default_port": DEFAULT_PORT}
            port = self._port
        if not cfg["enabled"]:
            out["status"], out["running"] = "disabled", False
        out["installed"] = self.command() is not None
        if with_stats and out["running"]:
            out["stats"] = summarize(self.probe(port, "/__bili/stats"))
        return out


def _count(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def summarize(stats: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The numbers the settings page shows from the proxy's /__bili/stats."""
    if not isinstance(stats, dict):
        return {}
    rows = [s for s in stats.get("sessions") or [] if isinstance(s, dict)]
    out: Dict[str, Any] = {"sessions": len(rows), "requests": sum(_count(s.get("requests")) for s in rows)}
    if rows:
        last = max(rows, key=lambda s: str(s.get("lastSeen") or ""))
        for key, name in (("contextTokens", "context_tokens"), ("contextWindow", "context_window")):
            if isinstance(last.get(key), (int, float)) and not isinstance(last.get(key), bool):
                out[name] = int(last[key])
    return out


# -- the single instance of the server ----------------------------------------------------------------------------
_MANAGER: Optional[Manager] = None


def get() -> Manager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = Manager()
    return _MANAGER


def start(server_port: int, context: Callable[[], int]) -> None:
    """Called once the server listens: the proxy starts now when it is on and installed."""
    try:
        m = get()
        m.server_port, m.context = server_port, context
        m.apply()
    except Exception:                                   # the proxy never keeps the server from starting
        pass


def stop() -> None:
    if _MANAGER is not None:
        _MANAGER.stop()


def set_enabled_port(values: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/billion-context: save, then make the process match. `restart` true starts it again."""
    old = load()
    save({k: values[k] for k in ("enabled", "port") if k in values})
    m = get()
    if values.get("restart") is True or load()["port"] != old["port"]:
        m.restart()
    else:
        m.apply()
    return m.view()


# -- the hand-over of one request -----------------------------------------------------------------------------------
def wants(headers: Any) -> bool:
    """Whether a request asked to be compressed and is not the one the proxy relays back."""
    return bool(headers.get(OPT_HEADER)) and not headers.get(HOP_HEADER) and not headers.get(PROXY_HOP)


def forward(handler: Any, raw: bytes, server_port: int) -> bool:
    """Send a streamed chat request through the proxy and copy its answer to the client.

    False when the request was not handed over (the caller answers it directly): not streaming, the proxy is not
    healthy, or it refused before the first byte. Once the first byte is out, the answer is the proxy's."""
    m = get()
    port = m.target()
    if port is None or b'"stream"' not in raw:
        return False
    try:
        if json.loads(raw).get("stream") is not True:
            return False
    except (ValueError, AttributeError):
        return False
    hdr = {"Content-Type": "application/json", HOP_HEADER: "1"}
    for name in ("Authorization", "x-api-key"):
        if handler.headers.get(name):
            hdr[name] = handler.headers[name]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    m.enter()
    gone = threading.Event()
    done = threading.Event()
    try:
        try:
            conn.connect()
            sock = conn.sock                            # the response takes the socket over; kept to stop it
            conn.request("POST", f"/bili/http://127.0.0.1:{server_port}/v1/chat/completions", raw, hdr)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException):
            m.failed()
            return False
        if resp.status != 200:
            resp.read(1 << 16)
            return False
        sock.settimeout(None)                      # a model may think for a long time without a byte

        handler._watch_client(gone)                     # the client hung up: close the upstream so the model stops

        def cancel() -> None:
            while not done.wait(0.3):
                if gone.is_set():
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return

        threading.Thread(target=cancel, daemon=True, name="bili-cancel").start()
        handler.send_response(200)
        handler.send_header("Content-Type", resp.getheader("Content-Type") or "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")
        handler._cors()
        handler.end_headers()
        try:
            while True:
                block = resp.read1(65536)
                if not block:
                    break
                handler.wfile.write(block)
                handler.wfile.flush()
        except (OSError, http.client.HTTPException):
            pass                                        # the client left, or the proxy ended the stream early
        return True
    finally:
        done.set()
        if handler.watch_done is not None:
            handler.watch_done.set()
        try:
            conn.close()
        except OSError:
            pass
        m.leave()
