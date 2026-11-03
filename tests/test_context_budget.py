"""The context budget follows the context the server reports for its model, with a named fallback."""
import time
import urllib.error

import pytest

from tadamoo.client import TadamooClient, TadamooError
from tadamoo.session import Session, SessionStore
from tadamoo.stack import (ANSWER_RESERVE_TOKENS, FALLBACK_CONTEXT_TOKENS, MIN_BUDGET_TOKENS, AgentConfig, build_agent, context_budget,
                           server_context_window)


class Model:
    """A client with a window to report (or an error to raise); it answers every request with a short text."""

    def __init__(self, window=131072, error=None):
        self.window, self.error, self.asked = window, error, 0

    def context_window(self, *a, **k):
        self.asked += 1
        if self.error is not None:
            raise self.error
        return self.window

    def chat_completion(self, messages, tools=None, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


def build(tmp_path, client, **kw):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False, **kw), client=client)


def unreachable():
    return TadamooError(0, "cannot reach the TaDaMoo server at http://127.0.0.1:7232")


# -- the client ------------------------------------------------------------------------------------

def test_the_client_reads_the_context_from_v1_status(monkeypatch):
    client = TadamooClient()
    asked = []
    monkeypatch.setattr(client, "_get", lambda path, timeout: asked.append(path) or {"loaded": True, "context": {"native": 131072, "max_positions": 131072}})
    assert client.context_window() == 131072 and asked == ["/v1/status"]
    monkeypatch.setattr(client, "_get", lambda path, timeout: {"cache_max_tokens": 65536})
    assert client.context_window() == 65536                              # a server that only has the older field


def test_the_client_says_nothing_for_a_model_that_is_not_loaded_or_a_status_without_a_number(monkeypatch):
    client = TadamooClient()
    monkeypatch.setattr(client, "_get", lambda path, timeout: {"loaded": False, "context": {"native": 4096}})
    assert client.context_window() is None                              # an engine that is not loaded reports a default
    monkeypatch.setattr(client, "_get", lambda path, timeout: {"loaded": True, "context": {}})
    assert client.context_window() is None
    monkeypatch.setattr(client, "_get", lambda path, timeout: {"context": {"native": 0}})
    assert client.context_window() is None


def test_the_client_raises_when_the_server_cannot_be_reached(monkeypatch):
    def refuse(*a, **k):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr("tadamoo.client.urllib.request.urlopen", refuse)
    with pytest.raises(TadamooError) as err:
        TadamooClient(base_url="http://127.0.0.1:1").context_window()
    assert err.value.status == 0


def test_the_status_helper_answers_none_for_anything_but_a_number():
    assert server_context_window(Model(131072)) == 131072
    assert server_context_window(Model(None)) is None
    assert server_context_window(Model(error=unreachable())) is None
    assert server_context_window(Model("big")) is None
    assert server_context_window(object()) is None and server_context_window(None) is None


# -- the budget ------------------------------------------------------------------------------------

def test_the_budget_is_the_window_less_a_tenth_and_the_room_for_the_answer():
    assert context_budget(131072, ANSWER_RESERVE_TOKENS) == int(131072 * 0.9) - ANSWER_RESERVE_TOKENS
    assert context_budget(100, 4096) == MIN_BUDGET_TOKENS                # never below the floor


def test_a_server_window_sets_the_budget_with_the_answer_reserve(tmp_path):
    client = Model(131072)
    built = build(tmp_path, client)
    assert built.agent.context.budget_tokens == context_budget(131072, ANSWER_RESERVE_TOKENS) == 109772
    assert client.asked == 1


def test_a_small_window_keeps_a_quarter_for_the_answer_and_the_floor_holds(tmp_path):
    assert build(tmp_path, Model(16384)).agent.context.budget_tokens == context_budget(16384, 4096)      # the answer's own limit is larger than a quarter
    assert build(tmp_path, Model(2048)).agent.context.budget_tokens == MIN_BUDGET_TOKENS


def test_an_unreachable_server_falls_back_to_the_old_window_and_says_so_once(tmp_path, capsys):
    built = build(tmp_path, Model(error=unreachable()))
    assert built.agent.context.budget_tokens == context_budget(FALLBACK_CONTEXT_TOKENS, 4096) == 25395
    err = capsys.readouterr().err
    assert err.count("did not report its context") == 1 and str(FALLBACK_CONTEXT_TOKENS) in err


def test_a_client_that_cannot_ask_gets_the_fallback_without_a_message(tmp_path, capsys):
    built = build(tmp_path, object())
    assert built.agent.context.budget_tokens == context_budget(FALLBACK_CONTEXT_TOKENS, 4096)
    assert "did not report" not in capsys.readouterr().err


def test_an_explicit_window_is_used_and_the_server_is_not_asked(tmp_path):
    client = Model(131072)
    built = build(tmp_path, client, context_window=65536)
    assert built.agent.context.budget_tokens == context_budget(65536, ANSWER_RESERVE_TOKENS)
    built.refresh_budget()
    assert client.asked == 0 and built.agent.context.budget_tokens == context_budget(65536, ANSWER_RESERVE_TOKENS)


def test_the_budget_is_taken_again_when_the_server_gives_another_window_and_kept_when_it_does_not(tmp_path, capsys):
    client = Model(error=unreachable())
    built = build(tmp_path, client)
    first = built.agent.context.budget_tokens
    built.refresh_budget()                                               # still no answer: nothing changes, nothing is said again
    assert built.agent.context.budget_tokens == first and capsys.readouterr().err.count("did not report") == 1
    client.error, client.window = None, 131072                           # the model has been loaded since
    built.refresh_budget()
    assert built.agent.context.budget_tokens == 109772 and "follows the server" in capsys.readouterr().err
    client.error = unreachable()
    built.refresh_budget()
    assert built.agent.context.budget_tokens == 109772                   # a failed question keeps the last known value
    client.error, client.window = None, 65536
    built.refresh_budget()
    assert built.agent.context.budget_tokens == context_budget(65536, ANSWER_RESERVE_TOKENS)


def test_the_context_manager_masks_late_when_the_window_is_large(tmp_path):
    big, small = build(tmp_path / "a", Model(131072)), build(tmp_path / "b", Model(error=unreachable()))
    msgs = [{"role": "user", "content": "go"}] + [m for i in range(6) for m in (
        {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": f"c{i}", "name": "read_file", "content": "x" * 20000})]
    assert not any(m.get("masked") for m in big.agent.context.prepare(msgs))
    assert any(m.get("masked") for m in small.agent.context.prepare(msgs))


def test_each_request_of_a_session_asks_the_server_again(tmp_path):
    client = Model(error=unreachable())
    built = build(tmp_path, client, permission_mode="auto", max_turns=3)
    events = []
    session = Session(built, SessionStore(tmp_path / "hist"), events.append, session_id="s", waits=(0,), sleep=lambda s: None)
    assert built.agent.context.budget_tokens == context_budget(FALLBACK_CONTEXT_TOKENS, 4096)
    client.error, client.window = None, 131072
    session.send("m1", "hello")
    for _ in range(200):
        if any(e["event"] == "done" for e in events):
            break
        time.sleep(0.05)
    assert built.agent.context.budget_tokens == 109772
