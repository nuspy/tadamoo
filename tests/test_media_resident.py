"""Models kept resident across the media phase: the loop mode of the two launchers, a session of the media worker over a stand-in LTX Studio (one lease, one model process, the reload, the death of the model process), the client's
session, and a production in resident mode with the work ordered by model. No model, no GPU."""
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from media_fake import Broker, make_ltx
from test_media import world  # noqa: F401  (the fixture)
from tadamoo.media import client, flashvsr_launch, ltx_launch
from tadamoo.media.errors import MediaError
from tadamoo.media.settings import DEFAULTS, MediaSettings
import pytest

pytest.importorskip("tadamoo.briefing")      # the production code is a private one, kept out of the repository
from tadamoo.briefing import produce, settings as settings_mod
from test_newscast_journalists import ENDRE, LILITH, photos, three_stories  # noqa: F401
from test_newscast import Jobs, Server

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
SESSION_FAKE = str(Path(__file__).with_name("session_fake.py"))
REQUEST = {"prompt": "a presenter speaks", "width": 896, "height": 512, "frames": 65, "fps": 25, "seed": 5, "negative": ""}


def resident_run(argv, jobs, env=None):
    """Feeds the jobs (one JSON line each) to a resident launcher and returns what it printed."""
    done = subprocess.run(argv, input="".join(json.dumps(j) + "\n" for j in jobs), capture_output=True, text=True, env={**os.environ, **(env or {})}, timeout=60)
    return done


def test_the_loop_mode_of_the_ltx_launcher_runs_every_job_in_one_process_with_its_own_save_quality_and_goes_on_after_a_failure(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(textwrap.dedent('''
        import json, os, sys
        def main():
            p = json.load(open(sys.argv[1]))
            if p.get("fail"):
                raise RuntimeError("the model did not load")
            opts = {"crf": "19", "preset": "veryfast"}
            open(p["output"], "w").write(json.dumps({"pid": os.getpid(), "env": [os.environ.get("LTX_X264_CRF"), os.environ.get("LTX_X264_PRESET")], "opts": opts}))
            print("OK:" + p["output"], flush=True)
        if __name__ == "__main__":
            main()
        '''), encoding="utf-8")
    jobs = []
    for i, (crf, fail) in enumerate((("4", False), ("", False), ("", True), ("18", False))):
        params = tmp_path / f"p{i}.json"
        params.write_text(json.dumps({"output": str(tmp_path / f"o{i}.txt"), "fail": fail}), encoding="utf-8")
        jobs.append({"id": i, "params": str(params), "env": {"LTX_X264_CRF": crf, "LTX_X264_PRESET": "slow"} if crf else {}})
    done = resident_run([sys.executable, ltx_launch.__file__, str(worker), "--resident"], jobs)
    assert done.returncode == 0 and "[resident] ready" in done.stdout
    assert "[resident] done 0" in done.stdout and "[resident] done 1" in done.stdout and "[resident] failed 2: RuntimeError: the model did not load" in done.stdout and "[resident] done 3" in done.stdout
    made = {i: json.loads((tmp_path / f"o{i}.txt").read_text()) for i in (0, 1, 3)}
    assert len({m["pid"] for m in made.values()}) == 1                                                           # one process for the three that ran
    assert made[0]["env"] == ["4", "slow"] and made[1]["env"] == [None, None] and made[3]["env"] == ["18", "slow"]            # each job has its own save quality from its own line


def test_the_loop_mode_of_the_enlarger_launcher_answers_each_job_and_reports_a_failure(tmp_path):
    runner = tmp_path / "runner.py"
    runner.write_text(textwrap.dedent('''
        import sys
        src, out = sys.argv[1], sys.argv[2]
        if "bad" in src:
            raise SystemExit(3)
        open(out, "wb").write(open(src, "rb").read() + b"y")
        '''), encoding="utf-8")
    jobs = []
    for i, name in enumerate(("a", "bad", "c")):
        src = tmp_path / f"{name}.mp4"
        src.write_bytes(b"video" + name.encode())
        jobs.append({"id": i, "args": [str(src), str(tmp_path / f"out_{name}.mp4")], "env": {}})
    done = resident_run([sys.executable, flashvsr_launch.__file__, str(runner), "--resident"], jobs, {"FLASHVSR_LAUNCH": "plain"})
    assert "[resident] ready" in done.stdout and "[resident] done 0" in done.stdout and "[resident] failed 1" in done.stdout and "[resident] done 2" in done.stdout
    assert (tmp_path / "out_a.mp4").read_bytes() == b"videoay" and (tmp_path / "out_c.mp4").read_bytes() == b"videocy" and not (tmp_path / "out_bad.mp4").exists()


def test_a_session_holds_one_lease_and_one_model_process_for_all_its_jobs_and_has_each_job_s_own_quality(world, tmp_path):
    settings = {**world["settings"], "resident": True}
    session = client.Session("video", settings, url=world["broker"].url)
    try:
        first = session.run({**REQUEST, "crf": 4, "crf_preset": "slow"}, tmp_path / "a" / "o.mp4")
        second = session.run(REQUEST, tmp_path / "b" / "o.mp4")
        third = session.run({**REQUEST, "crf": 18}, tmp_path / "c" / "o.mp4")
    finally:
        session.close()
    assert all(Path(r["path"]).is_file() and r["lease"]["owner"] == "video" for r in (first, second, third))
    pids = {(tmp_path / d / "started.pid").read_text() for d in "abc"}
    assert len(pids) == 1                                                                                           # the three jobs ran in one model process
    assert [(tmp_path / d / "env.txt").read_text() for d in "abc"] == ["4 slow", " ", "18 medium"]                    # each with its own quality
    begins = [p for p in world["broker"].paths() if p == "/api/heavy/begin" or "lease" in p]
    assert sum(1 for c in world["broker"].lease_calls() if c[1].get("owner") == "video") == 1                        # one lease for the three jobs
    assert session.opened == 1 and session.next_id == 3
    assert begins is not None


def test_a_session_starts_the_model_process_again_after_so_many_jobs_and_when_it_has_died(world, tmp_path):
    settings = {**world["settings"], "resident": True}
    session = client.Session("video", settings, url=world["broker"].url, reload_every=2)
    try:
        for name in "abc":
            session.run(REQUEST, tmp_path / name / "o.mp4")
        pids = [(tmp_path / d / "started.pid").read_text() for d in "abc"]
        assert pids[0] == pids[1] and pids[2] != pids[1]                                                            # reloaded before the third job
        with pytest.raises(MediaError, match="ended in the middle"):
            session.run({**REQUEST, "prompt": "diediedie"}, tmp_path / "d" / "o.mp4")                              # the model process dies in a job: that job fails
        again = session.run(REQUEST, tmp_path / "e" / "o.mp4")                                                      # the session is still there and starts a new model process
        assert Path(again["path"]).is_file() and (tmp_path / "e" / "started.pid").read_text() != pids[2]
    finally:
        session.close()
    assert session.opened == 1                                                                                      # and it was the one worker all along


def test_a_worker_that_dies_is_replaced_by_a_new_session_for_the_next_job(tmp_path):
    log = tmp_path / "log.txt"
    os.environ["SESSION_FAKE_LOG"] = str(log)
    try:
        settings = {**DEFAULTS, "ltx_dir": "", "idle_s": 30}
        session = client.Session("video", settings, command=[sys.executable, SESSION_FAKE])
        os.environ["SESSION_FAKE_DIE_ONCE"] = str(tmp_path / "marker")
        with pytest.raises(MediaError) as died:
            session.run(REQUEST, tmp_path / "a" / "o.mp4")
        assert died.value.code == "died"
        got = session.run(REQUEST, tmp_path / "b" / "o.mp4")
        session.close()
    finally:
        os.environ.pop("SESSION_FAKE_LOG", None)
        os.environ.pop("SESSION_FAKE_DIE_ONCE", None)
    assert Path(got["path"]).is_file() and session.opened == 2 and [l.split()[0] for l in log.read_text().splitlines()].count("start") == 2


def test_the_media_settings_have_the_resident_switch_on_and_a_reload_count(tmp_path):
    assert DEFAULTS["resident"] is True and DEFAULTS["resident_reload_every"] == 25
    s = MediaSettings(tmp_path / "media.json")
    got = s.write({"resident": False, "resident_reload_every": 8})
    assert got["resident"] is False and got["resident_reload_every"] == 8
    with pytest.raises(MediaError):
        s.write({"resident": "no"})
    with pytest.raises(MediaError):
        s.write({"resident_reload_every": 1000})


@pytest.fixture
def stage(tmp_path, monkeypatch, photos):
    server, jobs = Server(), Jobs()
    monkeypatch.setattr(client, "run", jobs)
    monkeypatch.setenv("SESSION_FAKE_LOG", str(tmp_path / "session.log"))
    yield {"server": server, "jobs": jobs, "tmp": tmp_path, "photos": photos, "log": tmp_path / "session.log"}
    server.stop()


def make(stage, resident, **kw):
    photos = stage["photos"]
    cfg = settings_mod.parse({"card_hold_s": 1.5, "voice_check": False, "voice_mode": "service", "journalists": [{**LILITH, "photo": photos[0]}, {**ENDRE, "photo": photos[1]}], "width": 320, "height": 214, "fps": 12, **kw})
    media = {**DEFAULTS, "idle_s": 30, "resident_reload_every": 0}
    return produce.make(three_stories(), cfg, stage["tmp"] / "work", produce.Options(url=stage["server"].url, media=media, swap="per-job", resident=resident,
                                                                                    session_command=[sys.executable, SESSION_FAKE]))


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_a_resident_production_runs_all_the_ltx_jobs_and_then_all_the_enhancements_and_says_what_it_saved(stage):
    manifest = make(stage, True)
    lines = stage["log"].read_text().splitlines()
    kinds = [l.split()[1] for l in lines if l.startswith("job ")]
    assert kinds.count("video") >= 3 and kinds.count("upscale") == kinds.count("video") - sum(1 for l in lines if l.startswith("job video") and "broll" in l)
    assert kinds == sorted(kinds, key=lambda k: k != "video")                                                         # every LTX job before the first enhancement: the work is ordered by model
    assert [l.split()[1] for l in lines if l.startswith("start ")] == ["video", "upscale"]                          # one worker for each kind: one lease each
    phases = [p["phase"] for p in manifest["phases"]]
    assert phases == ["voice", "stills", "takes", "broll", "enhance", "edit"]
    res = manifest["resident"]
    assert res["sessions"]["video"] == {"opened": 1, "jobs": kinds.count("video")} and res["sessions"]["upscale"]["opened"] == 1 and res["saved_s"] > 0
    work = stage["tmp"] / "work"
    assert all((t / "take_x2.mp4").is_file() for t in (work / "takes").iterdir()) and Path(manifest["output"]).is_file()
    assert all("enhanced_s" in v for k, v in manifest["files"].items() if k.startswith("take_"))
    assert not any(k in ("video", "upscale") for k, _, _ in stage["jobs"].asked)                                       # nothing went through a process of its own


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_without_the_switch_every_job_has_its_own_process_as_before_and_the_phases_are_as_before(stage):
    manifest = make(stage, False)
    assert not stage["log"].exists() and "resident" not in manifest
    assert [p["phase"] for p in manifest["phases"]] == ["voice", "stills", "takes", "broll", "edit"]
    assert any(k == "upscale" for k, _, _ in stage["jobs"].asked)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_a_resident_production_whose_worker_dies_opens_a_new_session_and_goes_on(stage, monkeypatch):
    monkeypatch.setenv("SESSION_FAKE_DIE_ONCE", str(stage["tmp"] / "marker"))
    manifest = make(stage, True)
    assert Path(manifest["output"]).is_file() and manifest["resident"]["sessions"]["video"]["opened"] == 2
    assert any("session died" in n and "tried again" in n for n in manifest["notes"])
