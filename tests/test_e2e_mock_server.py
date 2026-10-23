"""The whole stack against the real HTTP server driven by the scripted MockEngine (no GPU)."""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from harness.client import BasaltClient
from harness.headless import EXIT_CODES, main, parse_args, run_headless
from harness.stack import AgentConfig, build_agent
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[1]


class _NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


def tool_call(name, **params):
    body = "".join(f"<parameter={k}>\n{v}\n</parameter>\n" for k, v in params.items())
    return f"</think>\n\n<tool_call>\n<function={name}>\n{body}</function>\n</tool_call>"


def answer(text):
    return f"</think>\n\n{text}"


@pytest.fixture
def start_server():
    started = []

    def start(scripts, api_key=""):
        tok = ByteTokenizer()
        svc = Service(MockEngine(tok, scripts, max_context=16384), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        svc.telemetry = _NoTelemetry()
        svc.api_key = api_key
        httpd = serve(svc, port=0)
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


def config(ws, tmp_path, endpoint, **kw):
    return AgentConfig(workspace=str(ws), endpoint=endpoint, data_dir=str(tmp_path / "data"), **kw)


def test_a_task_runs_end_to_end_and_leaves_a_verifiable_audit_trail(start_server, ws, tmp_path):
    url = start_server([tool_call("write_file", path="hello.txt", content="hi there"), answer("File written.")])
    result = run_headless(config(ws, tmp_path, url), "write hello.txt")
    assert result["status"] == "success" and result["stop_reason"] == "final"
    assert (ws / "hello.txt").read_text(encoding="utf-8") == "hi there"
    assert result["final_output"] == "File written." and result["turns_count"] == 2
    assert result["tokens"]["prompt"] > 0 and result["changes"][0]["kind"] == "added"

    from harness.audit import AuditLedger
    ledger = AuditLedger(result["audit_log"])
    assert ledger.verify_integrity() == (True, None)
    actions = [r["action"] for r in ledger.records]
    assert "permission_granted" in actions and "checkpoint" in actions and "tool_executed" in actions


def test_a_shell_command_is_refused_without_a_confirmation_channel(start_server, ws, tmp_path):
    url = start_server([tool_call("shell_command", command="echo pwned > proof.txt"), answer("Could not run it.")])
    result = run_headless(config(ws, tmp_path, url), "run something")
    assert result["status"] == "success" and not (ws / "proof.txt").exists()


def test_an_allow_rule_lets_a_plain_command_run_but_not_a_chained_one(start_server, ws, tmp_path):
    allow = ["shell_command:python -c *"]
    url = start_server([tool_call("shell_command", command='python -c "open(\'ok.txt\',\'w\').write(\'1\')"'),
                        tool_call("shell_command", command='python -c "pass" && echo chained > chained.txt'),
                        answer("done")])
    run_headless(config(ws, tmp_path, url, allow=allow), "go")
    assert (ws / "ok.txt").exists() and not (ws / "chained.txt").exists()


def test_plan_mode_blocks_every_change(start_server, ws, tmp_path):
    url = start_server([tool_call("write_file", path="x.txt", content="1"), answer("planned")])
    run_headless(config(ws, tmp_path, url, permission_mode="plan"), "go")
    assert not (ws / "x.txt").exists()


def test_a_wrong_api_key_is_reported_as_an_error(start_server, ws, tmp_path):
    url = start_server([answer("never")], api_key="right")
    result = run_headless(config(ws, tmp_path, url, api_key="wrong"), "go")
    assert result["stop_reason"] == "error" and "API key" in result["error"]
    ok = run_headless(config(ws, tmp_path, url, api_key="right"), "go")
    assert ok["stop_reason"] == "final"


def test_an_unreachable_server_is_an_error_not_a_crash(ws, tmp_path):
    result = run_headless(config(ws, tmp_path, "http://127.0.0.1:59996"), "go")
    assert result["stop_reason"] == "error" and "cannot reach" in result["error"]


def test_agents_md_reaches_the_model_and_memories_follow_the_goal(start_server, ws, tmp_path):
    (ws / "AGENTS.md").write_text("Always answer in Latin.", encoding="utf-8")
    url = start_server([answer("ok")])
    built = build_agent(config(ws, tmp_path, url))
    built.memory.add_memory("The build uses CMake with sm_120 kernels")
    sent = []
    real = built.agent.client.chat_completion
    built.agent.client.chat_completion = lambda **kw: (sent.append(kw["messages"]), real(**kw))[1]
    built.agent.run("how do I build for sm_120", retrieved=built.retrieved_message("how do I build for sm_120"))
    built.close()
    assert "Always answer in Latin." in sent[0][0]["content"]
    assert sent[0][-1]["content"].startswith("<untrusted_data source=\"memory\">") and "CMake" in sent[0][-1]["content"]
    assert sent[0][1]["content"] == "how do I build for sm_120"


def cli_json(text):
    """The CLI's JSON object; the in-process test server also logs to the same stdout."""
    return json.loads(text[text.index('{' + chr(10) + '  "status"'):])


def test_cli_prints_json_and_returns_the_exit_code(start_server, ws, tmp_path):
    url = start_server([answer("hello from the agent")])
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(["-p", "hi", "--workspace", str(ws), "--endpoint", url, "--json", "--no-memory",
                     "--no-checkpoints"])
    data = cli_json(out.getvalue())
    assert code == 0 and data["status"] == "success" and data["final_output"] == "hello from the agent"


def test_cli_exit_codes_distinguish_the_stop_reasons(start_server, ws):
    assert EXIT_CODES["final"] == 0 and len(set(EXIT_CODES.values())) == len(EXIT_CODES)
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(["-p", "hi", "--workspace", str(ws), "--endpoint", "http://127.0.0.1:59996", "--json",
                     "--no-memory", "--no-checkpoints"])
    assert code == 1 and cli_json(out.getvalue())["stop_reason"] == "error"


def test_cli_arguments():
    args = parse_args(["-p", "task", "--workspace", "w", "--allow", "a", "--allow", "b", "--secret-env", "TOKEN",
                       "--permission-mode", "auto", "--json"])
    assert (args.prompt, args.workspace, args.allow, args.secret_env, args.permission_mode, args.json) == (
        "task", "w", ["a", "b"], ["TOKEN"], "auto", True)
    with pytest.raises(SystemExit):
        parse_args(["--workspace", "w"])


def test_a_missing_workspace_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="workspace folder not found"):
        build_agent(AgentConfig(workspace=str(tmp_path / "nope"), data_dir=str(tmp_path / "d")))
    with pytest.raises(ValueError, match="permission mode"):
        build_agent(AgentConfig(workspace=str(tmp_path), permission_mode="yolo", data_dir=str(tmp_path / "d")))
