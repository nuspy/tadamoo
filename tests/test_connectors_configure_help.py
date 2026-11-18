"""Guided help filling in a connector's settings: the model is told the field names only, never a value, and the chat is kept nowhere."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tadamoo.connectors import configure_help          # noqa: E402

VARIABLES = [{"name": "API_KEY", "required": True, "secret": True, "set": False}, {"name": "REGION", "required": False, "secret": False, "set": True}]


class Client:
    def __init__(self, content="An answer."):
        self.content, self.seen = content, []

    def side_context(self):
        return 60000

    def chat_completion(self, messages, **kw):
        self.seen.append(messages)
        return {"choices": [{"message": {"content": self.content}}]}


def test_the_model_is_told_field_names_and_their_state_never_a_value():
    client = Client()
    out = configure_help.ask(client, "demo", VARIABLES, "Where do I get the API key?", [])
    assert out == "An answer."
    system = client.seen[0][0]["content"]
    assert "API_KEY: required, secret, not set" in system and "REGION: optional, plain, already set" in system
    assert "Where do I get the API key?" not in system                                    # the question is its own message, not folded into the system prompt


def test_a_short_history_is_sent_back_and_nothing_longer_or_of_the_wrong_shape_is_kept():
    client = Client()
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}, {"role": "system", "content": "ignored"}, {"role": "user", "content": 5}]
    configure_help.ask(client, "demo", VARIABLES, "and then?", history)
    roles = [m["role"] for m in client.seen[0]]
    assert roles == ["system", "user", "assistant", "user"]                               # the bad-shaped and the system-role entries are dropped


def test_without_a_model_or_on_a_failure_the_person_gets_a_plain_answer_never_a_crash():
    assert configure_help.ask(None, "demo", VARIABLES, "help", []) == "There is no model to ask right now."

    class Boom(Client):
        def chat_completion(self, messages, **kw):
            raise RuntimeError("down")
    assert configure_help.ask(Boom(), "demo", VARIABLES, "help", []) == "The model did not answer this time."

    assert configure_help.ask(Client(""), "demo", VARIABLES, "help", []) == "I don't have an answer for that."


def test_a_connector_with_no_fields_still_gets_a_sensible_prompt():
    client = Client()
    configure_help.ask(client, "demo", [], "what does this do?", [])
    assert "(this connector reads no settings)" in client.seen[0][0]["content"]
