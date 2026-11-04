"""The shared transcript: cuts that keep the beginning and the end, markers wherever something is left out, and the record of what was cut."""
import json

from tadamoo.memory_extract import BATCH_CHARS, MESSAGE_CHARS, Extractor, outcome_of, prompt_for, read_ops
from tadamoo.personal_memory import PersonalMemory
from tadamoo.transcript import Cut, clip, fit, marker, message_text, render

KEY = bytes(range(32))


def test_a_long_text_keeps_its_start_and_end_within_the_limit_and_says_what_it_left_out():
    text = "A" * 300 + "M" * 4000 + "Z" * 300
    out, omitted = clip(text, 1000)
    assert len(out) == 1000 and out.startswith("A" * 300) and out.endswith("Z" * 300 if False else "Z" * 100)
    assert marker(omitted) in out and omitted == len(text) - (1000 - len(marker(omitted)) - 2)
    assert clip("short", 1000) == ("short", 0) and clip("x" * 10, 0) == ("x" * 10, 0)
    head_heavy, _ = clip(text, 1000, head_share=0.9)
    assert head_heavy.index("[...") > out.index("[...")


def test_a_limit_smaller_than_the_marker_still_names_what_is_gone():
    out, omitted = clip("x" * 500, 10)
    assert omitted == 500 and marker(500) in out


def test_a_list_over_its_budget_keeps_the_first_and_the_newest_items_with_one_marker():
    items = [f"item {i} " + "x" * 90 for i in range(40)]
    out, cut = fit(items, budget=1000)
    assert out[0] == items[0] and out[-1] == items[-1] and sum(1 for t in out if t.startswith("[...")) == 1
    gone = cut.items_omitted
    assert gone > 0 and marker(gone, "messages") in out and len(out) == 40 - gone + 1
    assert sum(len(t) + 1 for t in out) <= 1000 and cut.items == 40 and not cut.whole
    assert fit(items, budget=100_000)[0] == items and fit(items, budget=100_000)[1].whole


def test_a_newest_item_longer_than_the_room_left_is_cut_inside_and_still_present():
    out, cut = fit(["first", "y" * 5000], budget=600)
    assert out[0] == "first" and out[-1].startswith("y") and "characters omitted" in out[-1] and cut.clipped == 1 and cut.items_omitted == 0


def test_each_item_is_capped_and_the_counts_say_so():
    out, cut = fit(["a" * 100, "b" * 3000, "c" * 50], item_chars=[200, 500, 200])
    assert [len(t) for t in out] == [100, 500, 50]
    assert (cut.items, cut.clipped, cut.items_omitted) == (3, 1, 0) and cut.chars_omitted > 0 and cut.chars_in == 3150
    total = Cut()
    total.add(cut)
    total.add(cut)
    assert total.clipped == 2 and total.as_dict()["items"] == 6


def test_messages_render_with_roles_tool_results_cut_shorter_and_calls_shown():
    messages = [{"role": "user", "content": "find the invoice"},
                {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "search_code", "arguments": json.dumps({"q": "z" * 900})}}]},
                {"role": "tool", "name": "search_code", "content": "r" * 5000},
                {"role": "assistant", "content": [{"type": "text", "text": "done"}, {"type": "image_url"}]}]
    got = render(messages, item_chars=1500, result_chars=400)
    lines = got.text.split("\n\n")
    assert lines[0] == "USER: find the invoice" and lines[1].startswith("ASSISTANT: [call search_code]") and "characters omitted" in lines[1]
    assert lines[2].startswith("TOOL search_code: r") and len(lines[2]) <= 400 + len("TOOL search_code: ")
    assert lines[3] == "ASSISTANT: done\n[image]"
    assert got.cut.clipped == 2 and got.cut.items == 4 and got.cut.chars_omitted > 4000


def test_a_budget_in_tokens_cuts_the_middle_messages():
    messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i} " + "w" * 200} for i in range(60)]
    got = render(messages, budget_tokens=500, chars_per_token=2.0)
    assert len(got.text) <= 1000 + 10 and got.text.startswith("USER: message 0") and "message 59" in got.text
    assert marker(got.cut.items_omitted, "messages") in got.text and got.cut.items_omitted > 40
    assert message_text({"role": "tool", "content": "x"}) == "TOOL: x"


def test_the_extractor_prompt_reports_what_the_model_was_not_given():
    seen = Cut()
    messages = ["short note", "p" * 5000]
    prompt = prompt_for(messages, ["s"] * 3, "proj", [], seen)
    body = json.loads(prompt[1]["content"].split("\n", 1)[1])
    assert body["messages"][0] == "short note" and len(body["messages"][1]) == MESSAGE_CHARS and "characters omitted" in body["messages"][1]
    assert (seen.items, seen.clipped, seen.items_omitted) == (2, 1, 0) and seen.chars_omitted > 3000
    many = Cut()
    prompt_for(["m" * MESSAGE_CHARS] * 30, [], "", [], many)
    assert many.items_omitted > 0 and BATCH_CHARS < 30 * MESSAGE_CHARS


def test_the_outcome_tells_nothing_found_from_text_not_read_and_notes_not_kept():
    assert read_ops(json.dumps({"ops": []})) == ([], True)
    assert read_ops("I could not decide") == ([], False) and read_ops(json.dumps({"ops": "x"})) == ([], False)
    assert outcome_of(True, 0, 0) == "no_entry" and outcome_of(False, 0, 0) == "unreadable"
    assert outcome_of(True, 2, 0) == "rejected" and outcome_of(True, 2, 1) == "stored"


def run_once(tmp_path, reply, text="I moved to Verona last week", conversation="c1"):
    memory = PersonalMemory(tmp_path / "pm", key=KEY)
    memory.set_settings({"learn_long": True, "learn_short": True})

    class Model:
        def chat_completion(self, messages, **kw):
            return {"choices": [{"message": {"content": reply}}]}

    memory.enqueue({"text": text, "project": "", "conversation": conversation, "omitted": 0, "searches": ["a", "b"]})
    ex = Extractor(memory, lambda: Model(), lambda: True)
    assert ex.step()
    return memory, ex


def test_an_empty_answer_is_recorded_as_no_entry_with_what_was_seen(tmp_path):
    memory, ex = run_once(tmp_path, json.dumps({"ops": []}))
    run = memory.runs()[0]
    assert run == ex.last_run and run["outcome"] == "no_entry" and run["proposed"] == 0 and run["stored"] == 0 and run["conversation"] == "c1"
    assert run["saw"]["messages"] == 1 and run["saw"]["searches"] == 2 and run["saw"]["clipped"] == 0 and run["saw"]["items_omitted"] == 0 and run["saw"]["chars_omitted"] == 0
    assert "Verona" not in json.dumps(run)                                                 # counts only, never the words


def test_text_that_was_cut_is_visible_in_the_record_even_when_nothing_is_found(tmp_path):
    memory, _ = run_once(tmp_path, json.dumps({"ops": []}), text="q" * 9000)
    saw = memory.runs()[0]["saw"]
    assert memory.runs()[0]["outcome"] == "no_entry" and saw["clipped"] == 1 and saw["chars_omitted"] > 7000


def test_a_reply_that_is_not_an_answer_is_not_called_nothing_found(tmp_path):
    memory, _ = run_once(tmp_path, "Sorry, I cannot do that.")
    assert memory.runs()[0]["outcome"] == "unreadable" and memory.count() == 0


def test_stored_and_rejected_notes_are_told_apart_and_forgetting_the_conversation_removes_the_record(tmp_path):
    ok = json.dumps({"ops": [{"op": "add", "kind": "long", "section": "place", "text": "Lives in Verona", "origin": "said"}]})
    memory, _ = run_once(tmp_path, ok)
    assert memory.runs()[0]["outcome"] == "stored" and memory.runs()[0]["stored"] == 1
    bad = json.dumps({"ops": [{"op": "add", "kind": "long", "section": "place", "text": "password: abc", "origin": "said"}]})
    memory2, _ = run_once(tmp_path / "other", bad)
    assert memory2.runs()[0]["outcome"] == "rejected" and memory2.runs()[0]["proposed"] == 1
    memory.forget_conversation("c1")
    assert memory.runs() == []
