"""Moving the conversations' folder: a verified copy, the original kept, the choice recorded only after the copy is right, and nothing while one runs."""
import json
from pathlib import Path

import pytest

from serve import sessions_move, userdata


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    base = userdata.path("journal")
    sessions = base / "sessions"
    (sessions / "abc12345").mkdir(parents=True)
    (sessions / "abc12345" / "history.json").write_text(json.dumps({"messages": [{"role": "user", "content": "hi"}]}), encoding="utf-8")
    (sessions / "abc12345" / "archive.jsonl").write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    (sessions / "agent_sessions.json").write_text(json.dumps({"schema": "1.0", "sessions": {"abc12345": {}}}), encoding="utf-8")
    return base, tmp_path


def test_a_check_reports_what_would_be_copied_and_writes_nothing(world):
    base, tmp = world
    target = tmp / "chats"
    report = sessions_move.inspect(base, target, live=0)
    assert report["ok"] and report["files"] == 3 and report["bytes"] > 0 and report["problems"] == []
    assert not target.exists() and userdata.chosen_sessions_base() is None


def test_the_move_copies_everything_checks_it_keeps_the_original_and_records_the_new_place(world):
    base, tmp = world
    target = tmp / "chats"
    out = sessions_move.move(base, target, live=0)
    assert out["ok"] and out["restart"] and out["files"] == 3
    assert (target / "sessions" / "abc12345" / "history.json").read_text(encoding="utf-8") == (base / "sessions" / "abc12345" / "history.json").read_text(encoding="utf-8")
    assert (base / "sessions" / "agent_sessions.json").exists()                              # the original stays
    assert userdata.sessions_base() == target
    assert not (target / sessions_move.STAGING).exists()


def test_moving_back_to_the_default_forgets_the_choice(world):
    base, tmp = world
    sessions_move.move(base, tmp / "chats", live=0)
    again = sessions_move.inspect(tmp / "chats", base, 0)
    assert not again["ok"] and again["problems"][0]["code"] == "not_empty"                    # the original is still there: clear it by hand first
    (base / "sessions" / "abc12345" / "history.json").unlink()
    for f in sorted((base / "sessions").rglob("*"), reverse=True):
        f.unlink() if f.is_file() else f.rmdir()
    (base / "sessions").rmdir()
    sessions_move.move(tmp / "chats", base, live=0)
    assert userdata.chosen_sessions_base() is None and userdata.sessions_base() == base


@pytest.mark.parametrize("live, code", [(2, "active")])
def test_nothing_moves_while_a_conversation_runs(world, live, code):
    base, tmp = world
    with pytest.raises(sessions_move.MoveError) as e:
        sessions_move.move(base, tmp / "chats", live=live)
    assert e.value.code == code and not (tmp / "chats").exists() and userdata.chosen_sessions_base() is None


def test_a_relative_path_the_same_place_and_a_place_inside_are_refused(world):
    base, tmp = world
    assert sessions_move.inspect(base, Path("chats"), 0)["problems"][0]["code"] == "relative"
    assert {p["code"] for p in sessions_move.inspect(base, base, 0)["problems"]} >= {"same"}
    assert {p["code"] for p in sessions_move.inspect(base, base / "sessions" / "abc12345" / "x", 0)["problems"]} >= {"nested"}


def test_a_destination_that_already_holds_files_is_refused(world):
    base, tmp = world
    (tmp / "chats" / "sessions").mkdir(parents=True)
    (tmp / "chats" / "sessions" / "other.json").write_text("{}", encoding="utf-8")
    with pytest.raises(sessions_move.MoveError) as e:
        sessions_move.move(base, tmp / "chats", live=0)
    assert e.value.code == "not_empty" and userdata.chosen_sessions_base() is None


def test_a_copy_that_differs_is_not_kept(world, monkeypatch):
    base, tmp = world
    real = sessions_move._digest
    seen = []

    def lying(path):
        seen.append(path)
        return real(path) if len(seen) % 2 else "different"
    monkeypatch.setattr(sessions_move, "_digest", lying)
    with pytest.raises(sessions_move.MoveError) as e:
        sessions_move.move(base, tmp / "chats", live=0)
    assert e.value.code == "verify" and not (tmp / "chats" / "sessions").exists() and not (tmp / "chats" / sessions_move.STAGING).exists()
    assert userdata.chosen_sessions_base() is None


def test_files_that_name_the_old_folder_by_its_full_path_are_counted(world):
    base, tmp = world
    (base / "sessions" / "abc12345" / "history.json").write_text(json.dumps({"path": str(base / "sessions" / "abc12345")}), encoding="utf-8")
    assert sessions_move.inspect(base, tmp / "chats", 0)["paths_inside"] == 1
