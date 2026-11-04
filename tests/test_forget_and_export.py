"""Forgetting a conversation (its index pieces and the notes learned from it) and the Markdown export of the personal memory."""
import json
import time
from pathlib import Path

from tadamoo.forget import Forgetter
from tadamoo.memory_export import export_markdown, file_name, render
from tadamoo.memory_extract import Extractor, apply
from tadamoo.past import PastIndex
from tadamoo.personal_memory import PersonalMemory

KEY = bytes(range(32))


def make(tmp_path):
    memory = PersonalMemory(tmp_path / "pm", key=KEY, clock=lambda: 1_800_000_000.0)
    memory.set_settings({"learn_long": True, "learn_short": True})
    return memory


def test_a_note_remembers_the_conversation_it_came_from_and_old_notes_still_work(tmp_path):
    memory = make(tmp_path)
    note = memory.add("long", "place", "Lives in Verona", origin="said", conversation="c1")
    assert note["conversations"] == ["c1"]
    assert memory.add("long", "place", "Lives in Verona", origin="said", conversation="c2")["conversations"] == ["c1", "c2"]
    assert memory.add("long", "tastes", "Likes tea")["conversations"] == []
    old = memory._read()
    del old["items"][note["id"]]["conversations"]                                  # a note written before the field existed
    memory._write(old)
    assert memory.get(note["id"])["text"] == "Lives in Verona"
    assert memory.update(note["id"], text="Lives in Padova")["text"] == "Lives in Padova"
    assert memory.forget_conversation("c1")["notes"] == 0                           # an old note has no source: it stays
    assert memory.count() == 2


def test_a_mention_does_not_make_a_note_of_the_person_deletable_by_a_conversation(tmp_path):
    memory = make(tmp_path)
    memory.add("long", "place", "Lives in Verona", origin="manual")
    memory.add("long", "place", "Lives in Verona", origin="said", conversation="c1")
    assert memory.forget_conversation("c1") == {"queued": 0, "notes": 0, "kept": 0, "ids": []} and memory.count() == 1


def test_forgetting_deletes_what_only_that_conversation_taught_and_keeps_the_rest(tmp_path):
    memory = make(tmp_path)
    only = memory.add("long", "place", "Lives in Verona", conversation="c1")
    shared = memory.add("long", "tastes", "Likes black coffee", conversation="c1")
    memory.add("long", "tastes", "Likes black coffee", conversation="c2")
    other = memory.add("short", "facts", "Has a dentist visit on Friday", conversation="c2")
    memory.note("added", only)
    memory.note("added", other)
    memory.enqueue({"text": "I live in Verona", "conversation": "c1"})
    memory.enqueue({"text": "something else entirely", "conversation": "c2"})
    got = memory.forget_conversation("c1")
    assert (got["notes"], got["kept"], got["queued"], got["ids"]) == (1, 1, 1, [only["id"]])
    assert memory.get(only["id"]) is None
    assert memory.get(shared["id"])["conversations"] == ["c2"] and memory.get(other["id"]) is not None
    assert [j["text"] for j in memory.queue()] == ["something else entirely"]
    assert [line["id"] for line in memory.log()] == [other["id"]]                    # what was learned lately no longer lists the deleted note
    assert b"Verona" not in (tmp_path / "pm" / "memories.bin").read_bytes()
    assert memory.forget_conversation("") == {"queued": 0, "notes": 0, "kept": 0, "ids": []}


def test_the_extractor_tags_notes_with_the_conversation_and_drops_a_batch_forgotten_meanwhile(tmp_path):
    memory = make(tmp_path)
    reply = json.dumps({"ops": [{"op": "add", "kind": "long", "section": "place", "text": "Lives in Verona", "origin": "said"}]})

    class Model:
        def __init__(self, before=None):
            self.before = before

        def chat_completion(self, messages, **kw):
            if self.before:
                self.before()
            return {"choices": [{"message": {"content": reply}}]}

    memory.enqueue({"text": "I moved to Verona last week", "project": "", "conversation": "c1"})
    memory.enqueue({"text": "And my sister lives in Rome now", "project": "", "conversation": "c2"})
    ex = Extractor(memory, lambda: Model(), lambda: True)
    assert ex.step() and [i["conversations"] for i in memory.items()] == [["c1"]]
    assert [j["conversation"] for j in memory.queue()] == ["c2"]                    # one conversation at a time
    gone = Extractor(memory, lambda: Model(before=lambda: memory.forget_conversation("c2")), lambda: True)
    memory.add("long", "tastes", "x y z")
    assert gone.step() and memory.queue_len() == 0
    assert [i["text"] for i in memory.items(section="place")] == ["Lives in Verona"] and memory.count() == 2     # nothing was learned from c2


def test_an_update_tags_the_note_with_the_conversation_that_changed_it(tmp_path):
    memory = make(tmp_path)
    note = memory.add("long", "place", "Lives in Verona", origin="inferred", confidence=0.5)
    apply(memory, [{"op": "update", "id": note["id"], "text": "Lives in Padova", "origin": "said"}], True, True, conversation="c9")
    assert memory.get(note["id"])["conversations"] == ["c9"]


def test_forget_takes_the_pieces_out_of_the_index_and_the_notes_out_of_the_memory_and_the_export(tmp_path):
    memory, past = make(tmp_path), PastIndex(tmp_path / "past.db")
    past.add_session("c1", [{"role": "user", "content": "I moved to Verona"}, {"role": "assistant", "content": "nice"}])
    past.add_session("c2", [{"role": "user", "content": "tulips in Verona"}])
    memory.add("long", "place", "Lives in Verona", conversation="c1")
    memory.add("long", "tastes", "Likes tulips", conversation="c2")
    out = tmp_path / "vault"
    forgetter = Forgetter(past, memory, out)
    assert forgetter.forget("c1")["notes"] == 1 and not (out / "index.md").exists()      # nothing was exported: nothing is written
    memory.add("long", "place", "Lives in Verona", conversation="c1")
    export_markdown(memory, out)
    assert "Verona" in (out / "index.md").read_text(encoding="utf-8")
    got = forgetter.forget("c1")
    assert got == {"chunks": 0, "queued": 0, "notes": 1, "kept": 0}
    assert "Verona" not in (out / "index.md").read_text(encoding="utf-8") and not any("Verona" in p.read_text(encoding="utf-8") for p in out.rglob("*.md"))
    assert [h["session"] for h in past.search("Verona")] == ["c2"]
    past.close()


def test_keeping_a_conversation_out_drops_its_pieces_and_waiting_messages_but_not_the_notes(tmp_path):
    memory, past = make(tmp_path), PastIndex(tmp_path / "past.db")
    past.add_session("c1", [{"role": "user", "content": "owls at night"}])
    memory.add("long", "interests", "Likes owls", conversation="c1")
    memory.enqueue({"text": "owls at night, again", "conversation": "c1"})
    assert Forgetter(past, memory).keep_out("c1") == {"chunks": 1, "queued": 1}
    assert past.search("owls") == [] and memory.queue_len() == 0 and memory.count() == 1
    past.close()


def test_the_export_is_one_markdown_file_per_note_with_front_matter_and_an_index(tmp_path):
    memory = make(tmp_path)
    a = memory.add("long", "identity", "Is an engineer: works on \"TaDaMoo\"", origin="said", conversation="c1", source="proj")
    memory.add("long", "identity", "Is an engineer: works on \"TaDaMoo\"", origin="said", conversation="c2")
    b = memory.add("short", "topics", "Is comparing two e-bikes")
    out = tmp_path / "vault"
    got = export_markdown(memory, out)
    assert got == {"folder": str(out), "notes": 2, "long": 1, "short": 1}
    text = (out / "long" / file_name(a)).read_text(encoding="utf-8")
    lines = text.split("\n")
    assert lines[0] == "---" and lines.index("---", 1) > 1 and text.rstrip().endswith('Is an engineer: works on "TaDaMoo"')
    front = dict(l.split(": ", 1) for l in lines[1:lines.index("---", 1)])
    assert front["kind"] == "long" and front["section"] == "identity" and front["id"] == json.dumps(a["id"]) and front["created"] == time.strftime("%Y-%m-%d", time.localtime(1_800_000_000.0))
    assert front["source_conversation"] == '"c1"' and front["conversations"] == '["c1", "c2"]' and front["tags"] == '["identity"]' and front["project"] == '"proj"'
    plain = (out / "short" / file_name(b)).read_text(encoding="utf-8")
    assert "source_conversation" not in plain and "kind: short" in plain
    index = (out / "index.md").read_text(encoding="utf-8")
    assert f"(long/{file_name(a)})" in index and f"(short/{file_name(b)})" in index and "Long-term (1)" in index and "\r" not in text
    assert file_name(a).endswith(a["id"] + ".md") and " " not in file_name(a)


def test_a_new_export_replaces_the_old_one_and_the_store_is_not_touched(tmp_path):
    memory = make(tmp_path)
    gone = memory.add("long", "place", "Lives in Verona")
    keep = memory.add("long", "tastes", "Likes tea")
    out = tmp_path / "vault"
    export_markdown(memory, out)
    (out / "notes-of-mine.md").write_text("mine", encoding="utf-8")                  # a file outside long/ and short/ is left alone
    memory.delete(gone["id"])
    before = (tmp_path / "pm" / "memories.bin").read_bytes()
    assert export_markdown(memory, out)["notes"] == 1
    assert [p.name for p in (out / "long").glob("*.md")] == [file_name(keep)] and (out / "notes-of-mine.md").exists()
    assert (tmp_path / "pm" / "memories.bin").read_bytes() == before
    assert export_markdown(PersonalMemory(tmp_path / "empty", key=KEY), tmp_path / "v2")["notes"] == 0 and Path(tmp_path / "v2" / "index.md").exists()


def test_a_note_with_tags_exports_them():
    item = {"id": "pm_1", "kind": "long", "section": "tastes", "text": "Likes tea", "origin": "said", "confidence": 1.0, "created": 1_800_000_000.0,
            "updated": 1_800_000_000.0, "seen": 1_800_000_000.0, "tags": ["drinks", "tastes"]}
    assert 'tags: ["tastes", "drinks"]' in render(item)
