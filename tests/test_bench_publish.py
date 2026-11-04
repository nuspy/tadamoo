"""Preparing and publishing a result (tadamoo/bench/publish.py): two separate calls, an exact phrase, nothing sent by itself. CPU only."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.bench import publish
from tadamoo.bench.publish import BadConfirmation, NotConfigured, NotOffered, PackageChanged, PublishDesk, PublishError
from tadamoo.bench.runner import BenchRun, FakeAdapter
from tadamoo.eval import resume
from bench_support import MODEL, make_spec


@pytest.fixture(autouse=True)
def fixed_tree(monkeypatch):
    monkeypatch.setattr(resume, "git_state", lambda root: {"commit": "abc123", "dirty": False, "changes": ""})


def finished_run(tmp_path, kind="email", **spec_over):
    spec = make_spec("x", leaderboard=kind, **spec_over)
    run = BenchRun(tmp_path / "b", spec, {"trials": 1}, adapter=FakeAdapter(outcomes={("t2", 1): {"passed": False, "error": "timed out"}, ("t1", 1): {"events": [{"type": "stop", "reason": "completed"}]}}), model=MODEL,
                   power_limit_w=400, imported_commit="c0ffee")
    run.execute()
    return run, {spec.id: spec}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_package_holds_exactly_the_listed_files(tmp_path):
    run, specs = finished_run(tmp_path)
    pkg = publish.prepare_package(run, "email", specs)
    folder = Path(pkg["folder"])
    on_disk = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
    assert on_disk == {f["name"] for f in pkg["files"]} | {"manifest.json", "SUMMARY.txt"}
    assert {"entry.json", "trials_summary.json", "report.md", "message.txt"} == {f["name"] for f in pkg["files"]}
    for f in pkg["files"]:
        data = (folder / f["name"]).read_bytes()
        assert f["size"] == len(data) and f["sha256"] == hashlib.sha256(data).hexdigest()
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["destination"]["who"] == "Maintainers <m@example.org>" and manifest["destination"]["kind"] == "email"
    assert "Files that would leave this PC" in pkg["summary"] and "entry.json" in pkg["summary"]


def test_nothing_private_is_in_the_entry(tmp_path):
    run, specs = finished_run(tmp_path)
    pkg = publish.prepare_package(run, "email", specs)
    entry = json.loads((Path(pkg["folder"]) / "entry.json").read_text())
    assert entry["model"] == {"name": "Qwen3.8-Flash-Next", "quantisation": "IQ4_XS"} and entry["gpu_power_limit_w"] == 400 and entry["benchmark_commit"] == "c0ffee"
    for f in Path(pkg["folder"]).rglob("*"):
        if f.is_file():
            assert "D:/models" not in f.read_text(encoding="utf-8") and str(tmp_path) not in f.read_text(encoding="utf-8")


def test_a_pull_request_package_is_a_results_row(tmp_path):
    run, specs = finished_run(tmp_path, "csv_pr")
    pkg = publish.prepare_package(run, "csv_pr", specs)
    assert {f["name"] for f in pkg["files"]} == {"entry.json", "results_row.csv"}
    text = (Path(pkg["folder"]) / "results_row.csv").read_text()
    assert text.splitlines()[0].startswith("model,harness,resolution_rate") and "TaDaMoo" in text


def test_trajectories_leave_only_when_asked(tmp_path):
    run, specs = finished_run(tmp_path)
    assert not any(f["name"].startswith("trials/") for f in publish.prepare_package(run, "email", specs)["files"])
    assert any(f["name"].startswith("trials/") for f in publish.prepare_package(run, "email", specs, include_trajectories=True)["files"])


def test_a_destination_the_benchmark_does_not_offer_is_refused(tmp_path):
    run, specs = finished_run(tmp_path, "email")
    with pytest.raises(NotOffered, match="not 'csv_pr'"):
        publish.prepare_package(run, "csv_pr", specs)
    run2, specs2 = finished_run(tmp_path / "n", "none")
    with pytest.raises(NotOffered, match="no route"):
        publish.prepare_package(run2, "none", specs2)


def test_an_unfinished_run_has_nothing_to_publish(tmp_path):
    spec = make_spec("x")
    run = BenchRun(tmp_path / "b", spec, {}, adapter=FakeAdapter(), model=MODEL, power_limit_w=400)
    with pytest.raises(PublishError, match="not finished"):
        publish.prepare_package(run, "email", {spec.id: spec})


class Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, folder, manifest, destination):
        self.calls.append(manifest["destination"]["kind"])
        return {"sent": True, "transport": "spy"}


def desk_for(specs, spy=None, clock=None):
    return PublishDesk({"csv_pr": spy} if spy else None, specs, clock or Clock())


def test_publishing_needs_two_calls_and_the_exact_phrase(tmp_path):
    run, specs = finished_run(tmp_path, "csv_pr")
    spy = Spy()
    desk = desk_for(specs, spy)
    begun = desk.begin_publish(run, "csv_pr")
    assert spy.calls == [] and begun["token"] and begun["confirm_phrase"] in begun["summary"] and "Nothing has been sent" in begun["summary"]
    done = desk.finish_publish(begun["token"], begun["confirm_phrase"])
    assert spy.calls == ["csv_pr"] and done["sent"] and done["kind"] == "csv_pr"
    with pytest.raises(BadConfirmation, match="Unknown or already used"):
        desk.finish_publish(begun["token"], begun["confirm_phrase"])


def test_a_wrong_phrase_sends_nothing_and_three_wrong_burn_the_token(tmp_path):
    run, specs = finished_run(tmp_path, "csv_pr")
    spy = Spy()
    desk = desk_for(specs, spy)
    b = desk.begin_publish(run, "csv_pr")
    for wrong in (b["confirm_phrase"].lower(), "yes", ""):
        with pytest.raises(BadConfirmation, match="does not match"):
            desk.finish_publish(b["token"], wrong)
    with pytest.raises(BadConfirmation, match="Unknown or already used"):
        desk.finish_publish(b["token"], b["confirm_phrase"])
    assert spy.calls == []


def test_a_wrong_token_fails(tmp_path):
    run, specs = finished_run(tmp_path, "csv_pr")
    spy = Spy()
    desk = desk_for(specs, spy)
    b = desk.begin_publish(run, "csv_pr")
    with pytest.raises(BadConfirmation):
        desk.finish_publish("not-the-token", b["confirm_phrase"])
    assert spy.calls == []


def test_an_expired_token_fails(tmp_path):
    run, specs = finished_run(tmp_path, "csv_pr")
    clock, spy = Clock(), Spy()
    desk = desk_for(specs, spy, clock)
    b = desk.begin_publish(run, "csv_pr")
    clock.now += publish.TOKEN_LIFETIME_S + 1
    with pytest.raises(BadConfirmation, match="expired"):
        desk.finish_publish(b["token"], b["confirm_phrase"])
    assert spy.calls == []


@pytest.mark.parametrize("change", ["edit", "add", "remove"])
def test_a_changed_package_fails(tmp_path, change):
    run, specs = finished_run(tmp_path, "csv_pr")
    spy = Spy()
    desk = desk_for(specs, spy)
    b = desk.begin_publish(run, "csv_pr")
    folder = Path(b["folder"])
    if change == "edit":
        (folder / "results_row.csv").write_text("model\nsomething else\n")
    elif change == "add":
        (folder / "extra.txt").write_text("more")
    else:
        (folder / "results_row.csv").unlink()
    with pytest.raises(PackageChanged):
        desk.finish_publish(b["token"], b["confirm_phrase"])
    assert spy.calls == []


def test_a_pull_request_without_a_transport_is_not_configured(tmp_path):
    run, specs = finished_run(tmp_path, "csv_pr")
    desk = desk_for(specs)
    b = desk.begin_publish(run, "csv_pr")
    with pytest.raises(NotConfigured):
        desk.finish_publish(b["token"], b["confirm_phrase"])


@pytest.mark.parametrize("kind", ["email", "closed"])
def test_email_and_closed_boards_are_only_prepared(tmp_path, kind):
    run, specs = finished_run(tmp_path, kind)
    desk = desk_for(specs)
    b = desk.begin_publish(run, {"kind": kind})
    done = desk.finish_publish(b["token"], b["confirm_phrase"])
    assert done["sent"] is False and done["transport"] == "prepared_only" and "Nothing was sent" in done["message"]


def test_a_provisional_score_is_not_published(tmp_path):
    run, specs = finished_run(tmp_path, "csv_pr", official_cloud_judge=True)
    desk = desk_for(specs, Spy())
    with pytest.raises(PublishError, match="provisional"):
        desk.begin_publish(run, "csv_pr")
    assert publish.prepare_package(run, "csv_pr", specs)["manifest"]["provisional"] is True            # preparing is still allowed


def test_the_phrase_changes_with_the_package(tmp_path):
    run, specs = finished_run(tmp_path, "csv_pr")
    desk = desk_for(specs, Spy())
    a = desk.begin_publish(run, "csv_pr")["confirm_phrase"]
    assert a.startswith("PUBLISH ") and len(a) == len("PUBLISH ") + 8
    assert desk.begin_publish(run, "csv_pr")["confirm_phrase"] == a                  # the same package, the same phrase
