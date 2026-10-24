"""The client's streamed answers: the same whole answer as without streaming, with the text handed on as it comes."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.client import BasaltClient, BasaltError
from test_e2e_mock_server import answer, start_server, tool_call  # noqa: F401  (fixtures)

MESSAGES = [{"role": "user", "content": "ciao"}]


def test_a_streamed_answer_is_the_same_whole_answer_and_the_pieces_add_up(start_server):  # noqa: F811
    url = start_server(["pensiero breve</think>\n\nCiao, come stai? Tutto bene qui da me.", "pensiero breve</think>\n\nCiao, come stai? Tutto bene qui da me."])
    client = BasaltClient(base_url=url)
    pieces = []
    streamed = client.chat_completion(MESSAGES, on_delta=lambda kind, text: pieces.append((kind, text)))
    plain = client.chat_completion(MESSAGES)
    assert streamed["choices"][0]["message"]["content"] == plain["choices"][0]["message"]["content"] == "Ciao, come stai? Tutto bene qui da me."
    assert streamed["choices"][0]["message"]["reasoning_content"] == plain["choices"][0]["message"]["reasoning_content"]
    assert "".join(t for k, t in pieces if k == "content") == streamed["choices"][0]["message"]["content"]
    assert "".join(t for k, t in pieces if k == "reasoning") == streamed["choices"][0]["message"]["reasoning_content"]
    assert len(pieces) > 2 and streamed["usage"]["completion_tokens"] > 0 and streamed["choices"][0]["finish_reason"] == "stop"


def test_a_streamed_tool_call_arrives_whole(start_server):  # noqa: F811
    url = start_server([tool_call("write_file", path="a.txt", content="ciao"), tool_call("write_file", path="a.txt", content="ciao")])
    client = BasaltClient(base_url=url)
    streamed = client.chat_completion(MESSAGES, tools=[{"type": "function", "function": {"name": "write_file", "description": "w", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}}}], on_delta=lambda k, t: None)
    plain = client.chat_completion(MESSAGES, tools=[{"type": "function", "function": {"name": "write_file", "description": "w", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}}}])
    got = streamed["choices"][0]["message"]["tool_calls"]
    assert [c["function"]["name"] for c in got] == ["write_file"]
    assert got[0]["function"]["arguments"] and __import__("json").loads(got[0]["function"]["arguments"]) == {"path": "a.txt", "content": "ciao"}
    assert streamed["choices"][0]["finish_reason"] == "tool_calls" == plain["choices"][0]["finish_reason"]


def test_a_stream_that_stops_half_way_is_an_error_to_retry_not_an_answer():
    class Cut:
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"Ciao"}}]}\n'
            yield b"\n"

    from harness.client import _collect_stream
    with pytest.raises(BasaltError) as err:
        _collect_stream(Cut(), lambda k, t: None)
    assert err.value.status == 0


def test_an_error_inside_the_stream_is_an_error():
    class Bad:
        def __iter__(self):
            yield b'data: {"error":{"message":"the engine stopped"}}\n'

    from harness.client import _collect_stream
    with pytest.raises(BasaltError, match="the engine stopped"):
        _collect_stream(Bad(), lambda k, t: None)
