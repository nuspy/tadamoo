"""The error classifier is on by default: an error that waiting cannot cure ends the turn and says what it was; with it off every model error
is retried."""
import pytest

from harness import error_classifier as ec
from harness.client import BasaltError
from harness.session import parse_args
from harness.stack import AgentConfig, agent_settings
from test_session import Scripted, make, say


@pytest.mark.parametrize("status, message, kind", [
    (0, "cannot reach the server", ec.TRANSIENT), (None, "boom", ec.TRANSIENT), (503, "no model is loaded yet", ec.TRANSIENT),
    (429, "rate limit", ec.TRANSIENT), (500, "oops", ec.TRANSIENT), (408, "slow", ec.TRANSIENT), (400, "the model is still loading", ec.TRANSIENT),
    (400, "the prompt is over the context", ec.OVERFLOW), (400, "This model's maximum context length is 8192 tokens", ec.OVERFLOW),
    (413, "prompt is too long", ec.OVERFLOW), (400, "malformed request: tools[0].parameters", ec.DETERMINISTIC), (401, "invalid api key", ec.DETERMINISTIC),
    (404, "no such endpoint", ec.DETERMINISTIC), (422, "bad field", ec.DETERMINISTIC), (418, "teapot", ec.TRANSIENT),
])
def test_an_error_is_sorted_by_its_status_and_its_words(status, message, kind):
    assert ec.classify(status, message) == kind


def test_the_classifier_is_on_unless_turned_off():
    assert AgentConfig().error_classifier is True
    assert agent_settings(parse_args([]))["error_classifier"] is True
    assert agent_settings(parse_args(["--no-error-classifier"]))["error_classifier"] is False
    with pytest.raises(SystemExit):
        parse_args(["--error-classifier"])


def test_with_it_off_a_refused_request_is_retried(tmp_path):
    client = Scripted(BasaltError(400, "malformed request"), say("Ok now."))
    session, ev, store, built = make(tmp_path, client, classify_errors=False)
    session.send("t1", "hello")
    ev.wait("done")
    assert [e["event"] for e in ev.items if e["event"] == "retrying"] == ["retrying"]
    assert ev.wait("done")["stop_reason"] == "final"


def test_by_default_a_request_the_server_will_always_refuse_ends_the_turn_with_the_error(tmp_path):
    client = Scripted(BasaltError(400, "malformed request: tools[0]"), say("never"))
    session, ev, store, built = make(tmp_path, client)
    session.send("t1", "hello")
    done = ev.wait("done")
    assert done["stop_reason"] == "error" and "malformed request" in done["error"] and "retrying" not in ev.kinds()
    assert ev.wait("model_error")["retry"] is False and len(client.sent) == 1


def test_by_default_a_busy_or_loading_server_is_still_waited_for(tmp_path):
    client = Scripted(BasaltError(503, "no model is loaded yet"), BasaltError(0, "cannot reach"), BasaltError(400, "the prompt is over the context"), say("Fine."))
    session, ev, store, built = make(tmp_path, client)
    session.send("t1", "hello")
    done = ev.wait("done")
    assert done["stop_reason"] == "final" and ev.kinds().count("retrying") == 3
