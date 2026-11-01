"""The browser tools can drive a browser that is already running (its debugging address) and never start or close one."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.browser import Browser, BrowserError, external_endpoint, find_browser, register_browser_tools
from harness.headless import parse_args
from harness.session import parse_args as session_args
from harness.stack import agent_settings
from harness.tools.registry import ToolRegistry
from test_browser import shop  # noqa: F401 (fixture)

needs_browser = pytest.mark.skipif(find_browser() is None, reason="needs Edge or Chrome on this PC")


def test_the_address_of_a_running_browser_is_read_as_host_and_port():
    assert external_endpoint("http://127.0.0.1:9223") == ("127.0.0.1", 9223)
    assert external_endpoint("http://browser.local") == ("browser.local", 9222)
    assert external_endpoint("127.0.0.1:9333") == ("127.0.0.1", 9333)
    for bad in ("ws://127.0.0.1:9222/devtools/browser/x", "file:///tmp/x", "http://"):
        with pytest.raises(BrowserError):
            external_endpoint(bad)


def test_an_external_browser_is_neither_private_guarded_nor_persistent(tmp_path):
    b = Browser(tmp_path / "profile", persistent=True, cdp_url="http://127.0.0.1:9333")
    assert b.cdp_url and b.port == 9333 and b.cdp_host == "127.0.0.1"
    assert b.allow_private and not b.persistent and not b.keep_profile
    assert b.profile_dir != tmp_path / "profile"          # the given profile is never touched, let alone removed
    b.close()


def test_the_option_reaches_the_agent_settings_of_the_command_line_and_the_chat():
    args = ["--browser", "--browser-cdp-url", "http://127.0.0.1:9222"]
    assert agent_settings(parse_args(["-p", "task", *args]))["browser_cdp_url"] == "http://127.0.0.1:9222"
    assert agent_settings(session_args(args))["browser_cdp_url"] == "http://127.0.0.1:9222"
    assert agent_settings(parse_args(["-p", "task"]))["browser_cdp_url"] == ""


def test_a_browser_that_does_not_answer_is_reported_not_started(tmp_path, monkeypatch):
    b = Browser(cdp_url="http://127.0.0.1:1")
    monkeypatch.setattr("harness.browser.time.sleep", lambda s: None)
    with pytest.raises(BrowserError, match="no browser page answered"):
        b.start()
    assert b.proc is None
    b.close()


@needs_browser
def test_an_already_running_browser_is_driven_and_left_running(tmp_path, shop):
    owner = Browser(tmp_path / "owner", headless=True, allow_private=True)
    owner.start()
    registry = ToolRegistry()
    guest = register_browser_tools(registry, None, cdp_url=f"http://127.0.0.1:{owner.port}")
    try:
        page = registry.execute("browser_open", {"url": shop})
        assert "Hotel Cervino" in page and "Book a room" in page
        assert guest.proc is None                          # nothing was started
    finally:
        guest.close()
    try:
        assert owner._targets(), "the owner's browser must still answer"
    finally:
        owner.close()


def test_a_headless_run_hands_every_event_to_a_recorder_in_the_format_analysis_reads(tmp_path):
    """What the ClawBench driver does inside the container, with a scripted model: the events of a run end up as analysis.py's trajectory."""
    from harness.bench import analysis
    from harness.bench.adapters.containerkit import TrajectoryRecorder
    from harness.headless import run_headless
    from harness.stack import AgentConfig
    from test_agent_loop import ScriptedClient, call, say

    ws = tmp_path / "ws"
    ws.mkdir()
    out = tmp_path / "traj.jsonl"
    rec = TrajectoryRecorder(out)
    cfg = AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False, report=False, permission_mode="auto")
    result = run_headless(cfg, "write a file", client=ScriptedClient(call("write_file", {"path": "a.txt", "content": "ok"}, "w"), say("done")), on_event=rec.on_event)
    rec.stop("final")
    rec.close()
    assert result["status"] == "success"
    events = analysis._read_events(out)
    assert [e["type"] for e in events if e["type"] != "assistant"] == ["tool_call", "tool_result", "stop"]
    assert events[0]["name"] == "write_file" and events[0]["decision"] == "executed"
