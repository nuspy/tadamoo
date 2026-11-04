"""The Benchmarks tab's API: the overview, the long actions in a thread, the checks before a start, the lesson button and the two-step publishing, with a fake
BenchService (the service itself is tested in tests/test_bench_*.py), and the routes on this PC's own server."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

from tadamoo.lessons import LessonStore
from serve import handler
from serve.bench_api import BenchApi, BenchApiError
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
MODEL = {"name": "qwen3.8-flash-next", "file": "D:/m/q-IQ4_XS.gguf", "quantisation": "IQ4_XS", "endpoint": "http://127.0.0.1:7232"}
SPEC = {"id": "bfcl-v4", "subsets": [{"id": "smoke", "label": "a few"}, {"id": "full", "label": "all"}]}


class FakeService:
    def __init__(self):
        self.calls = []
        self.gate = None
        self.run_status = "done"
        self.candidates = [{"situation": "Benchmark trials ended in the class 'Timeout'", "went_wrong": "3 of 5 failed trials had this cause", "fix": "Run long commands in the background.",
                            "evidence": ["benchmark run: 3 trials"], "scope": "project", "signature": "bench:timeout:"}]

    def _wait(self):
        if self.gate:
            self.gate.wait(10)

    def list_benchmarks(self):
        return [dict(SPEC, status={"installed": False})]

    def runs(self):
        return [{"run_id": "r1", "benchmark": "bfcl-v4", "status": self.run_status, "created": "x", "done": 1, "total": 2, "provisional": False}]

    def install(self, i):
        self.calls.append(("install", i))
        self._wait()
        return {"ok": True, "meta": {"commit": "abc", "imported_at": "2026-11-01T10:00:00"}}

    def update(self, i):
        self.calls.append(("update", i))
        return {"ok": False, "error": "fetching failed: no route"}

    def remove(self, i):
        self.calls.append(("remove", i))
        return {"ok": True, "removed": True}

    def preflight(self, i):
        return {"ok": True, "id": i, "ready": False, "checks": [{"id": "docker_daemon", "ok": False, "detail": "x", "fix": "Start Docker Desktop", "required": True}]}

    def start_run(self, i, settings):
        self.calls.append(("start", i, settings))
        return {"ok": True, "run_id": "r2"}

    def stop_run(self, r):
        self.calls.append(("stop", r))
        return {"ok": True, "stopping": True}

    def resume_run(self, r):
        self.calls.append(("resume", r))
        return {"ok": True, "run_id": r}

    def run_state(self, r):
        return {"ok": r == "r1", "run_id": r, "status": "running", "error": "no such run"}

    def report(self, r):
        return {"ok": True, "classes": {"timeout": 3}, "candidate_lessons": self.candidates}

    def ranking(self, r, text=None):
        self.calls.append(("ranking", r, text))
        return {"ok": True, "basis": "entries" if text else "local_only"}

    def prepare_publish(self, r):
        return {"ok": True, "folder": "f", "files": [], "summary": "s"}

    def begin_publish(self, r, d):
        self.calls.append(("begin", r, d))
        return {"ok": False, "error": "This score is provisional"} if r == "prov" else {"ok": True, "token": "tok", "confirm_phrase": "PUBLISH 12345678", "summary": "s"}

    def finish_publish(self, token, phrase):
        self.calls.append(("finish", token, phrase))
        return {"ok": False, "error": "The phrase does not match", "error_type": "BadConfirmation"} if phrase != "PUBLISH 12345678" else {"ok": True, "sent": False}


def make(tmp_path, power=400.0, model=MODEL, busy=""):
    fake = FakeService()
    api = BenchApi(tmp_path / "b", lambda: power, lambda: model, lambda: busy, tmp_path / "lessons", service=fake)
    return api, fake


def test_the_overview_has_everything_the_tab_needs(tmp_path):
    api, _ = make(tmp_path)
    o = api.get("overview", {})
    assert [b["id"] for b in o["benchmarks"]] == ["bfcl-v4"] and o["runs"][0]["run_id"] == "r1" and o["model"] == MODEL
    assert o["power_limit_w"] == 400.0 and o["power_limit_measured"] is True and o["gpu_busy"] == "" and o["jobs"] == {}
    json.dumps(o)


def test_an_unreadable_gpu_state_is_reported_as_busy(tmp_path):
    api, _ = make(tmp_path)
    api.busy_source = lambda: 1 / 0
    assert "could not be read" in api.get("overview", {})["gpu_busy"]


def test_install_returns_at_once_and_the_page_polls(tmp_path):
    api, fake = make(tmp_path)
    fake.gate = threading.Event()
    assert api.post("install", {"id": "bfcl-v4"}) == {"ok": True, "started": True, "id": "bfcl-v4", "action": "install"}
    assert api.get("overview", {})["jobs"]["bfcl-v4"]["status"] == "running"
    with pytest.raises(BenchApiError, match="still going") as e:
        api.post("install", {"id": "bfcl-v4"})
    assert e.value.status == 409
    with pytest.raises(BenchApiError, match="going on"):
        api.post("remove", {"id": "bfcl-v4"})
    fake.gate.set()
    api.wait()
    job = api.get("overview", {})["jobs"]["bfcl-v4"]
    assert job["status"] == "done" and job["meta"]["commit"] == "abc"


def test_a_failed_update_shows_its_reason(tmp_path):
    api, _ = make(tmp_path)
    api.post("update", {"id": "bfcl-v4"})
    api.wait()
    job = api.get("overview", {})["jobs"]["bfcl-v4"]
    assert job["status"] == "failed" and "no route" in job["error"]


def test_remove_is_refused_while_a_run_goes_on(tmp_path):
    api, fake = make(tmp_path)
    fake.run_status = "running"
    with pytest.raises(BenchApiError, match="going on"):
        api.post("remove", {"id": "bfcl-v4"})
    fake.run_status = "done"
    assert api.post("remove", {"id": "bfcl-v4"})["removed"] is True


def test_start_passes_only_the_subset_and_the_trials(tmp_path):
    api, fake = make(tmp_path)
    assert api.post("start", {"id": "bfcl-v4", "subset": "smoke", "trials": 3})["started"]
    api.wait()
    assert fake.calls == [("start", "bfcl-v4", {"subset": "smoke", "trials": 3})]
    assert api.get("overview", {})["jobs"]["bfcl-v4"]["run_id"] == "r2"


def test_the_failed_attempts_allowed_are_passed_when_set_and_left_out_when_unlimited(tmp_path):
    api, fake = make(tmp_path)
    api.post("start", {"id": "bfcl-v4", "subset": "smoke", "trials": 1, "max_failed_attempts": 40})
    api.wait()
    api.post("start", {"id": "bfcl-v4", "subset": "smoke", "trials": 1, "max_failed_attempts": 0})
    api.wait()
    assert [c[2] for c in fake.calls] == [{"subset": "smoke", "trials": 1, "max_failed_attempts": 40}, {"subset": "smoke", "trials": 1}]


def test_the_reasoning_level_is_passed_when_chosen_and_left_out_otherwise(tmp_path):
    api, fake = make(tmp_path)
    api.post("start", {"id": "bfcl-v4", "subset": "smoke", "trials": 1, "reasoning": "medium"})
    api.wait()
    api.post("start", {"id": "bfcl-v4", "subset": "smoke", "trials": 1, "reasoning": ""})
    api.wait()
    assert [c[2] for c in fake.calls] == [{"subset": "smoke", "trials": 1, "reasoning": "medium"}, {"subset": "smoke", "trials": 1}]


def test_named_tasks_are_passed_once_each_and_left_out_when_empty(tmp_path):
    api, fake = make(tmp_path)
    api.post("start", {"id": "bfcl-v4", "subset": "smoke", "trials": 1, "tasks": ["fix-git", "raman-fitting", "fix-git"]})
    api.wait()
    api.post("start", {"id": "bfcl-v4", "subset": "smoke", "trials": 1, "tasks": []})
    api.wait()
    assert [c[2] for c in fake.calls] == [{"subset": "smoke", "trials": 1, "tasks": ["fix-git", "raman-fitting"]}, {"subset": "smoke", "trials": 1}]


@pytest.mark.parametrize("req,word", [({"subset": "smoke", "trials": 1, "tasks": "fix-git"}, "tasks"), ({"subset": "smoke", "trials": 1, "tasks": ["../x"]}, "tasks"),
                                       ({"subset": "smoke", "trials": 1, "reasoning": "max"}, "reasoning"), ({"subset": "smoke", "trials": 1, "reasoning": 3}, "reasoning"),
                                       ({"subset": "nope", "trials": 1}, "subset"), ({"subset": "smoke", "trials": 0}, "trials"), ({"subset": "smoke", "trials": 21}, "trials"),
                                       ({"subset": "smoke", "trials": True}, "trials"), ({"subset": "smoke", "trials": "2"}, "trials"),
                                       ({"subset": "smoke", "trials": 1, "max_failed_attempts": -1}, "attempts"), ({"subset": "smoke", "trials": 1, "max_failed_attempts": 100001}, "attempts"),
                                       ({"subset": "smoke", "trials": 1, "max_failed_attempts": "9"}, "attempts"), ({"subset": "smoke", "trials": 1, "max_failed_attempts": True}, "attempts"),
                                       ({"subset": "smoke", "trials": 1, "model": "gpt-5"}, "served model"), ({"subset": "smoke", "api_key": "x"}, "served model"),
                                       ({"subset": "smoke", "power_limit_w": 5}, "watts"), ({"subset": "smoke", "power_limit_w": "400"}, "watts")])
def test_bad_start_requests_never_reach_the_service(tmp_path, req, word):
    api, fake = make(tmp_path)
    with pytest.raises(BenchApiError, match=word):
        api.post("start", {"id": "bfcl-v4", **req})
    assert fake.calls == [] and api.get("overview", {})["jobs"] == {}


def test_no_loaded_model_refuses_a_start(tmp_path):
    api, fake = make(tmp_path, model=None)
    with pytest.raises(BenchApiError, match="No model is loaded") as e:
        api.post("start", {"id": "bfcl-v4", "subset": "smoke", "trials": 1})
    assert e.value.kind == "no_model" and fake.calls == []
    assert api.get("overview", {})["model"] is None


def test_an_unknown_power_limit_asks_the_person_to_type_it(tmp_path):
    api, fake = make(tmp_path, power=None)
    assert api.get("overview", {})["power_limit_w"] is None
    with pytest.raises(BenchApiError, match="Type it") as e:
        api.post("start", {"id": "bfcl-v4", "subset": "smoke", "trials": 1})
    assert e.value.kind == "power_limit_unknown"
    api.post("start", {"id": "bfcl-v4", "subset": "smoke", "trials": 1, "power_limit_w": 350})
    api.wait()
    assert fake.calls[0][0] == "start" and api.power() == 350.0 and api.get("overview", {})["power_limit_measured"] is False


def test_the_measured_limit_wins_over_a_typed_one_and_is_cached(tmp_path):
    reads = []
    api, _ = make(tmp_path)
    api.power_source = lambda: reads.append(1) or 400.0
    api.typed_power = 300.0
    assert api.power() == 400.0 and api.power() == 400.0 and len(reads) == 1


def test_another_job_blocks_a_start_until_it_ends(tmp_path):
    api, fake = make(tmp_path)
    fake.gate = threading.Event()
    api.post("install", {"id": "bfcl-v4"})
    with pytest.raises(BenchApiError, match="still going"):
        api.post("start", {"id": "bfcl-v4", "subset": "smoke", "trials": 1})
    fake.gate.set()
    api.wait()


def test_reading_validates_ids_and_turns_refusals_into_errors(tmp_path):
    api, _ = make(tmp_path)
    assert api.get("preflight", {"id": "bfcl-v4"})["checks"][0]["fix"] == "Start Docker Desktop"
    assert api.get("run", {"id": "r1"})["status"] == "running"
    assert api.get("report", {"id": "r1"})["classes"] == {"timeout": 3}
    assert api.get("ranking", {"id": "r1"})["basis"] == "local_only"
    with pytest.raises(BenchApiError, match="no such run"):
        api.get("run", {"id": "r9"})
    for bad in ("../x", "", "a b", None, "x" * 200):
        with pytest.raises(BenchApiError, match="'id'"):
            api.get("run", {"id": bad})
    with pytest.raises(BenchApiError):
        api.get("frob", {"id": "r1"})


def test_the_ranking_takes_the_pasted_results_file(tmp_path):
    api, fake = make(tmp_path)
    assert api.post("ranking", {"id": "r1", "leaderboard_text": "model,m\na,1\n"})["basis"] == "entries"
    assert fake.calls[-1] == ("ranking", "r1", "model,m\na,1\n")
    with pytest.raises(BenchApiError, match="1 MB"):
        api.post("ranking", {"id": "r1", "leaderboard_text": "x" * ((1 << 20) + 1)})
    with pytest.raises(BenchApiError):
        api.post("ranking", {"id": "r1", "leaderboard_text": 5})


def test_a_candidate_lesson_is_kept_only_on_an_explicit_request(tmp_path):
    api, _ = make(tmp_path)
    api.get("report", {"id": "r1"})
    assert LessonStore(tmp_path / "lessons", tmp_path / "lessons").list() == []             # reading the report keeps nothing
    got = api.post("add_lesson", {"id": "r1", "index": 0})
    assert got["action"] == "added"
    kept = LessonStore(tmp_path / "lessons", tmp_path / "lessons").list("global")
    assert [l.situation for l in kept] == ["Benchmark trials ended in the class 'Timeout'"]
    assert api.post("add_lesson", {"id": "r1", "index": 0})["action"] == "confirmed"
    for bad in ({"id": "r1", "index": 5}, {"id": "r1", "index": -1}, {"id": "r1", "index": True}, {"id": "r1"}):
        with pytest.raises(BenchApiError):
            api.post("add_lesson", bad)


def test_publishing_is_two_calls_and_errors_keep_their_kind(tmp_path):
    api, fake = make(tmp_path)
    assert api.post("prepare_publish", {"id": "r1"})["folder"] == "f"
    begun = api.post("begin_publish", {"id": "r1", "destination": "csv_pr"})
    assert begun["confirm_phrase"] == "PUBLISH 12345678" and ("finish", "tok", "x") not in fake.calls
    with pytest.raises(BenchApiError, match="does not match") as e:
        api.post("finish_publish", {"token": "tok", "phrase": "publish 12345678"})
    assert e.value.kind == "confirmation"
    assert api.post("finish_publish", {"token": "tok", "phrase": "PUBLISH 12345678"})["sent"] is False
    with pytest.raises(BenchApiError, match="provisional"):
        api.post("begin_publish", {"id": "prov", "destination": "csv_pr"})
    for bad in ({"token": "tok"}, {"phrase": "x"}, {"token": "tok", "phrase": 5}, {"token": "tok", "phrase": "x" * 500}):
        with pytest.raises(BenchApiError):
            api.post("finish_publish", bad)
    with pytest.raises(BenchApiError):
        api.post("begin_publish", {"id": "r1"})


def test_stop_and_resume_and_unknown_actions(tmp_path):
    api, fake = make(tmp_path)
    assert api.post("stop", {"id": "r1"})["stopping"] and api.post("resume", {"id": "r1"})["ok"]
    assert [c[0] for c in fake.calls] == ["stop", "resume"]
    with pytest.raises(BenchApiError) as e:
        api.post("run", {"id": "r1"})
    assert e.value.status == 404


class Routes(unittest.TestCase):
    """The routes on this PC's own server; a connection from away is refused."""

    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fake = FakeService()
        cls.api = BenchApi(Path(cls.tmp.name) / "b", lambda: 400.0, lambda: MODEL, lambda: "", Path(cls.tmp.name) / "lessons", service=cls.fake)
        cls.patch = mock.patch.object(handler, "_BENCHMARKS", cls.api)
        cls.patch.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.patch.stop()
        cls.tmp.cleanup()

    def call(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=headers, method=method), timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_reading_and_acting(self):
        status, over = self.call("GET", "/api/bench")
        self.assertEqual((status, over["benchmarks"][0]["id"], over["model"]["quantisation"]), (200, "bfcl-v4", "IQ4_XS"))
        self.assertEqual(self.call("GET", "/api/bench/preflight?id=bfcl-v4")[1]["ready"], False)
        self.assertEqual(self.call("GET", "/api/bench/run?id=r1")[1]["status"], "running")
        self.assertEqual(self.call("GET", "/api/bench/report?id=r1")[1]["classes"], {"timeout": 3})
        self.assertEqual(self.call("GET", "/api/bench/ranking?id=r1")[1]["basis"], "local_only")
        self.assertEqual(self.call("POST", "/api/bench/stop", {"id": "r1"})[1]["stopping"], True)
        self.assertEqual(self.call("POST", "/api/bench/ranking", {"id": "r1", "leaderboard_text": "a,b\n1,2\n"})[1]["basis"], "entries")
        begun = self.call("POST", "/api/bench/begin_publish", {"id": "r1", "destination": "csv_pr"})[1]
        self.assertEqual(begun["token"], "tok")
        status, err = self.call("POST", "/api/bench/finish_publish", {"token": "tok", "phrase": "no"})
        self.assertEqual((status, err["error"]["type"]), (400, "confirmation"))

    def test_bad_requests_are_refused(self):
        status, body = self.call("GET", "/api/bench/run?id=../x")
        self.assertEqual((status, body["error"]["type"]), (400, "bench"))
        self.assertEqual(self.call("POST", "/api/bench/stop", [1])[0], 400)
        self.assertEqual(self.call("POST", "/api/bench/start", {"id": "bfcl-v4", "subset": "x", "trials": 1})[0], 400)
        status, body = self.call("POST", "/api/bench/start", {"id": "bfcl-v4", "subset": "smoke", "trials": 1, "model": "gpt-5"})
        self.assertEqual(status, 400)

    def test_the_older_model_test_routes_are_still_theirs(self):
        status, body = self.call("POST", "/api/bench/frobnicate", {})
        self.assertEqual((status, body["error"]["message"]), (404, "not found"))

    def test_a_connection_from_away_is_refused(self):
        with mock.patch.object(handler.settings, "is_loopback", lambda address: False):
            self.assertEqual(self.call("GET", "/api/bench")[0], 403)
            self.assertEqual(self.call("GET", "/api/bench/run?id=r1")[0], 403)
            self.assertEqual(self.call("POST", "/api/bench/stop", {"id": "r1"})[0], 403)
        self.assertNotIn(("stop", "r9"), self.fake.calls)


class Glue(unittest.TestCase):
    """The handler's own sources for the service: the loaded model, the power limit and the GPU state (no real hardware is read)."""

    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.patches = [mock.patch.object(handler, "_BENCHMARKS", None), mock.patch.object(handler.userdata, "path", lambda name: base / name),
                       mock.patch.object(handler.benchrun, "power_limit_w", lambda: 400.0)]
        for p in cls.patches:
            p.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def test_the_overview_lists_the_five_benchmarks_with_the_served_model(self):
        with urllib.request.urlopen(self.base + "/api/bench", timeout=20) as r:
            over = json.loads(r.read())
        self.assertEqual([b["id"] for b in over["benchmarks"]], ["terminal-bench-2", "swe-bench-verified", "clawbench", "bfcl-v4", "tau3-bench"])
        self.assertEqual((over["model"]["name"], over["model"]["endpoint"].startswith("http://127.0.0.1:"), over["power_limit_w"]), (self.svc.model, True, 400.0))
        self.assertEqual(over["gpu_busy"], "")
        self.assertTrue(all(not b["status"]["installed"] for b in over["benchmarks"]))


if __name__ == "__main__":
    unittest.main()
