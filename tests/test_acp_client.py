"""Basalt as an ACP client: a fake agent in a subprocess, and Basalt's own ACP agent driven over real stdio."""
import sys
import threading
import time
from pathlib import Path

import pytest

from harness.acp_client import AcpClient, AcpError, run_task

HERE = Path(__file__).resolve().parent
FAKE = [sys.executable, str(HERE / "fake_acp_agent.py")]
BASALT_AGENT = [sys.executable, str(HERE / "fake_acp_basalt.py")]


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


def test_a_task_goes_through_initialize_new_session_and_prompt(ws):
    out = run_task(FAKE, ws, "hello")
    assert out["agent"] == "Fake Agent" and out["stop_reason"] == "end_turn"
    assert out["text"] == "Hi there"                                    # the chunks, joined; the banner line was ignored
    assert out["tool_calls"] == ["Read notes.txt (completed)"]


def test_a_permission_request_is_put_to_the_person_and_their_yes_is_sent_back(ws):
    asked = []
    out = run_task(FAKE, ws, "permission", ask=lambda title, details: asked.append((title, details)) or True)
    assert out["text"] == "outcome=yes"
    assert asked == [("Run rm -rf build", {"agent": "agent", "kind": "execute", "input": {"command": "rm -rf build"}})]


def test_a_no_or_nobody_to_ask_is_a_refusal(ws):
    assert run_task(FAKE, ws, "permission", ask=lambda t, d: False)["text"] == "outcome=no"
    assert run_task(FAKE, ws, "permission")["text"] == "outcome=no"
    assert run_task(FAKE, ws, "permission", ask=lambda t, d: 1 / 0)["text"] == "outcome=no"


def test_the_agent_gets_no_file_access_from_the_client(ws):
    assert run_task(FAKE, ws, "fs")["text"] == "fs refused"


def test_cancel_is_sent_to_the_agent_and_the_turn_ends(ws):
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    start = time.monotonic()
    out = run_task(FAKE, ws, "cancel", cancel=cancel)
    assert out["stop_reason"] == "cancelled" and out["text"] == "working"
    assert time.monotonic() - start < 15


def test_a_crash_reports_what_the_agent_wrote_to_stderr(ws):
    with pytest.raises(AcpError, match="the model key is missing"):
        run_task(FAKE, ws, "crash")


def test_a_silent_agent_times_out_and_is_killed(ws):
    client = AcpClient(FAKE, ws)
    with client:
        client.initialize()
        session = client.new_session()
        with pytest.raises(AcpError, match="did not answer"):
            client.prompt(session, "hang", timeout=1)
        proc = client.proc
    assert proc.poll() is not None


def test_a_newer_protocol_version_is_refused(ws):
    with pytest.raises(AcpError, match="version 2"):
        run_task(FAKE + ["--version", "2"], ws, "hello")


def test_a_missing_program_is_reported(ws):
    with pytest.raises(AcpError, match="not on this PC's PATH"):
        run_task(["no-such-acp-agent-xyz"], ws, "hello")


def test_basalts_own_agent_asks_through_us_and_runs_the_command_after_a_yes(ws, tmp_path):
    asked = []
    out = run_task(BASALT_AGENT + [str(ws), str(tmp_path / "data")], ws, "run it",
                   ask=lambda title, details: asked.append(title) or True, timeout=60)
    assert out["agent"] == "Basalt Agent" and out["stop_reason"] == "end_turn"
    assert out["text"].endswith("Done: the command was run.")
    assert len(asked) == 1 and "shell_command" in asked[0]
    assert any("shell_command" in c and "completed" in c for c in out["tool_calls"])


def test_basalts_own_agent_does_not_run_the_command_after_a_no(ws, tmp_path):
    out = run_task(BASALT_AGENT + [str(ws), str(tmp_path / "data")], ws, "run it", ask=lambda t, d: False, timeout=60)
    assert any("shell_command" in c and "failed" in c for c in out["tool_calls"])
