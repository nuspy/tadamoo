"""serve/limits.py: the time limits read from limits.json in the user data folder."""
import json

import pytest

from serve import limits
from tadamoo.tools.registry import DEFAULT_TOOL_TIMEOUT, ToolRegistry


@pytest.fixture(autouse=True)
def data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("BASALT_DATA", str(tmp_path))
    for name in limits.DEFAULTS:
        monkeypatch.delenv(limits.ENV_PREFIX + name.upper(), raising=False)
    return tmp_path


def test_without_a_file_the_silence_limits_are_off_and_a_tool_call_has_a_generous_one():
    assert limits.get("beat_timeout_s") is None and limits.get("idle_s") is None and limits.get("compare_wait_s") is None
    assert limits.get("tool_timeout_s") == DEFAULT_TOOL_TIMEOUT
    assert limits.get("beat_timeout_s", 45) == 45                        # the caller's default when nothing is set


def test_the_file_wins_over_the_environment_and_the_default(data_folder, monkeypatch):
    monkeypatch.setenv("BASALT_LIMIT_IDLE_S", "5")
    assert limits.get("idle_s") == 5
    (data_folder / limits.FILE).write_text(json.dumps({"idle_s": 60, "beat_timeout_s": 0, "compare_wait_s": None}), encoding="utf-8")
    assert limits.get("idle_s") == 60 and limits.get("beat_timeout_s", 45) is None and limits.get("compare_wait_s", 45) is None   # 0 and null: no limit


def test_a_tool_limit_is_never_unlimited(data_folder):
    for bad in (0, None, "off", -3, "abc", True):
        (data_folder / limits.FILE).write_text(json.dumps({"tool_timeout_s": bad}), encoding="utf-8")
        assert limits.get("tool_timeout_s") == DEFAULT_TOOL_TIMEOUT, bad
    (data_folder / limits.FILE).write_text(json.dumps({"tool_timeout_s": 120}), encoding="utf-8")
    assert limits.get("tool_timeout_s") == 120


def test_a_file_that_cannot_be_read_is_ignored(data_folder):
    (data_folder / limits.FILE).write_text("{not json", encoding="utf-8")
    assert limits.current()["tool_timeout_s"] == DEFAULT_TOOL_TIMEOUT and limits.current()["idle_s"] is None


def test_save_keeps_the_other_values_and_refuses_bad_ones(data_folder):
    limits.save({"idle_s": 30})
    assert limits.save({"beat_timeout_s": "120"})["idle_s"] == 30
    assert limits.get("beat_timeout_s") == 120
    assert limits.save({"idle_s": ""})["idle_s"] is None
    for bad in ({"nope": 1}, {"idle_s": "x"}):
        with pytest.raises(ValueError):
            limits.save(bad)


def test_the_registry_reads_the_persons_tool_limit_at_each_call_unless_it_was_given_one(data_folder):
    reg = ToolRegistry()
    assert reg.timeout_for("any") == DEFAULT_TOOL_TIMEOUT
    limits.save({"tool_timeout_s": 77})
    assert reg.timeout_for("any") == 77
    assert ToolRegistry(default_timeout=0.5).timeout_for("any") == 0.5
