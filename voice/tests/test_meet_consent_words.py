"""The words of the consent rule: what counts as an objection or as a yes, with word-boundary matching, in Italian and English."""
import pytest

from callbridge.meetbot import consent


@pytest.mark.parametrize("text", [
    "stop", "Stop.", "please STOP recording", "no", "No!", "nope", "non acconsento", "Non acconsento a essere registrato", "esci", "Esci subito", "leave",
    "can you leave please", "remove the bot", "don't record this", "Don't record", "do not record", "non registrare", "I do not consent",
    "non voglio essere registrato", "mi oppongo", "i object", "don’t record", "non sono d'accordo", "bus-stop", "no, I do not agree",
])
def test_objections_are_found(text):
    assert consent.objection_in(text, asking=True), text


@pytest.mark.parametrize("text", [
    "nobody said that", "I know it", "stopped", "stopping the line", "stoppage", "notes", "nothing", "leaves", "leaving", "removed", "escita", "uscita",
    "no problem", "no worries, go on", "nessun problema", "non c'è problema", "ok", "yes", "va bene", "the budget is fine", "",
])
def test_ordinary_words_are_not_objections(text):
    assert consent.objection_in(text, asking=True) == "", text


def test_a_bare_no_is_an_objection_only_while_consent_is_asked():
    assert consent.objection_in("no", asking=True) == "no"
    assert consent.objection_in("no, the second option", asking=False) == ""
    assert consent.objection_in("stop", asking=False) == "stop"
    assert consent.objection_in("non acconsento", asking=False)


def test_the_matched_word_is_returned_not_the_sentence():
    assert consent.objection_in("well, honestly, STOP it now", asking=False) == "stop"


@pytest.mark.parametrize("text", ["ok", "OK!", "okay", "yes", "Sì", "va bene", "consent", "acconsento", "d'accordo", "sure, fine"])
def test_a_yes_is_found(text):
    assert consent.consent_in(text), text


@pytest.mark.parametrize("text", ["token", "oktober", "yesterday", "so-so", "maybe"])
def test_a_yes_needs_the_whole_word(text):
    assert not consent.consent_in(text), text


def test_the_lists_live_in_one_place():
    for phrase in consent.STOP_PHRASES + consent.WINDOW_PHRASES:
        assert consent.objection_in(phrase, asking=True), phrase
    for phrase in consent.CONSENT_PHRASES:
        assert consent.consent_in(phrase), phrase


def test_the_window_is_kept_between_20_and_300():
    assert consent.window_of(45) == 45 and consent.window_of(20) == 20 and consent.window_of(300) == 300
    for bad in (19, 301, -1, 0, "45", None, True, 45.5 + 400):
        assert consent.window_of(bad) == consent.WINDOW_DEFAULT_S, bad
    assert consent.window_of(60.0) == 60


def test_the_announcement_says_who_for_whom_what_and_how_to_object():
    en = consent.announcement("TaDaMoo Bot", "Mario Rossi", "en")
    it = consent.announcement("TaDaMoo Bot", "Mario Rossi", "it")
    for text, ai, stop, minutes in ((en, "AI assistant", '"stop"', "minutes"), (it, "intelligenza artificiale", "«stop»", "verbale")):
        assert "TaDaMoo Bot" in text and "Mario Rossi" in text and ai in text and stop in text and minutes in text
    assert "no audio is recorded" in en and "non viene registrato" in it
    both = consent.chat_announcement("TaDaMoo Bot", "Mario Rossi")
    assert en in both and it in both


def test_explicit_mode_asks_for_an_ok():
    assert '"ok"' in consent.announcement("B", "M", "en", "explicit")
    assert '"ok"' not in consent.announcement("B", "M", "en", "window")


def test_the_bots_own_words_are_an_echo_but_a_lone_stop_never_is():
    said = [consent.announcement("TaDaMoo Bot", "Mario Rossi", "en")]
    assert consent.is_echo("Hello I am TaDaMoo Bot an AI assistant attending this meeting", said)
    assert not consent.is_echo("stop", said)
    assert not consent.is_echo("stop stop stop", said)
    assert not consent.is_echo("please stop right now", said)
    assert not consent.is_echo("the budget needs a decision on Friday", said)
