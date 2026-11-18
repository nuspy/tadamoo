"""The embedding service of the knowledge archives: a worker process (tadamoo/rag/embed_worker.py) run by a Python that has torch, the VRAM it needs
borrowed from the server's broker, and an `Embedder` over it for the store.

The model folders are looked for under `<models folder>/rag/embedding` (or the folder named in the settings). The Python is the one in the settings,
else the Docling GPU environment, else a system Python that has torch and transformers (looked for once, in the background). With none, or with the
service off, there is no embedder and every archive is searched by words.

The GPU is a lease (tadamoo/vram_lease.py, owner `embedding`), the biggest of three sizes that the broker can give: for the embedder and the reranker
together, else for one at a time (the worker loads one and unloads it for the other), else for the embedder alone. Without a lease a question is embedded on the CPU with the same model, indexing
runs on the CPU (slowly, and says so), and nothing is reranked. The worker and the lease are kept while the service is used and given back after
`idle_s` seconds without a request; the next request starts them again."""
from __future__ import annotations

import base64
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from tadamoo.rag.errors import RagError
from tadamoo.vram_lease import VramLease

WORKER = Path(__file__).with_name("embed_worker.py")
VENV_ROOTS = tuple(p for p in (os.environ.get("TADAMOO_VENVS", ""), "E:/Dev/venvs") if p)
VENV_NAMES = ("docling-gpu",)
VERSION = "q3e-1"                  # how a passage becomes text and a vector, and a question the vector it is compared with: another version means indexing again
INSTRUCTION = "Given a question, retrieve the passages of the documents that answer it"
OWNER = "embedding"
RETRY_S = 20.0                     # how long a refused lease is not asked for again
LOAD_S, CALL_S = 900.0, 600.0      # a model may take minutes to load from a cold disk; a batch, minutes on the CPU
FILE_ABOVE = 128                   # texts in one call above which the vectors come back in a file
GPU_BATCH, CPU_BATCH = 32, 2
AUTO_MAX_BYTES = 9 << 30           # automatic choice: the biggest model that fits the VRAM a lease can give
HEADROOM_MIB = {"embed": 1800, "both": 2400, "one": 5000}
POOL_MAX = 20


class WorkerError(RagError):
    pass


class Unavailable(Exception):
    """There is no embedder now: `code` is a short reason for the page, the message says it in words."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code, self.message = code, message or code


# -- finding the Python and the models ------------------------------------------------------------------

def probe(python: str, run: Callable[..., Any] = subprocess.run) -> str:
    """`cuda` or `cpu` when `python` has torch and transformers, else ""."""
    try:
        done = run([python, "-c", "import torch, transformers; print('cuda' if torch.cuda.is_available() else 'cpu')"], capture_output=True, text=True, timeout=120,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        return ""
    last = (done.stdout or "").strip().splitlines()[-1:] or [""]
    return last[0] if done.returncode == 0 and last[0] in ("cuda", "cpu") else ""


def system_pythons() -> List[str]:
    """The Pythons installed here: the launcher's list on Windows, `python3` elsewhere."""
    if sys.platform == "win32":
        try:
            out = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return [m.group(1) for m in re.finditer(r"(\S.*?python(?:w)?\.exe)\s*$", out, re.M) if Path(m.group(1)).is_file()]
    found = shutil.which("python3")
    return [found] if found else []


def _venv_python(root: Path) -> Optional[Path]:
    for rel in ("Scripts/python.exe", "bin/python"):
        if (root / rel).is_file():
            return root / rel
    return None


def _own_venv() -> Optional[Path]:
    from tadamoo.rag import embed_env
    return embed_env.shared()._venv_python()


def venv_python(roots: Sequence[str] = VENV_ROOTS, own: Optional[Callable[[], Optional[Path]]] = _own_venv) -> str:
    found_own = own() if own is not None else None
    if found_own is not None:
        return str(found_own)
    for root in roots:
        for name in VENV_NAMES:
            found = _venv_python(Path(root) / name)
            if found is not None:
                return str(found)
    return ""


def detect(pythons: Callable[[], List[str]] = system_pythons, probe_fn: Callable[[str], str] = probe) -> str:
    """The first system Python that has torch and transformers (one with CUDA before one without), else ""."""
    cpu_only = ""
    for python in pythons():
        got = probe_fn(python)
        if got == "cuda":
            return python
        cpu_only = cpu_only or (python if got == "cpu" else "")
    return cpu_only


def _weights(folder: Path) -> int:
    try:
        return sum(f.stat().st_size for f in folder.iterdir() if f.suffix in (".safetensors", ".bin") and f.is_file())
    except OSError:
        return 0


def model_folders(roots: Sequence[str]) -> List[Dict[str, Any]]:
    """The model folders under `roots` (those with a config.json): name, path, weights in bytes, and `kind` (`rerank` when the name says so, else `embed`)."""
    out, seen = [], set()
    for root in roots:
        try:
            entries = sorted(Path(root).iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir() and (p / "config.json").is_file() and p.name not in seen:
                seen.add(p.name)
                out.append({"name": p.name, "path": str(p), "bytes": _weights(p), "kind": "rerank" if "rerank" in p.name.lower() else "embed"})
    return out


def _size_tag(name: str) -> str:
    m = re.search(r"(\d+(?:\.\d+)?)b\b", name.lower())
    return m.group(1) if m else ""


def choose(models: Sequence[Dict[str, Any]], kind: str, name: str = "", like: str = "") -> Optional[Dict[str, Any]]:
    """The model of `kind` called `name`; with no name, the one of the same size as `like`, else the biggest that fits a lease, else the smallest."""
    mine = [m for m in models if m["kind"] == kind]
    if name:
        return next((m for m in mine if m["name"] == name), None)
    tag = _size_tag(like)
    if tag and (same := [m for m in mine if _size_tag(m["name"]) == tag]):
        return same[0]
    fits = [m for m in mine if m["bytes"] <= AUTO_MAX_BYTES]
    return max(fits, key=lambda m: m["bytes"]) if fits else min(mine, key=lambda m: m["bytes"], default=None)


def tiers(embed_bytes: int, rerank_bytes: int) -> Dict[str, int]:
    """The MiB to ask the broker for: the embedder and the reranker resident together, one of them at a time, the embedder alone (rounded up to 500)."""
    e, r = embed_bytes >> 20, rerank_bytes >> 20

    def up(n: int) -> int:
        return -(-n // 500) * 500
    return {"both": up(e + r + HEADROOM_MIB["both"]), "one": up(max(e, r) + HEADROOM_MIB["one"]), "embed": up(e + HEADROOM_MIB["embed"])}


# -- the worker process ---------------------------------------------------------------------------------

class _Worker:
    def __init__(self, argv: Sequence[str]) -> None:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "TOKENIZERS_PARALLELISM": "false"}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, creationflags=flags)
        self.lines: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.err: List[bytes] = []
        self.next_id = 0
        self.dead = False                                       # it ended or was ended: not to be used again
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()

    def _pump(self) -> None:
        for raw in self.proc.stdout:                                    # type: ignore[union-attr]
            self.lines.put(raw)
        self.lines.put(None)

    def _drain(self) -> None:
        for raw in self.proc.stderr:                                    # type: ignore[union-attr]
            self.err.append(raw)
            del self.err[:-30]

    def alive(self) -> bool:
        return not self.dead and self.proc.poll() is None

    def _tail(self) -> str:
        tail = b"".join(self.err).decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        return f" ({tail[0][:200]})" if tail[0] else ""

    def _next(self, timeout: float) -> Optional[dict]:
        try:
            raw = self.lines.get(timeout=timeout)
        except queue.Empty:
            return {}
        if raw is None:
            self.dead = True
            raise WorkerError("the embedding worker ended" + self._tail())
        try:
            event = json.loads(raw)
        except ValueError:
            return {}
        return event if isinstance(event, dict) else {}

    def wait_ready(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            event = self._next(0.2)
            if event.get("event") == "ready":
                return
            if event.get("event") == "error":
                raise WorkerError("the embedding worker could not start: " + str(event.get("message") or "unknown error"))
        self.close()
        raise WorkerError("the embedding worker did not start in time")

    def call(self, cmd: Dict[str, Any], timeout: float, on_progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
        self.next_id += 1
        cid = self.next_id
        try:
            self.proc.stdin.write((json.dumps({**cmd, "id": cid}) + "\n").encode("utf-8"))           # type: ignore[union-attr]
            self.proc.stdin.flush()                                                                 # type: ignore[union-attr]
        except OSError:
            self.dead = True
            raise WorkerError("the embedding worker ended" + self._tail()) from None
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            event = self._next(0.2)
            if event.get("id") != cid:
                continue
            if event.get("event") == "progress":
                if on_progress is not None:
                    on_progress(int(event.get("done") or 0), int(event.get("total") or 0))
                continue
            if not event.get("ok"):
                raise WorkerError(str(event.get("error") or "the embedding worker failed"))
            return event
        self.close()
        raise WorkerError("the embedding worker stopped answering and was ended")

    def close(self) -> None:
        self.dead = True
        try:
            self.proc.stdin.close()                                    # a closed stdin is the order to stop
        except OSError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


# -- the service ----------------------------------------------------------------------------------------

class EmbedService:
    """One worker for the server. `settings` returns the RagSettings dict, `models_dirs` the models folders of the program, `lease_url` the server's address."""

    def __init__(self, settings: Callable[[], Dict[str, Any]], models_dirs: Callable[[], Sequence[str]] = lambda: (), lease_url: Callable[[], str] = lambda: "",
                 argv: Optional[Sequence[str]] = None, lease_factory: Callable[..., Any] = VramLease, clock: Callable[[], float] = time.monotonic,
                 pythons: Callable[[], List[str]] = system_pythons, probe_fn: Callable[[str], str] = probe, venv_roots: Sequence[str] = VENV_ROOTS,
                 own_venv: Optional[Callable[[], Optional[Path]]] = _own_venv, tmp_dir: Optional[str] = None) -> None:
        self.settings, self.models_dirs, self.lease_url, self.argv = settings, models_dirs, lease_url, argv
        self.lease_factory, self.clock, self.pythons, self.probe_fn, self.venv_roots, self.own_venv, self.tmp_dir = \
            lease_factory, clock, pythons, probe_fn, venv_roots, own_venv, tmp_dir
        self._state = threading.RLock()
        self._io = threading.Lock()
        self._worker: Optional[_Worker] = None
        self._lease: Any = None
        self._device = ""
        self._mode = ""                                      # both | one | embed | cpu, as granted
        self._loaded: set = set()
        self._busy = 0
        self._last_used = 0.0
        self._refused_at = -1e9
        self._refusal = ("", "")                             # (reason, message) of the last refused lease
        self._detected: Optional[str] = None                 # a system Python found by looking (None: not looked for yet)
        self._detecting = False
        self._stop_idle = threading.Event()
        self.embedder = ServiceEmbedder(self)
        self.spec_ttl = 2.0                                  # seconds a look at the settings and the model folders is reused
        self._spec: Tuple[float, str, Any] = (-1e9, "", None)

    # -- what there is ---------------------------------------------------------------------------

    def roots(self, s: Dict[str, Any]) -> List[str]:
        if s.get("embed_models_dir"):
            return [s["embed_models_dir"]]
        return [str(Path(d) / "rag" / "embedding") for d in self.models_dirs()]

    def _python(self, s: Dict[str, Any]) -> str:
        if s.get("embed_python"):
            if Path(s["embed_python"]).is_file():
                return s["embed_python"]
            raise Unavailable("no_python", "the Python set for the embeddings is not there")
        found = venv_python(self.venv_roots, self.own_venv)
        if found:
            return found
        if self._detected is None:
            if not self._detecting:
                self._detecting = True
                threading.Thread(target=self._detect, name="embed-detect", daemon=True).start()
            raise Unavailable("detecting", "looking for a Python that has torch and transformers")
        if not self._detected:
            raise Unavailable("no_python", "no Python with torch and transformers was found: set one in the knowledge settings")
        return self._detected

    def _detect(self) -> None:
        try:
            self._detected = detect(self.pythons, self.probe_fn)
        finally:
            self._detecting = False

    def spec(self) -> Dict[str, Any]:
        """The Python and the models that would be used now; raises Unavailable with the reason when there is no embedder."""
        s = self.settings()
        key, now = json.dumps(s, sort_keys=True), self.clock()
        if self._spec[1] == key and now - self._spec[0] < self.spec_ttl:
            if isinstance(self._spec[2], Unavailable):
                raise self._spec[2]
            return self._spec[2]
        try:
            got: Any = self._look(s)
        except Unavailable as e:
            got = e
        self._spec = (now, key, got)
        if isinstance(got, Unavailable):
            raise got
        return got

    def _look(self, s: Dict[str, Any]) -> Dict[str, Any]:
        if not s.get("embed_enabled", True):
            raise Unavailable("off", "semantic search is turned off")
        python = self._python(s)
        models = model_folders(self.roots(s))
        emb = choose(models, "embed", s.get("embed_model", ""))
        if emb is None:
            raise Unavailable("no_model", "no embedding model found" + (f" named {s['embed_model']}" if s.get("embed_model") else "") + " under " + ", ".join(self.roots(s)))
        rer = choose(models, "rerank", s.get("rerank_model", ""), like=emb["name"]) if s.get("rerank", True) else None
        return {"python": python, "embed": emb, "rerank": rer, "settings": s, "models": models,
                "tiers": tiers(emb["bytes"], rer["bytes"] if rer else 0)}

    def provider(self) -> Optional["ServiceEmbedder"]:
        """The embedder for the store, or None when there is none (the reason is in `info()`)."""
        try:
            spec = self.spec()
        except Unavailable:
            return None
        self.embedder.model = spec["embed"]["name"]
        return self.embedder

    def info(self) -> Dict[str, Any]:
        """What the page says: whether semantic search is on, with which models, where it runs now and why not."""
        out: Dict[str, Any] = {"enabled": bool(self.settings().get("embed_enabled", True)), "available": False, "model": "", "reason": "", "message": "", "python": "",
                               "reranker": {"model": "", "enabled": bool(self.settings().get("rerank", True)), "available": False}, "device": self._device, "mode": self._mode,
                               "lease_mib": self._lease.granted_mib if self._lease is not None else 0, "lease_reason": self._refusal[0], "lease_message": self._refusal[1],
                               "models": [], "roots": []}
        s = self.settings()
        out["roots"] = self.roots(s)
        out["models"] = [{"name": m["name"], "kind": m["kind"], "bytes": m["bytes"]} for m in model_folders(out["roots"])]
        try:
            spec = self.spec()
        except Unavailable as e:
            out["reason"], out["message"] = e.code, e.message
            return out
        out.update(available=True, model=spec["embed"]["name"], python=spec["python"], tiers=spec["tiers"])
        out["reranker"].update(model=spec["rerank"]["name"] if spec["rerank"] else "", available=spec["rerank"] is not None)
        return out

    # -- the worker and its lease ------------------------------------------------------------------

    def _release(self) -> None:
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.release()

    def _stop_worker(self) -> None:
        worker, self._worker = self._worker, None
        self._stop_idle.set()
        self._loaded, self._device, self._mode = set(), "", ""
        if worker is not None:
            worker.close()
        self._release()

    def rest(self) -> None:
        """Gives the worker and the lease back now unless a request is running: after indexing, so the next reader of documents can have the GPU."""
        with self._state:
            if self._busy == 0:
                self._stop_worker()

    def close(self) -> None:
        with self._state:
            self._stop_worker()

    def _ask_lease(self, spec: Dict[str, Any]) -> Tuple[Any, str]:
        """(a granted lease, its mode) or (None, "") with the reason kept: the biggest of the sizes that fit; a refusal that is not `not_enough` stops the asking.
        Without a reranker only the embedder's size is asked for."""
        sizes = spec["tiers"]
        self._refused_at = self.clock()
        ladder = [("both", sizes["both"]), ("one", sizes["one"]), ("embed", sizes["embed"])] if spec["rerank"] else [("embed", sizes["embed"])]
        for mode, mib in ladder:
            lease = self.lease_factory(OWNER, mib, "semantic search in the knowledge archives", base_url=self.lease_url() or None)
            lease.acquire()
            if lease.gpu_ok:
                self._refusal, self._refused_at = ("", ""), -1e9
                return lease, mode
            self._refusal = (lease.reason, lease.message)
            if lease.reason != "not_enough":
                break
        return None, ""

    def _spawn(self, spec: Dict[str, Any], lease: Any, mode: str) -> None:
        argv = list(self.argv) if self.argv else [spec["python"], "-X", "utf8", str(WORKER)]
        worker = _Worker(argv)
        try:
            worker.wait_ready(LOAD_S)
            wanted = "cuda" if lease is not None else "cpu"
            got = worker.call({"cmd": "load", "role": "embed", "model_dir": spec["embed"]["path"], "device": wanted}, LOAD_S)
        except WorkerError:
            worker.close()
            if lease is not None:
                lease.release()
            raise
        device = str(got.get("device") or wanted)
        if lease is not None and device != "cuda":                  # the Python has no CUDA: the lease is of no use
            lease.release()
            lease, mode = None, ""
            self._refusal = ("no_cuda", "the Python that runs the embeddings has no CUDA")
        self._worker, self._lease, self._device, self._mode, self._loaded = worker, lease, device, mode if lease is not None else "cpu", {"embed"}
        self._stop_idle = threading.Event()
        threading.Thread(target=self._idle_loop, args=(self._stop_idle,), name="embed-idle", daemon=True).start()

    def _ensure(self) -> Dict[str, Any]:
        """A running worker; returns the spec."""
        spec = self.spec()
        worker = self._worker
        if worker is not None and (not worker.alive() or (self._lease is not None and getattr(self._lease, "lost", False))):
            self._stop_worker()
            worker = None
        if worker is not None and self._mode == "cpu" and self._busy == 0 and self.clock() - self._refused_at >= RETRY_S:
            lease, mode = self._ask_lease(spec)                  # the GPU may be free now
            if lease is not None:
                self._stop_worker()
                self._spawn(spec, lease, mode)
            return spec
        if worker is None:
            lease, mode = (None, "") if self.clock() - self._refused_at < RETRY_S else self._ask_lease(spec)
            self._spawn(spec, lease, mode)
        return spec

    def _idle_loop(self, stop: threading.Event) -> None:
        while not stop.wait(0.25):
            with self._state:
                if stop.is_set():
                    return
                if self._busy == 0 and self.clock() - self._last_used > float(self.settings().get("idle_s", 120)):
                    self._stop_worker()
                    return

    @contextmanager
    def _use(self) -> Iterator[Tuple[_Worker, Dict[str, Any]]]:
        with self._state:
            spec = self._ensure()
            self._busy += 1
            worker = self._worker
        try:
            with self._io:
                yield worker, spec                                      # type: ignore[misc]
        finally:
            with self._state:
                self._busy -= 1
                self._last_used = self.clock()

    def _need(self, worker: _Worker, spec: Dict[str, Any], role: str) -> None:
        """Has `role` loaded: with the two resident together both stay; with one at a time the other is unloaded first."""
        if role in self._loaded:
            return
        other = "rerank" if role == "embed" else "embed"
        if other in self._loaded and self._mode != "both":
            worker.call({"cmd": "unload", "role": other}, CALL_S)
            self._loaded.discard(other)
        worker.call({"cmd": "load", "role": role, "model_dir": spec[role]["path"], "device": self._device}, LOAD_S)
        self._loaded.add(role)

    # -- what the embedder asks --------------------------------------------------------------------

    def embed(self, texts: Sequence[str], kind: str) -> np.ndarray:
        """Float32 vectors (the worker's float16) of `texts` as passages or as a question; the order is kept."""
        with self._use() as (worker, spec):
            self._need(worker, spec, "embed")
            cmd: Dict[str, Any] = {"cmd": "embed", "texts": list(texts), "kind": kind, "instruction": INSTRUCTION, "batch": 8 if self._device == "cuda" else 1}
            path = ""
            if len(texts) > FILE_ABOVE:
                handle, path = tempfile.mkstemp(suffix=".npy", dir=self.tmp_dir)
                os.close(handle)
                cmd["out"] = path
            try:
                got = worker.call(cmd, CALL_S)
                if path:
                    return np.load(path, allow_pickle=False).astype(np.float32)
                shape = tuple(int(n) for n in got["shape"])
                return np.frombuffer(base64.b64decode(got["data"]), dtype=np.float16).reshape(shape).astype(np.float32)
            finally:
                if path:
                    Path(path).unlink(missing_ok=True)

    def rerank(self, query: str, passages: Sequence[str]) -> Optional[List[float]]:
        """The probability that each passage answers `query`, or None when reranking cannot be done now (off, no reranker model, no GPU lease)."""
        try:
            spec = self.spec()
        except Unavailable:
            return None
        if spec["rerank"] is None:
            return None
        with self._use() as (worker, spec):
            if spec["rerank"] is None or self._mode in ("cpu", "embed"):
                return None
            self._need(worker, spec, "rerank")
            return [float(x) for x in worker.call({"cmd": "rerank", "query": query, "passages": list(passages), "batch": 4}, CALL_S)["scores"]]

    def test(self) -> Dict[str, Any]:
        """Embeds one sentence as a question: how long it took and where."""
        started = time.monotonic()
        self.provider()
        try:
            vec = self.embed(["How is the keel of a sailing boat made?"], "query")
        except (Unavailable, RagError) as e:
            return {"ok": False, "error": getattr(e, "message", "") or str(e)}
        return {"ok": True, "model": self.embedder.model, "device": self._device, "mode": self._mode, "dim": int(vec.shape[1]), "seconds": round(time.monotonic() - started, 2),
                "reranker": "rerank" in self._loaded or self._mode == "both", "lease_reason": self._refusal[0], "lease_message": self._refusal[1]}


class ServiceEmbedder:
    """The `Embedder` of the store over the service: passages with `embed`, a question with `embed_query`."""
    version = VERSION

    def __init__(self, service: EmbedService) -> None:
        self.service, self.model = service, ""

    @property
    def device(self) -> str:
        return self.service._device

    @property
    def batch(self) -> int:
        return GPU_BATCH if self.service._device == "cuda" else CPU_BATCH

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        return self.service.embed(texts, "passage")

    def embed_query(self, text: str) -> np.ndarray:
        return self.service.embed([text], "query")[0]

    def rerank(self, query: str, passages: Sequence[str]) -> Optional[List[float]]:
        return self.service.rerank(query, passages)

    def rest(self) -> None:
        self.service.rest()
