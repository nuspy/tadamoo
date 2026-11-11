"""Where the choice of archives lives: `voice.json` (brain, meetings, copilot, telephony), the plan item of a meeting, and what the voice service reads from them. Ids are
stored; a bad entry is refused when it is written and ignored when it is read."""
import json

import pytest

from callbridge.config import Config
from callbridge.meetbot.errors import Refused
from callbridge.meetbot.plan_store import PlanStore, clean
from serve import voice_settings as vs
from voice import settings_bridge as sb

MEET = "https://meet.google.com/abc-defg-hij"
START = "2027-01-15T15:00:00+01:00"


@pytest.fixture
def file(tmp_path, monkeypatch):
    path = tmp_path / "voice.json"
    monkeypatch.setattr(vs, "_file", lambda: path)
    return path


def test_the_brain_keeps_the_archive_ids_for_the_avatar_and_the_voice_chat(file):
    assert vs.brain()["archives"] == []
    assert vs.set_brain({"archives": ["boats-1", "rice-2", "boats-1"]})["archives"] == ["boats-1", "rice-2"]
    assert vs.set_brain({"mode": "harness"})["archives"] == ["boats-1", "rice-2"]                                       # another change does not lose them
    for bad in ("boats-1", ["../x"], [1], ["a"] * 21, [""]):
        with pytest.raises(vs.VoiceSettingsError):
            vs.check_brain({"archives": bad})
    file.write_text(json.dumps({"brain": {"archives": ["ok-1", "../bad", 3, "ok-1"]}}), encoding="utf-8")
    assert vs.brain()["archives"] == ["ok-1"]


def test_meetings_calls_and_the_copilot_have_their_own_lists_and_the_private_output(file):
    got = vs.set_meetings({"archives": ["a-1"], "copilot_archives": ["b-2", "c-3"], "private_output": " Headphones "})
    assert got["archives"] == ["a-1"] and got["copilot_archives"] == ["b-2", "c-3"] and got["private_output"] == "Headphones"
    assert vs.meetings()["speak"] is False                                                                              # the careful defaults stand
    assert vs.set_telephony({"archives": ["a-1"]})["archives"] == ["a-1"] and vs.telephony()["transport"] == "off"
    with pytest.raises(vs.VoiceSettingsError):
        vs.check_meetings({"copilot_archives": "x"})
    with pytest.raises(vs.VoiceSettingsError):
        vs.check_telephony({"archives": [None]})


def test_the_voice_service_reads_them_and_a_missing_or_damaged_value_is_none(tmp_path):
    path = tmp_path / "voice.json"
    path.write_text(json.dumps({"brain": {"archives": ["a-1"]}, "meetings": {"archives": ["m-1"], "copilot_archives": ["c-1"], "private_output": "buds"},
                                "telephony": {"archives": ["t-1"]}}), encoding="utf-8")
    cfg = Config()
    for k, v in sb.overrides(sb.load(path)).items():
        setattr(cfg, k, v)
    assert cfg.brain_archives == ("a-1",) and cfg.meetings_archives == ("m-1",) and cfg.copilot_archives == ("c-1",) and cfg.calls_archives == ("t-1",) and cfg.meetings_private_output == "buds"
    path.write_text(json.dumps({"brain": {"archives": "x"}, "meetings": {"archives": [5, "../x"]}, "telephony": []}), encoding="utf-8")
    cfg2 = Config()
    for k, v in sb.overrides(sb.load(path)).items():
        setattr(cfg2, k, v)
    assert cfg2.brain_archives == () and cfg2.meetings_archives == () and cfg2.calls_archives == () and Config().brain_archives == ()


def test_a_plan_item_may_name_its_own_archives_or_follow_the_settings(tmp_path):
    store = PlanStore(tmp_path / "plan.json", clock=lambda: 1_800_000_000.0)
    item = store.add({"url": MEET, "start": START})
    assert "archives" not in item                                                                                       # follows the settings
    own = store.update(item["id"], {"archives": ["a-1", "b-2", "a-1"]}) if hasattr(store, "update") else None
    assert own["archives"] == ["a-1", "b-2"]
    assert store.update(item["id"], {"archives": []})["archives"] == []                                                  # explicitly none
    assert "archives" not in store.update(item["id"], {"archives": None})                                               # back to the settings
    assert PlanStore(tmp_path / "plan.json", clock=lambda: 1_800_000_000.0).get(item["id"]) == store.get(item["id"])
    for bad in ("a-1", ["../x"], [3], ["a"] * 21):
        with pytest.raises(Refused):
            clean({"archives": bad}, store.get(item["id"]))
