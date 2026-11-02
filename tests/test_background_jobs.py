"""Commands that run on in the background: started at once, followed, ended, and the model is told when one ends."""
import sys
import time

from tadamoo.tools import create_standard_tool_registry


def make(tmp_path):
    return create_standard_tool_registry(tmp_path)[0]


def wait_until(check, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.1)
    return False


def py(code):
    return f'"{sys.executable}" -c "{code}"'


def test_a_background_command_returns_at_once_and_its_output_is_read_when_it_ends(tmp_path):
    reg = make(tmp_path)
    started = reg.get("shell_command").handler(py("import time; time.sleep(1); print('hello from the job')"), background=True)
    assert started["job"] and started["state"] == "running"
    assert reg.get("job_status").handler(started["job"])["state"] == "running"
    assert "still running" in reg.get("job_log").handler(started["job"])["note"]
    assert wait_until(lambda: reg.get("job_status").handler(started["job"])["state"] == "finished")
    got = reg.get("job_log").handler(started["job"])
    assert "hello from the job" in got["output"] and got["exit_code"] == 0
    assert "job1" in reg.jobs.news() and reg.jobs.news() == ""


def test_a_job_can_be_ended_and_the_listing_shows_it(tmp_path):
    reg = make(tmp_path)
    started = reg.get("shell_command").handler(py("import time; time.sleep(60)"), background=True)
    time.sleep(0.5)
    reg.get("job_kill").handler(started["job"])
    assert wait_until(lambda: reg.get("job_status").handler(started["job"])["state"] == "killed")
    assert [j["state"] for j in reg.get("job_status").handler()["jobs"]] == ["killed"]


def test_an_unknown_job_is_an_error_that_names_the_known_ones(tmp_path):
    reg = make(tmp_path)
    try:
        reg.get("job_status").handler("job9")
    except KeyError as exc:
        assert "job9" in str(exc)
    else:
        raise AssertionError("no error")


def test_the_output_of_a_running_job_reaches_the_sink_and_job_log_as_it_is_written(tmp_path):
    reg = make(tmp_path)
    seen = []
    reg.jobs.sink = seen.append
    code = "import sys, time; print('first', flush=True); time.sleep(1.5); print('second', flush=True); sys.stderr.write('oops'); sys.stderr.flush()"
    started = reg.get("shell_command").handler(py(code), background=True)
    assert wait_until(lambda: "first" in reg.get("job_log").handler(started["job"]).get("output", ""))
    assert reg.get("job_status").handler(started["job"])["state"] == "running"
    assert wait_until(lambda: reg.get("job_status").handler(started["job"])["state"] == "finished")
    text = {s: "".join(e["text"] for e in seen if e.get("stream") == s) for s in ("out", "err")}
    assert "first" in text["out"] and "second" in text["out"] and "oops" in text["err"]
    states = [e["state"] for e in seen if "state" in e]
    assert states[0] == "running" and states[-1] == "finished"
