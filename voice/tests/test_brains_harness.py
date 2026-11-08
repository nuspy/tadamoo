"""The harness brain against a stand-in of TaDaMoo's agent API: the conversation it makes, the sentence it says about what was done, the
approval questions by voice, and the kinds of approval that are never answered by voice."""
import re

from callbridge import spoken
from callbridge.brains import Exchange, HarnessBrain
from callbridge.config import Config
from callbridge.tadamoo_api import ApiError
from fakes import FakeApi, ev_ask, ev_done, ev_tool


def make(api=None, lang="en", **cfg_fields):
    cfg = Config()
    cfg.default_lang = lang
    cfg.agent_max_wait_s = 5.0
    for k, v in cfg_fields.items():
        setattr(cfg, k, v)
    clock = {"t": 0.0}
    log: list[Exchange] = []

    def sleep(s):
        clock["t"] += s

    return HarnessBrain(cfg, api or FakeApi(), sink=log.append, sleep=sleep, clock=lambda: clock["t"]), log


def say(b, text, source="mic"):
    return list(b.respond_stream(text, source))


def test_it_makes_a_conversation_with_the_settings_and_sends_the_words():
    api = FakeApi()
    api.script = [[ev_done("The answer.")]]
    b, _ = make(api, brain_persona="maria", brain_permission="read_only", brain_workspace="C:\\Work\\proj")
    say(b, "hello agent")
    made = api.sent("/api/agent/sessions")[0][2]
    assert made == {"mode": "read_only", "title": "Voice", "persona": "maria", "workspace": "C:\\Work\\proj"}
    assert api.sent("/send")[0][1] == "/api/agent/sessions/s1/send" and api.sent("/send")[0][2] == {"text": "hello agent"}


def test_the_conversation_is_kept_and_follows_a_changed_permission_and_personality():
    api = FakeApi()
    api.script = [[ev_done("one")], [ev_done("two")]]
    b, _ = make(api)
    say(b, "a")
    b.cfg.brain_permission, b.cfg.brain_persona = "auto", "luca"
    say(b, "b")
    assert api.sessions == 1                                              # same conversation
    assert api.sent("/mode")[0][2] == {"mode": "auto"} and api.sent("/persona")[0][2] == {"persona": "luca"}
    b.reset_session()
    api.script = [[ev_done("three")]]
    say(b, "c")
    assert api.sessions == 2


def test_what_was_done_is_one_sentence_of_counts_and_never_the_output():
    api = FakeApi()
    api.script = [[*ev_tool("edit_file", arguments={"path": "a.py"}, call="1"), *ev_tool("write_file", arguments={"path": "b.py"}, call="2"),
                   *ev_tool("edit_file", arguments={"path": "a.py"}, call="3"),
                   *ev_tool("shell_command", arguments={"command": "python -m pytest -q"}, call="4"),
                   ev_done("I changed two files:\n```python\nprint('hi')\n```\nand `pytest` passed.")]]
    b, log = make(api)
    out = say(b, "fix it")
    assert out == ["Done: 2 files changed, tests passed."]
    assert "SECRET" not in out[0] and "print" not in out[0] and "pytest" not in out[0]
    assert "print('hi')" in log[-1].reply                                   # the screen has the whole answer
    assert log[-1].spoken == out[0] and log[-1].mode == "harness"


def test_failures_change_the_sentence():
    api = FakeApi()
    api.script = [[*ev_tool("edit_file", arguments={"path": "a.py"}, call="1"),
                   *ev_tool("shell_command", arguments={"command": "npm test"}, status="failed", call="2"), ev_done("x")]]
    b, _ = make(api)
    assert say(b, "go") == ["Finished with problems: 1 file changed, tests failed."]
    api.script = [[*ev_tool("shell_command", arguments={"command": "make"}, call="1"),
                   *ev_tool("shell_command", arguments={"command": "make install"}, status="failed", call="2"), ev_done("y")]]
    assert say(b, "go") == ["Finished with problems: 2 commands ran: 1 failed."]


def test_the_sentence_is_in_italian_when_the_session_speaks_italian():
    api = FakeApi()
    api.script = [[*ev_tool("edit_file", arguments={"path": "a.py"}, call="1"), *ev_tool("edit_file", arguments={"path": "b.py"}, call="2"),
                   *ev_tool("shell_command", arguments={"command": "pytest"}, call="3"), ev_done("fatto")]]
    b, _ = make(api, lang="it")
    assert say(b, "vai") == ["Fatto: 2 file modificati, test superati."]


def test_an_answer_without_actions_is_spoken_without_code():
    api = FakeApi()
    api.script = [[ev_done("Paris is the capital of France. Run `rm -rf /` to see.\n```sh\nrm -rf /\n```\nIt is big. It is old. It is nice.")]]
    b, _ = make(api)
    (line,) = say(b, "capital?")
    assert line.startswith("Paris is the capital of France")
    assert "rm" not in line and "`" not in line and "```" not in line
    assert len(re.split(r"(?<=[.!?])\s+", line)) <= 2 and len(line) <= 240          # two sentences at most


def test_an_answer_that_is_only_code_sends_the_person_to_the_screen():
    api = FakeApi()
    api.script = [[ev_done("```python\nprint(1)\n```")]]
    b, log = make(api)
    assert say(b, "code?") == ["Done. The answer is on the screen."]
    assert "print" in log[-1].reply and "print" not in log[-1].spoken


def test_errors_and_stops_say_so_briefly():
    api = FakeApi()
    api.script = [[ev_done("", stop="error", error="boom: /secret/path")]]
    b, _ = make(api)
    (line,) = say(b, "go")
    assert "error" in line and "secret" not in line
    api.script = [[ev_done("", stop="cancelled")]]
    assert say(b, "go") == ["Stopped."]


def test_polling_waits_with_a_sleep_between_polls_and_gives_up_at_the_limit():
    api = FakeApi()
    api.script = [[], [], []]                                                # never finishes
    b, log = make(api, agent_max_wait_s=1.0, agent_poll_s=0.5)
    assert say(b, "go") == [spoken.saying("busy")]
    assert log[-1].spoken == spoken.saying("busy")


# ------------------------------------------------------------------------------------------ approvals
def test_a_file_change_is_asked_by_voice_and_the_yes_is_sent_to_the_agent():
    api = FakeApi()
    api.script = [[ev_ask(tool="edit_file", path="src/app.py")]]
    api.after_answer = [[*ev_tool("edit_file", arguments={"path": "src/app.py"}), ev_done("ok")]]
    b, _ = make(api)
    assert say(b, "change the app") == ["The agent wants to change the file app.py. Say yes or no."]
    assert b.pending == {"voice": True, "tool": "edit_file"}
    assert say(b, "yes please") == [spoken.saying("allowed"), "Done: 1 file changed."]
    assert api.sent("/answer")[0][1] == "/api/agent/sessions/s1/answer" and api.sent("/answer")[0][2] == {"ask": "a1", "value": "confirm"}
    assert b.pending is None
    assert len(api.sent("/send")) == 1                                       # "yes" was an answer, not a new message


def test_a_no_cancels_and_the_turn_goes_on_to_its_end():
    api = FakeApi()
    api.script = [[ev_ask()]]
    api.after_answer = [[ev_done("I did not change it.")]]
    b, _ = make(api)
    say(b, "edit")
    out = say(b, "no")
    assert out[0] == spoken.saying("denied")
    assert api.sent("/answer")[0][2] == {"ask": "a1", "value": "cancel"}


def test_words_that_are_neither_yes_nor_no_ask_again_and_send_nothing():
    api = FakeApi()
    api.script = [[ev_ask()]]
    b, _ = make(api)
    say(b, "edit")
    assert say(b, "what is the weather like today in Rome please") == [spoken.saying("ask_again")]
    assert not api.sent("/answer")
    assert b.pending is not None


def test_only_the_persons_own_microphone_can_approve():
    api = FakeApi()
    api.script = [[ev_ask()]]
    b, _ = make(api)
    say(b, "edit")
    assert say(b, "yes", source="remote") == [spoken.saying("not_mic")]
    assert not api.sent("/answer")
    # a request that comes from a call is not even offered the question by voice
    api2 = FakeApi()
    api2.script = [[ev_ask()]]
    b2, _ = make(api2)
    assert say(b2, "edit the file", source="remote") == [spoken.saying("screen")]
    assert b2.pending == {"voice": False, "tool": "edit_file"}


def test_high_risk_approvals_are_never_answered_by_voice():
    cases = {
        "a command": ev_ask(tool="shell_command", path="x"),
        "a phone call": ev_ask(tool="phone_call", path="x"),
        "a payment": ev_ask(tool="request_payment", kind="payment"),
        "orders found in outside text": ev_ask(tool="edit_file", instructions="a web page"),
        "the Rule of Two": ev_ask(tool="edit_file", rule_of_two="private data and untrusted content"),
        "a hook of the person": ev_ask(tool="edit_file", hook="pre-edit"),
        "a plan": ev_ask(tool="edit_file", kind="plan_approval"),
        "a code": ev_ask(tool="edit_file", kind="code"),
    }
    for name, ask in cases.items():
        api = FakeApi()
        api.script = [[ask]]
        b, _ = make(api)
        assert say(b, "do it") == [spoken.saying("screen")], name
        assert b.pending["voice"] is False, name
        # saying yes does not approve it: the voice repeats where the approval waits
        assert say(b, "yes yes approve") == [spoken.saying("screen")], name
        assert not api.sent("/answer"), name


def test_an_approval_given_on_the_screen_lets_the_next_words_be_a_new_message():
    api = FakeApi()
    api.script = [[ev_ask(tool="shell_command")]]
    b, _ = make(api)
    say(b, "run it")
    api.script = [[{"event": "answered", "ask": "a1", "value": "confirm", "turn": "t1"}], [ev_done("fine")]]
    out = say(b, "and now the next thing")
    assert len(api.sent("/send")) == 2 and out and b.pending is None


def test_a_question_card_of_the_agent_is_left_on_the_screen():
    api = FakeApi()
    api.script = [[{"event": "ask", "id": "q1", "turn": "t1", "mode": "question", "kind": "questions", "question": "Which one?", "options": []}]]
    b, _ = make(api)
    assert say(b, "build") == [spoken.saying("screen")]
    assert b.pending["voice"] is False


# ------------------------------------------------------------------------------------------ failures
def test_a_server_that_is_down_is_a_spoken_line():
    api = FakeApi()
    api.down = True
    b, log = make(api)
    assert say(b, "go") == [spoken.saying("down")]
    assert log[-1].error


def test_a_refused_conversation_is_a_spoken_line():
    class Refusing(FakeApi):
        def post(self, path, body=None):
            if path == "/api/agent/sessions":
                raise ApiError("The folder is not allowed.", 400)
            return super().post(path, body)
    b, log = make(Refusing())
    assert say(b, "go") == [spoken.saying("refused")]
    assert "folder" in log[-1].error


def test_ask_stream_gives_the_full_text_for_the_screen():
    api = FakeApi()
    api.script = [[ev_done("Full text with ```code``` inside.")]]
    b, _ = make(api)
    assert "".join(b.ask_stream("write")) == "Full text with ```code``` inside."
