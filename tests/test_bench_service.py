"""The BenchService facade (tadamoo/bench/service.py) from install to publishing, with fakes only. CPU only."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.bench import importer, registry
from tadamoo.bench.importer import Result
from tadamoo.bench.runner import FakeAdapter
from tadamoo.bench.service import BenchService
from tadamoo.eval import resume
from bench_support import MODEL, make_remote, make_spec, ready_probes


@pytest.fixture(autouse=True)
def fixed_tree(monkeypatch):
    monkeypatch.setattr(resume, "git_state", lambda root: {"commit": "abc123", "dirty": False, "changes": ""})


@pytest.fixture
def env(tmp_path):
    remote = make_remote(tmp_path / "remote")
    spec = make_spec(remote, leaderboard="csv_pr")
    fake = FakeAdapter(id="fakebench", outcomes={("t2", 1): {"passed": False, "error": "timed out"}})

    def service(**over):
        kw = dict(specs={spec.id: spec}, adapters={spec.id: lambda: fake}, probes=ready_probes(), root=tmp_path)
        kw.update(over)
        return BenchService(tmp_path / "data" / "benchmarks", kw.pop("power", lambda: 400.0), kw.pop("model", lambda: dict(MODEL)), **kw)

    return service, spec, fake


def run_to_end(svc, settings=None):
    got = svc.start_run("fakebench", settings or {"trials": 2})
    assert got["ok"], got
    svc.wait(got["run_id"], 20)
    return got["run_id"]


def test_the_whole_story(env):
    service, spec, fake = env
    svc = service()
    listed = svc.list_benchmarks()
    assert listed[0]["id"] == "fakebench" and listed[0]["status"]["installed"] is False and listed[0]["adapter"]["available"]
    json.dumps(listed)
    assert svc.preflight("fakebench")["ready"] is False                              # not imported yet
    assert svc.start_run("fakebench", {})["ok"] is False
    inst = svc.install("fakebench")
    assert inst["ok"] and inst["meta"]["commit"] and svc.list_benchmarks()[0]["status"]["installed"]
    pre = svc.preflight("fakebench")
    assert pre["ready"] and not [c for c in pre["checks"] if "judge" in c["id"]]
    rid = run_to_end(svc)
    st = svc.run_state(rid)
    assert st["ok"] and st["status"] == "done" and st["done"] == 4 and st["running"] is False and st["power_limit_w"] == 400.0
    assert st["model"]["quantisation"] == "IQ4_XS" and st["benchmark_commit"] == inst["meta"]["commit"]
    assert [r["run_id"] for r in svc.runs()] == [rid]
    rep = svc.report(rid)
    assert rep["ok"] and rep["classes"] == {"timeout": 1} and Path(rep["report_path"]).is_file()
    rk = svc.ranking(rid, "model,resolution_rate\na,0.9\nb,0.5\nc,0.1\n")
    assert rk["ok"] and rk["position"] == 2 and rk["of"] == 4 and rk["basis"] == "entries" and rk["provisional"] is False
    assert svc.ranking(rid)["basis"] == "local_only"
    prep = svc.prepare_publish(rid)
    assert prep["ok"] and {f["name"] for f in prep["files"]} == {"entry.json", "results_row.csv"}
    b = svc.begin_publish(rid, "csv_pr")
    assert b["ok"] and b["token"]
    bad = svc.finish_publish(b["token"], "no")
    assert bad["ok"] is False and bad["error_type"] == "BadConfirmation"
    refused = svc.finish_publish(b["token"], b["confirm_phrase"])
    assert refused["ok"] is False and refused["error_type"] == "NotConfigured"       # no transport injected: nothing leaves


def test_publishing_through_an_injected_transport(env):
    service, spec, fake = env
    sent = []
    svc = service(transports={"csv_pr": lambda folder, manifest, dest: sent.append(manifest["files"]) or {"sent": True}})
    svc.install("fakebench")
    rid = run_to_end(svc)
    b = svc.begin_publish(rid, "csv_pr")
    assert sent == []
    assert svc.finish_publish(b["token"], b["confirm_phrase"])["sent"] is True and len(sent) == 1


def test_update_and_remove(env):
    service, spec, fake = env
    svc = service()
    svc.install("fakebench")
    assert svc.update("fakebench")["meta"]["changed"] is False
    assert svc.remove("fakebench") == {"ok": True, "removed": True}
    assert svc.remove("nope")["ok"] is False and svc.install("nope")["ok"] is False and svc.preflight("nope")["ok"] is False


def test_a_failed_install_is_an_error_value(env):
    service, spec, fake = env
    svc = service(runner=lambda args, cwd=None, timeout=None, env=None: Result(128, "", "fatal: no route to host"))
    got = svc.install("fakebench")
    assert got["ok"] is False and "no route to host" in got["error"]


@pytest.mark.parametrize("settings,word", [({"model": "gpt-5"}, "gpt-5"), ({"provider": "anthropic"}, "anthropic"), ({"api_key": "sk-1"}, "API key"),
                                            ({"judge_model": "claude"}, "claude"), ({"base_url": "https://api.openai.com"}, "api.openai.com"),
                                            ({"judge": "gpt-judge"}, "gpt-judge")])
def test_settings_naming_another_model_are_refused(env, settings, word):
    service, spec, fake = env
    svc = service()
    svc.install("fakebench")
    got = svc.start_run("fakebench", settings)
    assert got["ok"] is False and word in got["error"] and svc.runs() == []


def test_the_served_model_and_the_power_limit_are_recorded_and_required(env):
    service, spec, fake = env
    svc = service(power=lambda: None)
    svc.install("fakebench")
    assert "power limit" in svc.start_run("fakebench", {})["error"]
    svc = service(model=lambda: {})
    svc.install("fakebench")
    assert "served model" in svc.start_run("fakebench", {})["error"]


def test_a_benchmark_without_an_adapter_cannot_start(env):
    service, spec, fake = env
    svc = service(adapters={})
    svc.install("fakebench")
    got = svc.start_run("fakebench", {})
    assert got["ok"] is False and "not written yet" in got["error"]
    assert svc.list_benchmarks()[0]["adapter"]["available"] is False


def test_the_real_registry_lists_an_adapter_exactly_where_its_module_exists(tmp_path):
    import importlib.util
    from tadamoo.bench import adapters
    svc = BenchService(tmp_path / "b", lambda: 400.0, lambda: dict(MODEL), probes=ready_probes())
    listed = {b["id"]: b["adapter"]["available"] for b in svc.list_benchmarks()}
    assert len(listed) == len(registry.SPECS)
    assert listed == {i: importlib.util.find_spec(adapters.MODULES[i]) is not None for i in registry.IDS}


def test_another_gpu_session_blocks_a_start(env):
    service, spec, fake = env
    svc = service(gpu_busy=lambda: "night session")
    svc.install("fakebench")
    got = svc.start_run("fakebench", {})
    assert got["ok"] is False and "night session" in got["error"]


def test_only_one_run_at_a_time_and_stop_then_resume(env):
    service, spec, fake = env
    import threading
    gate = threading.Event()
    started = threading.Event()

    def hold(key):
        started.set()
        gate.wait(10)
    fake.on_trial = hold
    svc = service()
    svc.install("fakebench")
    first = svc.start_run("fakebench", {"trials": 2})["run_id"]
    assert started.wait(10)
    assert "still going" in svc.start_run("fakebench", {})["error"]
    assert svc.stop_run(first) == {"ok": True, "stopping": True}
    gate.set()
    svc.wait(first, 20)
    assert svc.run_state(first)["status"] == "stopped"
    fake.on_trial = None
    assert svc.resume_run(first)["ok"]
    svc.wait(first, 20)
    assert svc.run_state(first)["status"] == "done"
    assert svc.stop_run("nope")["ok"] is False and svc.run_state("../x")["ok"] is False


def test_a_cloud_judge_benchmark_is_labelled(env):
    service, spec, fake = env
    cloud = make_spec(spec.source, official_cloud_judge=True)
    svc = service(specs={cloud.id: cloud})
    svc.install("fakebench")
    rid = run_to_end(svc)
    st = svc.run_state(rid)
    assert st["provisional"] and "unofficial judge" in st["provisional_label"]
    assert svc.ranking(rid, "model,resolution_rate\na,0.9\n")["provisional"] is True
    assert svc.begin_publish(rid, "csv_pr")["ok"] is False


def test_the_base_folder_cannot_be_inside_the_repository():
    with pytest.raises(importer.BenchImportError):
        BenchService(importer.REPO_ROOT / "tmp_benchmarks", lambda: 1.0, lambda: dict(MODEL))
