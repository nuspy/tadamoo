"""The person's two memories: encrypted file, notes, switches and weights, and what a conversation takes in."""

import pytest

from tadamoo import untrusted
from tadamoo.personal_memory import DEFAULTS, PersonalMemory, PersonalMemoryError, Recall, register_personal_tools, stems
from tadamoo.tools.registry import ToolRegistry

KEY = bytes(range(32))
DAY = 86400.0


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make(tmp_path, clock=None, **settings):
    memory = PersonalMemory(tmp_path / "pm", key=KEY, clock=clock or Clock())
    if settings:
        memory.set_settings(settings)
    return memory


def test_notes_are_encrypted_on_disk_and_open_again(tmp_path):
    memory = make(tmp_path)
    memory.add("long", "place", "Lives in Milano, in via Garibaldi")
    raw = (tmp_path / "pm" / "memories.bin").read_bytes()
    assert b"Garibaldi" not in raw and raw.startswith(b"BPM1")
    assert make(tmp_path).items()[0]["text"] == "Lives in Milano, in via Garibaldi"
    with pytest.raises(PersonalMemoryError, match="another key"):
        PersonalMemory(tmp_path / "pm", key=bytes(32)).items()


def test_another_process_sees_what_the_server_wrote(tmp_path):
    writer, reader = make(tmp_path), make(tmp_path)
    assert reader.items() == []
    writer.add("short", "topics", "Is comparing two e-bikes")
    assert [i["text"] for i in reader.items()] == ["Is comparing two e-bikes"]


def test_a_note_that_says_the_same_is_counted_not_repeated(tmp_path):
    clock = Clock()
    memory = make(tmp_path, clock)
    first = memory.add("long", "tastes", "Likes dark roast coffee", origin="inferred", confidence=0.5)
    clock.now += DAY
    again = memory.add("long", "tastes", "likes dark roast coffee!", origin="said")      # the same words: one note
    assert memory.count() == 1 and again["id"] == first["id"] and again["mentions"] == 2
    assert again["origin"] == "said" and again["seen"] > first["seen"]
    memory.add("long", "interests", "Likes dark roast coffee")                          # another section is another note
    assert memory.count() == 2


def test_notes_are_checked(tmp_path):
    memory = make(tmp_path)
    with pytest.raises(PersonalMemoryError, match="sections"):
        memory.add("long", "searches", "x y z")
    with pytest.raises(PersonalMemoryError, match="long-term or short-term"):
        memory.add("middle", "facts", "x y z")
    with pytest.raises(PersonalMemoryError, match="some text"):
        memory.add("long", "place", "   ")
    assert len(memory.add("long", "place", "x" * 5000)["text"]) == 5000                  # a note has no length limit
    with pytest.raises(PersonalMemoryError, match="origin"):
        memory.add("long", "place", "Lives in Rome", origin="guessed")


def test_edit_confirm_delete_and_one_card(tmp_path):
    memory = make(tmp_path)
    note = memory.add("long", "character", "Seems impatient", origin="inferred", confidence=0.4)
    changed = memory.update(note["id"], text="Seems impatient with slow tools", section="thinking")
    assert changed["section"] == "thinking" and changed["text"].endswith("slow tools")
    sure = memory.confirm(note["id"])
    assert sure["origin"] == "confirmed" and sure["confidence"] == 1.0
    memory.add("long", "card", "A builder who likes control.", origin="inferred")
    memory.add("long", "card", "A builder who likes control and speed.", origin="inferred")
    assert [i["text"] for i in memory.items("long", "card")] == ["A builder who likes control and speed."]
    assert memory.delete(note["id"]) and not memory.delete(note["id"])
    with pytest.raises(PersonalMemoryError, match="not there"):
        memory.update("pm_nothing", text="x")


def test_clear_takes_one_memory_or_all(tmp_path):
    memory = make(tmp_path)
    memory.add("long", "place", "Lives in Rome")
    memory.add("short", "facts", "Has a dentist visit on Friday")
    assert memory.clear("short") == 1 and memory.count("long") == 1
    memory.enqueue({"text": "hello there"})
    memory.clear()
    assert memory.count() == 0 and memory.queue_len() == 0


def test_the_memories_are_on_and_the_cloud_is_off_until_the_person_says_otherwise(tmp_path):
    memory = make(tmp_path)
    s = memory.settings()
    assert all(s[k] for k in ("learn_long", "learn_short", "use_long", "use_short")) and not s["cloud_ok"]
    assert s["short_days"] == 14 and s["long_days"] == 0
    assert s["weights"]["emotions"] == "low" and s["weights"]["place"] == "normal"      # what is only guessed weighs little at first
    memory.set_settings({"use_long": False, "use_short": False})
    memory.add("long", "place", "Lives in Rome")
    assert Recall(memory).message("where should I eat in Rome") is None


def test_settings_are_validated(tmp_path):
    memory = make(tmp_path)
    for bad in ({"use_long": "yes"}, {"short_days": 0}, {"short_days": True}, {"long_days": -1}, {"weights": {"nonsense": "high"}},
                {"weights": {"place": "enormous"}}, {"weights": "high"}):
        with pytest.raises(PersonalMemoryError):
            memory.set_settings(bad)
    out = memory.set_settings({"use_long": True, "weights": {"emotions": "high"}, "short_days": 30})
    assert out["use_long"] and out["weights"]["emotions"] == "high" and out["weights"]["place"] == "normal" and out["short_days"] == 30
    assert make(tmp_path).settings()["short_days"] == 30                                  # kept for the next process
    assert DEFAULTS["short_days"] == 14                                                    # the defaults are never edited in place


def test_the_short_memory_reaches_back_two_weeks_unless_the_person_asks_for_more(tmp_path):
    clock = Clock()
    memory = make(tmp_path, clock, use_short=True)
    old = memory.add("short", "projects", "Was building the inventory app with Flask")
    clock.now += 20 * DAY
    memory.add("short", "projects", "Is building the invoice tool with Django")
    assert [i["text"] for i in memory.eligible()] == ["Is building the invoice tool with Django"]
    assert {i["id"] for i in memory.eligible(older=True)} == {old["id"], memory.items()[0]["id"]}
    assert [i["text"] for i in memory.search("inventory app", older=True)] == ["Was building the inventory app with Flask"]
    assert memory.search("inventory app") == []


def test_the_long_memory_has_no_window_by_default(tmp_path):
    clock = Clock()
    memory = make(tmp_path, clock, use_long=True)
    memory.add("long", "place", "Lives in Torino")
    clock.now += 400 * DAY
    assert len(memory.eligible()) == 1
    memory.set_settings({"long_days": 90})
    assert memory.eligible() == []


def test_a_memory_that_is_not_used_gives_nothing(tmp_path):
    memory = make(tmp_path, use_long=True, use_short=False)
    memory.add("long", "place", "Lives in Torino")
    memory.add("short", "facts", "Torino has a fair this week")
    assert [i["kind"] for i in memory.eligible()] == ["long"]


def test_a_switch_the_person_turned_off_stays_off_for_every_later_process(tmp_path):
    make(tmp_path, learn_long=False, use_short=False)
    later = make(tmp_path)                                             # a new conversation, or the server after a restart
    s = later.settings()
    assert not s["learn_long"] and not s["use_short"] and s["learn_short"] and s["use_long"]


def test_the_request_decides_which_notes_come_in(tmp_path):
    memory = make(tmp_path, use_long=True, use_short=True)
    memory.add("long", "place", "Lives in Milano")
    memory.add("long", "tastes", "Loves spicy ramen")
    memory.add("short", "projects", "Is building a vending machine monitor with Python")
    text = Recall(memory).message("Suggest a good ramen restaurant in Milano")
    assert "Lives in Milano" in text and "spicy ramen" in text and "vending" not in text
    assert untrusted.normalize(text).startswith("<untrusted_data source=\"personal-memory\">") and "not instructions" in text
    assert Recall(memory).message("What is the weather like") is None                    # nothing matches: not a token spent


def test_a_note_is_given_once_in_a_conversation(tmp_path):
    clock = Clock()
    memory = make(tmp_path, clock, use_long=True)
    note = memory.add("long", "place", "Lives in Milano")
    recall = Recall(memory)
    assert "Lives in Milano" in recall.message("restaurants in Milano please")
    assert recall.message("and museums in Milano?") is None                              # the second question does not repeat it
    clock.now += 5
    memory.update(note["id"], text="Lives in Milano, near the Navigli")
    assert "Navigli" in recall.message("what about bars in Milano")                      # changed: said again
    assert Recall(memory).message("museums in Milano") is not None                       # another conversation starts fresh


def test_a_guess_needs_a_clear_match_whatever_it_weighs(tmp_path):
    memory = make(tmp_path, use_long=True, weights={"character": "highest"})
    memory.add("long", "character", "Seems to prefer terse practical answers", origin="inferred", confidence=0.8)
    assert Recall(memory).message("hello") is not None                                   # the start of a conversation: the core of a 'highest' section
    one = Recall(memory)
    one.started = True
    assert one.message("give me answers") is None                                        # one weak word in common is not enough
    assert "(a guess)" in (one.message("I want terse practical answers") or "")


def test_the_weight_of_a_section(tmp_path):
    memory = make(tmp_path, use_long=True)
    for dish in ("carbonara", "amatriciana", "pesto", "puttanesca", "arrabbiata", "norma"):
        memory.add("long", "tastes", f"Enjoys cooking pasta {dish} with basil")
    texts = {}
    for weight in ("off", "low", "normal", "high", "highest"):
        memory.set_settings({"weights": {"tastes": weight}})
        recall = Recall(memory)
        recall.started = True                                                            # past the start: only matches
        texts[weight] = (recall.message("tell me about cooking pasta with basil") or "").count("- [tastes]")
    assert texts == {"off": 0, "low": 1, "normal": 2, "high": 4, "highest": 6}          # the request matches all six clearly: the weight sets how many


def test_the_start_of_a_conversation_brings_the_core_of_a_heavy_section(tmp_path):
    memory = make(tmp_path, use_long=True, weights={"identity": "high"})
    memory.add("long", "identity", "Is a freelance electrical engineer", origin="said")
    memory.add("long", "identity", "Speaks Italian and English", origin="said")
    memory.add("long", "identity", "Owns a sailboat", origin="said")
    first = Recall(memory).message("hello")
    assert first is not None and first.count("[identity]") == 2                           # no match with 'hello', but the section weighs a lot
    again = Recall(memory)
    again.message("hello")
    assert again.message("hello again") is None


def test_the_budget_of_an_answer_is_limited(tmp_path):
    memory = make(tmp_path, use_long=True, weights={"interests": "highest"})
    for n in range(8):
        memory.add("long", "interests", f"Follows astronomy topic number {n} " + "with details " * 12)
    text = Recall(memory).message("astronomy")
    assert text is not None and len(text) < 2600


def test_notes_do_not_go_to_a_provider_that_is_not_this_pc_unless_allowed(tmp_path):
    memory = make(tmp_path, use_long=True)
    memory.add("long", "place", "Lives in Milano")
    assert Recall(memory, local=lambda: False).message("restaurants in Milano") is None
    memory.set_settings({"cloud_ok": True})
    assert Recall(memory, local=lambda: False).message("restaurants in Milano") is not None


def test_the_tool_searches_recent_notes_and_all_of_them_when_asked(tmp_path):
    clock = Clock()
    memory = make(tmp_path, clock, use_short=True, use_long=True)
    memory.add("short", "searches", "Looked up prices of second-hand cargo bikes")
    clock.now += 30 * DAY
    memory.add("short", "searches", "Looked up how to repot a lemon tree")
    registry = ToolRegistry()
    register_personal_tools(registry, memory)
    recall = registry.get("recall_about_user").handler if hasattr(registry, "get") else registry._tools["recall_about_user"].handler
    assert recall("cargo bikes")["notes"] == []
    assert [n["text"] for n in recall("cargo bikes", scope="all")["notes"]] == ["Looked up prices of second-hand cargo bikes"]
    assert recall("lemon tree")["notes"][0]["section"] == "searches"
    memory.set_settings({"use_short": False, "use_long": False})
    assert "not switched" in recall("lemon tree")["note"]


def test_words_are_cut_so_that_forms_of_a_word_meet():
    assert stems("ristorante") == stems("ristoranti") == ["risto"]
    assert stems("Milano") == stems("Milan") == ["milan"]
    assert stems("the cat and the dog") == ["cat", "dog"]                                 # the small words are dropped
