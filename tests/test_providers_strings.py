"""The Providers page: its new strings in English and Italian, and what they say."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "serve" / "web"


def test_the_new_strings_exist_in_english_and_italian_and_say_what_was_asked():
    i18n = (WEB / "i18n.js").read_text(encoding="utf-8")
    source = (WEB / "providers.js").read_text(encoding="utf-8") + (WEB / "answering.js").read_text(encoding="utf-8")
    used = set(re.findall(r'T\("((?:pv|hd)\.[A-Za-z._]+)"', source)) | {"pv.primary", "pv.primary.help", "pv.primary.local", "pv.primary.option", "pv.local",
                                                                  "pv.local.help", "pv.f.load", "pv.f.load.hint", "pv.f.test", "pv.loaded", "hd.answers", "hd.answers.fell"}
    for key in used:
        if key.endswith("."):
            continue
        assert i18n.count(f'"{key}":') == 2, key                                  # once in English and once in Italian
    en, it = i18n.split('"pv.local":')[1].split(",")[0], i18n.split('"pv.local":')[2].split(",")[0]
    assert "From Local Engine" in en and "Dal motore locale" in it
    assert '"pv.f.load.hint": "To load the models, enter the address and API key"' in i18n
    assert '"pv.f.load.hint": "Per caricare i modelli inserisci indirizzo e API Key"' in i18n
    assert '"pv.f.load": "Load"' in i18n and '"pv.f.load": "Carica"' in i18n
    assert '"pv.f.test": "Test"' in i18n and '"pv.f.test": "Prova"' in i18n
    assert '"pv.primary": "Main model"' in i18n and '"pv.primary": "Modello primario"' in i18n
    assert "loaded in local engine" in i18n.lower() and "caricato nel motore locale" in i18n.lower()
    for engine in ("Basalt", "Strata", "llama.cpp", "LM Studio", "vLLM"):
        assert engine in i18n.split('"pv.local.help":')[1].split("\n")[0]
    assert "innerHTML" not in source
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="model-now"' in html and 'src="web/answering.js"' in html
