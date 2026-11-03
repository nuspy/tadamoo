"""The engine the Loader started in place of Basalt-Plus's own: Strata's Python server run as a managed child.

  HOST.activate(svc, engine_id, req)   stop Basalt-Plus's engine (or the other one), start this one, load the model
  HOST.stop()                          end the child and whatever it started
  HOST.status()                        {"engine", "phase": idle | starting | loading | running | error, "error", ...}
  HOST.forward(handler, method, path, body)   answer one /v1 request from the child (streams as it arrives)
  models(engine_id)                    the models the engine reads, found on this PC

One engine runs at a time. While the child runs, this server keeps its port and relays the OpenAI and Anthropic routes
to it, so the Chat, the agent and every other client see the same address. The child listens on a free loopback port."""
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

from serve import engine_install, engines, hardware
from serve.util.winjob import contain

READY_S = 120.0             # the child's server printing its ready file
LOAD_S = 900.0              # the model loading in it (Strata's own default wait)
PROXIED_GET = ("/v1/models", "/models", "/v1/status", "/props", "/slots")
PROXIED_POST = ("/v1/chat/completions", "/v1/messages", "/v1/messages/count_tokens", "/v1/responses",
                "/v1/images/generations")
HOP = {"connection", "transfer-encoding", "keep-alive", "server", "date", "content-length"}
# the engine flag each Loader control sets, per binary; a flag the binary lacks is refused with its name
FLAGS = {"max_context": "--max-context", "kv": "--kv", "spec": "--spec", "mtp": "--mtp",
         "expert_cache": "--expert-cache", "vram_reserve": "--vram-reserve-mib"}
DEEPSEEK_FLAGS = {"vram_reserve": "--reserve-mib"}


class Refused(Exception):
    """The engine cannot be started: HTTP status, a code the page translates, and the sentence."""

    def __init__(self, status: int, code: str, message: str, kind: str = "invalid_request_error"):
        super().__init__(message)
        self.status, self.code, self.message, self.kind = status, code, message, kind

    @property
    def body(self) -> dict:
        return {"error": {"type": self.kind, "code": self.code, "message": self.message}}


def _free_port() -> int:
    """A loopback port that is free and reachable: one that does not accept a connection to itself is skipped."""
    for _ in range(20):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
            except OSError:
                continue
            return port
    raise Refused(500, "spawn_failed", "no local port accepts a connection")


def _exe(name: str) -> Path:
    return engines.ROOT / "bin" / "strata" / (name + (".exe" if os.name == "nt" else ""))


def set_flag(args: list, flag: str, value) -> list:
    """`args` with `flag` set to `value` (replaced where present, else appended); a flag value of None removes it."""
    out, i = [], 0
    while i < len(args):
        if args[i] == flag:
            i += 2
            continue
        out.append(args[i])
        i += 1
    return out if value is None else [*out, flag, str(value)]


def _flags_ok(exe: Path, flags: list[str]) -> None:
    from serve import config
    known = config.engine_flags_of(str(exe))
    if known is None:
        return
    lacking = [f for f in flags if f not in known]
    if lacking:
        raise Refused(400, "flag_unsupported", f"{exe.stem} has no {', '.join(lacking)}")


def build_config(model: str, values: dict, port: int, family: str | None = None) -> tuple[dict, str]:
    """(the run config the child's server reads, the model family) for a Strata run config (.json) or a GGUF the
    Qwen3.6 binary reads; `values` are the Loader controls (serve/engines.py), set into the config's args."""
    path = Path(model)
    if not path.is_file():
        raise Refused(400, "model_missing", f"{model}: not a file on this PC")
    if path.suffix.lower() == ".json":
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise Refused(400, "config_unreadable", f"{model}: {e}") from None
        if not isinstance(cfg, dict) or not isinstance(cfg.get("args"), list):
            raise Refused(400, "config_unreadable", f"{model}: not a Strata run config (no args)")
        fam = cfg.get("arch") or "qwen4exp"
    elif path.suffix.lower() == ".gguf":
        fam = "qwen35moe"
        cfg = {"args": ["--model", str(path)], "arch": fam, "model_name": engines.FAMILIES[fam]["model_name"]}
    else:
        raise Refused(400, "model_unsupported", f"{model}: a Strata run config (.json) or a Qwen3.6 GGUF is needed")
    if fam not in engines.FAMILIES:
        raise Refused(400, "model_unsupported", f"{fam}: not a model family Strata reads here")
    spec = engines.FAMILIES[fam]
    exe = _exe(spec["exe"])
    if not exe.is_file():
        raise Refused(409, "binary_missing", f"{exe.name} is not in {exe.parent}: install the engine's files again")
    if family and family != fam:
        raise Refused(400, "family_mismatch", f"this model is {fam}, not {family}")
    try:
        values = engines.check_values("strata", values, fam)
    except ValueError as e:
        raise Refused(400, "control_unsupported", str(e)) from None
    args = list(cfg["args"])
    flags = []
    for name, value in values.items():
        if name == "tokenizer":
            continue
        if name == "parallel":
            cfg["parallel"] = value
            continue
        flag = (DEEPSEEK_FLAGS if fam == "deepseek41" else {}).get(name) or FLAGS[name]
        if name == "expert_cache" and fam == "deepseek41":
            args = set_flag(args, "--no-expert-cache", None)
            if value == 0:
                args.append("--no-expert-cache")
                flags.append("--no-expert-cache")
            continue
        args = set_flag(args, flag, value)
        flags.append(flag)
    _flags_ok(exe, flags)
    tok = values.get("tokenizer") or cfg.get("tokenizer")
    if not tok and path.suffix.lower() == ".gguf":
        for d in (path.parent / "tokenizer", path.parent.parent / "tokenizer"):
            if all((d / f).is_file() for f in ("vocab.json", "merges.txt", "token_type.json")):
                tok = str(d)
                break
    if not tok or not all((Path(tok) / f).is_file() for f in ("vocab.json", "merges.txt", "token_type.json")):
        raise Refused(400, "tokenizer_missing", "the model's tokenizer folder (with vocab.json, merges.txt and token_type.json) is needed: set it in the Strata tab")
    # the child listens on loopback only and this server checks the key, so the run config's own key and host go
    cfg = {k: v for k, v in cfg.items() if k not in ("api_key", "host", "port", "lib_dirs")}
    cfg.update(exe=str(exe), args=args, cwd=str(exe.parent), tokenizer=str(tok), port=port, host="127.0.0.1",
               arch=fam, model_name=cfg.get("model_name") or spec["model_name"])
    return cfg, fam


def _tail(path: Path, n: int = 6) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return " | ".join(l.strip() for l in lines[-n:] if l.strip())[-600:]


class Host:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.engine: str | None = None
        self.phase = "idle"
        self.error: dict | None = None
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.family = self.model = self.model_name = None
        self.since = 0.0
        self._gen = 0

    # -- state -------------------------------------------------------------------------------------------

    def status(self) -> dict:
        with self.lock:
            return {"engine": self.engine, "phase": self.phase, "error": self.error, "family": self.family,
                    "model": self.model, "model_name": self.model_name, "port": self.port}

    def active(self) -> bool:
        """A foreign engine owns the /v1 routes: it is starting, loading or serving."""
        with self.lock:
            return self.engine is not None and self.phase in ("starting", "loading", "running")

    # -- start / stop -------------------------------------------------------------------------------------

    def activate(self, svc, engine_id: str, req: dict, wait: bool = False) -> dict:
        """Start `engine_id` for `req` ({"model", "values", "family"}); Refused when it cannot. Returns the status once
        the child's server is starting; the model loads on a thread (`wait`: until it is ready)."""
        if engine_id != "strata":
            raise Refused(404, "unknown_engine", "not found")
        hw = hardware.machine()
        ok, code, text = engines.compatible(engine_id, hw)
        if not ok:
            raise Refused(409, code or "hardware", text or "this PC cannot run the engine")
        if not engines.installed(engine_id):
            raise Refused(409, "not_installed", "the engine is not installed: install it first")
        model = req.get("model")
        if not isinstance(model, str) or not model:
            raise Refused(400, "model_missing", "model: the Strata run config or GGUF to load")
        with self.lock:
            if self.phase in ("starting", "loading"):
                raise Refused(409, "busy", "an engine is loading already", "model_busy")
            port = _free_port()
            cfg, fam = build_config(model, req.get("values"), port, req.get("family"))
            self._stop_locked()
            # Basalt-Plus's own engine gives the GPU back first; a request still running there is not cut
            if svc is not None and svc.unload() == "busy":
                raise Refused(409, "busy", "Basalt-Plus is answering a request: try again when it has finished", "model_busy")
            self._gen += 1
            self.engine, self.phase, self.error = engine_id, "starting", None
            self.family, self.model, self.model_name, self.port = fam, model, cfg["model_name"], port
            self.since = time.time()
            gen = self._gen
            try:
                self._spawn(cfg, gen)
            except (OSError, Refused) as e:
                self._fail(gen, "spawn_failed", str(e))
                raise Refused(500, "spawn_failed", str(e), "server_error") from None
        if wait:
            deadline = time.time() + LOAD_S + READY_S
            while time.time() < deadline and self.status()["phase"] in ("starting", "loading"):
                time.sleep(0.1)
        return self.status()

    def _spawn(self, cfg: dict, gen: int) -> None:
        from serve import userdata
        run = userdata.path("strata-run")
        run.mkdir(parents=True, exist_ok=True)
        cfg_path, ready, log = run / "run.json", run / "ready.json", run / "strata.log"
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        try:
            ready.unlink()
        except OSError:
            pass
        bindir = Path(cfg["cwd"])
        if not (bindir / "serve" / "server.py").is_file():
            raise Refused(409, "binary_missing", f"the Python server is not in {bindir}: install the engine's files again")
        env = {**os.environ, "PYTHONPATH": str(bindir), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
        cmd = [sys.executable, "-m", "serve.server", "--engine", "strata", "--config", str(cfg_path), "--host", "127.0.0.1",
               "--port", str(cfg["port"]), "--lazy", "--ready-file", str(ready.resolve())]
        logf = open(log, "wb")
        try:
            self.proc = subprocess.Popen(cmd, cwd=str(bindir), env=env, stdin=subprocess.DEVNULL, stdout=logf,
                                         stderr=subprocess.STDOUT,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        finally:
            logf.close()
        contain(self.proc)
        threading.Thread(target=self._bring_up, args=(gen, self.proc, ready, log, float(cfg.get("engine_ready_s") or LOAD_S)),
                         daemon=True, name="engine-host").start()

    def _fail(self, gen: int, code: str, message: str) -> None:
        with self.lock:
            if gen != self._gen:
                return
            self.phase, self.error = "error", {"code": code, "message": message}
            self._stop_locked(keep_state=True)

    def _bring_up(self, gen: int, proc, ready: Path, log: Path, load_s: float) -> None:
        end = time.time() + READY_S
        while not ready.is_file():
            if proc.poll() is not None or time.time() > end or gen != self._gen:
                if gen == self._gen:
                    why = _tail(log) or "no output"
                    self._fail(gen, "start_failed" if proc.poll() is not None else "start_timeout",
                               f"the engine's server did not start: {why}")
                return
            time.sleep(0.2)
        with self.lock:
            if gen != self._gen:
                return
            self.phase = "loading"
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=load_s)
            c.request("POST", "/load", body=b"{}", headers={"Content-Type": "application/json"})
            r = c.getresponse()
            data = r.read()
            c.close()
        except (OSError, http.client.HTTPException) as e:
            if gen == self._gen:
                self._fail(gen, "load_failed", f"the model did not load: {e}; {_tail(log)}")
            return
        if r.status != 200:
            try:
                msg = json.loads(data).get("error", {}).get("message") or data.decode("utf-8", "replace")
            except ValueError:
                msg = data.decode("utf-8", "replace")
            self._fail(gen, "load_failed", f"the model did not load: {msg[:400]}")
            return
        with self.lock:
            if gen == self._gen:
                self.phase = "running"

    def _stop_locked(self, keep_state: bool = False) -> None:
        proc, self.proc = self.proc, None
        self._gen += 0 if keep_state else 1
        if proc is not None and proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if not keep_state:
            self.engine, self.phase, self.error, self.port = None, "idle", None, 0

    def stop(self) -> dict:
        with self.lock:
            self._stop_locked()
            return self.status()

    # -- relaying the /v1 routes ----------------------------------------------------------------------------

    def forward(self, h, method: str, target: str, body: bytes | None = None) -> None:
        """Answer the request `h` (a handler) from the child: status, headers and body as they arrive."""
        st = self.status()
        if st["phase"] != "running":
            msg = (f"the {st['engine']} engine is {'loading the model' if st['phase'] in ('starting', 'loading') else 'not running'}"
                   + (f": {st['error']['message']}" if st["error"] else ""))
            h._json(503, {"error": {"type": "server_error", "message": msg}})
            return
        headers = {k: v for k, v in (("Content-Type", h.headers.get("Content-Type")),
                                     ("Accept", h.headers.get("Accept")),
                                     ("anthropic-version", h.headers.get("anthropic-version"))) if v}
        c = http.client.HTTPConnection("127.0.0.1", st["port"], timeout=None)
        try:
            c.request(method, target, body=body, headers=headers)
            r = c.getresponse()
        except (OSError, http.client.HTTPException) as e:
            c.close()
            h._json(503, {"error": {"type": "server_error", "message": f"the {st['engine']} engine did not answer: {e}"}})
            return
        try:
            h.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in HOP and not k.lower().startswith("access-control"):
                    h.send_header(k, v)
            h._cors()
            h.end_headers()
            while True:
                chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
                if not chunk:
                    break
                h.wfile.write(chunk)
                h.wfile.flush()
        except (ConnectionError, OSError):
            pass                                                 # the client left: closing the relay ends the request
        finally:
            c.close()
            h.close_connection = True


HOST = Host()


def load_request(engine_id: str, req: dict) -> dict:
    """A load request with its saved profile folded in: {"model", "values", "family"}; the request's own values win."""
    out = {"model": req.get("model"), "values": dict(req.get("values") or {}), "family": req.get("family")}
    name = req.get("profile")
    if name:
        from serve import profiles
        found = next((p for p in profiles.engine_profiles(engine_id) if p["name"] == name), None)
        if found is None:
            raise Refused(404, "profile_missing", f"no {engine_id} profile named {name!r}")
        out["model"] = out["model"] or found["model"]
        out["values"] = {**found["values"], **out["values"]}
    return out


# -- the models an engine reads ------------------------------------------------------------------------------------

def _run_configs() -> list[Path]:
    from serve import models
    roots = [engines.ROOT / "bin" / "strata", *map(Path, models.dirs())]
    src = engine_install.sources().get("strata")
    if src and Path(src).is_dir():
        roots += [Path(src), Path(src).parent]
    seen, out = set(), []
    for d in roots:
        try:
            for f in sorted(d.glob("strata-*.json")):
                if f.resolve() not in seen and f.is_file():
                    seen.add(f.resolve())
                    out.append(f)
        except OSError:
            continue
    return out


def models(engine_id: str) -> list[dict]:
    """The models `engine_id` reads: Strata's run configs and the Qwen3.6 GGUFs it accepts (those with the MTP layer);
    {"id", "path", "kind", "family", "name", "size_bytes", "ok", "reason"}."""
    from serve import models as scan
    if engine_id != "strata":
        return []
    out = []
    for f in _run_configs():
        try:
            cfg = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(cfg, dict) and isinstance(cfg.get("args"), list):
            fam = cfg.get("arch") or "qwen4exp"
            out.append({"id": f.as_posix(), "path": f.as_posix(), "kind": "config", "family": fam,
                        "name": cfg.get("model_name") or f.stem, "size_bytes": None, "ok": fam in engines.FAMILIES,
                        "reason": None if fam in engines.FAMILIES else "family"})
    folders = scan.dirs()
    for m in scan.scan(folders)["models"] if folders else []:
        if m.get("kind") != "gguf" or m.get("role") != "model" or m.get("arch") != "qwen35moe":
            continue
        try:
            has = bool(scan.read_gguf(m["files"][0])["meta"].get("qwen35moe.nextn_predict_layers"))
        except (OSError, ValueError, KeyError, IndexError):
            has = False
        out.append({"id": m["path"], "path": m["path"], "kind": "gguf", "family": "qwen35moe",
                    "name": m.get("name") or Path(m["path"]).stem, "size_bytes": m.get("size_bytes"),
                    "ok": has, "reason": None if has else "nextn"})
    return out
