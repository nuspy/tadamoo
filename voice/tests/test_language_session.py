"""The voice order for a language (identity engines, the person's map, the standard voice, Piper, none) and the session that applies detections and
commands, with a stand-in TTS router that answers from the real catalogue. Only the person's own input changes anything."""
import pytest

from callbridge import language_names as names
from callbridge.engines import catalog
from callbridge.language_follow import FOLLOW, LOCKED, LanguageSettings
from callbridge.language_session import WARNING_STT_ENGLISH_ONLY, LanguageSession, state_note
from callbridge.language_voice import IDENTITY_ENGINES, choose_voice, tts_lang
from callbridge.voice_commands import LANG_TABLE, parse_command

LONG = "this is a sentence with enough words in it"


class FakeRouter:
    """Answers like the TtsRouter: the languages are the catalogue's, the voices are the ones given."""

    def __init__(self, active="kokoro", piper=(), clones=(), unavailable=()):
        self._active, self._piper, self._clones, self._unavailable = active, tuple(piper), tuple(clones), set(unavailable)

    @property
    def attivo(self):
        return self._active

    def motore_di_voce(self, voice):
        if voice in self._clones:
            return self._active if self._active in IDENTITY_ENGINES else "xtts"
        if voice in self._piper:
            return "piper"
        return "kokoro"

    def lingue_di(self, engine):
        s = catalog.spec(engine)
        return list(s.langs) if s is not None and s.langs else []

    def parla_lingua(self, engine, lang):
        langs = self.lingue_di(engine)
        return not langs or lang.lower().split("-")[0] in langs

    def engine_available(self, engine):
        return engine not in self._unavailable

    def is_piper(self, voice):
        return voice in self._piper

    def piper_voices(self):
        return list(self._piper)


def test_the_identity_engines_are_the_catalogs_cloning_engines():
    for engine in IDENTITY_ENGINES:
        assert catalog.spec(engine).voices_from == "cloned"
    assert {s.id for s in catalog.SPECS if s.voices_from == "cloned"} == set(IDENTITY_ENGINES)


def test_a_cloned_voice_keeps_its_identity_when_its_engine_speaks_the_language():
    r = FakeRouter("xtts", clones=["mia"])
    c = choose_voice("fr", voice="mia", engine="xtts", router=r)
    assert (c.ok, c.voice, c.engine, c.tts_lang, c.reason) == (True, "mia", "xtts", "fr", "identity")
    assert choose_voice("zh", voice="mia", engine="xtts", router=r).tts_lang == "zh"
    assert choose_voice("hu", voice="mia", router=r).reason == "identity"         # the engine is deduced from the voice


def test_a_cloning_engine_that_cannot_speak_the_language_falls_to_the_standard_voice_or_piper():
    r = FakeRouter("chatterbox", clones=["mia"], piper=["hu_HU-anna-medium"])
    c = choose_voice("hu", voice="mia", engine="chatterbox", router=r)             # chatterbox has no Hungarian
    assert (c.voice, c.engine, c.reason) == ("hu_HU-anna-medium", "piper", "standard")
    c = choose_voice("ja", voice="mia", engine="chatterbox", router=FakeRouter("chatterbox", clones=["mia"]))
    assert (c.voice, c.engine) == ("jf_alpha", "kokoro")


@pytest.mark.parametrize("code,gender,voice,lang", [("fr", None, "ff_siwis", "fr-fr"), ("en", "m", "am_michael", "en-us"), ("zh", "f", "zf_xiaoxiao", "cmn"),
                                                    ("it", "m", "im_nicola", "it"), ("pt", None, "pf_dora", "pt-br")])
def test_kokoro_uses_the_standard_voice_of_the_language_with_its_own_lang_string(code, gender, voice, lang):
    c = choose_voice(code, voice="if_sara", engine="kokoro", gender=gender, router=FakeRouter())
    assert (c.ok, c.voice, c.engine, c.tts_lang, c.reason) == (True, voice, "kokoro", lang, "standard")


def test_the_voice_the_person_mapped_comes_before_the_standard_one():
    voice_map = {"fr": {"engine": "piper", "voice": "fr_FR-siwis-medium"}}
    c = choose_voice("fr", voice="if_sara", engine="kokoro", voice_map=voice_map, router=FakeRouter(piper=["fr_FR-siwis-medium"]))
    assert (c.voice, c.engine, c.reason) == ("fr_FR-siwis-medium", "piper", "mapped")
    c = choose_voice("fr", voice="mia", engine="xtts", voice_map=voice_map, router=FakeRouter("xtts", clones=["mia"]))
    assert c.reason == "identity"                                                  # a multilingual engine keeps the identity even so


def test_a_language_kokoro_lacks_takes_an_installed_piper_voice_else_nothing():
    r = FakeRouter(piper=["de_DE-thorsten-medium", "de_DE-eva_k-x_low", "it_IT-riccardo-x_low"])
    c = choose_voice("de", voice="if_sara", engine="kokoro", router=r)
    assert (c.ok, c.voice, c.engine, c.reason) == (True, "de_DE-eva_k-x_low", "piper", "piper")
    none = choose_voice("de", voice="if_sara", engine="kokoro", router=FakeRouter())
    assert (none.ok, none.voice, none.reason) == (False, "if_sara", "none")
    assert choose_voice("hu", voice="if_sara", engine="kokoro", router=FakeRouter()).ok is False       # the standard Hungarian voice is Piper's and not installed


def test_a_missing_kokoro_model_means_no_standard_voice():
    c = choose_voice("fr", voice="hu_HU-anna-medium", engine="piper", router=FakeRouter(piper=["hu_HU-anna-medium"], unavailable=["kokoro"]))
    assert c.ok is False


def test_without_a_router_everything_is_allowed_and_the_voice_is_kept():
    c = choose_voice("de", voice="x", engine="", router=None)
    assert (c.ok, c.voice, c.reason) == (True, "x", "kept")


def test_tts_lang_spelling():
    assert tts_lang("fr", "kokoro") == "fr-fr" and tts_lang("fr", "xtts") == "fr" and tts_lang("de", "kokoro") == "de"
    assert tts_lang("zh", "") == "cmn"


# ------------------------------------------------------------------------------------------------ the session
def session(settings=None, language="it", voice="if_sara", engine="kokoro", router=None, **kw):
    settings = settings or LanguageSettings(enabled=("it", "en", "fr"))
    return LanguageSession(settings, language=language, voice=voice, engine=engine, router_fn=lambda: router or FakeRouter(), **kw)


def heard(lang, prob=0.95, seconds=3.0):
    return {"language": lang, "prob": prob, "duration_s": seconds}


def test_a_detection_of_the_owner_changes_voice_language_and_note():
    s = session()
    out = s.on_detection(LONG, heard("fr"), owner=True)
    assert (out.language, out.changed, out.voice, out.engine, out.tts_lang, out.reason, out.say) == ("fr", True, "ff_siwis", "kokoro", "fr-fr", "detected", "")
    assert out.note == state_note("fr") == "[state] reply_language=fr" and out.reply_name == "French"
    assert s.language == "fr" and s.mode == FOLLOW


def test_what_is_not_the_owners_changes_nothing():
    s = session()
    assert s.on_detection(LONG, heard("fr"), owner=False) is None
    assert s.on_command(parse_command("speak French"), owner=False) is None
    assert s.on_command(parse_command("sprich Französisch"), owner=False) is None
    assert s.on_command(parse_command("torna automatico"), owner=False) is None
    assert (s.language, s.mode, s.voice) == ("it", FOLLOW, "if_sara")
    s.on_command(parse_command("speak French"), owner=True)
    assert s.on_command(parse_command("back to automatic"), owner=False) is None and s.mode == LOCKED       # a participant cannot release the lock either


def test_no_info_and_not_enabled_languages_change_nothing():
    s = session()
    assert s.on_detection(LONG, None, owner=True) is None
    assert s.on_detection(LONG, heard("de"), owner=True) is None
    assert s.language == "it"


def test_an_explicit_command_locks_the_language_and_confirms_it_in_that_language():
    s = session()
    out = s.on_command(parse_command("parle français"), owner=True)
    assert (out.language, out.mode, out.changed, out.say, out.reason) == ("fr", LOCKED, True, "D'accord, je parle français.", "command")
    for _ in range(3):
        assert s.on_detection(LONG, heard("it", 0.99, 5.0), owner=True) is None
    assert s.language == "fr"
    assert s.stt_hint() == "fr"


def test_back_to_automatic_releases_the_lock_and_a_bare_word_in_conversation_is_not_a_command():
    s = session()
    assert s.on_command(parse_command("automatico"), owner=True) is None            # follow mode: that is a word in a conversation
    s.on_command(parse_command("parla inglese"), owner=True)
    out = s.on_command(parse_command("back to automatic"), owner=True)
    assert (out.mode, out.changed, out.reason, out.say) == (FOLLOW, False, "auto", "Okay, I'll follow your language.")
    assert s.stt_hint() == ""
    s.on_command(parse_command("parla francese"), owner=True)
    assert s.on_command(parse_command("automatico"), owner=True).reason == "auto"    # locked: a bare word is accepted


def test_a_language_the_person_did_not_enable_is_refused_with_the_way_to_enable_it():
    s = session()
    out = s.on_command(parse_command("sprich Deutsch"), owner=True)
    assert (out.changed, out.reason, out.warning) == (False, "refused", "not_enabled")
    assert "tedesco" in out.say and "Impostazioni > Voce" in out.say
    assert (s.language, s.mode) == ("it", FOLLOW)
    en = session(language="en").on_command(parse_command("speak German"), owner=True)
    assert "German" in en.say and "Settings > Voice" in en.say


def test_no_voice_for_the_language_keeps_the_previous_one_and_says_so_once_naming_the_install_button():
    s = session(LanguageSettings(enabled=("it", "en", "de")))
    out = s.on_detection(LONG, heard("de"), owner=True)
    assert (out.changed, out.warning, out.language, out.voice) == (False, "no_voice", "it", "if_sara")
    assert "tedesco" in out.say and "Installa" in out.say and "Impostazioni > Voce" in out.say and "italiano" in out.say
    assert s.language == "it"
    assert s.on_detection(LONG, heard("de"), owner=True) is None                     # said once; the detection is ignored from now on
    out = s.on_command(parse_command("sprich Deutsch"), owner=True)                  # asked again: it is answered again
    assert out.warning == "no_voice" and out.say and s.mode == FOLLOW
    s.apply_settings(LanguageSettings(enabled=("it", "en", "de"), voices={"de": {"engine": "piper", "voice": "de_DE-x"}}))
    out = s.on_detection(LONG, heard("de"), owner=True)
    assert (out.language, out.voice, out.engine) == ("de", "de_DE-x", "piper")       # a voice was mapped: the language works now


def test_a_command_that_finds_no_voice_does_not_lock_but_keeps_an_earlier_lock():
    s = session(LanguageSettings(enabled=("it", "en", "de")))
    s.on_command(parse_command("sprich Deutsch"), owner=True)
    assert s.mode == FOLLOW
    s.on_command(parse_command("parla inglese"), owner=True)
    s.on_command(parse_command("sprich Deutsch"), owner=True)
    assert s.mode == LOCKED and s.language == "en"


def test_the_panel_asks_through_request():
    s = session()
    assert s.request("fr").language == "fr" and s.mode == LOCKED
    assert s.request("auto").mode == FOLLOW
    assert s.request("xx").reason == "refused"


def test_with_follow_off_the_command_still_works_and_automatic_is_refused():
    s = session(LanguageSettings(follow=False, enabled=("it", "en", "fr")))
    assert s.mode == LOCKED and s.detecting is False
    assert s.on_detection(LONG, heard("fr"), owner=True) is None
    assert s.on_command(parse_command("parle français"), owner=True).language == "fr"
    assert s.on_command(parse_command("torna automatico"), owner=True).warning == "auto_off"


def test_a_fixed_recognizer_language_keeps_the_follow_off_and_is_reported():
    s = session(stt_fixed=True)
    assert s.mode == LOCKED and s.stt_hint() == "" and "stt_language_fixed" in s.warnings()


def test_the_english_only_recognizer_is_reported_when_another_language_is_enabled():
    s = session()
    assert s.warnings("small.en") == [WARNING_STT_ENGLISH_ONLY] and s.warnings("small") == []
    assert session(LanguageSettings(enabled=("en",)), language="en").warnings("small.en") == []
    assert s.status("small.en")["warnings"] == [WARNING_STT_ENGLISH_ONLY] and s.status()["enabled"] == ["it", "en", "fr"]


def test_a_multilingual_engine_keeps_the_voice_through_changes():
    s = session(voice="mia", engine="xtts", router=FakeRouter("xtts", clones=["mia"]))
    out = s.on_detection(LONG, heard("fr"), owner=True)
    assert (out.voice, out.engine, out.tts_lang) == ("mia", "xtts", "fr")
    out = s.on_command(parse_command("speak English"), owner=True)
    assert (out.voice, out.engine, out.tts_lang, out.changed) == ("mia", "xtts", "en", True)


def test_locking_the_current_language_confirms_without_a_change():
    out = session().on_command(parse_command("parla italiano"), owner=True)
    assert (out.changed, out.mode, out.say) == (False, LOCKED, "Va bene, parlo in italiano.")


def test_the_current_language_is_always_enabled_and_the_confirmations_exist_for_every_language():
    s = session(LanguageSettings(enabled=("en",)), language="it")
    assert s.status()["enabled"] == ["en", "it"]
    for code in names.CODES:
        assert names.confirmation(code)
    for info in LANG_TABLE.values():
        assert info["switch"]
