"""The technical session's look at whether the request was followed as named."""
from types import SimpleNamespace

from harness import review, side_uses


class Judge:
    def __init__(self, word):
        self.word, self.sent = word, []

    def stays_local(self):
        return True

    def chat_completion(self, messages, **kw):
        self.sent.append(messages)
        return {"choices": [{"message": {"content": self.word}}]}


def call(name, **args):
    return SimpleNamespace(tool_name=name, arguments=args, is_error=False)


def test_the_calls_are_listed_short_and_a_long_list_keeps_its_start_and_end():
    one = review.call_lines([call("shell_command", command="pip   install\nmteb"), call("write_file", path="a.txt", content="x" * 5000)])
    assert one.splitlines() == ["shell_command: pip install mteb", "write_file: a.txt"]
    many = review.call_lines([call("shell_command", command=f"c{i}") for i in range(100)]).splitlines()
    assert len(many) == review.LINES_HEAD + 1 + review.LINES_TAIL and "40 calls left out" in many[review.LINES_HEAD] and many[0].endswith("c0") and many[-1].endswith("c99")


def test_a_no_becomes_the_note_a_yes_is_empty_and_no_session_or_a_switch_off_is_none(monkeypatch):
    monkeypatch.setattr(review, "side_extra_for", lambda *a, **k: {"cache_slot": 1})
    side_uses.save("request_review", on=True)
    note = review.make_review(Judge("no: sentence-transformers was used instead of mteb"))("use mteb", "done", "shell_command: python st.py")
    assert note.startswith("[harness] Compared with the request: sentence-transformers was used instead of mteb")
    judge = Judge("yes")
    assert review.make_review(judge)("use mteb", "done", "shell_command: python mteb.py") == ""
    assert "use mteb" in judge.sent[0][1]["content"] and "mteb.py" in judge.sent[0][1]["content"]
    assert review.make_review(None)("x", "y", "") is None
    monkeypatch.setattr(review, "side_extra_for", lambda *a, **k: {})
    assert review.make_review(Judge("no: x"))("x", "y", "") is None
    side_uses.save("request_review", on=False)
    monkeypatch.setattr(review, "side_extra_for", lambda *a, **k: {"cache_slot": 1})
    assert review.make_review(Judge("no: x"))("x", "y", "") is None
