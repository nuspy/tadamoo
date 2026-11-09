"""What the PC engines share: the parsing of a process's lines, one process at a time, the process tree that stops, the frames file and its writer.
The tree test starts two tiny Python processes of its own (no model, no GPU, no network)."""
import os
import subprocess
import sys
import threading
import time

import pytest

from voice.avatar_engines import pack
from voice.avatar_engines.runners import _frames
from voice.tests import engine_pack_fakes as fakes


def test_lines_are_events_or_nothing():
    assert pack.parse_line('{"event": "progress", "phase": "head", "percent": 12.5}') == {"event": "progress", "phase": "head", "percent": 12.5}
    assert pack.parse_line('{"event": "done", "frames": 3}')["frames"] == 3
    assert pack.parse_line('{"no": "event"}') is None and pack.parse_line("{broken") is None and pack.parse_line("") is None
    assert pack.parse_line("  7%|#         | 7/100 [00:01<00:12, 7.1it/s]") == {"event": "progress", "phase": "", "percent": 7}
    assert pack.parse_line("100%|##########| 5/5") ["percent"] == 100
    assert pack.parse_line("plain log line") is None


def test_a_job_keeps_the_last_lines_for_its_error(tmp_path):
    out = [f"line {i}" for i in range(30)]
    job = pack.Job(["x"], tmp_path, {}, "test", spawner=fakes.Spawner(out=out, code=3))
    with pytest.raises(pack.EngineFailed) as err:
        job.run()
    assert "line 29" in str(err.value) and "line 5" not in str(err.value) and len(job.tail) == pack.TAIL_LINES


def test_an_upstream_script_without_a_done_event_ends_well_on_exit_code_zero(tmp_path):
    assert pack.Job(["x"], tmp_path, {}, "prep", spawner=fakes.Spawner(code=0), needs_done=False).run() == {"event": "done"}
    with pytest.raises(pack.EngineFailed):
        pack.Job(["x"], tmp_path, {}, "prep", spawner=fakes.Spawner(code=0)).run()


def test_engines_run_one_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(pack, "kill_tree", lambda proc: proc.kill())
    gate = threading.Event()
    first = pack.Job(["x"], tmp_path, {}, "first", spawner=fakes.Spawner(gate=gate), needs_done=False)
    t = threading.Thread(target=first.run)
    t.start()
    deadline = time.time() + 5
    while first.proc is None and time.time() < deadline:
        time.sleep(0.01)
    with pytest.raises(pack.EngineBusy):
        pack.Job(["x"], tmp_path, {}, "second", spawner=fakes.Spawner(), needs_done=False).run()
    gate.set()
    t.join(5)
    pack.Job(["x"], tmp_path, {}, "third", spawner=fakes.Spawner(), needs_done=False).run()        # free again


CHILD = "import time\ntime.sleep(60)\n"
PARENT = ("import json, subprocess, sys, time\n"
          "child = subprocess.Popen([sys.executable, '-c', " + repr(CHILD) + "])\n"
          "print(json.dumps({'event': 'progress', 'phase': 'child', 'percent': child.pid}), flush=True)\n"
          "time.sleep(60)\n")


def test_stop_kills_the_whole_process_tree(tmp_path):
    psutil = pytest.importorskip("psutil")
    pids = []
    job = pack.Job([sys.executable, "-c", PARENT], tmp_path, dict(os.environ), "tree", lambda e: pids.append(int(e["percent"])))
    result = []

    def run():
        try:
            job.run()
        except pack.EngineFailed as e:
            result.append(str(e))
    t = threading.Thread(target=run)
    t.start()
    deadline = time.time() + 20
    while not pids and time.time() < deadline:
        time.sleep(0.05)
    assert pids, "the child never reported"
    parent_pid, child_pid = job.proc.pid, pids[0]
    assert psutil.pid_exists(child_pid) and psutil.pid_exists(parent_pid)
    job.stop()
    t.join(20)
    deadline = time.time() + 10
    while (psutil.pid_exists(child_pid) or psutil.pid_exists(parent_pid)) and time.time() < deadline:
        time.sleep(0.05)
    assert not psutil.pid_exists(child_pid) and not psutil.pid_exists(parent_pid)
    assert result and "stopped" in result[0]


def test_the_frames_file_round_trips_and_refuses_a_bad_header(tmp_path):
    path = tmp_path / "f"
    fakes.frames_file(path, 3, 6, 4)
    got = list(pack.read_frames(path))
    assert [(f.width, f.height, len(f.i420)) for f in got] == [(6, 4, 36)] * 3
    path.write_bytes(b"XXXX" + b"\x02\x00\x00\x00\x02\x00\x00\x00" + b"\x00" * 6)
    with pytest.raises(pack.EngineFailed, match="bad header"):
        list(pack.read_frames(path))
    path.write_bytes(pack.HEADER.pack(pack.MAGIC, 3, 2) + b"\x00" * 9)                  # odd width
    with pytest.raises(pack.EngineFailed, match="bad header"):
        list(pack.read_frames(path))


def test_feeding_stops_when_asked_and_paces_by_the_rate():
    naps, got, stop = [], [], threading.Event()

    def sink(frame):
        got.append(frame)
        if len(got) == 2:
            stop.set()
    assert pack.feed_frames(iter(range(10)), sink, 50, naps.append, stop) == 2
    assert naps == [0.02, 0.02]


def test_the_runner_writes_i420_frames_scaled_down_never_up(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    video = tmp_path / "v.mp4"
    out = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
    if not out.isOpened():
        pytest.skip("this OpenCV cannot write mp4")
    for i in range(4):
        out.write(np.full((48, 64, 3), 40 * i, np.uint8))
    out.release()
    dest = tmp_path / "frames"
    assert _frames.write_frames(str(video), str(dest), 24) == (4, 32, 24)
    frames = list(pack.read_frames(dest))
    assert len(frames) == 4 and (frames[0].width, frames[0].height) == (32, 24) and frames[0].bgr().shape == (24, 32, 3)
    assert _frames.write_frames(str(video), str(dest), 480) == (4, 64, 48)             # a larger height leaves the size alone
    assert _frames.write_frames(str(video), str(dest), 0) == (4, 64, 48)
    with pytest.raises(RuntimeError, match="no frames"):
        _frames.write_frames(str(tmp_path / "missing.mp4"), str(dest), 24)
