"""The shared part of the container adapters (harness/bench/adapters/containerkit.py): the container tools, the trajectory, our agent and the bash baseline
with their commands run in a local bash standing in for the container. No Docker, no model."""
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import editing
from harness.bench import analysis
from harness.bench.adapters import containerkit as ck
from harness.bench.adapters.containerkit import AgentOutcome, ContainerTools, ExecOut, TrajectoryRecorder
from harness.bench.runner import TrialResult
from harness.tools import create_standard_tool_registry
from test_session import Scripted, call, say

BASH = shutil.which("bash")
if BASH and "system32" in BASH.lower().replace("/", "\\"):             # the WSL launcher is not a bash of this PC
    BASH = None
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash to stand in for the container")


@pytest.fixture(autouse=True)
def _no_clock(monkeypatch):
    monkeypatch.setattr("harness.stack.local_now", None)


def local_exec(cwd: Path, log=None):
    def run(command, timeout):
        if log is not None:
            log.append(command)
        try:
            p = subprocess.run([BASH, "-c", command], cwd=cwd, capture_output=True, timeout=timeout or 60)
        except subprocess.TimeoutExpired:
            return ExecOut(124, "", "timed out", True)
        return ExecOut(p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"))
    return run


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "container"
    root.mkdir()
    return root, ContainerTools(local_exec(root))


# -- the tools ---------------------------------------------------------------------------------------------------------

def test_wrap_command_survives_quotes_dollars_and_lines(box):
    root, tools = box
    command = "echo \"a 'b' $HOME\" > out.txt\nprintf '%s\\n' \"line two\" >> out.txt"
    assert tools.run(command, 30).rc == 0
    text = (root / "out.txt").read_text(encoding="utf-8")
    assert text.splitlines()[1] == "line two" and "'b'" in text and "$HOME" not in text


def test_shell_command_answers_like_the_harness_and_times_out(box):
    _root, tools = box
    ok = tools.shell_command("echo hi; echo oops >&2; exit 3")
    assert ok["exit_code"] == 3 and ok["stdout"].strip() == "hi" and ok["stderr"].strip() == "oops" and not ok["success"] and not ok["timed_out"]
    slow = tools.shell_command("sleep 20", timeout=1)
    assert slow["timed_out"] and slow["exit_code"] == 124
    with pytest.raises(ValueError, match="greater than 0"):
        tools.shell_command("true", timeout=-1)


def test_long_output_keeps_its_start_and_its_end(box):
    _root, tools = box
    out = tools.shell_command("python3 -c 'print(\"x\" * 50000)' 2>/dev/null || printf 'x%.0s' $(seq 1 50000)")
    assert out["truncated"] and "characters cut" in out["stdout"] and len(out["stdout"]) < 21000


def test_write_read_and_edit_roundtrip_with_unicode_and_a_big_file(box):
    root, tools = box
    body = "è àccènt\n" + "line\n" * 30000
    assert tools.write_file("deep/er/a.txt", body)["bytes"] == len(body.encode("utf-8"))
    assert (root / "deep" / "er" / "a.txt").read_bytes() == body.encode("utf-8")
    assert tools.read_file("deep/er/a.txt", offset=1, limit=2).startswith("è àccènt\nline\n[lines 1-2 of")
    tools.write_file("b.py", "def f():\n    return 1\n")
    result = tools.edit_file("b.py", target="return 1", replacement="return 2")
    assert result["changed"] and (root / "b.py").read_text() == "def f():\n    return 2\n"
    again = tools.edit_file("b.py", target="return 1", replacement="return 2")     # the same edit twice
    assert not again["changed"] and "already applied" in again["note"]


def test_an_edit_keeps_windows_line_endings_and_a_bad_edit_raises(box):
    root, tools = box
    (root / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    tools.edit_file("w.txt", target="two", replacement="2")
    assert (root / "w.txt").read_bytes() == b"one\r\n2\r\nthree\r\n"
    with pytest.raises(editing.EditError):
        tools.edit_file("w.txt", target="nothing like this", replacement="x")
    with pytest.raises(editing.EditError, match="either"):
        tools.edit_file("w.txt", target="one", replacement="1", edits=[{"target": "a", "replacement": "b"}])
    assert (root / "w.txt").read_bytes() == b"one\r\n2\r\nthree\r\n"


def test_reading_a_missing_or_binary_file_is_an_error_and_numbers_are_optional(box):
    root, tools = box
    with pytest.raises(FileNotFoundError, match="nope.txt"):
        tools.read_file("nope.txt")
    (root / "bin.dat").write_bytes(b"\x00\x01\x02" * 100)
    with pytest.raises(ValueError, match="binary"):
        tools.read_file("bin.dat")
    (root / "t.txt").write_text("a\nb\nc\n")
    assert tools.read_file("t.txt", numbered=True).splitlines()[1].strip() == "2\tb"
    assert tools.read_file("t.txt", offset=9) .startswith("[offset 9 is past the end")


def test_list_and_search(box):
    root, tools = box
    (root / "pkg").mkdir()
    (root / "pkg" / "m.py").write_text("alpha = 1\nbeta = 2\n")
    (root / "top.txt").write_text("alpha\n")
    names = {e["name"]: e["type"] for e in tools.list_directory(".")}
    assert names == {"pkg": "directory", "top.txt": "file"}
    hits = tools.search_code("alp.a", ".")
    assert "pkg/m.py:1:alpha = 1" in hits.replace("\\", "/") and "top.txt:1:alpha" in hits.replace("\\", "/")
    assert "m.py" not in tools.search_code("alpha", ".", glob="*.txt")
    assert tools.search_code("zzz_not_here", ".").startswith("No match")
    with pytest.raises(FileNotFoundError):
        tools.list_directory("missing")


def test_install_keeps_only_container_tools_and_they_run_through_the_registry(box, tmp_path):
    root, tools = box
    registry, _sandbox = create_standard_tool_registry(tmp_path / "host")
    assert "browse_url" in registry.names() and "repo_map" in registry.names()
    tools.install(registry)
    assert set(registry.names()) <= set(ck.KEPT_TOOLS) and {"shell_command", "read_file", "write_file", "edit_file", "list_directory", "search_code"} <= set(registry.names())
    registry.execute("write_file", {"path": "x.txt", "content": "from the registry"})
    assert (root / "x.txt").read_text() == "from the registry" and not (tmp_path / "host" / "x.txt").exists()
    assert registry.execute("shell_command", {"command": "cat x.txt"})["stdout"] == "from the registry"
    schema = {s["function"]["name"]: s["function"]["parameters"] for s in registry.schemas()}
    assert "background" not in schema["shell_command"]["properties"]


# -- the trajectory ----------------------------------------------------------------------------------------------------

def lines(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines()]


def test_trajectory_events_are_in_the_format_the_analysis_reads(tmp_path):
    rec = TrajectoryRecorder(tmp_path / "t" / "trajectory.jsonl")
    rec.on_event({"type": "tool_start", "call_id": "1", "name": "shell_command", "arguments": {"command": "frobnicate"}})
    rec.on_event({"type": "tool_end", "call_id": "1", "name": "shell_command", "status": "completed", "decision": "executed",
                  "output": json.dumps({"exit_code": 127, "stdout": "", "stderr": "bash: frobnicate: command not found"})})
    rec.on_event({"type": "tool_start", "call_id": "2", "name": "write_file", "arguments": {"path": "a", "content": "x" * 5000}})
    rec.on_event({"type": "tool_end", "call_id": "2", "name": "write_file", "status": "failed", "decision": "denied", "output": "refused by the permission layer"})
    rec.on_event({"type": "todo", "items": []})
    rec.on_event({"type": "compaction", "dropped": 3})
    rec.stop("timeout")
    rec.close()
    events = lines(tmp_path / "t" / "trajectory.jsonl")
    kinds = [e["type"] for e in events]
    assert kinds == ["tool_call", "tool_result", "tool_call", "tool_result", "plan", "compaction", "stop"]
    assert events[0]["decision"] == "executed" and "command not found" in events[1]["error"]
    assert events[2]["decision"] == "refused" and len(events[2]["args"]["content"]) < 700
    assert events[-1] == {"type": "stop", "reason": "timeout"}
    res = TrialResult(passed=False)
    cls, _why, _names = analysis.classify(res, analysis._read_events(tmp_path / "t" / "trajectory.jsonl"), "")
    assert cls == "timeout"                                      # the stop reason outranks the missing command, as the analysis orders its rules


def test_stop_names(tmp_path):
    assert ck.stop_name("budget", "the time budget of 600 seconds is used") == "timeout"
    assert ck.stop_name("error", "400: maximum context length exceeded") == "context_overflow"
    assert ck.stop_name("final", "") == "final" and ck.stop_name("", "") == "final"


def test_the_local_engine_is_the_only_model_address():
    assert ck.local_endpoint("http://127.0.0.1:7232/", {"endpoint": "http://127.0.0.1:7232"}) == "http://127.0.0.1:7232"
    with pytest.raises(ValueError, match="not the local engine"):
        ck.local_endpoint("https://api.example.com/v1", {"endpoint": "http://127.0.0.1:7232"})
    with pytest.raises(ValueError, match="no model address"):
        ck.local_endpoint("", {})


def test_server_faults_are_told_from_the_agent_failing():
    assert ck.is_server_fault(AgentOutcome(stop_reason="error", error="cannot reach the Basalt server at http://127.0.0.1:7232: refused"))
    assert not ck.is_server_fault(AgentOutcome(stop_reason="error", error="maximum context length exceeded"))
    assert not ck.is_server_fault(AgentOutcome(stop_reason="final"))


def test_agent_settings_may_not_reach_outside_the_sandbox():
    assert ck.agent_overrides({"agent_config": {"compaction": False, "temperature": 0.2}}) == {"compaction": False, "temperature": 0.2}
    assert ck.agent_overrides({"reasoning": "low", "max_failed_attempts": 7}) == {"reasoning_effort": "low", "max_failed_attempts": 7}
    assert ck.agent_overrides({"reasoning": "", "max_failed_attempts": 0}) == {}
    with pytest.raises(ValueError, match="browser.*payments_dir|payments_dir.*browser"):
        ck.agent_overrides({"agent_config": {"browser": True, "payments_dir": "x"}})
    with pytest.raises(ValueError, match="not_a_field"):
        ck.agent_overrides({"agent_config": {"not_a_field": 1}})


# -- our agent in the container ----------------------------------------------------------------------------------------

def test_the_harness_agent_acts_in_the_container_and_leaves_a_trajectory(tmp_path):
    root = tmp_path / "container"
    root.mkdir()
    commands = []
    client = Scripted(call("write_file", path="hello.txt", content="hi from the agent"), call("shell_command", command="cat hello.txt && frobnicate"), say("Done."))
    traj = TrajectoryRecorder(tmp_path / "trial" / "trajectory.jsonl")
    out = ck.run_harness_agent("make hello.txt", local_exec(root, commands), endpoint="http://127.0.0.1:7232", work_dir=tmp_path / "trial", trajectory=traj,
                               environment_note="The system is the task's Linux container.", client=client, max_turns=8,
                               overrides={"final_review": False})
    traj.close()
    assert out.stop_reason == "final" and out.final == "Done." and out.turns == 3 and out.tool_calls == 2
    assert (root / "hello.txt").read_text() == "hi from the agent" and not (tmp_path / "trial" / "host-workspace" / "hello.txt").exists()
    system = client.sent[0][0]["content"]
    assert "the task's Linux container" in system and "Windows" not in system
    events = lines(tmp_path / "trial" / "trajectory.jsonl")
    assert [e["type"] for e in events if e["type"] != "assistant"] == ["tool_call", "tool_result", "tool_call", "tool_result", "stop"]
    assert events[3]["error"] and "command not found" in events[3]["error"]
    assert events[-1] == {"type": "stop", "reason": "final"}


def test_a_model_call_has_no_time_limit_of_its_own(tmp_path):
    root = tmp_path / "container"
    root.mkdir()
    for seconds in (600.0, None):
        client = Scripted(say("Done."))
        traj = TrajectoryRecorder(tmp_path / f"trial{seconds}" / "trajectory.jsonl")
        ck.run_harness_agent("x", local_exec(root), endpoint="http://127.0.0.1:7232", work_dir=tmp_path / f"trial{seconds}", trajectory=traj,
                             environment_note="The system is the task's Linux container.", client=client, max_seconds=seconds, max_turns=2)
        traj.close()
        assert client.call_timeout is None


def test_the_agent_cannot_use_a_tool_that_acts_on_this_pc(tmp_path):
    root = tmp_path / "container"
    root.mkdir()
    client = Scripted(call("browse_url", url="http://example.com"), say("ok"))
    traj = TrajectoryRecorder(tmp_path / "trial" / "trajectory.jsonl")
    out = ck.run_harness_agent("x", local_exec(root), endpoint="http://127.0.0.1:7232", work_dir=tmp_path / "trial", trajectory=traj, environment_note="n", client=client, max_turns=4,
                               overrides={"final_review": False})
    traj.close()
    tool_text = json.dumps(client.sent[1][-1])
    assert out.stop_reason == "final" and "browse_url" in tool_text and "tool" in tool_text.lower()


def test_a_stop_request_ends_the_agent(tmp_path):
    root = tmp_path / "container"
    root.mkdir()
    cancel = threading.Event()

    def first():
        cancel.set()
        return call("shell_command", command="true")
    client = Scripted(first, say("never reached"))
    traj = TrajectoryRecorder(tmp_path / "trial" / "trajectory.jsonl")
    out = ck.run_harness_agent("x", local_exec(root), endpoint="http://127.0.0.1:7232", work_dir=tmp_path / "trial", trajectory=traj, environment_note="n",
                               client=client, cancel=cancel, max_turns=6)
    traj.close()
    assert out.stop_reason == "cancelled"


# -- the baseline ------------------------------------------------------------------------------------------------------

class Counting(Scripted):
    def chat_completion(self, messages, tools=None, **kwargs):
        self.tools_seen = tools
        reply = super().chat_completion(messages, tools=tools, **kwargs)
        reply["usage"] = {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 40}}
        return reply


def test_the_baseline_has_one_bash_tool_and_stops_on_an_answer(tmp_path):
    root = tmp_path / "container"
    root.mkdir()
    client = Counting(call("bash", command="echo one > f.txt; cat f.txt"), say("finished"))
    traj = TrajectoryRecorder(tmp_path / "trajectory.jsonl")
    out = ck.run_bash_baseline("fix it", local_exec(root), client=client, trajectory=traj, max_steps=10)
    traj.close()
    assert [t["function"]["name"] for t in client.tools_seen] == ["bash"]
    assert out.stop_reason == "final" and out.final == "finished" and out.turns == 2 and out.tool_calls == 1
    assert (out.prompt_tokens, out.completion_tokens, out.cached_tokens) == (200, 20, 80)
    assert (root / "f.txt").read_text().strip() == "one"
    tool_message = client.sent[1][-1]
    assert tool_message["role"] == "tool" and json.loads(tool_message["content"])["stdout"].strip() == "one"
    assert [e["type"] for e in lines(tmp_path / "trajectory.jsonl")] == ["tool_call", "tool_result", "assistant", "stop"]


def test_the_baseline_reads_the_template_style_call_and_rejects_other_tools(tmp_path):
    root = tmp_path / "container"
    root.mkdir()
    xml = {"choices": [{"message": {"role": "assistant", "content": "<tool_call>\n<function=bash>\n<parameter=command>\necho xml > x.txt\n</parameter>\n</function>\n</tool_call>"}}]}
    client = Counting(xml, call("read_file", path="x.txt"), say("done"))
    traj = TrajectoryRecorder(tmp_path / "trajectory.jsonl")
    out = ck.run_bash_baseline("t", local_exec(root), client=client, trajectory=traj, max_steps=10)
    traj.close()
    assert (root / "x.txt").read_text().strip() == "xml"
    assert "call the bash tool" in client.sent[2][-1]["content"] and out.stop_reason == "final"
    events = lines(tmp_path / "trajectory.jsonl")
    assert [e["decision"] for e in events if e["type"] == "tool_call"] == ["executed", "invalid"]


def test_the_baseline_has_a_step_limit_a_time_limit_and_reports_a_server_fault(tmp_path):
    root = tmp_path / "container"
    root.mkdir()
    forever = Counting(*[call("bash", command="true") for _ in range(10)])
    traj = TrajectoryRecorder(tmp_path / "trajectory.jsonl")
    out = ck.run_bash_baseline("t", local_exec(root), client=forever, trajectory=traj, max_steps=3)
    assert out.stop_reason == "max_turns" and out.turns == 3
    broken = Counting(RuntimeError("cannot reach the Basalt server at http://127.0.0.1:7232"))
    out2 = ck.run_bash_baseline("t", local_exec(root), client=broken, trajectory=traj, max_steps=3)
    traj.close()
    assert out2.stop_reason == "error" and ck.is_server_fault(out2)


def test_settings_of_the_run_reach_the_agent_config(tmp_path):
    root = tmp_path / "container"
    root.mkdir()
    client = Scripted(*[call("shell_command", command="true") for _ in range(6)])
    traj = TrajectoryRecorder(tmp_path / "trajectory.jsonl")
    out = ck.run_harness_agent("x", local_exec(root), endpoint="http://127.0.0.1:7232", work_dir=tmp_path / "trial", trajectory=traj, environment_note="n", client=client,
                               max_turns=50, overrides=ck.agent_overrides({"agent_config": {"max_turns": 2}}))
    traj.close()
    assert out.stop_reason == "max_turns" and out.tool_calls == 2     # the run's own setting wins over the adapter's default
