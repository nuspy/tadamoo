"""read_file gives the model what it asks for (all of a file, or a range of lines) and says what it left out;
TokenJuice shortens noisy results (a shell result's stdout and stderr) and never an explicit read."""
import json

import pytest

from tadamoo.agent_loop import AgentLoop, FINAL
from tadamoo.context import TokenJuiceCompactor
from tadamoo.tools import create_standard_tool_registry
from tadamoo.tools.filesystem import WorkspaceSandbox
from tests.test_agent_loop import ScriptedClient, call, say


@pytest.fixture
def sb(tmp_path):
    return WorkspaceSandbox(tmp_path / "ws")


def numbered(n, width=20):
    return "".join(f"line {i:05d} " + "x" * width + "\n" for i in range(1, n + 1))


def test_a_file_of_500_lines_is_read_whole(sb):
    text = numbered(500)
    (sb.root / "long.txt").write_text(text, encoding="utf-8", newline="")
    assert sb.read_file("long.txt") == text


def test_offset_and_limit_return_that_range_and_say_where_it_is(sb):
    (sb.root / "long.txt").write_text(numbered(500), encoding="utf-8", newline="")
    out = sb.read_file("long.txt", offset=200, limit=3)
    assert out.startswith("line 00200 ") and "line 00202 " in out and "line 00203 " not in out
    assert out.rstrip().endswith("[lines 200-202 of 500 shown; read_file with offset=203 shows the next lines]")
    tail = sb.read_file("long.txt", offset=499, limit=50)
    assert "line 00500 " in tail and tail.rstrip().endswith("[lines 499-500 of 500 shown]")


def test_a_file_over_the_limit_says_how_long_it_is_and_the_next_offset(sb):
    (sb.root / "big.txt").write_text(numbered(2000), encoding="utf-8", newline="")
    out = sb.read_file("big.txt", max_chars=5000)
    note = out.rsplit("\n", 1)[-1]
    shown = out.count("\n")
    assert "of 2000 shown" in note and "at most 5000 characters" in note and f"offset={shown + 1} " in note
    nxt = sb.read_file("big.txt", offset=shown + 1, max_chars=5000)
    assert nxt.startswith(f"line {shown + 1:05d} ")


def test_an_offset_past_the_end_and_a_line_longer_than_the_limit_are_stated(sb):
    (sb.root / "short.txt").write_text("a\nb\n", encoding="utf-8")
    past = sb.read_file("short.txt", offset=9)
    assert "past the end" in past and "2 lines" in past
    (sb.root / "one.txt").write_text("y" * 9000, encoding="utf-8")
    out = sb.read_file("one.txt", max_chars=1000)
    assert out.startswith("y" * 1000) and "line 1 has 9000 characters and only the first 1000 are shown" in out
    with pytest.raises(ValueError, match="whole numbers"):
        sb.read_file("one.txt", offset="soon")
    (sb.root / "empty.txt").write_text("", encoding="utf-8")
    assert sb.read_file("empty.txt") == ""


def run_read(ws, tmp_path, name, args):
    registry, _ = create_standard_tool_registry(ws)
    compactor = TokenJuiceCompactor(ws / ".tadamoo" / "artifacts", display_dir=".tadamoo/artifacts")
    client = ScriptedClient(call("read_file", {"path": name, **args}), say("ok"))
    agent = AgentLoop(client=client, tool_registry=registry, output_filter=compactor.as_filter())
    agent.run("read it")
    assert agent.stop_reason == FINAL
    return [m for m in client.sent[-1] if m["role"] == "tool"][0]["content"], compactor


def test_the_middle_of_a_500_line_file_reaches_the_model_through_the_loop(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "long.txt").write_text(numbered(500), encoding="utf-8", newline="")
    content, _ = run_read(ws, tmp_path, "long.txt", {})
    assert "line 00250 " in content and "lines hidden" not in content


def test_a_saved_result_read_again_is_not_shortened_again(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".tadamoo" / "artifacts").mkdir(parents=True)
    compactor = TokenJuiceCompactor(ws / ".tadamoo" / "artifacts", display_dir=".tadamoo/artifacts")
    shortened = compactor.compress_output("".join(f"out {i}\n" for i in range(300)), "shell_command")
    assert "lines hidden" in shortened
    artifact = next((ws / ".tadamoo" / "artifacts").iterdir()).name
    content, _ = run_read(ws, tmp_path, f".tadamoo/artifacts/{artifact}", {})
    assert "out 150" in content and "lines hidden" not in content


def test_a_shell_result_keeps_its_shape_and_loses_the_middle_of_its_output(tmp_path):
    c = TokenJuiceCompactor(tmp_path / "art", display_dir=".tadamoo/artifacts")
    stdout = "".join(f"step {i}\n" for i in range(300))
    result = {"stdout": stdout, "stderr": "", "exit_code": 0, "success": True}
    out = c.compress_output(json.dumps(result), "shell_command")
    assert "\n" not in out
    data = json.loads(out)
    assert data["exit_code"] == 0 and data["success"] is True
    assert "step 0\n" in data["stdout"] and "step 299" in data["stdout"] and "step 150\n" not in data["stdout"]
    assert "lines hidden in total; full output saved to .tadamoo/artifacts/shell_command_stdout_" in data["stdout"]
    saved = next(p for p in (tmp_path / "art").iterdir() if "stdout" in p.name)
    assert saved.read_text(encoding="utf-8") == stdout


def test_a_very_long_line_in_a_shell_result_is_cut_and_says_so(tmp_path):
    c = TokenJuiceCompactor(tmp_path / "art", display_dir=".tadamoo/artifacts")
    out = c.compress_output(json.dumps({"stdout": "z" * 30_000, "exit_code": 0}), "shell_command")
    data = json.loads(out)
    assert len(out) < 4000 and "characters hidden" in data["stdout"] and "full output saved to" in data["stdout"]


def test_a_short_shell_result_is_untouched_and_a_dict_without_long_text_is_not_rewritten(tmp_path):
    c = TokenJuiceCompactor(tmp_path / "art")
    small = json.dumps({"stdout": "ok\n", "exit_code": 0})
    assert c.compress_output(small, "shell_command") == small
    assert list((tmp_path / "art").iterdir()) == []
