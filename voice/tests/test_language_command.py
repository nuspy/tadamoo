"""The spoken language commands: a language by name in any supported language, back to automatic, and what is not a command."""
import pytest

from callbridge.language_names import CODES, find_languages, label, parse_language_command
from callbridge.voice_commands import parse_command


def lang(text):
    cmd = parse_command(text)
    return cmd["lang"] if cmd and cmd["kind"] == "lang" else None


@pytest.mark.parametrize("text,code", [
    ("avatar, speak French", "fr"), ("speak french please", "fr"), ("switch to English", "en"), ("talk in Spanish", "es"), ("reply in German", "de"),
    ("parla francese", "fr"), ("parla in francese", "fr"), ("parlami in inglese", "en"), ("cambia lingua a tedesco", "de"), ("passa all'italiano", "it"),
    ("rispondi in spagnolo", "es"), ("voglio parlare giapponese", "ja"), ("metti il portoghese", "pt"),
    ("parle français", "fr"), ("parlez italien s'il vous plaît", "it"), ("passe en allemand", "de"), ("réponds en anglais", "en"),
    ("sprich Deutsch", "de"), ("sprich bitte Italienisch", "it"), ("wechsle auf Französisch", "fr"), ("antworte auf Englisch", "en"),
    ("habla español", "es"), ("habla francés", "fr"), ("responde en inglés", "en"),
    ("fala português", "pt"), ("muda para italiano", "it"),
    ("beszélj magyarul", "hu"), ("speak Hungarian", "hu"), ("parla ungherese", "hu"), ("speak Chinese", "zh"), ("parla cinese", "zh"),
    ("speak russian", "ru"), ("parla olandese", "nl"), ("speak polish", "pl"), ("speak Turkish", "tr"), ("parla coreano", "ko"),
    ("speak arabic", "ar"), ("parla ceco", "cs"), ("speak hindi", "hi"), ("speak British English", "en"),
])
def test_a_language_is_asked_for_in_any_supported_language(text, code):
    assert lang(text) == code


@pytest.mark.parametrize("text,code", [("parla français", "fr"), ("parle italiano", "it"), ("speak italiano", "it"), ("sprich français", "fr"),
                                       ("habla english", "en"), ("parle deutsch", "de"), ("日本語で speak", "ja"), ("speak 中文", "zh")])
def test_a_language_can_be_named_in_its_own_language(text, code):
    assert lang(text) == code


def test_the_gender_still_comes_with_the_command():
    assert parse_command("cambia lingua a inglese femmina") == {"kind": "lang", "lang": "en", "gender": "f"}
    assert parse_command("speak french male")["gender"] == "m"
    assert parse_command("parla in francese")["gender"] is None


@pytest.mark.parametrize("text", [
    "torna automatico", "back to automatic", "retour en automatique", "automatisch", "zurück zu automatisch", "volver a automático",
    "voltar ao automático", "modalità automatica", "automatic language", "lingua automatica", "follow my language", "segui la mia lingua",
    "suis ma langue", "folge meiner Sprache", "avatar, automatico",
])
def test_back_to_automatic_in_any_supported_language(text):
    cmd = parse_language_command(text)
    assert cmd is not None and cmd["kind"] == "lang_auto", text


def test_a_bare_automatic_word_is_marked_bare_so_that_the_caller_can_ignore_it_in_conversation():
    assert parse_language_command("automatico") == {"kind": "lang_auto", "bare": True}
    assert parse_language_command("torna automatico") == {"kind": "lang_auto", "bare": False}


@pytest.mark.parametrize("text", [
    "ciao come stai", "ho parlato con il tedesco ieri", "I speak French at home every day", "talk about German cars with me", "parlami del francese",
    "ti spiego come cambiare lingua sul telefono quando sei in viaggio", "what is the weather in Paris", "speak", "french",
    "parla francese o tedesco", "switch from English to French", "parle-moi de l'allemand", "the automatic transmission is broken",
    "", "   ", "passa all'agente", "nuova sessione",
])
def test_what_is_not_a_language_command_is_none(text):
    cmd = parse_command(text)
    assert cmd is None or cmd["kind"] not in ("lang", "lang_auto"), text


def test_find_languages_in_order_and_the_table_is_consistent():
    assert find_languages("dal francese all'italiano") == ["fr", "it"]
    assert find_languages("nothing here") == []
    for code in CODES:
        assert label(code, "en") and label(code, "it")


def test_the_old_commands_still_work():
    assert parse_command("new session") == {"kind": "new_session"}
    assert parse_command("switch to the agent") == {"kind": "brain", "mode": "harness"}
