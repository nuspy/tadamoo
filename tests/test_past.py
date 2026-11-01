import pytest

from harness.past import CHUNK_CHARS, PER_SESSION, PastIndex, pieces, text_of


def convo(*lines):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": text} for i, text in enumerate(lines)]


def make(tmp_path, **kw):
    return PastIndex(tmp_path / "past.db", **kw)


def test_text_is_cut_at_paragraph_ends_and_never_past_the_size():
    text = "\n\n".join(["alpha " * 40] * 12)
    cut = pieces(text, 700)
    assert len(cut) > 1 and all(len(p) <= 700 for p in cut)
    assert "".join(cut).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")
    assert [len(p) for p in pieces("x" * 2500, 1000)] == [1000, 1000, 500]
    assert pieces("   ") == []


def test_only_the_words_of_the_person_and_the_assistant_are_kept():
    parts = [{"type": "text", "text": "see this"}, {"type": "image_url", "image_url": {"url": "data:..."}}, {"type": "text", "text": "and this"}]
    assert text_of({"content": parts}) == "see this\nand this" and text_of({"content": None}) == ""


def test_a_question_finds_the_conversation_two_weeks_ago(tmp_path):
    past = make(tmp_path)
    past.add_session("s1", convo("Quali eventi dell'Unione Europea ci sono a novembre?", "Il vertice sul clima di Bruxelles, il 14 novembre."),
                     title="Eventi UE", project="", kind="chat", at=1000.0)
    past.add_session("s2", convo("How do I build sm_120 kernels?", "Use nvcc with the sm_120 target."), title="Build", project="E:/basalt", kind="agent", at=2000.0)
    hit = past.search("vertice di Bruxelles")[0]
    assert hit["session"] == "s1" and hit["turn"] == 1 and hit["kind"] == "chat" and hit["title"] == "Eventi UE" and "Bruxelles" in hit["snippet"]
    assert past.search("sm_120")[0]["session"] == "s2"
    assert past.search("unione europea")[0]["session"] == "s1"                  # accents and case do not matter


def test_a_word_that_is_in_no_conversation_finds_nothing_and_a_blank_query_too(tmp_path):
    past = make(tmp_path)
    past.add_session("s1", convo("hello there", "general kenobi"))
    assert past.search("zeppelin") == [] and past.search("   ") == [] and past.search('"') == []


def test_a_search_can_be_narrowed_by_date_project_kind_and_routine(tmp_path):
    past = make(tmp_path)
    past.add_session("old", convo("budget plan", "ok"), project="A", kind="chat", at=100.0)
    past.add_session("new", convo("budget plan", "ok"), project="B", kind="agent", at=200.0)
    past.add_session("run", convo("budget plan", "ok"), project="B", kind="routine", routine="weekly", at=300.0)
    assert {h["session"] for h in past.search("budget")} == {"old", "new", "run"}
    assert {h["session"] for h in past.search("budget", since=150.0)} == {"new", "run"}
    assert [h["session"] for h in past.search("budget", project="A")] == ["old"]
    assert [h["session"] for h in past.search("budget", kind="agent")] == ["new"]
    assert [h["session"] for h in past.search("budget", routine="weekly")] == ["run"]


def test_one_conversation_does_not_fill_the_whole_answer(tmp_path):
    past = make(tmp_path)
    past.add_session("big", convo(*(["tariffs again"] * 10)))
    past.add_session("other", convo("tariffs", "yes"))
    sessions = [h["session"] for h in past.search("tariffs", limit=10)]
    assert sessions.count("big") == PER_SESSION and "other" in sessions


def test_indexing_a_conversation_again_replaces_it(tmp_path):
    past = make(tmp_path)
    past.add_session("s", convo("first topic: owls", "yes"))
    past.add_session("s", convo("second topic: bats", "yes"))
    assert past.search("owls") == [] and past.search("bats")[0]["session"] == "s"
    assert past.drop("s") > 0 and past.count() == 0 and past.search("bats") == []


def test_tool_results_and_system_messages_are_not_indexed(tmp_path):
    past = make(tmp_path)
    past.add_session("s", [{"role": "system", "content": "secretword"}, {"role": "user", "content": "hi"}, {"role": "tool", "content": "toolword"},
                           {"role": "assistant", "content": "hello"}])
    assert past.search("secretword") == [] and past.search("toolword") == [] and past.count() == 2


def test_the_redaction_runs_before_anything_is_stored(tmp_path):
    past = make(tmp_path, redact=lambda t: t.replace("hunter2", "[REDACTED]"))
    past.add_session("s", convo("my password is hunter2", "noted"))
    assert past.search("hunter2") == [] and past.open("s")["messages"][0]["text"] == "my password is [REDACTED]"


def test_a_hit_is_opened_as_it_was_said_by_turn(tmp_path):
    past = make(tmp_path)
    past.add_session("s", convo("one", "two", "three", "four", "five", "six"), title="T", project="P", at=5.0)
    got = past.open("s", 2, 2)
    assert [m["text"] for m in got["messages"]] == ["three", "four"] and got["title"] == "T" and got["project"] == "P" and not got["more"]
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
    assert past.open("nope")["messages"] == []


def test_a_long_reading_is_cut_and_says_so(tmp_path):
    past = make(tmp_path)
    past.add_session("s", convo(*(["word " * 300] * 6)))
    got = past.open("s", max_chars=4000)
    assert got["more"] and 0 < len(got["messages"]) < 6


def test_a_long_message_is_kept_in_several_pieces_and_stays_one_turn(tmp_path):
    past = make(tmp_path)
    past.add_session("s", convo("start " + "\n\n".join(["lorem ipsum dolor " * 80] * 8), "ok"))
    assert past.count() > 2 and {m["turn"] for m in past.open("s", max_chars=10 ** 6)["messages"]} == {1}
    assert all(len(m["text"]) <= CHUNK_CHARS for m in past.open("s", max_chars=10 ** 6)["messages"])


def test_the_list_of_conversations_is_newest_first(tmp_path):
    past = make(tmp_path)
    past.add_session("a", convo("x", "y"), kind="chat", at=1.0)
    past.add_session("b", convo("x", "y", "z", "w"), kind="routine", routine="r", at=2.0)
    got = past.sessions()
    assert [s["session"] for s in got] == ["b", "a"] and got[0]["turns"] == 2 and got[0]["routine"] == "r"


def test_a_kind_that_does_not_exist_is_refused(tmp_path):
    with pytest.raises(ValueError, match="kind"):
        make(tmp_path).add_session("s", convo("a", "b"), kind="dream")
