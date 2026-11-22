"""The grammar and language check of the Chat: the list language -> model, when the check is offered (a translation into a language that has a model), and the
check itself with a model that answers by rule: code and links are put away and come back untouched, an answer that lost a marker or is empty or is far
too long is asked again and then kept as written, and every part of a long text is sent in turn."""
import pytest

from serve import langcheck as lc
from tadamoo import digest


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path))


def test_a_language_is_known_by_its_code_and_by_its_names():
    assert [lc.language_of(x) for x in ("hu", "Hungarian", "ungherese", "Magyar", "Deutsch", "klingon", 7)] == ["hu", "hu", "hu", "hu", "de", "", ""]


def test_the_list_keeps_a_model_for_a_language_and_survives_a_restart():
    assert lc.read() == {"auto_offer": True, "languages": {}}
    lc.set_language("hu", "ollama-1", "OpenEuroLLM-Hungarian")
    lc.set_language("de", "ollama-2")
    assert lc.read()["languages"] == {"hu": {"provider": "ollama-1", "model": "OpenEuroLLM-Hungarian"}, "de": {"provider": "ollama-2", "model": ""}}
    lc.remove_language("de")
    lc.set_auto_offer(False)
    assert lc.read() == {"auto_offer": False, "languages": {"hu": {"provider": "ollama-1", "model": "OpenEuroLLM-Hungarian"}}}


def test_the_list_refuses_what_it_cannot_use():
    with pytest.raises(lc.LangCheckError):
        lc.set_language("tlh", "p")
    with pytest.raises(lc.LangCheckError):
        lc.set_language("hu", "")
    with pytest.raises(lc.LangCheckError):
        lc.set_language("hu", "cloud-1", providers=[{"id": "ollama-1"}])
    with pytest.raises(lc.LangCheckError):
        lc.set_auto_offer("yes")


@pytest.mark.parametrize("request_text,target", [
    ("Translate this report into Hungarian, please.", "hu"),
    ("traduci in ungherese il testo qui sotto", "hu"),
    ("Please translate to German:\n\nHello", "de"),
    ("Write a poem in Italian", ""),                                              # not a translation
    ("Translate this text", ""),                                                  # no language named
    ("Translate into Italian\n[long answer: mode=translate; words=5000; language=de]", "de"),
])
def test_the_target_of_a_translation(request_text, target):
    assert lc.translation_target(request_text) == target


def test_the_check_is_offered_only_for_a_translation_into_a_language_with_a_model_and_when_the_offer_is_on():
    assert lc.offer("Translate into Hungarian: Hello") == ""
    lc.set_language("hu", "ollama-1")
    assert lc.offer("Translate into Hungarian: Hello") == "hu"
    assert lc.offer("Translate into German: Hello") == "" and lc.offer("Write about Hungary") == ""
    lc.set_auto_offer(False)
    assert lc.offer("Translate into Hungarian: Hello") == ""


class Model:
    def __init__(self, fix=lambda part: part.replace("teh", "the"), script=None):
        self.fix, self.script, self.calls = fix, list(script or []), []

    def __call__(self, system, part):
        self.calls.append((system, part))
        if not self.script:
            return self.fix(part)
        step = self.script.pop(0)
        return step(part) if callable(step) else step


def test_code_links_and_maths_never_reach_the_model_and_come_back_untouched():
    text = "Teh report is here: https://example.com/a_b?c=1\n\n```python\nteh = 1\n```\n\nUse `teh()` and see [the docs](docs/teh.md).\n\n$$x = teh$$\n"
    model = Model()
    out = lc.check_text(model, text, "en")
    assert out["text"] == text.replace("Teh report", "Teh report")                     # the prose has nothing the fake fixes (it fixes "teh", the prose has "Teh")
    assert all("```" not in part and "example.com" not in part and "teh()" not in part and "teh.md" not in part and "x = teh" not in part for _, part in model.calls)
    assert out["kept"] == 0 and out["parts"] == 1
    fixed = lc.check_text(Model(), "I think teh plan is good: https://example.com\n\n```\nteh\n```", "en")
    assert fixed["text"] == "I think the plan is good: https://example.com\n\n```\nteh\n```" and fixed["changed"] == 1


def test_an_answer_that_lost_a_marker_is_empty_or_far_too_long_is_asked_again_then_kept():
    text = "Look at `code()` and fix teh text please, it is a long enough sentence to be judged."
    model = Model(script=[lambda part: part.replace("⟦IC1⟧ ", "").replace("teh", "the"), ""])
    out = lc.check_text(model, text, "en")
    assert len(model.calls) == 2 and "markers" in model.calls[1][0]
    assert out["text"] == text and out["kept"] == 1 and out["changed"] == 0
    model = Model(script=["⟦IC1⟧ " + " ".join(f"w{i}" for i in range(400))])               # far too long, then the model's own good answer
    out = lc.check_text(model, text, "en")
    assert out["text"] == "Look at `code()` and fix the text please, it is a long enough sentence to be judged." and out["changed"] == 1 and "rewrite" in model.calls[1][0]


def test_a_long_text_is_sent_part_by_part_in_order_and_the_stop_ends_the_work():
    text = "\n\n".join(f"Paragraph {i}: " + " ".join(f"teh word{i}x{j}" for j in range(60)) for i in range(12))
    model = Model()
    out = lc.check_text(model, text, "en")
    assert out["parts"] == len(model.calls) > 1 and out["changed"] == out["parts"]
    assert out["text"].index("Paragraph 0") < out["text"].index("Paragraph 11") and "teh" not in out["text"]

    class Job:
        n = 0

        def progress(self, done, total):
            self.n = done

        def stopped(self):
            return self.n >= 1

    with pytest.raises(digest.Stopped):
        lc.check_text(Model(), text, "en", Job())


def test_an_unknown_language_or_an_empty_or_huge_text_is_refused():
    for args in (("x", "tlh"), ("  ", "en"), ("a" * (lc.MAX_TEXT_CHARS + 1), "en")):
        with pytest.raises(lc.LangCheckError):
            lc.check_text(Model(), *args)
