"""The page's view of checkpoints: the setting, and list, restore and delete for the folder a conversation works in."""
from pathlib import Path

import pytest

from tadamoo.checkpoints import CheckpointManager
from tadamoo.stack import data_dir_for
from serve import checkpoints_api
from serve.agent_runs import RunError


@pytest.fixture
def rec(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("one", encoding="utf-8")
    return {"workspace": str(ws), "state": "idle"}


def take(rec, text="before write_file"):
    ws = checkpoints_path(rec)
    return CheckpointManager(ws, storage_dir=data_dir_for(ws) / "checkpoints").create(text, [ws / "a.txt"])


def checkpoints_path(rec):
    return Path(rec["workspace"])


def test_the_setting_is_read_and_written_and_a_bad_position_is_refused():
    assert checkpoints_api.set_mode("soft")["mode"] == "soft"
    assert checkpoints_api.mode_view() == {"mode": "soft", "modes": ["on", "soft", "off"]}
    assert checkpoints_api.set_mode("default")["mode"] == ""
    with pytest.raises(RunError) as err:
        checkpoints_api.set_mode("sometimes")
    assert err.value.code == "bad_mode"


def test_the_list_shows_files_and_size_and_a_restore_puts_them_back(rec):
    cp = take(rec)
    ws = checkpoints_path(rec)
    (ws / "a.txt").write_text("two", encoding="utf-8")
    view = checkpoints_api.listing(rec)
    (row,) = view["checkpoints"]
    assert row["id"] == cp and row["files"] == 1 and row["bytes"] == 3 and row["paths"][0]["existed"] is True and view["bytes"] == 3
    assert checkpoints_api.restore(rec, cp)["restored"] == cp
    assert (ws / "a.txt").read_text(encoding="utf-8") == "one"


def test_delete_removes_one_or_all_and_nothing_without_a_name(rec):
    first, second = take(rec), take(rec, "second")
    with pytest.raises(RunError) as err:
        checkpoints_api.delete(rec, None)
    assert err.value.code == "no_checkpoint" and len(checkpoints_api.listing(rec)["checkpoints"]) == 2
    assert checkpoints_api.delete(rec, first) == {"deleted": 1}
    assert [c["id"] for c in checkpoints_api.listing(rec)["checkpoints"]] == [second]
    assert checkpoints_api.delete(rec, None, everything=True) == {"deleted": 1}
    assert checkpoints_api.listing(rec)["checkpoints"] == []


def test_a_running_agent_or_an_unknown_checkpoint_or_a_missing_folder_is_refused(rec):
    cp = take(rec)
    for state in ("busy", "asking"):
        with pytest.raises(RunError) as err:
            checkpoints_api.delete({**rec, "state": state}, cp)
        assert err.value.code == "busy"
    with pytest.raises(RunError) as err:
        checkpoints_api.restore(rec, "cp_9_9")
    assert err.value.code == "no_checkpoint"
    with pytest.raises(RunError) as err:
        checkpoints_api.listing({"workspace": rec["workspace"] + "-gone"})
    assert err.value.code == "folder_not_found"
