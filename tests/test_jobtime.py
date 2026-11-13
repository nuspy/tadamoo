"""The longest a job may take, from the times jobs really took (tadamoo/jobtime.py): the ratios kept, the limit, the settings that move it, and a media job that goes over it ends with an error of the step."""
import json
import time
import wave

import pytest

from media_fake import Broker, make_ltx
from tadamoo import jobtime
from tadamoo.media import client, limits
from tadamoo.media.errors import MediaError
from tadamoo.media.settings import DEFAULTS, MediaSettings


def test_without_a_measure_the_provisional_ratios_stand_in_and_the_factors_are_3_and_2(tmp_path):
    t = jobtime.Timing(tmp_path / "t.json")
    assert t.ratio("voice") == (1.0, 0) and t.ratio("avatar") == (42.0, 0)
    assert t.limit("voice", 100) == 300 and t.limit("video", 20) == 20 * 42 * 2 == 1680 and t.limit("modify", 20) == 1680


def test_the_limit_follows_the_median_of_what_was_recorded_and_keeps_the_last_ones(tmp_path):
    t = jobtime.Timing(tmp_path / "t.json")
    for r in (10, 30, 20):
        assert t.record("avatar", r * 5.0, 5.0) == r
    assert t.ratio("avatar") == (20.0, 3) and t.limit("avatar", 10) == 2 * 20 * 10 and t.ratio("video") == (42.0, 0)
    for _ in range(80):
        t.record("voice", 0.5, 1.0)
    assert len(json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))["voice"]) == jobtime.KEEP
    assert t.record("voice", 0, 5) is None and t.record("voice", 5, 0) is None and t.record("nothing", 5, 5) is None


def test_the_factors_and_the_floor_are_the_media_settings_and_a_short_job_gets_the_floor(tmp_path):
    t = jobtime.Timing(tmp_path / "t.json", {**DEFAULTS, "time_factor_audio": 5.0, "time_factor_video": 1.5, "time_min_s": 30})
    assert t.limit("voice", 100) == 500 and t.limit("avatar", 10) == 1.5 * 42 * 10 and t.limit("voice", 1) == 30
    assert jobtime.Timing(tmp_path / "u.json").limit("voice", 1) == jobtime.FLOOR_S


def test_the_settings_hold_the_factors_as_numbers_in_range(tmp_path):
    s = MediaSettings(tmp_path / "m.json")
    assert (s.read()["time_factor_audio"], s.read()["time_factor_video"], s.read()["time_min_s"]) == (3.0, 2.0, 120)
    assert s.write({"time_factor_video": 2.5, "time_min_s": 60})["time_factor_video"] == 2.5
    for bad in ({"time_factor_audio": 0.5}, {"time_factor_video": "2"}, {"time_factor_video": True}, {"time_min_s": 99999}):
        with pytest.raises(MediaError):
            s.write(bad)


def test_the_length_of_a_recording_is_read_from_its_header(tmp_path):
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 4000)
    assert jobtime.duration_of(path) == pytest.approx(0.5) and jobtime.duration_of(tmp_path / "none.wav") == 0.0


def test_a_deadline_says_the_numbers_once_it_is_passed(tmp_path):
    t = jobtime.Timing(tmp_path / "t.json", {"time_min_s": 0})
    t.record("voice", 0.01, 1.0)
    d = jobtime.Deadline(t, "voice", 1.0)
    d.check()
    time.sleep(0.2)
    with pytest.raises(jobtime.Overrun, match="voice took longer than 0 seconds .*median of 1 jobs"):
        d.check()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    ltx = make_ltx(tmp_path / "ltx")
    broker = Broker()
    client.forget_checks()
    yield {"settings": {**DEFAULTS, "ltx_dir": str(ltx), "wait_s": 5, "idle_s": 30, "time_min_s": 1}, "broker": broker, "tmp": tmp_path}
    broker.stop()


def clip(world, prompt, tag):
    request = {k: v for k, v in limits.video_request(prompt, seed=1).items() if k != "notes"}
    return client.run("video", request, world["tmp"] / tag / "o.mp4", world["settings"], url=world["broker"].url)


def test_a_job_that_ends_well_records_its_ratio_and_the_next_one_is_limited_by_it(world):
    got = clip(world, "a fox", "a")
    assert got["work_seconds"] > 0
    kept = json.loads((world["tmp"] / "data" / "job_times.json").read_text(encoding="utf-8"))["video"]
    assert len(kept) == 1 and kept[0] == pytest.approx(got["work_seconds"] / 2.6, rel=0.05)


def test_a_job_over_its_limit_is_ended_with_an_error_of_the_step(world):
    jobtime.Timing(settings=world["settings"]).record("video", 0.001, 1.0)             # what jobs took: a thousandth of a second for each second of clip
    began = time.monotonic()
    with pytest.raises(MediaError, match="took longer than 1 seconds") as err:
        clip(world, "a fox slow", "b")
    assert err.value.code == "timeout" and time.monotonic() - began < 20
    assert world["broker"].paths()[-1].endswith("/release")                              # the lease is back
