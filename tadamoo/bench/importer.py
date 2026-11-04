"""Fetching a benchmark into the user data folder: `<base>/<id>/source` holds a shallow clone of the default branch, `<base>/<id>/meta.json` the commit
that was resolved and the date. Nothing is ever written inside the repository, and every command goes through an injectable runner."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .registry import BenchSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
META = "meta.json"
SOURCE = "source"
LICENCE_NAMES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "NOTICE")
CLONE_TIMEOUT_S = 3600


class BenchImportError(Exception):
    """An import, update or removal that could not be done; the message says why in plain words."""


class Result:
    """What a command returned: the exit code and the text it wrote."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


Runner = Callable[..., Result]


def default_runner(args: Sequence[str], cwd: Optional[Path] = None, timeout: Optional[float] = None, env: Optional[Dict[str, str]] = None) -> Result:
    """Runs a command and captures its output; a program that is missing or too slow is a failed result, not an exception."""
    try:
        p = subprocess.run(list(args), cwd=cwd, capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return Result(127, "", f"{args[0]}: not found")
    except subprocess.TimeoutExpired:
        return Result(124, "", f"{args[0]}: timed out")
    except OSError as exc:
        return Result(126, "", str(exc))
    return Result(p.returncode, _text(p.stdout), _text(p.stderr))


def _text(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or (len(raw) > 1 and raw[1:2] == b"\x00"):         # what `wsl` writes on Windows
        return raw.decode("utf-16", errors="replace").replace("\x00", "")
    return raw.decode("utf-8", errors="replace")


def ensure_outside_repo(base_dir: Path, repo: Path = REPO_ROOT) -> Path:
    """The resolved base folder; refused when it lies inside the repository (benchmark code and data never go there)."""
    base = Path(base_dir).resolve()
    if base == repo or repo in base.parents:
        raise BenchImportError(f"{base} is inside the repository; benchmarks are kept in the user data folder, outside it")
    return base


def bench_dir(base_dir: Path, spec: BenchSpec) -> Path:
    return Path(base_dir) / spec.id


def source_dir(base_dir: Path, spec: BenchSpec) -> Path:
    return bench_dir(base_dir, spec) / SOURCE


def folder_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _writable_then(func: Callable[[str], Any], path: str, _exc: Any) -> None:
    os.chmod(path, stat.S_IWRITE)                 # git marks its objects read-only, which Windows will not delete
    func(path)


def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, onerror=_writable_then)


def _clone_args(spec: BenchSpec, dest: Path) -> List[str]:
    args = ["git", "clone", "--depth", "1"]
    if spec.sparse:
        args += ["--filter=blob:none", "--sparse"]
    if spec.branch:
        args += ["--branch", spec.branch]
    return args + [spec.source, str(dest)]


def import_benchmark(spec: BenchSpec, base_dir: Path, runner: Runner = default_runner, clock: Callable[[], float] = time.time) -> Dict[str, Any]:
    """Fetches the benchmark at the head of its branch (the default one unless the spec names one) and records the commit and the date. A copy already
    there is replaced only when the new one was fetched completely. Returns the meta that was written."""
    base = ensure_outside_repo(base_dir)
    folder = bench_dir(base, spec)
    previous = read_meta(base, spec)
    base.mkdir(parents=True, exist_ok=True)
    staging = base / f".{spec.id}.{uuid.uuid4().hex[:8]}.new"
    try:
        (staging).mkdir()
        clone = staging / SOURCE
        got = runner(_clone_args(spec, clone), None, CLONE_TIMEOUT_S)
        if got.returncode != 0:
            raise BenchImportError(f"fetching {spec.source} failed: {_tail(got)}")
        if spec.sparse:
            picked = runner(["git", "sparse-checkout", "set", *spec.sparse], clone, CLONE_TIMEOUT_S)
            if picked.returncode != 0:
                raise BenchImportError(f"checking out {', '.join(spec.sparse)} failed: {_tail(picked)}")
        commit = runner(["git", "rev-parse", "HEAD"], clone, 60)
        if commit.returncode != 0 or not commit.stdout.strip():
            raise BenchImportError(f"the fetched copy has no readable commit: {_tail(commit)}")
        branch = runner(["git", "rev-parse", "--abbrev-ref", "HEAD"], clone, 60)
        now = clock()
        meta = {"id": spec.id, "title": spec.title, "source": spec.source, "branch": branch.stdout.strip() if branch.returncode == 0 else spec.branch,
                "commit": commit.stdout.strip(), "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)), "imported_epoch": now,
                "licence": spec.licence, "licence_files": _keep_licences(clone, staging), "size_bytes": folder_size(clone),
                "previous_commit": previous.get("commit") if previous else None}
        meta["changed"] = meta["previous_commit"] != meta["commit"]
        (staging / META).write_text(json.dumps(meta, indent=2), encoding="utf-8")
        old = None
        if folder.exists():
            old = base / f".{spec.id}.{uuid.uuid4().hex[:8]}.old"
            folder.rename(old)
        staging.rename(folder)
        if old is not None:
            _rmtree(old)
        return meta
    finally:
        _rmtree(staging)


def _keep_licences(clone: Path, target: Path) -> List[str]:
    kept = []
    for name in LICENCE_NAMES:
        f = clone / name
        if f.is_file():
            shutil.copyfile(f, target / name)
            kept.append(name)
    return kept


def _tail(got: Result) -> str:
    text = (got.stderr or got.stdout).strip().splitlines()
    return text[-1] if text else f"exit code {got.returncode}"


def update(spec: BenchSpec, base_dir: Path, runner: Runner = default_runner, clock: Callable[[], float] = time.time) -> Dict[str, Any]:
    """Imports again at the current head; the meta says whether the commit changed."""
    return import_benchmark(spec, base_dir, runner, clock)


def remove(spec: BenchSpec, base_dir: Path) -> bool:
    """Deletes the benchmark's folder (runs are kept apart and untouched). False when it was not there."""
    base = ensure_outside_repo(base_dir)
    folder = bench_dir(base, spec)
    if not folder.exists():
        return False
    _rmtree(folder)
    return True


def read_meta(base_dir: Path, spec: BenchSpec) -> Optional[Dict[str, Any]]:
    try:
        return json.loads((bench_dir(base_dir, spec) / META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status(spec: BenchSpec, base_dir: Path) -> Dict[str, Any]:
    """{installed, commit, imported_at, branch, size_bytes, path}; installed only when both the meta and the copy are there."""
    folder = bench_dir(base_dir, spec)
    meta = read_meta(base_dir, spec)
    if meta is None or not (folder / SOURCE).is_dir():
        return {"installed": False, "commit": None, "imported_at": None, "branch": None, "size_bytes": 0, "path": str(folder)}
    return {"installed": True, "commit": meta.get("commit"), "imported_at": meta.get("imported_at"), "branch": meta.get("branch"),
            "size_bytes": folder_size(folder), "path": str(folder)}
