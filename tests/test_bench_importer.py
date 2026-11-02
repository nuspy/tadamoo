"""Importing, updating and removing a benchmark (harness/bench/importer.py) with a local git repository standing in for the benchmark. CPU only."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.bench import importer
from harness.bench.importer import BenchImportError, Result
from bench_support import commit_files, git, make_remote, make_spec


@pytest.fixture
def remote(tmp_path):
    return make_remote(tmp_path / "remote")


def test_import_records_commit_date_licence_and_size(tmp_path, remote):
    spec = make_spec(remote)
    base = tmp_path / "data" / "benchmarks"
    meta = importer.import_benchmark(spec, base, clock=lambda: 1_800_000_000)
    assert meta["commit"] == git(remote, "rev-parse", "HEAD")
    assert meta["imported_at"].startswith("2027") or meta["imported_at"].startswith("2026")
    assert meta["licence_files"] == ["LICENSE"] and (base / "fakebench" / "LICENSE").is_file()
    assert (base / "fakebench" / "source" / "tasks" / "a.txt").is_file()
    assert meta["size_bytes"] > 0 and meta["previous_commit"] is None
    st = importer.status(spec, base)
    assert st["installed"] and st["commit"] == meta["commit"] and st["size_bytes"] > 0
    assert not [p for p in base.iterdir() if p.name.startswith(".")]            # no staging folder left


def test_status_of_something_not_imported(tmp_path, remote):
    st = importer.status(make_spec(remote), tmp_path / "b")
    assert st["installed"] is False and st["commit"] is None


def test_update_takes_the_new_head_and_says_so(tmp_path, remote):
    spec = make_spec(remote)
    base = tmp_path / "b"
    first = importer.import_benchmark(spec, base)
    again = importer.update(spec, base)
    assert again["changed"] is False and again["commit"] == first["commit"]
    new = commit_files(Path(remote), {"tasks/b.txt": "b\n"}, "second")
    changed = importer.update(spec, base)
    assert changed["changed"] and changed["previous_commit"] == first["commit"] and changed["commit"] == new
    assert (base / "fakebench" / "source" / "tasks" / "b.txt").is_file()


def test_a_failed_fetch_keeps_the_old_copy(tmp_path, remote):
    spec = make_spec(remote)
    base = tmp_path / "b"
    first = importer.import_benchmark(spec, base)

    def broken(args, cwd=None, timeout=None, env=None):
        return Result(128, "", "fatal: unable to access")

    with pytest.raises(BenchImportError, match="unable to access"):
        importer.update(spec, base, runner=broken)
    assert importer.status(spec, base)["commit"] == first["commit"]
    assert not [p for p in base.iterdir() if p.name.startswith(".")]


def test_the_commands_are_a_shallow_clone_of_the_default_branch(tmp_path):
    seen = []

    def runner(args, cwd=None, timeout=None, env=None):
        seen.append(list(args))
        if args[:2] == ["git", "clone"]:
            Path(args[-1]).mkdir(parents=True)
        return Result(0, "abc123\n", "")

    spec = make_spec("https://example.org/x.git")
    importer.import_benchmark(spec, tmp_path / "b", runner)
    assert seen[0][:4] == ["git", "clone", "--depth", "1"] and "--branch" not in seen[0] and seen[0][-2] == "https://example.org/x.git"
    seen.clear()
    importer.import_benchmark(make_spec("https://example.org/x.git", branch="dev"), tmp_path / "b2", runner)
    assert seen[0][4:6] == ["--branch", "dev"]


def test_remove_deletes_only_that_benchmark(tmp_path, remote):
    spec = make_spec(remote)
    base = tmp_path / "b"
    importer.import_benchmark(spec, base)
    (base / "runs" / "r1").mkdir(parents=True)
    assert importer.remove(spec, base) is True
    assert not (base / "fakebench").exists() and (base / "runs" / "r1").is_dir()
    assert importer.remove(spec, base) is False


def test_a_folder_inside_the_repository_is_refused(remote):
    inside = importer.REPO_ROOT / "benchmarks_test_tmp"
    spec = make_spec(remote)
    with pytest.raises(BenchImportError, match="inside the repository"):
        importer.import_benchmark(spec, inside)
    with pytest.raises(BenchImportError):
        importer.remove(spec, inside)
    assert not inside.exists()


def test_a_benchmark_that_is_one_folder_of_a_large_repository_is_fetched_sparse():
    from harness.bench import importer, registry
    spec = registry.SPECS["bfcl-v4"]
    args = importer._clone_args(spec, Path("dest"))
    assert "--sparse" in args and "--filter=blob:none" in args and spec.sparse == ("berkeley-function-call-leaderboard",)
    assert "--sparse" not in importer._clone_args(registry.SPECS["clawbench"], Path("dest"))
