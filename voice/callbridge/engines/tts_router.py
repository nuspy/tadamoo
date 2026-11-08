"""Router TTS: smista la sintesi fra tutti i motori del catalogo, esponendo la
STESSA interfaccia di `KokoroTtsEngine` (`model_dir`, `load()`, `synth()`,
`close()`).

Cosi' i chiamanti (`handy_compat` /tts/test e /tts/stream, `NativeTtsClient`,
l'avatar) restano invariati: chiedono `synth(text, voice, lang, speed)`.

Come si sceglie il motore, in ordine:
  1. `engine` esplicito passato a `synth()` — quello che la GUI manda quando
     l'utente ha scelto modello e voce nei due dropdown;
  2. altrimenti si deduce dalla voce, come si e' sempre fatto: voce clonata ->
     motore di cloning corrente, voce Piper -> Piper, il resto -> Kokoro.

Il punto 2 resta perche' le voci salvate nelle persone e nella config sono nomi
nudi: senza deduzione, ogni configurazione esistente smetterebbe di funzionare.

I motori pesanti (Chatterbox, XTTS, CosyVoice, Orpheus, Dia) girano come
subprocess in venv separati e vengono creati PIGRAMENTE, al primo uso: chi usa
solo Kokoro non paga nulla.

I pesi si leggono solo da `<models folder>/tts/<id modello>/` e le voci clonate
stanno in `<models folder>/tts/voices/`. Un motore il cui modello manca solleva
`models_store.ModelNotInstalled` (messaggio in parole semplici): non si ripiega su
un altro motore e non si scarica nulla.

`attiva()` implementa lo **hot swap**: cambiare motore dalla GUI scarica dalla
VRAM quello precedente e scalda subito il nuovo. Senza, ogni motore provato
restava caldo per sempre e la VRAM si riempiva finche' tutto finiva in shared
memory (dove la sintesi rallenta di ordini di grandezza).
"""

from __future__ import annotations

import logging
import os
import threading
import time

from .. import models_store
from ..sospensione import in_sospensione, richiedi_attivi
from . import catalog
from .chatterbox_tts import ChatterboxTtsEngine
from .kokoro_tts import KokoroTtsEngine
from .piper_tts import PiperTtsEngine, _default_piper_dir
from .venv_tts import VenvTtsEngine

log = logging.getLogger("callbridge.engines.tts_router")

_CLONE_EXTS = (".wav", ".mp3", ".flac", ".ogg")


def _piper_dir(cfg, model_dir: str) -> str:
    return getattr(cfg, "piper_model_dir", "") or _default_piper_dir(model_dir)


def _nuovo_chatterbox(cfg) -> ChatterboxTtsEngine:
    """Oggetto Chatterbox (leggero: non avvia nulla fino a `load`/`synth`)."""
    return ChatterboxTtsEngine(
        getattr(cfg, "chatterbox_venv_python", ""),
        getattr(cfg, "chatterbox_helper", ""),
        getattr(cfg, "chatterbox_device", "cuda"),
        getattr(cfg, "chatterbox_gpu_memory", "dedicated"))


def disponibile_senza_router(cfg, s) -> bool:
    """`catalog.disponibile` per chi NON ha un `TtsRouter`: stesse regole, ma si
    guardano solo i file su disco. Non crea il router, non carica motori, non
    importa nulla di pesante. Serve a mostrare l'elenco dei motori anche da
    sospeso (vedi `engines.catalogo_motori`)."""
    from . import catalog
    from .kokoro_tts import _default_kokoro_dir
    model_dir = (getattr(cfg, "kokoro_model_dir", "") or _default_kokoro_dir())
    if s.id == "piper":
        if not bool(getattr(cfg, "piper_enabled", True)):
            return False
        return PiperTtsEngine(_piper_dir(cfg, model_dir)).available
    if s.id == "chatterbox":
        if not bool(getattr(cfg, "clone_tts_enabled", True)):
            return False
        return (not catalog.modello_mancante(cfg, s)
                and _nuovo_chatterbox(cfg).available)
    return catalog.disponibile(cfg, s, None)


class TtsRouter:
    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self._kokoro = KokoroTtsEngine(
            cfg.kokoro_model_dir,
            getattr(cfg, "tts_device", "cpu"),
            getattr(cfg, "tts_gpu_memory", "dedicated"),
            getattr(cfg, "kokoro_precision", "fp16"))
        self.model_dir = self._kokoro.model_dir
        self._clone_enabled = bool(getattr(cfg, "clone_tts_enabled", True))
        self._chatterbox: ChatterboxTtsEngine | None = None
        # Piper: engine per le lingue assenti da Kokoro (Ungherese, ...). Le voci
        # Piper hanno nomi propri (es. hu_HU-anna-medium), distinti dalle Kokoro.
        self._piper_enabled = bool(getattr(cfg, "piper_enabled", True))
        self._piper = PiperTtsEngine(
            _piper_dir(cfg, self.model_dir),
            getattr(cfg, "piper_device", "cpu"),
            getattr(cfg, "piper_gpu_memory", "dedicated")) \
            if self._piper_enabled else None
        # Motori in venv separato (XTTS, CosyVoice, Orpheus, Dia): istanziati
        # al primo uso, uno per id.
        self._venv_engines: dict[str, VenvTtsEngine] = {}
        self._lock = threading.Lock()
        # Hot swap: motore attivo e stato del precaricamento, per motore.
        # Scritture/letture di dizionari sono atomiche sotto GIL: niente lock,
        # cosi' `stato_motori()` (polling della GUI) non si blocca mai dietro
        # un warm-up che sta caricando gigabyte.
        self._attivo = (getattr(cfg, "tts_engine", "") or "kokoro").strip().lower()
        self._stato: dict[str, dict] = {}

    # --- motori in venv, pigri --------------------------------------------

    def _get_venv_engine(self, engine_id: str) -> VenvTtsEngine | None:
        s = catalog.spec(engine_id)
        if s is None or not s.venv:
            return None
        with self._lock:
            eng = self._venv_engines.get(engine_id)
            if eng is None:
                eng = VenvTtsEngine(s, self._cfg)
                self._venv_engines[engine_id] = eng
            return eng

    def _venv_disponibile(self, engine_id: str) -> bool:
        """Il motore e' di tipo venv? Controllo a costo zero, senza istanziarlo.

        Serve a decidere se liberare la memoria PRIMA di crearlo: chiedere a
        `_get_venv_engine` lo istanzierebbe (e avvierebbe l'helper) proprio
        nell'istante in cui il vecchio motore e' ancora dentro."""
        s = catalog.spec(engine_id)
        return s is not None and bool(s.venv)

    def engine_available(self, engine_id: str) -> bool:
        """Il motore puo' sintetizzare adesso? (per /tts/models is_downloaded)"""
        return catalog.disponibile(self._cfg, catalog.spec(engine_id), self) \
            if catalog.spec(engine_id) else False

    def catalogo(self) -> list[dict]:
        """Catalogo completo con voci e disponibilita' reale, per la GUI."""
        return catalog.elenca(self._cfg, self)

    def voci_di(self, engine_id: str) -> list[dict]:
        s = catalog.spec(engine_id)
        return catalog.voci(self._cfg, s, self) if s is not None else []

    def motore_di_voce(self, voice: str) -> str:
        """Motore a cui appartiene `voice` ("" se nessuno la conosce).

        Stessa deduzione di `synth` quando il motore non e' indicato, ma
        risolta sul catalogo: serve a sapere CHI parlera' prima di parlare."""
        voice = (voice or "").strip()
        if not voice:
            return ""
        # Il motore ATTIVO ha la precedenza quando conosce questa voce: le voci
        # clonate appartengono a piu' motori di cloning insieme, e attribuirle
        # sempre a Chatterbox direbbe le lingue sbagliate quando a parlare e'
        # un altro (CosyVoice e Chatterbox non coprono le stesse).
        attivo = self._attivo
        if attivo and any(v.get("voice") == voice for v in self.voci_di(attivo)):
            return attivo
        if self.is_cloned(voice):
            return "chatterbox"
        if self.is_piper(voice):
            return "piper"
        for s in catalog.SPECS:
            if any(v.get("voice") == voice for v in self.voci_di(s.id)):
                return s.id
        return "kokoro"

    def lingue_di(self, engine_id: str) -> list[str]:
        """Lingue dichiarate dal motore ([] = non dichiara vincoli)."""
        s = catalog.spec(engine_id)
        return list(s.langs) if s is not None and s.langs else []

    def parla_lingua(self, engine_id: str, lang: str) -> bool:
        """Il motore sa pronunciare questa lingua?

        Chi non dichiara nulla (Piper: dipende dalle voci installate) si
        considera capace: meglio provare che vietare a torto."""
        lingue = self.lingue_di(engine_id)
        if not lingue:
            return True
        return (lang or "").lower().replace("_", "-").split("-")[0] in lingue

    # --- hot swap del motore attivo ---------------------------------------

    def _engine_obj(self, engine_id: str):
        """Engine gia' istanziato, SENZA crearne di nuovi (a differenza dei
        `_get_*`): serve a sapere cosa e' in memoria, non a caricarlo."""
        if engine_id == "kokoro":
            return self._kokoro
        if engine_id == "piper":
            return self._piper
        if engine_id == "chatterbox":
            return self._chatterbox
        return self._venv_engines.get(engine_id)

    def _caricato(self, engine_id: str) -> bool:
        """Il motore occupa memoria ADESSO."""
        o = self._engine_obj(engine_id)
        if o is None:
            return False
        if engine_id == "piper":
            return bool(getattr(o, "_voices", None))
        # Kokoro su CUDA gira in un helper (`_proc`), su CPU in-process (`_k`):
        # guardare un solo attributo lo darebbe per scarico meta' delle volte.
        proc = getattr(o, "_proc", None)
        if proc is not None and proc.poll() is None:
            return True
        return getattr(o, "_k", None) is not None

    def _set_stato(self, engine_id: str, state: str, message: str = "",
                   seconds: float = 0.0) -> None:
        self._stato[engine_id] = {"state": state, "message": message,
                                  "seconds": round(seconds, 2)}

    def stato_motori(self) -> dict[str, dict]:
        """Stato di ciascun motore del catalogo, per la GUI.

        `state`: "ready" (in memoria) | "loading" (warm-up in corso) |
        "unloaded" | "error". Lo stato registrato da `attiva()` vale solo
        finche' concorda con la realta': un motore caricato pigramente dalla
        prima sintesi risulta "ready" anche se nessuno lo ha mai attivato.
        """
        out: dict[str, dict] = {}
        for s in catalog.SPECS:
            reg = self._stato.get(s.id) or {}
            state = reg.get("state") or ""
            reale = self._caricato(s.id)
            if state != "loading":
                if reale:
                    state = "ready"
                elif state != "error":
                    state = "unloaded"
            out[s.id] = {
                "state": state,
                "message": reg.get("message", ""),
                "seconds": reg.get("seconds", 0.0),
                "active": s.id == self._attivo,
                "available": self.engine_available(s.id),
            }
        return out

    @property
    def attivo(self) -> str:
        return self._attivo

    def attiva(self, engine_id: str, precarica: bool = True) -> dict:
        """Rende `engine_id` il motore attivo: scarica gli altri dalla memoria e
        (in background) scalda questo.

        Il warm-up e' asincrono perche' il primo caricamento dei modelli grossi
        richiede decine di secondi: la GUI riceve subito l'esito dello scarico e
        segue il resto con `stato_motori()`.
        """
        s = catalog.spec(engine_id)
        if s is None:
            raise ValueError(f"motore sconosciuto: {engine_id}")
        if in_sospensione():
            # Da sospeso la scelta del motore si SALVA soltanto: niente warm-up.
            precarica = False
        precedente = self._attivo
        self._attivo = engine_id
        scaricati = self._scarica(tranne=engine_id)
        if not precarica:
            return {"selected": engine_id, "previous": precedente,
                    "unloaded": scaricati, "warming": False}
        if not catalog.disponibile(self._cfg, s, self):
            perche = (catalog.modello_mancante(self._cfg, s)
                      or "motore non installato (venv/helper mancanti)")
            self._set_stato(engine_id, "error", perche)
            return {"selected": engine_id, "previous": precedente,
                    "unloaded": scaricati, "warming": False,
                    "message": perche}
        self._set_stato(engine_id, "loading")
        threading.Thread(target=self._precarica, args=(engine_id,), daemon=True,
                         name=f"tts-warm-{engine_id}").start()
        return {"selected": engine_id, "previous": precedente,
                "unloaded": scaricati, "warming": True}

    def scarica_tutti(self) -> list[str]:
        """Libera la memoria di TUTTI i motori TTS (bottone "scarica" della GUI)."""
        return self._scarica(tranne="")

    def _scarica(self, tranne: str) -> list[str]:
        """Chiude ogni motore diverso da `tranne` e restituisce quelli che erano
        davvero in memoria."""
        scaricati: list[str] = []
        with self._lock:
            for eid, eng in list(self._venv_engines.items()):
                if eid == tranne:
                    continue
                if self._caricato(eid):
                    scaricati.append(eid)
                try:
                    eng.close()
                except Exception:  # noqa: BLE001
                    log.warning("chiusura di %s fallita", eid, exc_info=True)
                self._venv_engines.pop(eid, None)
                self._set_stato(eid, "unloaded")
            if tranne != "chatterbox" and self._chatterbox is not None:
                if self._caricato("chatterbox"):
                    scaricati.append("chatterbox")
                try:
                    self._chatterbox.close()
                except Exception:  # noqa: BLE001
                    log.warning("chiusura di chatterbox fallita", exc_info=True)
                self._chatterbox = None
                self._set_stato("chatterbox", "unloaded")
            if tranne != "kokoro" and self._caricato("kokoro"):
                self._kokoro.close()
                scaricati.append("kokoro")
                self._set_stato("kokoro", "unloaded")
            if tranne != "piper" and self._caricato("piper"):
                self._piper._voices.clear()
                scaricati.append("piper")
                self._set_stato("piper", "unloaded")
        if scaricati:
            # Chiudere l'oggetto non basta: onnxruntime e il caching allocator di
            # torch trattengono la VRAM finche' non si forza il rilascio.
            try:
                from . import _release_gpu_memory
                _release_gpu_memory()
            except Exception:  # noqa: BLE001
                pass
            log.info("TTS: scaricati dalla memoria %s", ", ".join(scaricati))
        return scaricati

    def _voce_di_prova(self, engine_id: str) -> str:
        """Voce da usare per il warm-up: la prima del motore, preferendo quella
        gia' scelta dall'utente se questo motore ce l'ha."""
        voci = self.voci_di(engine_id)
        if not voci:
            return ""
        preferita = (getattr(self._cfg, "default_voice", "") or "").strip()
        for v in voci:
            if v.get("voice") == preferita:
                return preferita
        return str(voci[0].get("voice") or "")

    def _precarica(self, engine_id: str) -> None:
        """Carica davvero il modello in memoria sintetizzando una frase corta.

        Avviare il subprocess non basta: gli helper caricano i pesi alla PRIMA
        sintesi, quindi senza questa frase il primo turno di conversazione
        pagherebbe comunque l'attesa completa.
        """
        t0 = time.perf_counter()
        try:
            s = catalog.spec(engine_id)
            voce = self._voce_di_prova(engine_id)
            lingua = (getattr(self._cfg, "default_lang", "") or "it").lower()[:2]
            if s is not None and s.langs and lingua not in s.langs:
                lingua = s.langs[0]
            testo = "Pronto." if lingua == "it" else "Ready."
            if not voce:
                # Motore a voci clonate senza nessuna voce clonata: si avvia solo
                # il processo, i pesi restano fuori dalla memoria.
                eng = self._get_venv_engine(engine_id)
                if eng is not None:
                    eng.load()
                self._set_stato(engine_id, "unloaded",
                                "nessuna voce disponibile: modello non caricato",
                                time.perf_counter() - t0)
                return
            if engine_id == "kokoro":
                self._kokoro.load()
                self._kokoro.synth(testo, voce, lingua, 1.0)
            elif engine_id == "piper" and self._piper is not None:
                self._piper.synth(testo, voce, lingua, 1.0)
            elif engine_id == "chatterbox":
                self._get_chatterbox().synth(testo, voce, lingua, 1.0)
            else:
                eng = self._get_venv_engine(engine_id)
                if eng is None:
                    raise RuntimeError("motore senza helper")
                eng.synth(testo, voce, lingua, 1.0)
            dt = time.perf_counter() - t0
            self._set_stato(engine_id, "ready", "", dt)
            log.info("TTS: motore '%s' pronto in %.1fs (voce di prova: %s)",
                     engine_id, dt, voce)
        except Exception as e:  # noqa: BLE001
            self._set_stato(engine_id, "error", str(e), time.perf_counter() - t0)
            log.error("TTS: precaricamento di '%s' fallito: %s", engine_id, e)

    # --- Chatterbox pigro -------------------------------------------------

    def _get_chatterbox(self) -> ChatterboxTtsEngine:
        with self._lock:
            if self._chatterbox is None:
                self._chatterbox = _nuovo_chatterbox(self._cfg)
            return self._chatterbox

    @property
    def clone_available(self) -> bool:
        """True se il clone (venv + helper) e' disponibile su disco."""
        if not self._clone_enabled:
            return False
        try:
            return self._get_chatterbox().available
        except Exception:  # noqa: BLE001
            return False

    def _cloned_ref(self, voice: str) -> str | None:
        """Path del wav di riferimento se `voice` e' una voce clonata, altrimenti None."""
        if not voice:
            return None
        d = models_store.voices_dir()
        if not d or os.path.basename(voice) != voice:
            return None
        for ext in _CLONE_EXTS:
            cand = os.path.join(d, voice + ext)
            if os.path.isfile(cand):
                return cand
        return None

    def is_cloned(self, voice: str) -> bool:
        return self._clone_enabled and self._cloned_ref(voice) is not None

    def is_piper(self, voice: str) -> bool:
        return self._piper is not None and self._piper.has_voice(voice)

    @property
    def piper_available(self) -> bool:
        return self._piper is not None and self._piper.available

    def piper_voices(self) -> list[str]:
        return self._piper.list_voices() if self._piper is not None else []

    # --- interfaccia KokoroTtsEngine -------------------------------------

    def load(self) -> None:
        """Scalda il motore ATTIVO, non Kokoro d'ufficio.

        Caricare Kokoro qui era un residuo di quando era l'unico motore, e
        rientrava dalla finestra: il pre-warm all'avvio del server e ogni
        `/models/load` lo riportavano in VRAM anche quando l'attivo era XTTS,
        lasciandone due dentro insieme. Passa da `_solo_questo` perche' la
        regola vale anche qui: chi entra sfratta chi non serve piu'.
        """
        richiedi_attivi()
        attivo = (self._attivo or "").strip().lower()
        if attivo and attivo != "kokoro":
            self._solo_questo(attivo)
            eng = self._engine_obj(attivo) or self._get_venv_engine(attivo)
            if eng is not None and hasattr(eng, "load"):
                eng.load()
                return
            # Motore attivo non caricabile: meglio Kokoro caldo che niente.
            log.warning("TTS: motore attivo '%s' non scaldabile, uso kokoro", attivo)
        self._solo_questo("kokoro")
        self._kokoro.load()

    def synth(self, text: str, voice: str, lang: str, speed: float,
              engine: str = ""):
        richiedi_attivi()
        # Numeri/unita'/sigle in forma pronunciabile: qui perche' e' il punto di
        # passaggio COMUNE a tutti i motori e a tutti i chiamanti (sessioni
        # vocali, /tts/test, avatar). E' idempotente, quindi non da' fastidio se
        # il testo e' gia' stato normalizzato a monte (nel worker, dove va fatto
        # PRIMA dello split sulle virgole: "3,5 ms" non deve spezzarsi).
        if (lang or "").lower().startswith("it") and getattr(
                self._cfg, "tts_normalize_text", True):
            from ..text_normalize import normalizza_per_tts
            text = normalizza_per_tts(text)
        eng = (engine or "").strip().lower()
        if not eng and not (voice or "").strip():
            # Senza motore e senza voce vale il motore scelto nelle impostazioni.
            eng = self._attivo
        if not eng:
            # Motore non indicato: lo decide l'AVATAR ATTIVO, che e' l'unica
            # scelta di motore rimasta (non c'e' piu' un motore "di sistema"
            # scelto a mano in Voice Engine). Serve perche' la deduzione dal
            # nome della voce, qui sotto, conosce solo clonate/Piper/Kokoro:
            # uno speaker XTTS come "Chandra MacFarland" finiva a Kokoro, che
            # rispondeva "Voice not found" (misurato su /tts/test).
            eng = self._engine_avatar_attivo(voice)
        if eng:
            return self._synth_con(eng, text, voice, lang, speed)
        # Nessun avatar e nessun motore: si deduce dalla voce, come si e' sempre
        # fatto. Clonata -> Chatterbox, Piper -> Piper, il resto -> Kokoro.
        # Anche qui vale la regola della VRAM: il motore che serve entra da solo
        # in memoria, quindi gli altri devono uscire.
        if self.is_cloned(voice):
            self._solo_questo("chatterbox")
            return self._get_chatterbox().synth(text, voice, lang, speed)
        if self.is_piper(voice):
            self._solo_questo("piper")
            return self._piper.synth(text, voice, lang, speed)
        # Voci proprie di un motore del catalogo (es. "ryan" di Qwen3-TTS, gli
        # speaker di serie di XTTS): finivano a Kokoro, che non le conosce.
        m = self.motore_di_voce(voice)
        if m and m not in ("kokoro", "piper", "chatterbox"):
            # La voce e' di un motore il cui modello puo' mancare: lo si dice,
            # invece di farla leggere a Kokoro che non la conosce.
            self._richiedi_modello(m)
            # Solo se disponibile: `_synth_con` ripiega richiamando `synth`, e con
            # un motore assente si girerebbe in tondo.
            if self.engine_available(m):
                return self._synth_con(m, text, voice, lang, speed)
        self._solo_questo("kokoro")
        return self._kokoro.synth(text, voice, lang, speed)

    # --- streaming ---------------------------------------------------------

    def _motore_per(self, voice: str, engine: str = "") -> str:
        """Il motore che pronuncera' `voice`, con le stesse precedenze di
        `synth`: esplicito, poi avatar attivo, poi dedotto dalla voce."""
        eng = (engine or "").strip().lower() or self._engine_avatar_attivo(voice)
        return eng or self.motore_di_voce(voice)

    def puo_streammare(self, voice: str, engine: str = "") -> bool:
        """Il motore di questa voce sa consegnare l'audio a pezzi?"""
        s = catalog.spec(self._motore_per(voice, engine))
        return bool(s is not None and s.streaming and s.venv
                    and catalog.disponibile(self._cfg, s, self))

    def synth_stream(self, text: str, voice: str, lang: str, speed: float,
                     engine: str = ""):
        richiedi_attivi()
        """Generatore di (float32 mono, sample_rate) mentre il motore genera.

        Con un motore che non sa fare streaming (o non disponibile) restituisce
        la frase intera in un solo pezzo: chi chiama non deve distinguere.
        """
        eng_id = self._motore_per(voice, engine)
        s = catalog.spec(eng_id)
        if (s is not None and s.streaming and self._venv_disponibile(eng_id)
                and catalog.disponibile(self._cfg, s, self)):
            if (lang or "").lower().startswith("it") and getattr(
                    self._cfg, "tts_normalize_text", True):
                from ..text_normalize import normalizza_per_tts
                text = normalizza_per_tts(text)
            from .vram import caricamento
            self._solo_questo(eng_id)
            eng = self._get_venv_engine(eng_id)
            if eng is not None and eng.available:
                with caricamento(f"tts:{eng_id}"):
                    yield from eng.synth_stream(text, voice, lang, speed)
                return
        yield self.synth(text, voice, lang, speed, engine)

    def voce_avatar_attivo(self) -> dict:
        """{"engine", "voice"} dell'avatar attivo (`avatar_persona`), {} se non
        c'e'. Legge il manifest da disco: il router non puo' dipendere da
        AvatarClient (vive nel bridge, il router anche nel compat server)."""
        persona = (getattr(self._cfg, "avatar_persona", "") or "").strip()
        if not persona:
            return {}
        try:
            import json as _json
            from pathlib import Path
            p = (Path(getattr(self._cfg, "avatar_dir", ".")) / "persone"
                 / persona / "manifest.json")
            return _json.loads(p.read_text(encoding="utf-8")).get("voice") or {}
        except Exception as e:  # noqa: BLE001 - manifest assente/illeggibile
            log.debug("TTS: voce dell'avatar '%s' non leggibile: %s", persona, e)
            return {}

    def _engine_avatar_attivo(self, voice: str) -> str:
        """Motore dell'avatar attivo, ma SOLO se la voce richiesta e' la sua.

        Con una voce diversa (test di un'altra voce, cambio lingua vocale) il
        motore dell'avatar sarebbe quello sbagliato: meglio lasciar dedurre.
        """
        v = self.voce_avatar_attivo()
        eng = (v.get("engine") or "").strip().lower()
        if not eng:
            return ""
        richiesta = (voice or "").strip().lower()
        sua = (v.get("voice") or "").strip().lower()
        return eng if (not richiesta or richiesta == sua) else ""

    def _richiedi_modello(self, engine_id: str) -> None:
        """`ModelNotInstalled` se il modello del motore non e' nella cartella dei
        modelli (prima di scaricare altri motori o avviare un helper)."""
        s = catalog.spec(engine_id)
        mid = catalog.model_id(self._cfg, s) if s is not None else ""
        if mid:
            models_store.require(mid)

    def _solo_questo(self, engine_id: str) -> None:
        """Lascia in memoria SOLO `engine_id`, scaricando gli altri.

        `attiva()` lo fa quando il motore si cambia dal menu, ma un motore entra
        in memoria anche per via traversa: una sintesi con una voce di un altro
        motore (l'avatar usa la voce della persona, che puo' appartenere a un
        motore diverso da quello attivo). Senza questo si finiva con due motori
        TTS caricati insieme.

        Si interviene solo quando c'e' davvero qualcos'altro dentro: chiamarlo a
        ogni frase non deve costare nulla.
        """
        altri = [s.id for s in catalog.SPECS
                 if s.id != engine_id and self._caricato(s.id)]
        if altri:
            log.info("TTS: '%s' richiesto, libero la memoria di %s",
                     engine_id, ", ".join(altri))
            self._scarica(tranne=engine_id)

    def _synth_con(self, engine_id: str, text: str, voice: str, lang: str,
                   speed: float):
        """Sintesi col motore RICHIESTO. Se non e' disponibile si ripiega sulla
        deduzione dalla voce: meglio una voce diversa che nessun audio in mezzo
        a una conversazione."""
        # Lo scarico degli altri avviene in ogni ramo che sintetizza DAVVERO:
        # metterlo prima svuoterebbe la memoria anche quando poi si ripiega su
        # un altro motore, pagando due caricamenti invece di uno.
        from .vram import caricamento
        if engine_id == "kokoro":
            self._solo_questo(engine_id)
            with caricamento("tts:kokoro"):
                return self._kokoro.synth(text, voice, lang, speed)
        if engine_id == "piper" and self._piper is not None:
            self._solo_questo(engine_id)
            with caricamento("tts:piper"):
                return self._piper.synth(text, voice, lang, speed)
        if engine_id == "chatterbox":
            self._solo_questo(engine_id)
            with caricamento("tts:chatterbox"):
                return self._get_chatterbox().synth(text, voice, lang, speed)
        # Gli altri rami scaricano PRIMA di sintetizzare; qui invece si istanziava
        # il motore nuovo e solo dopo si liberava il vecchio, tenendone due in
        # memoria proprio nell'istante del picco. Si controlla la disponibilita'
        # (che non alloca), poi si libera, poi si carica.
        if self._venv_disponibile(engine_id):
            self._richiedi_modello(engine_id)
            self._solo_questo(engine_id)
            eng = self._get_venv_engine(engine_id)
            if eng is not None and eng.available:
                with caricamento(f"tts:{engine_id}"):
                    return eng.synth(text, voice, lang, speed)
        log.warning("motore '%s' non disponibile: ripiego sulla voce '%s'",
                    engine_id, voice)
        return self.synth(text, voice, lang, speed)

    def usa_cartelle(self, cfg) -> None:
        """Segue le cartelle dei modelli della config (Kokoro e Piper le leggono
        a ogni caricamento; i motori in venv le ricevono all'avvio)."""
        self._kokoro.model_dir = (getattr(cfg, "kokoro_model_dir", "")
                                  or self._kokoro.model_dir)
        self.model_dir = self._kokoro.model_dir
        if self._piper is not None:
            self._piper.model_dir = _piper_dir(cfg, self.model_dir)

    def reconfigure(self, cfg, solo: "set[str] | None" = None) -> None:
        """Applica device/memoria/precisione correnti e scarta i modelli caricati
        (reload pigro). Stesso oggetto: i riferimenti esterni restano validi.

        `solo`: ids dei motori il cui campo e' cambiato. Gli altri NON si
        toccano (restano in memoria cosi' come sono). None = tutti, come prima."""
        self._cfg = cfg

        def tocca(eid: str) -> bool:
            return solo is None or eid in solo

        # Kokoro
        if tocca("kokoro"):
            self._kokoro.device = getattr(cfg, "tts_device", "cpu")
            self._kokoro.memory = getattr(cfg, "tts_gpu_memory", "dedicated")
            self._kokoro.precision = getattr(cfg, "kokoro_precision", "fp16")
            self._kokoro.close()          # chiude anche l'helper GPU, se attivo
        # Piper
        if self._piper is not None and tocca("piper"):
            self._piper.device = getattr(cfg, "piper_device", "cpu")
            self._piper.memory = getattr(cfg, "piper_gpu_memory", "dedicated")
            self._piper._voices.clear()
        # Chatterbox: chiudi il subprocess -> ricreato col nuovo device al prossimo uso
        if self._chatterbox is not None and tocca("chatterbox"):
            try:
                self._chatterbox.close()
            except Exception:  # noqa: BLE001
                pass
            self._chatterbox = None
        # Motori in venv: il device si passa all'avvio, quindi si riavviano solo
        # se e' davvero cambiato (riavviarli tutti costerebbe un reload da GB).
        for eid, eng in list(self._venv_engines.items()):
            if not tocca(eid):
                continue
            try:
                eng.reconfigure(cfg)
            except Exception:  # noqa: BLE001
                log.warning("reconfigure di %s fallito", eid, exc_info=True)
        # Gli stati registrati parlavano dei modelli appena scartati: azzerarli
        # evita che la GUI mostri "pronto" su un motore che va ricaricato.
        if solo is None:
            self._stato.clear()
        else:
            for eid in solo:
                self._stato.pop(eid, None)

    def close(self) -> None:
        if self._chatterbox is not None:
            self._chatterbox.close()
        for eng in list(self._venv_engines.values()):
            try:
                eng.close()
            except Exception:  # noqa: BLE001
                pass
        self._venv_engines.clear()
