"""How the runner and the service find the adapters (tadamoo/bench/adapters/__init__.py): lazily, one module per benchmark, a missing module is only a missing
adapter. CPU only."""
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_support import MODEL
from tadamoo.bench import adapters, registry
from tadamoo.bench.runner import ADAPTERS, AdapterNotWritten, FakeAdapter, get_adapter
from tadamoo.bench.service import BenchService


def test_the_table_names_a_module_for_every_benchmark():
    assert set(adapters.MODULES) == set(registry.IDS)
    assert adapters.MODULES["terminal-bench-2"].endswith(".terminal_bench") and adapters.MODULES["swe-bench-verified"].endswith(".swe_bench")


def test_load_returns_the_adapter_class_of_a_module():
    assert adapters.load("terminal-bench-2").__name__ == "TerminalBenchAdapter"
    assert adapters.load("swe-bench-verified")().id == "swe-bench-verified"


def test_a_missing_module_is_a_missing_adapter_and_a_broken_one_is_an_error(tmp_path, monkeypatch):
    with pytest.raises(adapters.AdapterModuleMissing, match="no adapter module is listed"):
        adapters.load("unknown")
    with pytest.raises(adapters.AdapterModuleMissing, match="does not exist yet"):
        adapters.load("x", {"x": "tadamoo.bench.adapters.not_written_yet"})
    (tmp_path / "bench_adapter_broken.py").write_text("import a_package_nobody_has\n", encoding="utf-8")
    (tmp_path / "bench_adapter_empty.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "bench_adapter_fine.py").write_text(textwrap.dedent("class A:\n    id = 'fine'\nADAPTER = A\n"), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ModuleNotFoundError, match="a_package_nobody_has"):
        adapters.load("x", {"x": "bench_adapter_broken"})            # a fault inside an adapter is not hidden as "not written"
    with pytest.raises(adapters.AdapterModuleMissing, match="does not define ADAPTER"):
        adapters.load("x", {"x": "bench_adapter_empty"})
    assert adapters.load("x", {"x": "bench_adapter_fine"})().id == "fine"


def test_get_adapter_looks_in_the_table_first_and_in_the_modules_only_when_asked():
    with pytest.raises(AdapterNotWritten):
        get_adapter("terminal-bench-2")
    with pytest.raises(AdapterNotWritten):
        get_adapter("terminal-bench-2", {})
    with pytest.raises(AdapterNotWritten):
        get_adapter("terminal-bench-2", {}, lazy=False)
    assert get_adapter("terminal-bench-2", lazy=True).id == "terminal-bench-2"
    assert get_adapter("terminal-bench-2", {"terminal-bench-2": FakeAdapter}, lazy=True).id == "fake"       # an entry in the table wins
    assert "terminal-bench-2" not in ADAPTERS                                                              # loading does not fill the table


def test_the_service_finds_the_adapters_by_default_and_keeps_an_explicit_table(tmp_path):
    svc = BenchService(tmp_path / "base", lambda: 450.0, lambda: MODEL, root=tmp_path)
    listed = {b["id"]: b["adapter"] for b in svc.list_benchmarks()}
    assert listed["terminal-bench-2"]["available"] and listed["swe-bench-verified"]["available"]
    fixed = BenchService(tmp_path / "base2", lambda: 450.0, lambda: MODEL, adapters={}, root=tmp_path)
    assert not any(b["adapter"]["available"] for b in fixed.list_benchmarks())
