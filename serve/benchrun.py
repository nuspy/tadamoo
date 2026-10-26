"""The standard tests on one model, run in a server of its own, and the job that runs them for the Model converter page.

A test needs the GPU and the model's memory, so it runs only when nothing else holds them. One session loads the model with the standard
settings and measures, in this order: the memory it took, the decode speed with exact and with relaxed drafting, the prefill speed, an
image request, and a small maths test (the first questions of gsm8k, scored on the final number). The decode speed without the drafter
needs another load and is taken last. What each test found is kept by serve/modelbench.py under the settings it ran with.
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from serve import loader, modelbench, userdata

ROOT = Path(__file__).resolve().parents[1]
GiB = 1 << 30
GSM8K_URL = "https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/test.jsonl"
PROMPTS = {"conv": "Explain how a B-tree differs from an LSM tree, with a worked example of each. About 600 words.",
           "prose": "Write an essay on the Odyssey."}
LOAD_TIMEOUT_S = 1800


class Refused(Exception):
    """A request the job cannot take: the HTTP status and the body."""

    def __init__(self, status: int, body: dict):
        super().__init__((body.get("error") or {}).get("message", ""))
        self.status, self.body = status, body


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------------------------------------ the settings
def power_limit_w() -> Optional[float]:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=power.limit", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return float(out.strip().splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def vram_used_mib() -> Optional[int]:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return int(out.strip().splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def standard_settings(overrides: Optional[dict] = None) -> Dict[str, Any]:
    """The settings every model is tested with, so that the figures compare: the longest context that costs no quality (int8 KV), the drafter on,
    one slot, images read by the engine with the projector released between them."""
    from serve import hardware
    s = {"max-context": 131072, "kv": "int8", "spec": 7, "spec-min-p": 0.5, "prefill": "auto", "resident-experts": True, "vram-reserve-mib": 1536,
         "ple-io": "direct", "pool-workers": hardware.pool_workers()[0], "parallel": 1, "vision": "dynamic", "vision-hold": "release",
         "vision-projector": "auto"}
    for k, v in (overrides or {}).items():
        if k in s and v is not None:
            s[k] = v
    return s


def config_object(model_path: str, settings: Dict[str, Any], port: int, alias: str) -> Dict[str, Any]:
    server = {"model": model_path, "alias": alias, "host": "127.0.0.1", "port": port}
    engine = {}
    for k, v in settings.items():
        (server if k in ("parallel", "vision", "vision-hold", "vision-projector") else engine)[k] = v
    if server.get("vision") != "dynamic":
        server.pop("vision-hold", None)
    return {"server": server, "engine": engine}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------------------------------------ memory
def memory_row(tag: str, pid: Optional[int] = None) -> Dict[str, Any]:
    s = loader.system_now(0)
    row: Dict[str, Any] = {"tag": tag, "ram_used_gib": round((s["ram_total"] - s["ram_available"]) / GiB, 2),
                           "commit_used_gib": round(s["commit_used"] / GiB, 2), "vram_used_mib": vram_used_mib()}
    if pid:
        try:
            import psutil
            root = psutil.Process(pid)
            rss = priv = 0
            for p in [root] + root.children(recursive=True):
                try:
                    mi = p.memory_info()
                    rss += mi.rss
                    priv += getattr(mi, "private", mi.vms)
                except psutil.Error:
                    pass
            row.update(procs_rss_gib=round(rss / GiB, 2), procs_private_gib=round(priv / GiB, 2))
        except ImportError:
            pass
    return row


# ------------------------------------------------------------------------------------------------ one server
class Session:
    """A server of its own on a free port, started for one test run and ended with it."""

    def __init__(self, model_path: str, settings: Dict[str, Any], workdir: Path, cancelled: Callable[[], bool]):
        self.model_path, self.settings, self.cancelled = model_path, settings, cancelled
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.config_path = workdir / f"bench-{self.port}.json"
        self.config_path.write_text(json.dumps(config_object(model_path, settings, self.port, "bench"), indent=1), encoding="utf-8")
        self.log_path = workdir / f"bench-{self.port}.log"
        self.proc: Optional[subprocess.Popen] = None
        self.load_seconds: Optional[float] = None

    def start(self) -> bool:
        exe = ROOT / "bin" / ("basalt.exe" if os.name == "nt" else "basalt")
        log = open(self.log_path, "w", encoding="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        t0 = time.time()
        self.proc = subprocess.Popen([str(exe), "serve", "--config", str(self.config_path), "--port", str(self.port)], stdout=log, stderr=subprocess.STDOUT,
                                     cwd=str(ROOT), creationflags=flags)
        try:
            from serve.util.winjob import contain
            contain(self.proc)
        except Exception:  # noqa: BLE001 - outside Windows, or no job: the process still runs
            pass
        while time.time() - t0 < LOAD_TIMEOUT_S:
            if self.cancelled():
                raise Cancelled()
            if self.proc.poll() is not None:
                return False
            try:
                if json.load(urllib.request.urlopen(self.base + "/health", timeout=3)).get("loaded"):
                    self.load_seconds = round(time.time() - t0, 1)
                    return True
            except (OSError, ValueError):
                pass
            time.sleep(2)
        return False

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            proc.kill()
        time.sleep(6)                                    # the memory is given back a moment later

    def log_tail(self, n: int = 12) -> str:
        try:
            return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
        except OSError:
            return ""

    # requests
    def post(self, body: dict, timeout: float = 900) -> dict:
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=timeout))

    def stream(self, body: dict, timeout: float = 900):
        """(text, tokens, time to the first token, decode tokens per second, total seconds)."""
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps({**body, "stream": True}).encode(), headers={"Content-Type": "application/json"})
        t0 = time.time()
        first = last = None
        n = 0
        text: List[str] = []
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                if self.cancelled():
                    raise Cancelled()
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:") or line.endswith("[DONE]"):
                    continue
                ch = ((json.loads(line[5:]).get("choices") or [{}])[0].get("delta")) or {}
                piece = (ch.get("content") or "") + (ch.get("reasoning_content") or "")
                if piece:
                    now = time.time()
                    first = first or now
                    last = now
                    n += 1
                    text.append(piece)
        dec = (n - 1) / (last - first) if n > 1 and last > first else 0.0
        return "".join(text), n, (first - t0) if first else None, dec, time.time() - t0


# ------------------------------------------------------------------------------------------------ the tests
NO_THINK = {"enable_thinking": False}


def test_speed(s: Session, relax: List[float], reps: int, say) -> List[dict]:
    rows = []
    for rep in range(reps):
        for r in relax:                                  # interleaved: every setting meets the same drift
            for name, prompt in PROMPTS.items():
                say(f"decode, relaxed drafting {r or 'off'}, {name}")
                body = {"messages": [{"role": "user", "content": prompt}], "temperature": 0.6, "top_p": 0.95, "top_k": 20, "max_tokens": 900,
                        "chat_template_kwargs": NO_THINK, "relaxed_drafting": r if r else False, "seed": 100 + rep}
                _, n, ttft, dec, _ = s.stream(body)
                rows.append({"relax": r, "prompt": name, "rep": rep, "tokens": n, "decode_tok_s": round(dec, 1), "ttft_s": round(ttft or 0, 2)})
    return rows


def test_prefill(s: Session, say) -> Optional[float]:
    """Prompt tokens per second on a long prompt that has not been read before."""
    best = None
    for k in range(2):
        say("prefill")
        text = f"[{time.time():.3f}-{k}] Read the text and say in one sentence what it is.\n\n" + ("The quick brown fox jumps over the lazy dog. " * 1500)
        t0 = time.time()
        res = s.post({"messages": [{"role": "user", "content": text}], "max_tokens": 1, "temperature": 0, "chat_template_kwargs": NO_THINK})
        took = time.time() - t0
        tokens = (res.get("usage") or {}).get("prompt_tokens") or 0
        if tokens and took > 0:
            best = round(tokens / took, 0)                # the second is the one kept: the first warms the paths up
    return best


def test_image(s: Session, say) -> List[dict]:
    path = ROOT / "assets" / "basalt-lockup-dark.png"
    if not path.is_file():
        return []
    img = base64.b64encode(path.read_bytes()).decode()
    body = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + img}},
                                                      {"type": "text", "text": "What is in this image? One sentence."}]}],
            "temperature": 0, "max_tokens": 80, "chat_template_kwargs": NO_THINK}
    tries = []
    for k in range(2):
        say("image")
        text, _, _, _, total = s.stream(body)
        tries.append({"try": k, "seconds": round(total, 2), "text": text[:200]})
    return tries


def gsm8k_file() -> Path:
    path = modelbench.store_dir() / "gsm8k_test.jsonl"
    if not path.is_file():
        with urllib.request.urlopen(GSM8K_URL, timeout=60) as r:
            data = r.read()
        path.write_bytes(data)
    return path


def final_number(text: str) -> Optional[float]:
    found = (re.findall(r"answer\s*[:=]?\s*\$?\s*(-?[\d,]*\.?\d+)", text, flags=re.I) or re.findall(r"####\s*(-?[\d,]*\.?\d+)", text)
             or re.findall(r"(-?[\d,]*\.?\d+)", text))
    if not found:
        return None
    try:
        return float(found[-1].replace(",", ""))
    except ValueError:
        return None


def test_quality(s: Session, n: int, say, progress) -> Dict[str, Any]:
    questions = [json.loads(line) for line in gsm8k_file().read_text(encoding="utf-8").splitlines() if line.strip()][:n]
    ok = tokens = 0
    wrong = []
    t0 = time.time()
    for i, q in enumerate(questions):
        say(f"maths questions {i + 1} of {len(questions)}")
        progress((i + 1) / len(questions))
        gold = float(q["answer"].split("####")[-1].strip().replace(",", ""))
        body = {"messages": [{"role": "user", "content": q["question"] + "\n\nSolve it step by step, briefly. End with a last line of the form 'Answer: <number>'."}],
                "temperature": 0, "max_tokens": 600, "chat_template_kwargs": NO_THINK, "relaxed_drafting": False}
        text, k, _, _, _ = s.stream(body)
        tokens += k
        got = final_number(text)
        if got is not None and abs(got - gold) < 1e-6:
            ok += 1
        else:
            wrong.append(i)
    return {"n": len(questions), "correct": ok, "acc": round(ok / max(1, len(questions)), 4), "tokens": tokens, "seconds": round(time.time() - t0, 1), "wrong": wrong[:60]}


def run_tests(model_path: str, settings: Dict[str, Any], steps: List[str], relax: List[float], reps: int, n: int, baseline: bool, workdir: Path,
              cancelled: Callable[[], bool], say: Callable[[str], None], progress: Callable[[float], None]) -> Dict[str, Any]:
    """Loads the model in a server of its own and runs the steps asked for; the dict modelbench.record_run takes (only the steps run)."""
    run: Dict[str, Any] = {"config": config_object(model_path, settings, 0, "bench"), "power_limit_w": power_limit_w(), "at": time.time()}
    mem: List[dict] = [memory_row("before")]
    s = Session(model_path, settings, workdir, cancelled)
    try:
        say("loading the model")
        progress(0.02)
        ready = s.start()
        run["ready"] = ready
        run["load_seconds"] = s.load_seconds
        if not ready:
            run["error"] = s.log_tail() or "the server did not start"
            run["mem"] = mem
            return run
        time.sleep(5)
        mem.append(memory_row("loaded", s.proc.pid))
        total = max(1, sum(w for step, w in (("speed", 0.35), ("image", 0.05), ("quality", 0.45)) if step in steps))
        done = 0.0
        if "speed" in steps:
            run["speed"] = test_speed(s, relax, reps, say)
            pre = test_prefill(s, say)
            if pre:
                run["prefill_tok_s"] = pre
            done += 0.35
            progress(0.1 + 0.8 * done / total)
        if "image" in steps and settings.get("vision") != "off":
            mem.append(memory_row("before image", s.proc.pid))
            run["image"] = test_image(s, say)
            mem.append(memory_row("after image", s.proc.pid))
            time.sleep(3)
            mem.append(memory_row("idle after image", s.proc.pid))
            done += 0.05
            progress(0.1 + 0.8 * done / total)
        if "quality" in steps:
            base = 0.1 + 0.8 * done / total
            run["gsm8k"] = test_quality(s, n, say, lambda f: progress(base + 0.8 * 0.45 / total * f))
    finally:
        s.stop()
    mem.append(memory_row("after stop"))
    run["mem"] = mem
    if baseline and "speed" in steps and int(settings.get("spec") or 0) >= 2 and not cancelled():
        say("decode speed without the drafter: loading again")
        s2 = Session(model_path, {**settings, "spec": 0}, workdir, cancelled)
        try:
            if s2.start():
                time.sleep(3)
                rows = test_speed(s2, [0.0], 1, say)
                vals = [r["decode_tok_s"] for r in rows if r["decode_tok_s"]]
                if vals:
                    run["decode_off"] = round(sum(vals) / len(vals), 1)
        finally:
            s2.stop()
    progress(1.0)
    return run


# ------------------------------------------------------------------------------------------------ the job
class Jobs:
    """One test run at a time: its state for GET /api/bench/job, its cancellation and the results it stored."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: Dict[str, Any] = {"phase": "idle"}
        self.cancel_flag = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def status(self) -> Dict[str, Any]:
        with self.lock:
            return dict(self.state)

    def running(self) -> bool:
        with self.lock:
            return self.state.get("phase") == "running"

    def _set(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)

    def cancel(self) -> bool:
        if not self.running():
            return False
        self.cancel_flag.set()
        return True

    def start(self, model: Dict[str, Any], steps: List[str], overrides: Optional[dict], relax: List[float], reps: int, n: int, baseline: bool, reuse: Any,
              precheck: Optional[Callable[[str, Dict[str, Any]], None]] = None, runner=None) -> Dict[str, Any]:
        """Starts the tests on `model` ({"path", "name", ...}); a step with an answer under the same settings is offered for reuse first (409 `reuse`
        with the dates) unless `reuse` says "reuse", "redo" or, for each step, one of them."""
        path = model["path"]
        ident = modelbench.fingerprints(path)
        if ident is None:
            raise Refused(400, {"error": {"type": "invalid_request_error", "message": "that is not a model file this PC can read"}})
        steps = [s for s in modelbench.STEPS if s in steps] or list(modelbench.STEPS)
        relax = sorted({float(r) for r in relax} | {0.0}) if relax else list(modelbench.RELAX_DEFAULT)
        settings = standard_settings(overrides)
        power = power_limit_w()
        keyed = {**settings, "relax": relax, "reps": reps, "power": power, "n": n}
        have = modelbench.availability(ident, keyed, steps)
        existing = {s: v for s, v in have.items() if v}
        per = reuse if isinstance(reuse, dict) else {}
        decide = lambda step: per.get(step) or (reuse if reuse in ("reuse", "redo") else None)      # noqa: E731
        if existing and any(decide(step) not in ("reuse", "redo") for step in existing):
            raise Refused(409, {"error": {"type": "confirmation_required", "code": "reuse",
                                          "message": "this model was already tested with these settings: reuse the results or test again"},
                                "available": existing, "steps": steps})
        todo = [step for step in steps if not (step in existing and decide(step) == "reuse")]
        if not todo:
            self._set(phase="done", reused=steps, steps=[], model=path, finished=time.time(), message="")
            return self.status()
        if precheck is not None:
            precheck(path, settings)
        with self.lock:
            if self.state.get("phase") == "running":
                raise Refused(409, {"error": {"type": "busy", "code": "busy", "message": "a test is already running"}})
            self.cancel_flag.clear()
            self.state = {"phase": "running", "model": path, "name": model.get("name"), "steps": todo, "reused": [s for s in steps if s not in todo],
                          "started": time.time(), "finished": None, "error": None, "progress": 0.0, "message": "", "settings": settings}
        workdir = Path(userdata.path("bench")) / "work"
        workdir.mkdir(parents=True, exist_ok=True)
        run_fn = runner or run_tests

        def work():
            try:
                run = run_fn(path, settings, todo, relax, reps, n, baseline, workdir, self.cancel_flag.is_set, lambda t: self._set(message=t),
                             lambda f: self._set(progress=round(min(1.0, f), 3)))
                if self.cancel_flag.is_set():
                    return self._set(phase="cancelled", finished=time.time())
                if run.get("error") or not run.get("ready", True):
                    return self._set(phase="failed", finished=time.time(), error=run.get("error") or "the model did not load")
                info = {k: model.get(k) for k in ("name", "path", "quant", "size_bytes", "experts_bytes", "mtp", "vision")}
                keep = {"config", "power_limit_w", "at"}
                for step, fields in (("memory", ("mem", "load_seconds", "ready")), ("speed", ("speed", "prefill_tok_s", "decode_off")), ("image", ("image",)),
                                     ("quality", ("gsm8k",))):
                    if step in todo:
                        keep |= set(fields)
                stored = modelbench.record_run(path, info, {k: v for k, v in run.items() if k in keep})
                self._set(phase="done", finished=time.time(), progress=1.0, stored=stored, message="")
            except Cancelled:
                self._set(phase="cancelled", finished=time.time())
            except Exception as e:  # noqa: BLE001 - shown on the page
                self._set(phase="failed", finished=time.time(), error=str(e))
        self.thread = threading.Thread(target=work, name="bench-run", daemon=True)
        self.thread.start()
        return self.status()


JOBS = Jobs()
