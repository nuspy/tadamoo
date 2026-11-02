"""Install an engine into bin/<engine>/ from a local folder or an archive the person points to.

  sources()            {engine: path} the Loader remembers (machine.json "engine_sources")
  set_source(id, path) remember a source
  start(id, source, remember)   (HTTP status, body): check the source, then copy on a thread
  job(id)              the copy's progress: {"status": running | done | error, "copied", "total", "file", "error"}

A source is a folder or a .zip holding the engine's binaries and, for Strata, its Python server tree. No packages are
published yet, so a release download answers "not available yet" and fetches nothing."""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

from serve import loader

CHUNK = 1 << 20
SKIP_DIRS = {"__pycache__", "tests", ".git"}
EXE = ".exe" if os.name == "nt" else ""
# per engine: where it lands under bin/, the file that marks its binary dir, the file patterns copied from it, and
# the Python tree (marker file, folders) that sits beside or above it
SPEC = {
    "strata": {"dest": "strata", "main": "strata" + EXE, "binaries": ("strata*" + EXE, "*.dll" if os.name == "nt" else "*.so"),
               "tree_marker": "serve/server.py", "tree": ("serve", "strata_control", "strata_cli", "tools", "data")},
    "basalt": {"dest": "", "main": "basalt" + EXE, "binaries": ("basalt*" + EXE, "*.dll" if os.name == "nt" else "*.so"),
               "tree_marker": None, "tree": ()},
}
_jobs: dict[str, dict] = {}


def _root() -> Path:
    from serve import engines
    return engines.ROOT                                          # one place that says where bin/ is

_lock = threading.Lock()


class InstallError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _error(status: int, code: str, message: str, kind: str = "invalid_request_error") -> tuple[int, dict]:
    return status, {"error": {"type": kind, "code": code, "message": message}}


def sources() -> dict:
    got = loader.machine_settings().get("engine_sources")
    return {k: v for k, v in got.items() if isinstance(v, str)} if isinstance(got, dict) else {}


def set_source(engine_id: str, path: str | None) -> dict:
    """Remember (or forget, with None or "") the source of an engine; ValueError when the engine or the path is not."""
    if engine_id not in SPEC:
        raise ValueError(f"{engine_id}: no such engine")
    got = sources()
    if path:
        if not isinstance(path, str) or not os.path.exists(path):
            raise ValueError(f"{path}: not a folder or archive on this PC")
        got[engine_id] = str(path)
    else:
        got.pop(engine_id, None)
    loader.save_machine_settings({"engine_sources": got})
    return got


def job(engine_id: str) -> dict | None:
    with _lock:
        j = _jobs.get(engine_id)
        return dict(j) if j else None


def _binary_dir(root: Path, spec: dict) -> Path | None:
    candidates = [root, root / "bin", *sorted(root.glob("build*")), *sorted(root.glob("*")), *sorted(root.glob("*/bin")),
                  *sorted(root.glob("*/build*"))]
    for d in candidates:
        if d.is_dir() and (d / spec["main"]).is_file():
            return d
    return None


def _tree_root(bindir: Path, root: Path, spec: dict) -> Path | None:
    marker = spec["tree_marker"]
    if not marker:
        return None
    for d in (bindir, root, bindir.parent, bindir.parent.parent):
        if (d / marker).is_file():
            return d
    return None


def plan_files(root: Path, engine_id: str) -> list[tuple[Path, str]]:
    """[(source file, path under the engine's folder)] a source holds; InstallError when it lacks the engine."""
    spec = SPEC[engine_id]
    bindir = _binary_dir(root, spec)
    if bindir is None:
        raise InstallError("source_incomplete", f"{spec['main']} was not found in {root}")
    out: list[tuple[Path, str]] = []
    seen = set()
    for pattern in spec["binaries"]:
        for f in sorted(bindir.glob(pattern)):
            if f.is_file() and f.name not in seen:
                seen.add(f.name)
                out.append((f, f.name))
    if spec["tree_marker"]:
        tree = _tree_root(bindir, root, spec)
        if tree is None:
            raise InstallError("source_incomplete", f"{spec['tree_marker']} (the Python server) was not found near {bindir}")
        for name in spec["tree"]:
            top = tree / name
            if not top.is_dir():
                if name in ("serve",):
                    raise InstallError("source_incomplete", f"the folder {name} is missing from {tree}")
                continue
            for dirpath, dirs, files in os.walk(top):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for fn in files:
                    if fn.startswith("test_") or fn.endswith((".pyc", ".bak")):
                        continue
                    f = Path(dirpath) / fn
                    out.append((f, f.relative_to(tree).as_posix()))
    return out


def _copy(files: list[tuple[Path, str]], dest: Path, engine_id: str) -> None:
    for src, rel in files:
        target = dest / rel
        with _lock:
            _jobs[engine_id]["file"] = rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".part")
            with open(src, "rb") as fi, open(tmp, "wb") as fo:
                while True:
                    block = fi.read(CHUNK)
                    if not block:
                        break
                    fo.write(block)
                    with _lock:
                        _jobs[engine_id]["copied"] += len(block)
            os.replace(tmp, target)
        except PermissionError as e:
            raise InstallError("in_use", f"{rel} cannot be replaced (is the engine running? stop it first): {e}") from None
        except OSError as e:
            raise InstallError("copy_failed", f"{rel}: {e}") from None


def _unpack(archive: Path, into: Path) -> None:
    root = into.resolve()
    try:
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                target = (into / info.filename).resolve()
                if root != target and root not in target.parents:
                    raise InstallError("source_incomplete", f"{info.filename}: outside the archive's folder")
            z.extractall(into)
    except zipfile.BadZipFile:
        raise InstallError("source_incomplete", f"{archive} is not a readable .zip") from None


def _run(engine_id: str, source: Path, files: list[tuple[Path, str]] | None) -> None:
    spec = SPEC[engine_id]
    dest = _root() / "bin" / spec["dest"]
    stage = None
    try:
        if files is None:                                        # an archive: unpack it, then see what it holds
            (_root() / "bin").mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=".install-", dir=_root() / "bin"))
            _unpack(source, stage)
            files = plan_files(stage, engine_id)
            with _lock:
                _jobs[engine_id]["total"] = sum(f.stat().st_size for f, _ in files)
        dest.mkdir(parents=True, exist_ok=True)
        _copy(files, dest, engine_id)
        with _lock:
            _jobs[engine_id].update(status="done", file="")
    except InstallError as e:
        with _lock:
            _jobs[engine_id].update(status="error", error={"code": e.code, "message": e.message})
    except Exception as e:                                       # the thread must always end the job
        with _lock:
            _jobs[engine_id].update(status="error", error={"code": "copy_failed", "message": str(e)})
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


def start(engine_id: str, source: str | None = None, remember: bool = False) -> tuple[int, dict]:
    """(HTTP status, body) of an install request. 200: already installed (no source given); 202: copying; 501: no
    source and no package to download; 400/409: the source or a running copy."""
    from serve import engines
    if engine_id not in engines.ENGINES:
        return _error(404, "unknown_engine", "not found")
    label = engines.ENGINES[engine_id]["label"]
    if source is None and engines.installed(engine_id):
        return 200, {"engine": engine_id, "status": "installed"}
    source = source or sources().get(engine_id)
    if not source:
        return _error(501, "not_available_yet",
                      f"installing the {label} engine is not available yet: point to a local folder or archive that holds it",
                      "not_available")
    if not isinstance(source, str) or not os.path.exists(source):
        return _error(400, "source_missing", f"{source}: not a folder or archive on this PC")
    cur = job(engine_id)
    if cur and cur["status"] == "running":
        return _error(409, "install_running", f"{label} is being installed already", "model_busy")
    src = Path(source)
    files = None
    total = 0
    try:
        if src.is_dir():
            files = plan_files(src, engine_id)
            total = sum(f.stat().st_size for f, _ in files)
        elif src.suffix.lower() != ".zip":
            return _error(400, "source_incomplete", f"{source}: a folder or a .zip archive is needed")
    except InstallError as e:
        return _error(400, e.code, e.message)
    if remember:
        set_source(engine_id, str(src))
    with _lock:
        _jobs[engine_id] = {"engine": engine_id, "status": "running", "copied": 0, "total": total, "file": "",
                            "error": None, "source": str(src)}
    threading.Thread(target=_run, args=(engine_id, src, files), daemon=True, name=f"install-{engine_id}").start()
    return 202, {"engine": engine_id, "status": "running", "total": total}
