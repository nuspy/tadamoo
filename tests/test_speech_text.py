"""The text made fit for the voice (one rule table, checked with a list of offending patterns for each language) and the check that reads the voice back."""
import base64
import http.server
import io
import json
import threading
import wave

import pytest

from tadamoo.speech import service as speech, textnorm, voicecheck

# (language, what a script may contain, what the voice engine must be given)
CASES = [
    # a full stop at the end of a piece is read as "punto": it goes, the pause is the silence between pieces
    ("it", "Domani le lezioni riprendono regolarmente.", "Domani le lezioni riprendono regolarmente"),
    ("it", "Per oggi è tutto...", "Per oggi è tutto"),
    ("hu", "Az Európai Bizottság felfüggesztette a kifizetést.", "Az Európai Bizottság felfüggesztette a kifizetést"),
    ("it", "Davvero?", "Davvero?"),
    ("it", "Basta!!!", "Basta!"),
    ("it", "Ma come?!", "Ma come?"),
    ("hu", "Igen,", "Igen"),
    ("hu", ", igen", "igen"),
    # addresses are never read
    ("hu", "Írta a Telex (https://telex.hu/belfold/2026/11/12/x), ma.", "Írta a Telex, ma"),
    ("hu", "Lásd www.hvg.hu oldalon.", "Lásd Hvg oldalon"),
    ("it", "Scrivi a redazione@example.com subito", "Scrivi a subito"),
    ("en", "See https://www.reuters.com/world/x for more", "See Reuters for more"),
    ("hu", "A forrás: 444.hu/cikk/1", "A forrás, négyszáznegyvennégy"),
    # emoji and emoticons
    ("it", "Che bello 😀 davvero :) ;) <3", "Che bello davvero"),
    ("hu", "Köszönöm 🇭🇺 ❤️", "Köszönöm"),
    # markdown and markup
    ("it", "**Attenzione**: il `ministro` > ha detto", "Attenzione, il ministro ha detto"),
    ("it", "# Titolo\n- primo\n- secondo", "Titolo, primo, secondo"),
    ("en", "A [link](https://example.org/x) and <b>bold</b> text", "A link and bold text"),
    # quotes of every kind, apostrophes inside words stay
    ("hu", "Azt mondta: „Nincs pénz” és »igen«.", "Azt mondta, Nincs pénz és igen"),
    ("it", "Disse «basta» e “fine” dell'Istruzione", "Disse basta e fine dell'Istruzione"),
    ("en", "It's Smith's \"plan\"", "It's Smith's plan"),
    # brackets are asides; dashes and ellipses are pauses; colons and semicolons are commas
    ("it", "Il ministro (ieri) ha parlato", "Il ministro, ieri, ha parlato"),
    ("it", "Il piano - dicono - è fatto", "Il piano, dicono, è fatto"),
    ("it", "Prima ... poi", "Prima, poi"),
    ("hu", "Három ok: egy; kettő", "Három ok, egy, kettő"),
    ("hu", "Délután 12:30 órakor", "Délután tizenkettő harminc órakor"),
    ("hu", "Reggel 8:00", "Reggel nyolc óra"),
    ("hu", "Elnök – szerint", "Elnök, szerint"),
    # slashes, ampersands, percent, currency
    ("hu", "A NATO/EU vezetők", "A Nato vagy é ú vezetők"),
    ("it", "Rossi & Bianchi", "Rossi e Bianchi"),
    ("hu", "4,5%-a és 12 €-t", "négy egész öt tized százaléka és tizenkettő euró"),
    ("hu", "4,5%-kal csökkent, 12%-os", "négy egész öt tized százalékkal csökkent, tizenkettő százalékos"),
    ("hu", "19%-a, 4,25 százalékkal", "tizenkilenc százaléka, négy egész huszonöt század százalékkal"),
    ("it", "il 25% dei €100", "il 25 per cento dei 100 euro"),
    ("en", "about $5 and 3%", "about 5 dollars and 3 percent"),
    # numbers and dates
    ("hu", "3.000 fős tüntetés", "háromezer fős tüntetés"),
    ("hu", "2026-11-12-én", "kétezer-huszonhat november tizenkettedikén"),
    ("hu", "2026. november 1-jén és november 3.", "kétezer-huszonhat november elsején és november harmadik"),
    ("hu", "394 milliárd, 1002 fő, 12 hónap", "háromszázkilencvennégy milliárd, ezerkettő fő, tizenkettő hónap"),
    ("it", "il 12/11/2026 alle", "il 12 novembre 2026 alle"),
    ("en", "on 12.11.2026 we", "on 12 November 2026 we"),
    # abbreviations and initials
    ("hu", "Dr. Kovács, pl. a Fidesz, kb. háromszáz fő, stb.", "doktor Kovács, például a Fidesz, körülbelül háromszáz fő, és így tovább"),
    ("it", "Il sig. Rossi, ecc.", "Il signor Rossi, eccetera"),
    ("en", "Mr. Smith, e.g. a vote", "mister Smith, for example a vote"),
    ("it", "J. K. Rowling scrive", "J K Rowling scrive"),
    # acronyms: a word when the language says so, else letter by letter
    ("it", "La BCE e la NATO", "La bi ci e e la Nato"),
    ("hu", "Az EU döntött", "Az é ú döntött"),
    # repeated names, stray symbols, line breaks, spaces before marks
    ("hu", "Telex (https://telex.hu)", "Telex"),
    ("it", "Prima§ seconda ×  terza", "Prima paragrafo seconda terza"),
    ("it", "Una riga\nun'altra  riga ,  ok", "Una riga un'altra riga, ok"),
]


@pytest.mark.parametrize("lang,text,want", CASES)
def test_the_text_given_to_the_voice(lang, text, want):
    assert textnorm.normalise(text, lang) == want


def test_a_pronunciation_list_goes_first_and_whole_words_only():
    text = "Ursula von der Leyen è a Bruxelles, non a Von der Leyenburg."
    got = textnorm.normalise(text, "it", {"von der Leyen": "fon der Lajen", "Bruxelles": "Brussel"})
    assert got == "Ursula fon der Lajen è a Brussel, non a Von der Leyenburg"


def test_every_rule_of_the_table_is_documented_and_named_once():
    ids = [r[0] for r in textnorm.RULES]
    assert len(ids) == len(set(ids)) and all(r[1] and callable(r[2]) for r in textnorm.RULES)


def test_the_normaliser_leaves_clean_text_alone_and_is_idempotent():
    for lang, text, _ in CASES:
        once = textnorm.normalise(text, lang)
        assert textnorm.normalise(once, lang) == once, (lang, text, once)
    assert textnorm.normalise("Una frase semplice, con una virgola", "it") == "Una frase semplice, con una virgola"


# ---- the pieces
def test_the_pieces_given_to_the_engine_never_end_in_a_full_stop():
    text = "Prima frase abbastanza lunga da riempire il pezzo. " * 8 + "Fine."
    parts = [textnorm.normalise(p, "it") for p in speech.pieces(text)]
    assert len(parts) >= 2 and all(p and not p.endswith((".", ",", "…")) for p in parts)


# ---- reading the voice back
def words(text, step=0.3):
    return [{"text": " " + w, "start_s": round(i * step, 2), "end_s": round(i * step + step - 0.05, 2)} for i, w in enumerate(text.split())]


def test_a_transcript_equal_to_the_text_is_fine_whatever_the_case_the_accents_and_the_marks():
    c = voicecheck.compare("Il ministro è qui, davvero.", words("il ministro e qui davvero"))
    assert c.ok and c.similarity == 1.0 and not c.extra_tail and c.missing == 0


def test_digits_are_not_compared_and_a_different_word_is():
    assert voicecheck.compare("Sono 394 milliardi", words("sono milliardi")).ok
    bad = voicecheck.compare("Domani le lezioni riprendono regolarmente", words("domani le elezioni riprendono"))
    assert not bad.ok and bad.missing >= 1


def test_sounds_after_the_last_written_word_are_found_and_the_time_to_cut_at_is_given():
    c = voicecheck.compare("Domani le lezioni riprendono regolarmente", words("domani le lezioni riprendono regolarmente punto tale"))
    assert not c.ok and c.extra_tail == ["punto", "tale"] and c.trim_at_s == pytest.approx(4 * 0.3 + 0.25)
    assert voicecheck.compare("a b c", words("a b c")).trim_at_s is None
    assert not voicecheck.compare("Per oggi è tutto", words("Per oggi è tutto. it")).ok


def wav(seconds):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(1000)
        w.writeframes(b"\x01\x02" * int(1000 * seconds))
    return out.getvalue()


class Voice:
    """A voice service that reads what it is given, and a speech-to-text that hears what the test scripts: a list of transcripts, one for each reading."""

    def __init__(self, heard):
        self.heard, self.asked, self.reads = list(heard), [], 0
        voice = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/tts/test":
                    data = json.loads(body)
                    voice.asked.append(data["text"])
                    out = {"status": "ok", "audio_b64": base64.b64encode(wav(3.0)).decode()}
                else:
                    voice.reads += 1
                    h = voice.heard.pop(0) if voice.heard else voice.asked[-1]
                    out = {"segments": [{"words": words(h)}]}
                raw = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_a_tail_the_engine_added_is_cut_off_and_a_misread_piece_is_read_again():
    v = Voice(["domani le lezioni riprendono regolarmente punto tale"])
    try:
        out = speech.speak_checked(v.url, "Domani le lezioni riprendono regolarmente.", "xtts", "v", "it")
    finally:
        v.stop()
    entry = out.report[0]
    assert v.asked == ["Domani le lezioni riprendono regolarmente"] and entry["cut_tail"] == ["punto", "tale"] and entry["ok"] and entry["tries"] == 1
    assert speech.seconds(out.wav) == pytest.approx(5 * 0.3 + 0.12 - 0.05, abs=0.1)
    v = Voice(["domani le elezioni riprendono", "domani le lezioni riprendono regolarmente"])
    try:
        out = speech.speak_checked(v.url, "Domani le lezioni riprendono regolarmente.", "xtts", "v", "it")
    finally:
        v.stop()
    assert out.report[0]["tries"] == 2 and out.report[0]["ok"] and len(v.asked) == 2


def test_a_piece_that_never_matches_is_kept_with_its_flag_after_the_tries():
    v = Voice(["una cosa tutta diversa", "un'altra cosa", "ancora altro"])
    try:
        out = speech.speak_checked(v.url, "Domani le lezioni riprendono regolarmente", "xtts", "v", "it")
    finally:
        v.stop()
    assert out.report[0]["tries"] == speech.TRIES and out.report[0]["ok"] is False and out.report[0]["heard"] and len(out.wav) > 100


def test_without_a_speech_to_text_the_voice_is_used_unchecked_and_says_so(monkeypatch):
    v = Voice([])
    monkeypatch.setattr(voicecheck, "transcribe", lambda *a, **k: (_ for _ in ()).throw(OSError("the speech-to-text does not answer")))
    try:
        out = speech.speak_checked(v.url, "Ciao a tutti", "xtts", "v", "it")
        unchecked = speech.speak_checked(v.url, "Ciao a tutti", "xtts", "v", "it", check=False)
    finally:
        v.stop()
    assert out.report[0]["checked"] is False and "does not answer" in out.report[0]["note"] and speech.seconds(out.wav) == pytest.approx(3.0)
    assert unchecked.report[0] == {"text": "Ciao a tutti", "tries": 1}


def test_the_pieces_of_a_number_or_a_date_in_a_transcript_are_spelled_together():
    words = [{"text": t, "start_s": i, "end_s": i + 1} for i, t in enumerate(["Az", "EU", "4", ",5", "%", "-kal", "csökkentette", "november", "12", "-én"])]
    check = voicecheck.compare("Az é ú négy egész öt tized százalékkal csökkentette november tizenkettedikén", words, "hu")
    assert check.missing == 0 and check.similarity == 1.0


@pytest.mark.parametrize("written,spoken", [
    ("a 2028-2034-es költségvetés", "a kétezer-huszonnyolc és kétezer-harmincnégy közötti költségvetés"),
    ("Kijev 2027-es hiánya", "Kijev kétezer-huszonhetes hiánya"),
    ("2027-ben választások", "kétezer-huszonhétben választások"),
    ("Az 1375-ös kormányhatározat", "Az ezerháromszázhetvenötös kormányhatározat"),
    ("a 4. és 5. klaszter, a 312. cikk", "a negyedik és ötödik klaszter, a háromszáztizenkettedik cikk"),
    ("2026-ig és 2026-tól", "kétezer-huszonhatig és kétezer-huszonhattól"),
    ("Végén 5. Utána jön", "Végén öt. Utána jön"),
])
def test_hungarian_ordinals_and_numbers_with_an_ending_are_said_in_words(written, spoken):
    assert textnorm.normalise(written, "hu") == spoken
