"""Optimize prompt: the guards, the check of a rewrite, the one short call on the technical session, and the session around it (what the person
sees, what the model reads, what is stored). CPU only, scripted models."""
import copy
import json
import threading
import time

import pytest

from tadamoo import intake as it
from tadamoo import optimize as opt
from tadamoo import questions as qs
from tadamoo.session import Session, SessionStore, parse_args
from tadamoo.stack import AgentConfig, build_agent

ASK = "Please write a short script that reads data.csv and prints how many rows it has."
BETTER = "Write a short Python script that reads the file data.csv, counts its rows and prints the number of rows."


def say(text, finish="stop"):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}]}


class Model:
    """Plays the technical session when it gets the fixed instruction, and the conversation's model otherwise; keeps every request of each."""

    def __init__(self, rewrites=(), answers=(), side=30000, stays=True):
        self.rewrites, self.answers, self.side, self.stays = list(rewrites), list(answers), side, stays
        self.short, self.main = [], []
        self.lock = threading.Lock()

    def side_context(self):
        return self.side

    def stays_local(self):
        return self.stays

    def chat_completion(self, messages, tools=None, **kw):
        with self.lock:
            is_short = messages[0]["content"] == opt.SYSTEM_PROMPT
            (self.short if is_short else self.main).append({"messages": copy.deepcopy(messages), "tools": tools, **kw})
            step = (self.rewrites if is_short else self.answers).pop(0)
        if callable(step):
            step = step()
        if isinstance(step, BaseException):
            raise step
        return step if isinstance(step, dict) else say(step)


# -- what must survive ----------------------------------------------------------------------------------------------------------

RICH = (r'Apri https://example.com/a_b?x=1 e C:\dati\file_2.csv, poi scrivi "ciao mondo" a @marco entro 3 giorni e correggi `getUserName()` in main.py.'
        "\n```py\nx = 1\n```")


def test_every_kind_of_protected_content_is_found():
    kinds = {(k, v) for k, v in opt.protected(RICH)}
    assert ("link", "https://example.com/a_b?x=1") in kinds and ("path", r"C:\dati\file_2.csv") in kinds and ("file", "main.py") in kinds
    assert ("quote", "ciao mondo") in kinds and ("mention", "@marco") in kinds and ("number", "3") in kinds
    assert ("code", "getUserName()") in kinds and ("code", "```py\nx = 1\n```") in kinds
    assert opt.protected("and/or he said/she said 1/2") == [("number", "1"), ("number", "2")]              # a slash inside words is not a path
    assert opt.protected("mail a.b@c.org now") == [("address", "a.b@c.org")]                                 # an address is not a mention
    assert opt.protected("Copy src/app/main.txt to build/out/ok.txt, not he/she/it") == [("path", "src/app/main.txt"), ("path", "build/out/ok.txt")]
    assert opt.protected(r"see docs\guide\intro.md and a/b/c/d") == [("path", r"docs\guide\intro.md"), ("path", "a/b/c/d")]


def test_a_rewrite_that_keeps_all_of_it_passes_and_one_that_drops_any_is_named():
    keep = "Per favore: " + RICH
    assert opt.lost(RICH, keep) is None
    for kind, broken in (("link", RICH.replace("https://example.com/a_b?x=1", "il link")), ("path", RICH.replace(r"C:\dati\file_2.csv", "il file")),
                         ("quote", RICH.replace('"ciao mondo"', '"ciao"')), ("mention", RICH.replace("@marco", "Marco")), ("number", RICH.replace("3 giorni", "tre giorni")),
                         ("code", RICH.replace("x = 1", "x = 2")), ("file", RICH.replace("main.py", "il file principale"))):
        assert opt.lost(RICH, broken)[0] == kind, kind


def test_numbers_are_whole_tokens_and_quotes_ignore_spacing():
    assert opt.lost("Use port 8080 and \"two   words\" please", "Use port 8080 and \"two words\" please") is None
    assert opt.lost("Use port 80 now please", "Use port 8080 now please")[0] == "number"
    assert opt.lost("Version 3.14 is the one", "Version 3.14. is the one") is None


def test_the_length_the_opener_and_the_leaks_are_judged():
    assert opt.judge(ASK, BETTER) == ("", "")
    assert opt.judge(ASK, "Write a script.")[0] == "ratio"                                    # under half
    assert opt.judge("fix the parser bug now", "x" * 400)[0] == "ratio"                          # over four times
    for answer in ("Sure, here is the script you asked for.", "Certamente! Ecco lo script.", "I'll write that script now and print the rows.", "Ecco il testo riscritto"):
        assert opt.judge("scrivi uno script che legge data.csv e conta le righe", answer + " " * 5 + "legge data.csv e conta le righe righe righe")[0] == "answer", answer
    assert opt.judge("Sure, write a poem about the sea please", "Sure, write a poem about the sea, please, and keep it short")[0] == ""      # the person began that way
    assert opt.judge(ASK, "Rewrite the request as a clear, complete, self-contained instruction: " + BETTER)[0] == "leak"
    assert opt.judge(ASK, "<message>" + BETTER)[0] == "leak"
    assert opt.judge(ASK, "   ")[0] == "empty"
    assert opt.judge(ASK, BETTER.replace("data.csv", "the file"))[0] == "lost"


def test_a_wrapper_the_instruction_forbade_is_taken_off():
    assert opt.tidy('"' + BETTER + '"') == BETTER and opt.tidy("\u00ab" + BETTER + "\u00bb") == BETTER
    assert opt.tidy("```\n" + BETTER + "\n```") == BETTER
    assert opt.tidy('"a" and "b"') == '"a" and "b"'                                           # quotes inside are the text's own
    assert opt.tidy("```py\nx\n```\nand more ```y```").startswith("```py")


# -- the guards ---------------------------------------------------------------------------------------------------------------

def test_the_guards_leave_these_messages_alone():
    cases = {"/status now please sir": "command", "Target Loop \u00b7 task 1 of 2: do the thing now": "command", "[harness] You were set aside for an urgent request": "command",
             "[planner] the plan changed in the meantime": "command", qs.ANSWER_HEADER + "\n1. Which? -> build\n": "answer", "ok": "short", "fix the bug": "short",
             "   ": "empty", "Write the story of the dragon [long answer: mode=write; words=5000]": "long", "word " * 1000: "big"}
    for text, why in cases.items():
        assert opt.skip_reason(text) == why, text
    assert opt.skip_reason(ASK, origin="planner") == "origin" and opt.skip_reason(ASK, attended=False) == "unattended"
    assert opt.skip_reason(ASK) == "" and opt.skip_reason("uno due tre quattro") == ""


def test_the_previous_message_is_the_persons_own_words_and_nothing_else():
    messages = [{"role": "system", "content": "s"},
                {"role": "user", "content": "Cleaned old request", "raw": "Old request as typed  \r\n"},
                {"role": "user", "content": "[harness] Notes", "note": True},
                {"role": "assistant", "content": "Done."},
                {"role": "user", "content": qs.ANSWER_HEADER + "\n1. a -> b"},
                {"role": "user", "content": "<untrusted_data source=\"memory\">x</untrusted_data>"},
                {"role": "user", "content": "<documents>\n<document index=\"1\"><source>a.txt</source></document>\n</documents>\n\nAnd this one"}]
    assert opt.previous_words(messages, len(messages)) == "And this one"
    assert opt.previous_words(messages, 6) == "Old request as typed"
    assert opt.previous_words(messages, 1) == ""


# -- the one call -------------------------------------------------------------------------------------------------------------

def test_the_call_is_one_short_request_on_the_technical_session():
    model = Model([BETTER])
    got = opt.Optimizer(model, True).run(ASK, previous="Earlier I asked for a CSV.")
    assert got.state == "sent" and got.text == BETTER
    (call,) = model.short
    assert call["tools"] is None and call["temperature"] == 0.0 and call["timeout"] == 8.0
    assert call["extra"] == {"reasoning_effort": "none", "cache_slot": "side", "side_use": "optimize"}
    assert call["messages"][0] == {"role": "system", "content": opt.SYSTEM_PROMPT}
    body = call["messages"][1]["content"]
    assert body == "<previous>\nEarlier I asked for a CSV.\n</previous>\n<message>\n" + ASK + "\n</message>"
    assert opt.prompt_for(ASK)[1]["content"] == "<message>\n" + ASK + "\n</message>"
    assert 128 <= call["max_tokens"] <= 2000


def test_the_system_instruction_says_what_the_owner_asked():
    for words in ("clear, complete, self-contained", "same language", "every fact, name, number, path, link, code block, quotation and constraint",
                  "Do not add requirements", "Do not answer the request", "Output only the rewritten text"):
        assert words in opt.SYSTEM_PROMPT


def test_the_output_is_capped_at_about_one_and_a_half_times_the_input_and_never_over_2000_tokens():
    assert opt.output_cap("a b c d") == 128                                                    # a floor, so a short request has room to be completed
    small, big = opt.output_cap("x" * 600), opt.output_cap("x" * 3900)
    assert small == int(1.5 * 200) and big == 1950 and opt.output_cap("x" * 40000) == 2000
    assert opt.MAX_INPUT_CHARS == 4000 and opt.skip_reason("w " * 2001) == "big"


def test_without_a_technical_session_or_with_the_option_off_or_on_another_route_there_is_no_call():
    for model, on in ((Model(side=0), True), (Model(), False), (Model(stays=False), True), (None, True)):
        got = opt.Optimizer(model, on).run(ASK)
        assert got.state == "skipped" and (model is None or model.short == [])
    assert opt.Optimizer(Model([BETTER]), True).run("ok").reason == "short"


def test_ready_says_in_a_word_why_no_call_will_be_made():
    assert opt.Optimizer(Model(), True).ready(ASK) == "" and opt.Optimizer(Model(), False).ready(ASK) == "off"
    assert opt.Optimizer(Model(side=0), True).ready(ASK) == "no_session" and opt.Optimizer(Model(stays=False), True).ready(ASK) == "route"
    assert opt.Optimizer(Model(), True).ready("ok") == "short" and opt.Optimizer(Model(), True).ready(ASK, attended=False) == "unattended"
    assert opt.Optimizer(None, True).ready(ASK) == "off"


def test_a_failed_slow_or_unchanged_call_sends_the_original_without_a_word():
    for step in (RuntimeError("down"), TimeoutError("slow"), {"nothing": 1}, say(None)):
        got = opt.Optimizer(Model([step]), True).run(ASK)
        assert got.state == "skipped" and got.text == "", step
    assert opt.Optimizer(Model([" " + ASK + "\n"]), True).run(ASK).reason == "same"


def test_a_reply_that_was_cut_a_lost_item_an_answer_and_a_bad_size_are_rejected_with_the_reason():
    model = Model([say(BETTER, "length"), BETTER.replace("data.csv", "the table"), "Sure, here is the script: " + BETTER, "Script."])
    got = [opt.Optimizer(model, True).run(ASK) for _ in range(4)]
    assert [(g.state, g.reason) for g in got] == [("rejected", "cut"), ("rejected", "lost"), ("rejected", "answer"), ("rejected", "ratio")]
    assert got[1].detail == "file: data.csv" and all(g.text == "" for g in got)


# -- the session around it -----------------------------------------------------------------------------------------------------

class Events:
    def __init__(self):
        self.items, self.cond = [], threading.Condition()

    def __call__(self, event):
        with self.cond:
            self.items.append(event)
            self.cond.notify_all()

    def wait(self, kind, timeout=10.0, after=0):
        end = time.monotonic() + timeout
        with self.cond:
            while True:
                for e in self.items[after:]:
                    if e["event"] == kind:
                        return e
                left = end - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"no {kind} event; got {[e['event'] for e in self.items]}")
                self.cond.wait(left)


@pytest.fixture(autouse=True)
def _no_clock(monkeypatch):
    monkeypatch.setattr("tadamoo.stack.local_now", None)


def session_with(tmp_path, model, on=True, mode="off", unattended=False):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False, max_turns=6), client=model)
    events = Events()
    session = Session(built, SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets), events, session_id="s1", sleep=lambda s: None, unattended=unattended)
    session.intake = it.Intake(mode, model)
    session.optimizer = opt.Optimizer(model, on)
    return session, events, built


def users(request):
    return [m for m in request["messages"] if m["role"] == "user"]


def test_the_model_reads_the_rewrite_and_the_person_keeps_their_words_in_the_history(tmp_path):
    model = Model([BETTER], ["Done."])
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", ASK)
    assert ev.wait("done")["final"] == "Done."
    sent = ev.wait("optimized")
    assert sent["state"] == "sent" and sent["text"] == BETTER and sent["original"] == ASK
    assert users(model.main[0])[-1]["content"] == BETTER and "raw" not in users(model.main[0])[-1] and "opt" not in users(model.main[0])[-1]
    message = next(m for m in built.agent.messages if m.get("role") == "user" and m.get("opt"))
    assert message["content"] == BETTER and message["raw"] == ASK and message["opt"] == {"state": "sent"}
    assert built.agent.last_user_text == ASK                                                        # what a quote or a check is made against
    saved = json.loads((tmp_path / "hist" / "history.json").read_text(encoding="utf-8"))["messages"]
    stored = next(m for m in saved if m.get("opt"))
    assert (stored["content"], stored["raw"]) == (BETTER, ASK)


def test_the_side_session_is_asked_only_by_this_call_and_the_model_call_is_untouched(tmp_path):
    model = Model([BETTER], ["Done."])
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", ASK)
    ev.wait("done")
    assert model.short[0]["extra"]["cache_slot"] == "side"
    assert "cache_slot" not in (model.main[0].get("extra") or {})


def test_a_later_turn_keeps_the_rewrite_byte_for_byte_and_makes_no_new_call_for_it(tmp_path):
    model = Model([BETTER, "Also add a header row to the result file, please."], ["One.", "Two."])
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", ASK)
    ev.wait("done")
    n = len(ev.items)
    session.send("m2", "Now also add a header row to the result file.")
    ev.wait("done", after=n)
    first, second = users(model.main[0]), users(model.main[1])
    assert first[-1]["content"] == BETTER and second[0]["content"] == BETTER                       # the prefix the cache holds is the same bytes
    assert len(model.short) == 2
    assert model.short[1]["messages"][1]["content"].startswith("<previous>\n" + ASK + "\n</previous>")      # the person's words, not the rewrite


def test_the_page_is_told_the_prompt_is_being_rewritten_and_when_that_ends(tmp_path):
    model = Model([BETTER], ["Done."])
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", ASK)
    ev.wait("done")
    names = [e["event"] for e in ev.items]
    assert names.index("optimizing") < names.index("optimized") < names.index("done") and ev.wait("optimizing")["turn"] == "m1"
    failing = Model([RuntimeError("down")], ["Done."])
    session2, ev2, built2 = session_with(tmp_path / "failing", failing)
    session2.send("m1", ASK)
    ev2.wait("done")
    ended = ev2.wait("optimized")
    assert (ended["state"], ended["reason"]) == ("skipped", "failed") and "text" not in ended          # the line on the page is closed even when nothing came of it
    short = Model([BETTER], ["Done."])
    session3, ev3, built3 = session_with(tmp_path / "short", short)
    session3.send("m1", "ok")
    ev3.wait("done")
    assert not [e for e in ev3.items if e["event"] in ("optimizing", "optimized")]                      # nothing is shown for a message that is left alone


def test_stop_during_the_rewrite_gives_it_up_sends_the_original_and_does_not_stop_the_task(tmp_path):
    gate = threading.Event()
    model = Model([lambda: (gate.wait(10), say(BETTER))[1]], ["Done."])
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", ASK)
    ev.wait("optimizing")
    session.cancel()
    try:
        assert ev.wait("done")["final"] == "Done."
        stopped = ev.wait("optimized")
        assert (stopped["state"], stopped["reason"]) == ("skipped", "stopped")
        assert users(model.main[0])[-1]["content"] == ASK and not any(m.get("opt") for m in built.agent.messages)
    finally:
        gate.set()


def test_a_rejected_rewrite_sends_the_original_and_says_why(tmp_path):
    model = Model([BETTER.replace("data.csv", "the table")], ["Done."])
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", ASK)
    ev.wait("done")
    note = ev.wait("optimized")
    assert (note["state"], note["reason"], note["detail"]) == ("rejected", "lost", "file: data.csv") and "text" not in note
    assert users(model.main[0])[-1]["content"] == ASK
    message = next(m for m in built.agent.messages if m.get("opt"))
    assert message["content"] == ASK and message["opt"]["reason"] == "lost"


def test_nothing_is_said_when_the_call_fails_and_the_original_goes(tmp_path):
    model = Model([TimeoutError("8 s")], ["Done."])
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", ASK)
    ev.wait("done")
    assert [e["state"] for e in ev.items if e["event"] == "optimized"] == ["skipped"]                  # it only closes the "rewriting" line; the page shows no note
    assert users(model.main[0])[-1]["content"] == ASK


@pytest.mark.parametrize("text", ["/status please show me", "Target Loop \u00b7 task 1 of 2: " + ASK, "ok", "fix it", qs.ANSWER_HEADER + "\n1. Which? -> build\n" + qs.GO_ON])
def test_commands_short_messages_cards_and_target_loop_are_not_rewritten(tmp_path, text):
    model = Model([BETTER], ["Done."])
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", text)
    ev.wait("done")
    assert model.short == [] and users(model.main[0])[-1]["content"] == text


def test_off_unattended_and_send_my_original_instead_make_no_call(tmp_path):
    for n, kw in enumerate(({"on": False}, {"unattended": True})):
        model = Model([BETTER], ["Done."])
        session, ev, built = session_with(tmp_path / f"case{n}", model, **kw)
        session.send("m1", ASK)
        ev.wait("done")
        assert model.short == [] and users(model.main[0])[-1]["content"] == ASK
    model = Model([BETTER], ["Done."])
    session, ev, built = session_with(tmp_path / "again", model)
    session.send("m1", ASK, optimize=False)
    ev.wait("done")
    assert model.short == [] and users(model.main[0])[-1]["content"] == ASK and not any(m.get("opt") for m in built.agent.messages)


def test_the_command_turns_it_on_and_off_for_the_next_message(tmp_path):
    model = Model([BETTER], ["One.", "Two."])
    session, ev, built = session_with(tmp_path, model, on=False)
    session.send("m1", ASK)
    ev.wait("done")
    n = len(ev.items)
    session.optimizer.enabled = True
    session.send("m2", ASK + " Thanks.")
    ev.wait("done", after=n)
    assert len(model.short) == 1


def test_it_works_with_the_prompt_analysis_off_and_the_triage_reads_the_original(tmp_path):
    triage = json.dumps({"evidence": [], "intent": "create", "size": "small", "ambiguity": "none", "missing_info": [], "risks": [], "needs_plan": False,
                         "stated_constraints": [], "subtasks": []})
    long_ask = ASK + " Then write the result to out.json and send a copy to the team by email when the job is done, please."
    model = Model(["Write a script that reads data.csv, counts its rows, writes the number to out.json and emails a copy to the team when done."],
                  [triage, "Done."])
    session, ev, built = session_with(tmp_path, model, mode="full")
    session.send("m1", long_ask)
    ev.wait("done")
    assert len(model.short) == 1 and long_ask[:40] in model.main[0]["messages"][1]["content"]        # the triage form is made from the person's words
    assert any(m["content"].startswith("Write a script that reads data.csv") for m in users(model.main[1]))


def test_the_option_and_its_command_are_process_arguments(tmp_path):
    assert parse_args([]).optimize_prompt == "off" and parse_args(["--optimize-prompt", "on"]).optimize_prompt == "on"


def test_the_short_call_asks_for_the_session_like_the_others():
    quick = Model(["Write a script that reads data.csv and counts the rows in it."])
    assert opt.Optimizer(quick, True).run("write a script that reads data.csv please").state == "sent"
    assert quick.short[0]["extra"].get("cache_slot") == "side" and quick.short[0]["extra"].get("reasoning_effort") == "none"
