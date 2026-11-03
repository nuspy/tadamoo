"""Personal skills: from the requests of a conversation to the values that change and to a script."""
import json
from datetime import datetime

import pytest

from tadamoo.personal_skills import distill
from tadamoo.personal_skills.check import check
from test_session import Scripted, make, say

DAY = datetime(2026, 10, 26, 15, 0).timestamp()


def call(i, name, **arguments):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def result(i, text):
    return {"role": "tool", "tool_call_id": f"c{i}", "name": "x", "content": text}


MESSAGES = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "[harness] The user's plans now: none"},
    {"role": "user", "content": "[planner] a step failed"},
    {"role": "user", "content": "trova le notizie di oggi riguardanti la guerra in Ucraina e salvale", "raw": "trova le notizie # di oggi # riguardanti # la guerra in Ucraina # e salvale", "ts": DAY},
    call(1, "search_web", query="guerra in Ucraina notizie"), result(1, "1. Title - https://news.example/a\n2. Other - https://news.example/b"),
    call(2, "browse_url", url="https://news.example/a"), result(2, "page text about the war"),
    call(3, "write_file", path="notizie/report-2026-10-26.md", content="# Notizie\n..."), result(3, "Wrote 20 bytes"),
    call(4, "shell_command", command="rm /nope"), result(4, "Tool execution error: not found"),
    {"role": "assistant", "content": "Saved the report.", "ts": DAY},
    {"role": "user", "content": "e mandami anche il file notes.txt della cartella docs", "ts": DAY + 60},
    call(5, "read_file", path="docs/notes.txt"), result(5, "the notes"),
    {"role": "assistant", "content": "Here are the notes.", "ts": DAY + 61},
]


def test_requests_are_numbered_as_the_page_counts_them():
    reqs = distill.requests_of(MESSAGES)
    assert [(r["n"], r["index"]) for r in reqs] == [(1, 3), (2, 13)]                      # the harness's own notes are not requests
    assert reqs[0]["raw"].count("#") == 4 and reqs[0]["text"] == "trova le notizie di oggi riguardanti la guerra in Ucraina e salvale"
    assert reqs[1]["raw"] == reqs[1]["text"]


def test_the_steps_are_the_recorded_calls_in_order_with_what_failed_marked():
    reqs = distill.requests_of(MESSAGES)
    steps = distill.steps_of(MESSAGES, reqs, [1, 2])
    assert [(s["turn"], s["kind"], s.get("name"), s.get("ok")) for s in steps] == [
        (1, "tool", "search_web", True), (1, "tool", "browse_url", True), (1, "tool", "write_file", True), (1, "tool", "shell_command", False),
        (1, "answer", None, None), (2, "tool", "read_file", True), (2, "answer", None, None)]
    assert steps[0]["arguments"] == {"query": "guerra in Ucraina notizie"} and steps[1]["result"] == "page text about the war"
    assert [s["name"] for s in distill.steps_of(MESSAGES, reqs, [2]) if s["kind"] == "tool"] == ["read_file"]


def test_what_changes_is_found_from_the_marks_the_persons_words_and_the_dates():
    reqs = distill.requests_of(MESSAGES)
    found = distill.candidates(reqs, distill.steps_of(MESSAGES, reqs, [1, 2]))
    by_value = {c["value"]: c for c in found}
    assert [c["source"] for c in found[:2]] == ["mark", "mark"] and {c["value"] for c in found[:2]} == {"di oggi", "la guerra in Ucraina"}
    assert by_value["2026-10-26"]["source"] == "date" and by_value["2026-10-26"]["type"] == "date" and by_value["2026-10-26"]["default"] == "oggi"
    assert by_value["2026-10-26"]["used_in"] == [{"tool": "write_file", "argument": "path"}]
    assert by_value["docs/notes.txt"]["source"] == "words" and by_value["docs/notes.txt"]["used_in"] == [{"tool": "read_file", "argument": "path"}]
    assert "search_web" not in by_value and "Notizie" not in str(by_value.keys())                    # tool names and content the model wrote are not candidates


def test_a_phrase_with_a_name_keeps_it_and_says_where_it_was_used():
    reqs = [{"n": 1, "index": 0, "raw": "meteo a # città: Roma # domani", "text": "meteo a Roma domani", "ts": None}]
    steps = [{"turn": 1, "kind": "tool", "name": "browse_url", "arguments": {"url": "https://meteo.example/roma"}, "result": "sun", "ok": True}]
    (c,) = distill.candidates(reqs, steps)
    assert (c["value"], c["name_hint"], c["source"]) == ("Roma", "città", "mark") and c["used_in"] == [{"tool": "browse_url", "argument": "url"}]


class Model:
    def __init__(self, *answers):
        self.answers, self.sent = list(answers), []

    def chat_completion(self, messages, **kwargs):
        self.sent.append(messages)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {"choices": [{"message": {"role": "assistant", "content": answer}}]}


PROPOSAL = json.dumps({"name": "Rassegna", "description": "Collects the news of a day", "params": [
    {"name": "Argomento", "label": "Topic", "explanation": "what to look for", "type": "text", "value_seen": "la guerra in Ucraina"},
    {"name": "giorno", "label": "Day", "explanation": "which day", "type": "date", "value_seen": "2026-10-26", "default": "oggi"},
    {"name": "invented", "label": "x", "type": "text", "value_seen": "something nobody wrote"},
    {"name": "bad type", "type": "color"}, {"name": "dup"}, {"name": "dup"}, "not an object"]})


def test_the_model_names_the_parameters_and_what_it_invented_is_marked():
    reqs = distill.requests_of(MESSAGES)
    steps = distill.steps_of(MESSAGES, reqs, [1])
    model = Model("```json\n" + PROPOSAL + "\n```")
    got = distill.propose(model, reqs, steps)
    assert got["name"] == "Rassegna" and [p["name"] for p in got["params"]] == ["argomento", "giorno", "invented", "bad_type", "dup"]
    assert [p["unverified"] for p in got["params"]] == [False, False, True, False, False]
    assert got["params"][1]["default"] == "oggi" and got["params"][3]["type"] == "text"
    prompt = model.sent[0][1]["content"]
    assert "# di oggi #" in prompt and "'di oggi' (mark" in prompt and "search_web" in prompt                  # it sees what the person wrote, and what was found


def test_if_the_model_cannot_answer_the_candidates_are_offered_as_they_are():
    reqs = distill.requests_of(MESSAGES)
    got = distill.propose(Model(RuntimeError("down")), reqs, distill.steps_of(MESSAGES, reqs, [1]))
    assert "could not name" in got["note"] and [p["value_seen"] for p in got["params"]][:2] == ["di oggi", "la guerra in Ucraina"]
    assert all(p["name"] for p in got["params"])


def test_the_script_is_asked_with_the_calls_and_the_parameters_wired_in():
    reqs = distill.requests_of(MESSAGES)
    steps = distill.steps_of(MESSAGES, reqs, [1])
    params = [{"name": "topic", "type": "text", "explanation": "what", "value_seen": "guerra in Ucraina"}, {"name": "day", "type": "date", "value_seen": "2026-10-26"}]
    good = "```python\nfrom tadamoo_skill import tool\n\ndef main(params):\n    return tool('search_web', query=f'{params.topic} notizie') + str(params.day)\n```"
    model = Model(good)
    got = distill.write_script(model, "Rassegna", "news", params, reqs, steps)
    assert got["problems"] == [] and got["unused"] == [] and check(got["script"]) == []
    prompt = model.sent[0][1]["content"]
    assert '"query": "<<params.topic>> notizie"' in prompt and "notizie/report-<<params.day>>.md" in prompt
    assert "FAILED and was not part of the way" in prompt and "rm /nope" not in prompt.split("FAILED")[0].split("5.")[-1]


def test_a_script_that_breaks_the_rules_is_sent_back_with_what_is_wrong():
    bad = "```python\nimport os\n\ndef run(params):\n    return os.getcwd()\n```"
    fixed = "```python\ndef main(params):\n    return params.topic\n```"
    model = Model(bad, fixed)
    reqs = distill.requests_of(MESSAGES)
    got = distill.write_script(model, "x", "", [{"name": "topic", "type": "text"}, {"name": "unused", "type": "text"}], reqs, distill.steps_of(MESSAGES, reqs, [1]))
    assert got["problems"] == [] and got["unused"] == ["unused"] and "def main" in got["script"]
    assert "function main" in model.sent[1][-1]["content"] and "line 1" in model.sent[1][-1]["content"]
    giving_up = Model(bad, bad, bad)
    got = distill.write_script(giving_up, "x", "", [], reqs, distill.steps_of(MESSAGES, reqs, [1]))
    assert got["problems"] and len(giving_up.sent) == 3                                  # two tries to repair, then what is left is shown to the person


def test_code_is_taken_from_the_fence_or_from_the_whole_answer():
    assert distill.extract_code("Here:\n```python\nx = 1\n```\nbye") == "x = 1\n"
    assert distill.extract_code("<think>hmm</think>def main(params): pass") == "def main(params): pass\n"


# ---- what a conversation keeps of the marks -------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_clock(monkeypatch):
    monkeypatch.setattr("tadamoo.stack.local_now", None)


def test_the_model_is_given_the_request_without_the_marks_and_the_history_keeps_what_was_written(tmp_path):
    client = Scripted(say("ok"))
    session, ev, store, built = make(tmp_path, client)
    text = "trova le notizie # di oggi # riguardanti # la guerra in Ucraina #"
    session.send("m1", text)
    ev.wait("done")
    assert client.sent[0][1]["content"] == "trova le notizie di oggi riguardanti la guerra in Ucraina"
    assert all("raw" not in m for m in client.sent[0])                                    # never sent to the server
    message = built.agent.messages[1]
    assert message["content"].startswith("trova le notizie di oggi") and message["raw"] == text
    saved = json.loads((store.dir / "history.json").read_text(encoding="utf-8"))["messages"][0]
    assert saved["raw"] == text


def test_a_message_without_marks_is_kept_as_it_is(tmp_path):
    client = Scripted(say("ok"))
    session, ev, store, built = make(tmp_path, client)
    session.send("m1", "see issue #12 and # a title")
    ev.wait("done")
    assert client.sent[0][1]["content"] == "see issue #12 and # a title" and "raw" not in built.agent.messages[1]
