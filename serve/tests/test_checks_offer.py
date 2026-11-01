"""The one-time offer of a folder's own checks: when it is still due, what the person's answer saves, and that it is never asked twice."""
import json
import sys
from pathlib import Path

import pytest

from serve.agent_sessions import SessionManager
from serve.coding_prefs import CodingPrefs
from serve.tests.test_agent_sessions import STUB, kinds, until

PYTEST_CMD = "python -m pytest -x -q"


@pytest.fixture(autouse=True)
def own_data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("BASALT_DATA", str(tmp_path / "data"))


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = ['tests']\n[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    (root / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    return root


def prefs(tmp_path):
    return CodingPrefs(tmp_path / "coding.json")


def test_the_offer_is_due_once_for_a_folder_with_detected_checks(tmp_path, folder):
    p = prefs(tmp_path)
    assert p.offer_pending(folder) and "--offer-checks" in p.flags(folder)
    p.mark_offered(folder)
    assert not p.offer_pending(folder) and "--offer-checks" not in p.flags(folder)


def test_no_offer_for_an_empty_folder_a_folder_with_no_checks_or_one_already_set_up(tmp_path, folder):
    p = prefs(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "notes.txt").write_text("x", encoding="utf-8")
    assert not p.offer_pending(empty) and not p.offer_pending(plain)
    p.accept(folder, own="make check")
    assert not p.offer_pending(folder)                                              # the person typed their own
    other = tmp_path / "other"
    (other / "tests").mkdir(parents=True)
    (other / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    p.accept(other, accepted=[{"command": PYTEST_CMD, "gate": True}])
    assert not p.offer_pending(other)


def test_accepting_the_ticked_checks_makes_them_run_and_asks_no_more(tmp_path, folder):
    p = prefs(tmp_path)
    p.settle_offer(folder, [PYTEST_CMD, "rm -rf /", 7])                              # only what the folder detects counts
    got = p.effective(folder)
    assert got == {"safe": [PYTEST_CMD], "checks": [PYTEST_CMD]}
    assert not p.offer_pending(folder) and "--safe-command" in p.flags(folder) and "--project-check" in p.flags(folder)
    p.settle_offer(folder, [PYTEST_CMD])                                            # twice is the same
    assert p.effective(folder)["checks"] == [PYTEST_CMD]


def test_ticking_nothing_is_a_no_that_is_remembered(tmp_path, folder):
    p = prefs(tmp_path)
    p.settle_offer(folder, [])
    assert p.effective(folder) == {"safe": [], "checks": []} and not p.offer_pending(folder)
    stored = json.loads((tmp_path / "coding.json").read_text(encoding="utf-8"))["projects"]
    assert list(stored.values())[0]["declined"] is True
    p.accept(folder, accepted=[{"command": PYTEST_CMD, "gate": True}])               # the person may still accept from the settings
    assert p.effective(folder)["checks"] == [PYTEST_CMD]


@pytest.fixture
def mgr(tmp_path):
    made = SessionManager(tmp_path / "j", "http://127.0.0.1:1", command=lambda sid, rec, history: [sys.executable, STUB, "--session-id", sid, "--history-dir", str(history), "--die-times", "0"],
                          tick_s=0.1, coding=CodingPrefs(tmp_path / "coding.json"))
    yield made
    made.shutdown()


def answer_json(*picked):
    return "ask:" + json.dumps({"v": 1, "status": "submitted", "answers": {"checks": {"picked": list(picked)}}})


def test_the_card_shown_marks_the_folder_and_the_answer_is_saved_and_sent_to_the_conversation(tmp_path, folder, mgr):
    sid = mgr.create(str(folder))["id"]
    assert mgr.coding.offer_pending(folder)
    mgr.send(sid, "offer")
    until(lambda: mgr.get(sid)["question"])
    assert mgr.get(sid)["question"]["offer"]["question"] == "checks"
    assert not mgr.coding.offer_pending(folder)                                      # offered: not again, whatever comes next
    mgr.answer(sid, "k3", answer_json("o1"))
    assert mgr.coding.effective(folder)["checks"] == [PYTEST_CMD]
    until(lambda: "checks" in kinds(mgr, sid))                                       # the running conversation was told at once
    until(lambda: kinds(mgr, sid).count("done") == 2)


def test_an_empty_answer_declines_and_a_skip_saves_nothing(tmp_path, folder, mgr):
    sid = mgr.create(str(folder))["id"]
    mgr.send(sid, "offer")
    until(lambda: mgr.get(sid)["question"])
    mgr.answer(sid, "k3", answer_json())
    assert mgr.coding.effective(folder) == {"safe": [], "checks": []}
    stored = json.loads(Path(mgr.coding.path).read_text(encoding="utf-8"))["projects"]
    assert list(stored.values())[0]["declined"] is True
    until(lambda: kinds(mgr, sid).count("done") == 2)
    again = mgr.create(str(folder))["id"]
    mgr.send(again, "offer")
    until(lambda: mgr.get(again)["question"])
    mgr.answer(again, "k3", "ask:" + json.dumps({"v": 1, "status": "skipped"}))
    assert mgr.coding.effective(folder) == {"safe": [], "checks": []}
