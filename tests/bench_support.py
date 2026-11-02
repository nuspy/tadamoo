"""Shared fakes for the benchmark tests: a small local git repository standing in for a benchmark, a spec pointing at it, and probes that see a ready machine."""
import os
import subprocess
from pathlib import Path

from harness.bench import preflight
from harness.bench.importer import Result
from harness.bench.registry import BenchSpec, Needs, Subset

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True).stdout.strip()


def make_remote(folder: Path, files=None) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    git(folder, "init", "-q")
    commit_files(folder, files or {"LICENSE": "Apache License fake\n", "tasks/a.txt": "a\n"}, "first")
    return str(folder)


def commit_files(folder: Path, files, message: str) -> str:
    for name, text in files.items():
        p = folder / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", message)
    return git(folder, "rev-parse", "HEAD")


def make_spec(source: str, **over) -> BenchSpec:
    base = dict(id="fakebench", title="Fake bench", source_kind="git", source=source, branch="", licence="Apache-2.0", needs=Needs(internet=False),
                subsets=(Subset("all", "All"),), scoring="verifier", primary_metric="resolution_rate", leaderboard="email", leaderboard_target="Maintainers <m@example.org>",
                disk_gb=0.001)
    base.update(over)
    return BenchSpec(**base)


def ok_runner(args, cwd=None, timeout=None, env=None):
    return Result(0, "ok", "")


def ready_probes(**over) -> preflight.Probes:
    p = preflight.Probes(runner=ok_runner, which=lambda name: f"/bin/{name}", disk_free=lambda path: 10 ** 12, gpu_busy=None, online=lambda host: True,
                         python=(3, 11, 0), platform="win32")
    for k, v in over.items():
        setattr(p, k, v)
    return p


MODEL = {"name": "Qwen3.8-Flash-Next", "file": "D:/models/qwen38-iq4_xs.gguf", "quantisation": "IQ4_XS", "endpoint": "http://127.0.0.1:7232"}
