"""The technical session's look at whether the request was followed as named."""
from types import SimpleNamespace

from tadamoo import review, side_uses


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
    note = review.make_review(Judge('no: "use mteb" was not followed: sentence-transformers was used instead'))("use mteb", "done", "shell_command: python st.py")
    assert note.startswith('[harness] Compared with the request: "use mteb" was not followed') and "keep your result" in note
    judge = Judge("yes")
    assert review.make_review(judge)("use mteb", "done", "shell_command: python mteb.py") == ""
    assert "use mteb" in judge.sent[0][1]["content"] and "mteb.py" in judge.sent[0][1]["content"]
    assert review.make_review(Judge("no: the wrong commit was merged"))("merge my changes into master", "done", "shell_command: git merge x") == ""       # a no that quotes nothing is a judgment of the result
    assert review.make_review(Judge('no: "use the newest commit" was not followed'))("merge my changes into master", "done", "") == ""         # words the request does not hold
    assert review.make_review(Judge("no: “MERGE my   changes” was not followed"))("merge my changes into master", "done", "").startswith("[harness] Compared")
    assert review.make_review(None)("x", "y", "") is None
    monkeypatch.setattr(review, "side_extra_for", lambda *a, **k: {})
    assert review.make_review(Judge('no: "x y z" was not followed'))("x y z", "y", "") is None
    side_uses.save("request_review", on=False)
    monkeypatch.setattr(review, "side_extra_for", lambda *a, **k: {"cache_slot": 1})
    assert review.make_review(Judge('no: "x y z" was not followed'))("x y z", "y", "") is None
