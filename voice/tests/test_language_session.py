"""The voice order for a language (the selected engine keeps its voice or its per-language voice; Piper only when it cannot; else nothing) and the
session that applies detections and commands, with a stand-in TTS router that answers from the real catalogue and refuses every engine change.
Only the person's own input changes anything."""
import pytest

from callbridge import language_names as names
from callbridge.engines import catalog
from callbridge.language_follow import FOLLOW, LOCKED, LanguageSettings
from callbridge.language_session import WARNING_STT_ENGLISH_ONLY, LanguageSession, state_note
from callbridge.language_voice import IDENTITY_ENGINES, choose_voice, tts_lang
from callbridge.voice_commands import LANG_TABLE, parse_command

LONG = "this is a sentence with enough words in it"


class FakeRouter:
    """Answers like the TtsRouter: the languages are the catalogue's, the voices are the ones given. It records every call that would change
    the engine in memory, and none may happen: the session only asks questions."""

    def __init__(self, active="kokoro", piper=(), clones=(), unavailable=()):
        self._active, self._piper, self._clones, self._unavailable = active, tuple(piper), tuple(clones), set(unavailable)
        self.changes: list[str] = []

    @property
    def attivo(self):
        return self._active

    def attiva(self, *a, **k):
        self.changes.append("attiva")

    def _solo_questo(self, *a, **k):
        self.changes.append("_solo_questo")

    def load(self, *a, **k):
        self.changes.append("load")

    def synth(self, *a, **k):
        self.changes.append("synth")

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


def test_a_cloning_engine_that_cannot_speak_the_language_is_routed_to_piper_not_to_another_engine():
    r = FakeRouter("chatterbox", clones=["mia"], piper=["hu_HU-anna-medium"])
    c = choose_voice("hu", voice="mia", engine="chatterbox", router=r)             # chatterbox has no Hungarian
    assert (c.ok, c.voice, c.engine, c.tts_lang, c.reason) == (True, "hu_HU-anna-medium", "piper", "hu", "fallback")
    c = choose_voice("ja", voice="mia", engine="chatterbox", router=FakeRouter("chatterbox", clones=["mia"]))     # no Piper voice: nothing, not Kokoro
    assert (c.ok, c.voice, c.engine, c.reason) == (False, "mia", "chatterbox", "engine_cannot")


@pytest.mark.parametrize("code,gender,voice,lang", [("fr", None, "ff_siwis", "fr-fr"), ("en", "m", "am_michael", "en-us"), ("zh", "f", "zf_xiaoxiao", "cmn"),
                                                    ("it", "m", "im_nicola", "it"), ("pt", None, "pf_dora", "pt-br")])
def test_kokoro_uses_the_standard_voice_of_the_language_with_its_own_lang_string(code, gender, voice, lang):
    c = choose_voice(code, voice="if_sara", engine="kokoro", gender=gender, router=FakeRouter())
    assert (c.ok, c.voice, c.engine, c.tts_lang, c.reason) == (True, voice, "kokoro", lang, "standard")


def test_the_voice_the_person_mapped_for_the_selected_engine_comes_before_the_standard_one():
    c = choose_voice("fr", voice="if_sara", engine="kokoro", voice_map={"fr": "ff_siwis"}, router=FakeRouter())
    assert (c.voice, c.engine, c.reason) == ("ff_siwis", "kokoro", "mapped")
    c = choose_voice("fr", voice="mia", engine="xtts", voice_map={"fr": "ff_siwis"}, router=FakeRouter("xtts", clones=["mia"]))
    assert c.reason == "identity" and c.voice == "mia"                              # a multilingual engine keeps the identity even so
    c = choose_voice("fr", voice="if_sara", engine="kokoro", voice_map={"fr": "fr_FR-siwis-medium"}, router=FakeRouter(piper=["fr_FR-siwis-medium"]))
    assert (c.voice, c.engine, c.reason) == ("ff_siwis", "kokoro", "standard")      # a voice of another engine in the map is not used by this one


def test_a_language_kokoro_lacks_goes_to_the_mapped_or_an_installed_piper_voice_else_nothing():
    r = FakeRouter(piper=["de_DE-thorsten-medium", "de_DE-eva_k-x_low", "it_IT-riccardo-x_low"])
    c = choose_voice("de", voice="if_sara", engine="kokoro", router=r)
    assert (c.ok, c.voice, c.engine, c.reason) == (True, "de_DE-eva_k-x_low", "piper", "fallback")
    c = choose_voice("de", voice="if_sara", engine="kokoro", voice_map={"de": "de_DE-thorsten-medium"}, router=r)
    assert (c.voice, c.reason) == ("de_DE-thorsten-medium", "fallback")
    none = choose_voice("de", voice="if_sara", engine="kokoro", router=FakeRouter())
    assert (none.ok, none.voice, none.engine, none.reason) == (False, "if_sara", "kokoro", "engine_cannot")
    assert choose_voice("hu", voice="if_sara", engine="kokoro", router=FakeRouter()).ok is False       # Hungarian: Piper's voice is not installed
    assert choose_voice("de", voice="if_sara", engine="kokoro", router=FakeRouter(piper=["de_DE-x"], unavailable=["piper"])).ok is False


def test_with_piper_selected_its_own_voice_for_the_language_or_nothing():
    r = FakeRouter("piper", piper=["it_IT-riccardo-x_low", "fr_FR-siwis-medium"])
    assert choose_voice("fr", voice="it_IT-riccardo-x_low", engine="piper", router=r).voice == "fr_FR-siwis-medium"
    c = choose_voice("de", voice="it_IT-riccardo-x_low", engine="piper", router=r)
    assert (c.ok, c.reason) == (False, "voice_missing")


def test_without_a_router_the_voice_is_kept():
    c = choose_voice("de", voice="x", engine="", router=None)
    assert (c.ok, c.voice, c.reason) == (True, "x", "kept")


def test_tts_lang_spelling():
    assert tts_lang("fr", "kokoro") == "fr-fr" and tts_lang("fr", "xtts") == "fr" and tts_lang("de", "kokoro") == "de"
    assert tts_lang("zh", "") == "cmn"


# ------------------------------------------------------------------------------------------------ the session
def session(settings=None, language="it", voice="if_sara", engine="kokoro", router=None, **kw):
    settings = settings or LanguageSettings(enabled=("it", "en", "fr"))
    router = router or FakeRouter()
    s = LanguageSession(settings, language=language, voice=voice, engine=engine, router_fn=lambda: router, **kw)
    s.router = router
    return s


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
    assert "tedesco" in out.say.lower() and "Impostazioni > Voce" in out.say
    assert (s.language, s.mode) == ("it", FOLLOW)
    en = session(language="en").on_command(parse_command("speak German"), owner=True)
    assert "German" in en.say and "Settings > Voice" in en.say


GERMAN = LanguageSettings(enabled=("it", "en", "de"))


def test_without_a_voice_for_the_language_even_in_piper_the_previous_one_stays_and_the_install_button_is_named_once():
    s = session(GERMAN)
    out = s.on_detection(LONG, heard("de"), owner=True)
    assert (out.changed, out.warning, out.language, out.voice) == (False, "no_voice", "it", "if_sara")
    assert "tedesco" in out.say.lower() and "Installa" in out.say and "Impostazioni > Voce" in out.say and "italiano" in out.say
    assert s.language == "it"
    assert s.on_detection(LONG, heard("de"), owner=True) is None                     # said once; the detection is ignored from now on
    out = s.on_command(parse_command("sprich Deutsch"), owner=True)                  # asked again: it is answered again
    assert out.warning == "no_voice" and out.say and s.mode == FOLLOW
    s.router._piper = ("de_DE-x",)
    s.apply_settings(GERMAN)                                                         # the person installed a Piper voice and saved the settings
    out = s.on_detection(LONG, heard("de"), owner=True)
    assert (out.language, out.voice, out.engine, out.warning) == ("de", "de_DE-x", "piper", "fallback")


def test_a_language_the_engine_cannot_speak_goes_to_piper_with_one_notice_and_comes_back():
    router = FakeRouter(piper=["de_DE-x"])
    s = session(GERMAN, router=router)
    out = s.on_detection(LONG, heard("de"), owner=True)
    assert (out.changed, out.engine, out.voice, out.tts_lang, out.warning) == (True, "piper", "de_DE-x", "de", "fallback")
    assert out.pre_say == "Tedesco non è disponibile nella voce if_sara: uso Piper."
    assert "Piper" in out.pre_say and "if_sara" in out.pre_say and s.routed_to_piper and s.status()["engine_fallback"] is True
    assert (s.base_voice, s.base_engine) == ("if_sara", "kokoro")                    # the selection is not rewritten
    out = s.on_detection(LONG + " words", heard("en"), owner=True)                     # back to a language Kokoro speaks
    assert (out.changed, out.engine, out.voice, out.tts_lang, out.pre_say) == (True, "kokoro", "af_heart", "en-us", "")
    assert not s.routed_to_piper
    out = s.on_detection(LONG, heard("de"), owner=True)                              # and to Piper again: the notice was already given
    assert out.engine == "piper" and out.pre_say == ""
    assert router.changes == []                                                      # the session never activated, loaded or swapped anything


def test_a_command_for_a_language_in_piper_confirms_in_that_language_after_the_notice():
    s = session(GERMAN, router=FakeRouter(piper=["de_DE-x"]))
    out = s.on_command(parse_command("sprich Deutsch"), owner=True)
    assert (out.mode, out.engine, out.say, out.warning) == (LOCKED, "piper", "In Ordnung, ich spreche Deutsch.", "fallback")
    assert "Piper" in out.pre_say


def test_a_command_that_finds_no_voice_does_not_lock_but_keeps_an_earlier_lock():
    s = session(GERMAN)
    s.on_command(parse_command("sprich Deutsch"), owner=True)
    assert s.mode == FOLLOW
    s.on_command(parse_command("parla inglese"), owner=True)
    s.on_command(parse_command("sprich Deutsch"), owner=True)
    assert s.mode == LOCKED and s.language == "en"


@pytest.mark.parametrize("text", ["use Piper", "usa piper", "cambia motore", "switch to the Kokoro engine", "usa il motore italiano", "change the model to French",
                                  "passa al modello francese", "utilise le moteur français", "wechsle die Engine auf Deutsch", "set the voice engine to xtts",
                                  "cambia il motore vocale in inglese", "speak French with Piper"])
def test_no_phrase_selects_an_engine_or_a_model(text):
    cmd = parse_command(text)
    assert cmd is None or cmd["kind"] not in ("lang", "lang_auto"), text
    router = FakeRouter(piper=["fr_FR-x"])
    s = session(router=router)
    assert s.on_command(cmd, owner=True) is None and s.engine == "kokoro" and router.changes == [] and router.attivo == "kokoro"


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


def test_a_multilingual_engine_keeps_the_voice_through_changes_and_nothing_is_swapped():
    router = FakeRouter("xtts", clones=["mia"])
    s = session(voice="mia", engine="xtts", router=router)
    out = s.on_detection(LONG, heard("fr"), owner=True)
    assert (out.voice, out.engine, out.tts_lang) == ("mia", "xtts", "fr")
    out = s.on_command(parse_command("speak English"), owner=True)
    assert (out.voice, out.engine, out.tts_lang, out.changed) == ("mia", "xtts", "en", True)
    assert router.changes == [] and router.attivo == "xtts"


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
