"""Server Handy-compatibile servito dai motori NATIVI del bridge.

Replica l'API che Handy esponeva su :8720 (HTTP + WebSocket) usando gli engine
in-process (`callbridge.engines`), cosi' i consumatori esterni che parlavano
con Handy restano INVARIATI. In particolare una skill che usava `kokoro_tts.py`
apre `ws://127.0.0.1:8720/tts/stream` col protocollo config/text/flush/end.

Implementato con **aiohttp** (HTTP + WS sulla stessa porta, implementazione WS
propria — evita l'incompatibilita' uvicorn<->websockets>=16 che dava 403).

Endpoint replicati (stessa forma di Handy):
  WS   /tts/stream   config/text/flush/end -> ready + audio_start + PCM f32 + audio_end
  WS   /stt/stream   PCM f32 16k -> ready + speech_start + utterance{text}
  GET  /health
  GET  /models/status      cosa e' caricato (forma Handy)
  GET  /models/available   voci/lingue TTS + stato STT (forma Handy)
  POST /models/load        {stt_model?, tts?} -> {status: ready}
  POST /models/unload      {component} -> {status: ok}
  POST /tts/test           {text, lang, voice?} -> {status, audio_b64, voice, duration_ms}
  POST /transcribe         multipart file -> {text}

Va avviato SOLO con Handy spento (stessa porta 8720).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import queue
import re
import threading
import time
import wave

import numpy as np

from . import models_store
from .config import Config
from .engines import _get_stt_engine, _get_tts_engine
from .engines import catalog
from .engines.kokoro_tts import NativeTtsClient
from .engines.whisper_stt import NativeSttClient
from .stt_client import STT_RATE

log = logging.getLogger("callbridge.handy_compat")

# Catalogo voci Kokoro (stessa forma di Handy /models/available -> tts.voices).
_KOKORO_VOICES = [
    ("it", "Italiano", "if_sara"), ("it", "Italiano", "im_nicola"),
    ("en-us", "English (US)", "af_heart"), ("en-us", "English (US)", "am_michael"),
    ("en-gb", "English (UK)", "bf_emma"), ("en-gb", "English (UK)", "bm_george"),
    ("fr-fr", "Francais", "ff_siwis"),
    ("es", "Espanol", "ef_dora"), ("es", "Espanol", "em_alex"),
    ("pt-br", "Portugues", "pf_dora"), ("pt-br", "Portugues", "pm_alex"),
    ("ja", "Nihongo", "jf_alpha"), ("ja", "Nihongo", "jm_kumo"),
    ("cmn", "Zhongwen", "zf_xiaoxiao"), ("cmn", "Zhongwen", "zm_yunxi"),
    ("hi", "Hindi", "hf_alpha"), ("hi", "Hindi", "hm_omega"),
]


# Nomi lingua per le voci Piper (prefisso locale es. "hu_HU" -> etichetta GUI).
_PIPER_LANG_NAMES = {
    "hu": "Magyar (Hungarian)", "pl": "Polski (Polish)", "cs": "Cestina (Czech)",
    "ro": "Romana (Romanian)", "sk": "Slovencina (Slovak)", "tr": "Turkce (Turkish)",
    "nl": "Nederlands (Dutch)", "sv": "Svenska (Swedish)", "uk": "Ukrainian",
    "el": "Ellinika (Greek)", "fi": "Suomi (Finnish)", "da": "Dansk (Danish)",
    "no": "Norsk (Norwegian)", "ru": "Russian", "ar": "Arabic",
}


def _piper_voice_entries(tts_engine) -> list[dict]:
    """Voci Piper presenti, forma Handy tts.voices: {lang, language, voice}."""
    voices = getattr(tts_engine, "piper_voices", lambda: [])()
    out = []
    for name in voices:
        # es. "hu_HU-anna-medium" -> lang "hu"
        loc = name.split("-", 1)[0]           # "hu_HU"
        lang = loc.split("_", 1)[0].lower()   # "hu"
        out.append({"lang": lang,
                    "language": _PIPER_LANG_NAMES.get(lang, loc),
                    "voice": name})
    return out


def _pcm_f32_to_wav_bytes(samples: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def _decode_audio(raw: bytes) -> tuple[np.ndarray, int]:
    """Decodifica un file audio (WAV/FLAC/OGG...) in float32 mono + sample rate."""
    import soundfile as sf
    audio, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return np.ascontiguousarray(audio, dtype=np.float32), int(sr)


def _resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out:
        return x
    n = int(round(len(x) * sr_out / sr_in))
    return np.interp(np.linspace(0, 1, n, endpoint=False),
                     np.linspace(0, 1, len(x), endpoint=False), x).astype(np.float32)


_CLONE_EXTS = ("wav", "mp3", "flac", "ogg")
_CLONE_NAME = re.compile(r"^\w[\w .-]{0,63}$")


def _cloned_dir() -> str:
    """Cartella dei campioni delle voci clonate: <cartella modelli>/tts/voices."""
    return models_store.voices_dir()


def _model_missing(e: Exception) -> dict | None:
    """Il corpo di errore di una risposta 409 quando manca un modello, altrimenti None."""
    if isinstance(e, models_store.ModelNotInstalled):
        return {"status": "error", "code": "model_not_installed", "model": e.model, "message": str(e)}
    return None


def _list_cloned() -> list[dict]:
    """Voci clonate presenti su disco (forma Handy: name/format/size/modified)."""
    d = _cloned_dir()
    out = []
    try:
        for fn in sorted(os.listdir(d)):
            ext = os.path.splitext(fn)[1].lstrip(".").lower()
            if ext not in _CLONE_EXTS:
                continue
            p = os.path.join(d, fn)
            st = os.stat(p)
            out.append({"name": os.path.splitext(fn)[0], "format": ext,
                        "size_bytes": st.st_size,
                        "modified": int(st.st_mtime)})
    except OSError:
        pass
    return out


# Id storici usati dalla GUI/config prima del catalogo unico.
_ALIAS_ENGINE = {"kokoro-voices": "kokoro", "kokoro_voices": "kokoro"}


def _engine_id_valido(engine_id: str) -> str:
    e = (engine_id or "").strip().lower()
    return _ALIAS_ENGINE.get(e, e) or "kokoro"


def _stt_stato(stt_engine, stt_loaded: bool) -> dict:
    ri = stt_engine.runtime_info() if hasattr(stt_engine, "runtime_info") else {}
    return {"current_model": ri.get("model") if stt_loaded else None,
            "loaded": stt_loaded, "state": ri.get("state"),
            "device": ri.get("running_on"), "precision": ri.get("precision"),
            "pid": ri.get("pid"), "vram_mb": ri.get("vram_mb")}


def build_app(cfg: Config):
    from aiohttp import web

    tts_engine = _get_tts_engine(cfg)
    stt_engine = _get_stt_engine(cfg)

    # CORS permissivo (come faceva Handy): la GUI Tauri (origine tauri://...)
    # fa fetch cross-origin verso :8720; senza questi header la webview blocca
    # con "Failed to fetch". I WebSocket non passano dal CORS del fetch.
    @web.middleware
    async def _cors(request, handler):
        if request.method == "OPTIONS":
            return web.Response(headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            })
        resp = await handler(request)
        if not isinstance(resp, web.WebSocketResponse):
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    # --- HTTP ------------------------------------------------------------

    async def health(_req):
        return web.json_response({"status": "ok"})

    async def models_status(_req):
        stt_loaded = getattr(stt_engine, "_model", None) is not None
        # Stato per motore (chi occupa memoria, chi sta scaldando). Prima si
        # leggeva `tts_engine._k`, che sul router non esiste: il TTS risultava
        # "non caricato" sempre, anche mentre parlava.
        motori = (tts_engine.stato_motori()
                  if hasattr(tts_engine, "stato_motori") else {})
        attivo = sel["model"]
        st_attivo = motori.get(attivo) or {}
        tts_loaded = st_attivo.get("state") == "ready"
        return web.json_response({
            "idle_seconds": 0, "idle_unload_seconds": 0,
            # L'UNICO STT: modello, device e precisione REALI del processo vivo
            # (non l'intenzione in config), piu' lo stato ("loading" mentre
            # ricarica dopo un cambio).
            "stt": _stt_stato(stt_engine, stt_loaded),
            "tts": {"current_model": attivo if tts_loaded else None,
                    "loaded": tts_loaded, "selected_model": attivo,
                    "state": st_attivo.get("state", "unloaded"),
                    "message": st_attivo.get("message", ""),
                    "load_seconds": st_attivo.get("seconds", 0.0)},
            "engines": motori,
        })

    async def models_available(_req):
        """Voci del motore SELEZIONATO (non piu' il solo catalogo Kokoro).

        Kokoro e Piper restano uniti: sono i due motori builtin fra cui il
        router sceglie da solo in base alla voce, e i chiamanti storici si
        aspettano di trovarle insieme qui.
        """
        stt_loaded = getattr(stt_engine, "_model", None) is not None
        engine_id = sel["model"]
        if engine_id in ("kokoro", "piper"):
            voci = [{"lang": l, "language": name, "voice": v}
                    for l, name, v in _KOKORO_VOICES] \
                + _piper_voice_entries(tts_engine)
        else:
            voci = (tts_engine.voci_di(engine_id)
                    if hasattr(tts_engine, "voci_di") else [])
        return web.json_response({
            "stt": {"current_model": cfg.stt_model, "loaded": stt_loaded},
            "tts": {"engine": engine_id, "selected_model": engine_id,
                    "voices": voci},
        })

    async def models_load(req):
        """Carica in memoria il motore ATTIVO (non piu' solo Kokoro) e lo STT."""
        try:
            body = await req.json() if req.can_read_body else {}
        except Exception:  # noqa: BLE001
            body = {}
        try:
            info = {}
            if body.get("tts", True):
                if hasattr(tts_engine, "attiva"):
                    # Stessa strada dello hot swap: scarica gli altri e scalda questo.
                    info = await asyncio.to_thread(tts_engine.attiva, sel["model"])
                else:
                    await asyncio.to_thread(tts_engine.load)
            # `tts: false` = NON toccare il TTS. Prima cadeva su `load()`, che
            # scalda Kokoro: chi non sa quale voce servira' (il gateway: dipende
            # dalla persona del turno) si ritrovava Kokoro in VRAM a ogni
            # connessione, per poi doverlo sfrattare appena serviva XTTS.
            await asyncio.to_thread(stt_engine.load)
            return web.json_response({"status": "ready", **info})
        except Exception as e:  # noqa: BLE001
            log.error("models/load nativo: %s", e)
            return web.json_response(_model_missing(e) or {"status": "error", "message": str(e)},
                                     status=409 if _model_missing(e) else 200)

    async def models_unload(req):
        """Libera la memoria dei motori TTS (prima era un no-op che mentiva)."""
        try:
            body = await req.json() if req.can_read_body else {}
        except Exception:  # noqa: BLE001
            body = {}
        comp = (body.get("component") or "all").strip().lower()
        scaricati: list[str] = []
        try:
            if comp in ("tts", "all") and hasattr(tts_engine, "scarica_tutti"):
                scaricati = await asyncio.to_thread(tts_engine.scarica_tutti)
            # Lo STT NON si scarica: e' unico (dettatura + sessioni voce) e deve
            # restare sempre caldo. `component: "stt"|"all"` lo lascia dov'e'.
        except Exception as e:  # noqa: BLE001
            log.error("models/unload nativo: %s", e)
            return web.json_response({"status": "error", "message": str(e)})
        kept = ["stt"] if comp in ("stt", "all") else []
        return web.json_response({"status": "ok", "unloaded": scaricati,
                                  "kept": kept})

    async def tts_test(req):
        body = await req.json()
        text = (body.get("text") or "").strip()
        voice = body.get("voice") or cfg.default_voice
        lang = body.get("lang") or cfg.default_lang
        # `engine`/`model`: motore scelto nel dropdown della GUI. Assente ->
        # lo deduce il router dalla voce (comportamento storico).
        engine = _engine_id_valido(body.get("engine") or body.get("model") or "")             if (body.get("engine") or body.get("model")) else ""
        if not text:
            return web.json_response({"status": "error", "message": "empty text"})
        try:
            samples, sr = await asyncio.to_thread(
                tts_engine.synth, text, voice, lang, cfg.tts_speed, engine)
        except Exception as e:  # noqa: BLE001
            log.error("tts/test nativo: %s", e)
            return web.json_response(_model_missing(e) or {"status": "error", "message": str(e)},
                                     status=409 if _model_missing(e) else 200)
        wav = _pcm_f32_to_wav_bytes(samples, sr)
        return web.json_response({
            "status": "ok", "voice": voice, "engine": engine or "auto",
            "sample_rate": int(sr),
            "duration_ms": int(1000 * len(samples) / sr),
            "audio_b64": base64.b64encode(wav).decode("ascii")})

    async def transcribe(req):
        reader = await req.multipart()
        field = await reader.next()
        raw = await field.read(decode=False)
        audio, sr = await asyncio.to_thread(_decode_audio, raw)
        if sr != STT_RATE:
            audio = _resample(audio, sr, STT_RATE)
        # `?segments=1` aggiunge i tempi (servono a chi diarizza: senza sapere
        # QUANDO e' stata detta una frase non la si attribuisce a una voce),
        # `?words=1` li vuole per parola. Senza, la risposta resta quella
        # storica — {"text": ...} — per chi i tempi non li usa.
        vero = ("1", "true", "yes")
        vuole_segmenti = req.query.get("segments", "") in vero
        vuole_parole = req.query.get("words", "") in vero
        lingua = req.query.get("lang") or cfg.stt_language or None
        if not vuole_segmenti or not hasattr(stt_engine, "transcribe_detailed"):
            text = await asyncio.to_thread(
                stt_engine.transcribe, audio, lingua)
            return web.json_response({"text": text})
        r = await asyncio.to_thread(stt_engine.transcribe_detailed, audio,
                                    lingua, vuole_parole)
        return web.json_response(r)

    # --- engine TTS selezionabili (dropdown GUI) -------------------------
    # La selezione e' PERSISTENTE (cfg.tts_engine / cfg.default_voice): prima
    # viveva solo in memoria e a ogni riavvio del bridge si tornava a Kokoro.
    sel = {"model": _engine_id_valido(getattr(cfg, "tts_engine", "") or "kokoro"),
           "voice": cfg.default_voice}

    async def tts_models(_req):
        from .engines.kokoro_tts import available_precisions
        precisions = sorted(available_precisions(tts_engine.model_dir).keys())
        # Catalogo REALE (motori presenti su disco + loro voci): una sola fonte
        # di verita' condivisa con /tts/voices e con la GUI.
        modelli = (tts_engine.catalogo()
                   if hasattr(tts_engine, "catalogo") else [])
        return web.json_response({
            "kokoro_precisions": precisions,
            "kokoro_precision": getattr(cfg, "kokoro_precision", "fp16"),
            "models": modelli,
            "selected": sel["model"], "selected_voice": sel["voice"],
        })

    async def tts_voices(req):
        """Voci di UN motore — alimenta il secondo dropdown della GUI."""
        engine_id = _engine_id_valido(req.query.get("model") or sel["model"])
        voci = (tts_engine.voci_di(engine_id)
                if hasattr(tts_engine, "voci_di") else [])
        return web.json_response({"model": engine_id, "voices": voci})

    def _persisti(campo: str, valore: str) -> None:
        """Scrive la scelta in config.json, best-effort: se il salvataggio non
        riesce la sessione corrente funziona lo stesso, si perde solo la
        persistenza (e resta a log)."""
        try:
            setattr(cfg, campo, valore)
            cfg.save()
        except Exception as e:  # noqa: BLE001
            log.warning("selezione TTS non persistita (%s=%s): %s",
                        campo, valore, e)

    async def tts_model_set(req):
        body = await req.json()
        richiesto = (body.get("model_id") or "").strip()
        if not richiesto:
            return web.json_response({"status": "ok", "selected": sel["model"]})
        engine_id = _engine_id_valido(richiesto)
        if catalog.spec(engine_id) is None:
            return web.json_response(
                {"status": "error", "message": f"motore sconosciuto: {richiesto}"})
        precedente = sel["model"]
        sel["model"] = engine_id
        _persisti("tts_engine", engine_id)
        # Hot swap: il motore precedente lascia la VRAM e il nuovo si scalda
        # subito (in background). Senza, restavano caldi tutti quelli provati.
        info: dict = {}
        if hasattr(tts_engine, "attiva"):
            try:
                info = await asyncio.to_thread(tts_engine.attiva, engine_id)
            except Exception as e:  # noqa: BLE001
                log.warning("hot swap verso '%s' fallito: %s", engine_id, e)
                info = {"message": str(e)}
        return web.json_response({"status": "ok", "selected": engine_id,
                                  "previous": precedente, **info})

    async def tts_voice_set(req):
        body = await req.json()
        voce = (body.get("voice") or "").strip()
        if voce:
            sel["voice"] = voce
            _persisti("default_voice", voce)
        return web.json_response({"status": "ok", "voice": sel["voice"]})

    # --- voci clonate (campioni in <cartella modelli>/tts/voices) --------

    async def voices_cloned(_req):
        return web.json_response({"voices": _list_cloned()})

    async def voice_clone(req):
        body = await req.json()
        name = (body.get("name") or "").strip()
        fmt = (body.get("format") or "wav").lstrip(".")
        b64 = body.get("audio_b64") or ""
        if not name or not b64:
            return web.json_response({"status": "error", "message": "name/audio mancante"})
        if not _CLONE_NAME.match(name) or fmt.lower() not in _CLONE_EXTS:
            return web.json_response({"status": "error", "message": "nome o formato non valido"},
                                     status=400)
        d = _cloned_dir()
        if not d:
            return web.json_response({"status": "error", "message": "nessuna cartella dei modelli vocali"},
                                     status=409)
        try:
            raw = base64.b64decode(b64)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{name}.{fmt.lower()}"), "wb") as f:
                f.write(raw)
        except Exception as e:  # noqa: BLE001
            return web.json_response({"status": "error", "message": str(e)})
        return web.json_response({"status": "ok",
                                  "voices": [v["name"] for v in _list_cloned()]})

    async def voice_clone_delete(req):
        name = req.match_info.get("name", "")
        d = _cloned_dir()
        removed = False
        try:
            for fn in (os.listdir(d) if d else ()):
                if os.path.splitext(fn)[0] == name:
                    os.remove(os.path.join(d, fn))
                    removed = True
        except Exception as e:  # noqa: BLE001
            return web.json_response({"status": "error", "message": str(e)})
        if not removed:
            return web.json_response({"status": "error", "message": "voce non trovata"})
        return web.json_response({"status": "ok",
                                  "voices": [v["name"] for v in _list_cloned()]})

    # --- WebSocket TTS ---------------------------------------------------

    async def tts_stream(req):
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(req)
        client = NativeTtsClient(tts_engine, cfg)
        client.start()
        await ws.send_json({"type": "ready"})

        async def pump():
            while not ws.closed:
                try:
                    await ws.send_json({"type": "error", "code": "model_not_installed",
                                        "message": client.errors.get_nowait()})
                    continue
                except queue.Empty:
                    pass
                try:
                    seg = client.audio.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.02)
                    continue
                # Il testo viaggia con l'audio: chi lo consegna deve poter
                # dire QUALE frase e' stata pronunciata, non solo quanti
                # secondi sono usciti. Sono due cose diverse appena una
                # risposta viene interrotta a meta'.
                await ws.send_json({"type": "audio_start",
                                    "sample_rate": int(seg.sample_rate),
                                    "testo": getattr(seg, "testo", "")})
                await ws.send_bytes(np.ascontiguousarray(
                    seg.samples, dtype="<f4").tobytes())
                await ws.send_json({"type": "audio_end"})

        task = asyncio.create_task(pump())
        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                t = data.get("type")
                if t == "config":
                    # `engine`: senza, il router lo deduce dal nome della voce e
                    # sbaglia motore per le voci omonime fra Kokoro/XTTS/
                    # Chatterbox. Opzionale, per restare compatibili con i
                    # client che non lo mandano.
                    client.configure(data.get("voice", "if_sara"),
                                     data.get("lang", "it"),
                                     float(data.get("speed", 1.0)),
                                     data.get("engine", "") or "")
                elif t == "text":
                    client.send_text(data.get("chunk", ""))
                elif t in ("flush", "end"):
                    client.flush()
                elif t == "abort":
                    # Interruzione: si butta il testo non ancora sintetizzato e
                    # i segmenti gia' pronti ma non ancora spediti. Senza,
                    # l'audio della risposta abbandonata continuava ad arrivare
                    # e veniva riprodotto all'inizio del turno dopo.
                    gen = client.abort()
                    await ws.send_json({"type": "aborted", "generation": gen})
        finally:
            task.cancel()
            client.close()
        return ws

    # --- WebSocket STT ---------------------------------------------------

    async def stt_stream(req):
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(req)
        # Lingua per QUESTA connessione: `?lang=it`. Senza, vale il campo di
        # config; se anche quello e' vuoto, Whisper la indovina da solo — ed e'
        # li' che nascono le trascrizioni in cinese su un "mi senti" detto in
        # italiano: su enunciati brevi l'auto-rilevamento sbaglia spesso.
        lang_conn = (req.query.get("lang") or "").strip()
        client = NativeSttClient(
            stt_engine, lang=(lang_conn or cfg.stt_language or None),
            vad_threshold=cfg.stt_vad_threshold,
            endpoint_silence_s=cfg.stt_endpoint_silence_s,
            min_utterance_s=cfg.stt_min_utterance_s,
            vad_model=cfg.stt_vad_model, speech_prob=cfg.stt_vad_speech_prob,
            chunk_silence_s=getattr(cfg, 'stt_chunk_silence_s', 0.0),
            chunk_min_s=getattr(cfg, 'stt_chunk_min_s', 2.0))
        client.start()
        await ws.send_json({"type": "ready"})

        async def pump():
            while not ws.closed:
                sent = False
                try:
                    client.speech_events.get_nowait()
                    await ws.send_json({"type": "speech_start"})
                    sent = True
                except queue.Empty:
                    pass
                try:
                    text = client.utterances.get_nowait()
                    await ws.send_json({"type": "utterance", "text": text})
                    sent = True
                except queue.Empty:
                    pass
                if not sent:
                    await asyncio.sleep(0.02)

        task = asyncio.create_task(pump())
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    client.send_audio(np.frombuffer(msg.data, dtype="<f4"))
                elif msg.type == web.WSMsgType.TEXT:
                    # {"type":"flush"}: il chiamante sa gia' che l'enunciato e'
                    # finito (pulsante rilasciato) e non vuole aspettare che il
                    # VAD lo deduca dal silenzio.
                    try:
                        if json.loads(msg.data).get("type") == "flush":
                            client.flush()
                    except (ValueError, AttributeError):
                        pass
        finally:
            task.cancel()
            client.close()
        return ws

    app = web.Application(middlewares=[_cors])
    app.add_routes([
        web.get("/health", health),
        web.get("/models/status", models_status),
        web.get("/models/available", models_available),
        web.post("/models/load", models_load),
        web.post("/models/unload", models_unload),
        web.post("/tts/test", tts_test),
        web.post("/transcribe", transcribe),
        web.get("/tts/models", tts_models),
        web.get("/tts/voices", tts_voices),
        web.post("/tts/model", tts_model_set),
        web.post("/tts/voice", tts_voice_set),
        web.get("/voices/cloned", voices_cloned),
        web.post("/voices/clone", voice_clone),
        web.delete("/voices/cloned/{name}", voice_clone_delete),
        web.get("/tts/stream", tts_stream),   # aiohttp: WS upgrade su GET
        web.get("/stt/stream", stt_stream),
    ])
    return app


def run_handy_compat(cfg: Config) -> None:
    """Avvia (bloccante) il server Handy-compatibile nativo. Usare in un thread."""
    from aiohttp import web

    # Pre-scalda i motori: le prime richieste non pagano il load (che bloccherebbe).
    # Sospeso (stato su disco): niente TTS all'avvio. Lo STT si carica comunque.
    from .engines import in_sospensione
    try:
        if in_sospensione():
            log.info("Handy-compat: TTS NON precaricato, i componenti sono sospesi")
        else:
            _get_tts_engine(cfg).load()
    except Exception as e:  # noqa: BLE001
        log.warning("Handy-compat: pre-load TTS fallito: %s", e)
    try:
        _get_stt_engine(cfg).load()
    except Exception as e:  # noqa: BLE001
        log.warning("Handy-compat: pre-load STT fallito: %s", e)

    async def _serve() -> None:
        app = build_app(cfg)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, cfg.handy_compat_host, cfg.handy_compat_port)
        try:
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        stop = asyncio.Event()
        # Il thread che SERVE davvero si registra qui: se il controllo di avvio ne ha
        # lanciato un secondo (il primo era fermo nella creazione del loop), vale
        # chi ha preso la porta; l'altro esce con "indirizzo in uso" e non conta.
        _server["thread"] = threading.current_thread()
        _server["loop"], _server["stop"] = asyncio.get_running_loop(), stop
        log.info("Handy-compat (nativo) in ascolto su http://%s:%d "
                 "(WS /tts/stream, /stt/stream)", cfg.handy_compat_host,
                 cfg.handy_compat_port)
        try:
            await stop.wait()            # gira finche' non lo ferma `ferma_compat`
        finally:
            await runner.cleanup()       # chiude porta e connessioni aperte
            _server["loop"] = _server["stop"] = None
            log.info("Handy-compat fermato (porta %d libera)", cfg.handy_compat_port)

    _server["t_loop"] = time.monotonic()      # da qui `_sorveglia` conta lo stallo
    asyncio.run(_serve())


# --- avvio/arresto del server (Sospendi / Riprendi) ----------------------------------
# Il server :8720 NON serve alla dettatura (che parla direttamente con lo STT
# unico, `dictation.py` -> `engines.stt_shared`): serve a sessioni voce, gateway,
# telefonate, copilota e skill esterne, tutti sospesi con «Sospendi».
_server: dict = {"loop": None, "stop": None, "thread": None, "t_loop": None}
_server_lock = threading.Lock()

# Su questa macchina la creazione del loop asyncio (il socketpair interno di
# Windows) a volte NON completa l'accept e il thread resta fermo per sempre: misurato
# 1-2 volte su 30 anche in un processo vuoto con la sola `import numpy`, quindi non
# e' del bridge. Il server delle voci si riavvia a ogni Riprendi: senza un
# controllo, il 3-7% delle volte resterebbe giu' in silenzio.
ATTESA_LOOP_S = 15.0
TENTATIVI_AVVIO = 3


def compat_attivo() -> bool:
    t = _server["thread"]
    return t is not None and t.is_alive() and _server["stop"] is not None


def avvia_compat(cfg: Config) -> bool:
    """Avvia il server in un thread (no-op se gia' acceso). True se avviato ora."""
    with _server_lock:
        t = _server["thread"]
        if t is not None and t.is_alive():
            return False

        _server["stop"] = _server["t_loop"] = None
        _server["thread"] = threading.Thread(target=_corpo, args=(cfg,),
                                             name="handy-compat", daemon=True)
        _server["thread"].start()
        threading.Thread(target=_sorveglia, args=(cfg,), name="handy-compat-watch",
                         daemon=True).start()
        return True


def _corpo(cfg: Config) -> None:
    try:
        run_handy_compat(cfg)
    except Exception as e:  # noqa: BLE001
        if isinstance(e, OSError) and _server["stop"] is not None:
            log.info("Handy-compat: un altro thread serve gia' la porta (%s)", e)
        else:
            log.error("Handy-compat non avviato: %s", e)


def _sorveglia(cfg: Config) -> None:
    """Se il loop non parte entro ATTESA_LOOP_S dal suo avvio, abbandona il
    thread fermo e ne lancia uno nuovo (al piu' TENTATIVI_AVVIO volte)."""
    for tentativo in range(1, TENTATIVI_AVVIO + 1):
        while True:
            time.sleep(0.2)
            t = _server["thread"]
            if _server["stop"] is not None or t is None or not t.is_alive():
                return                      # su, oppure uscito (errore gia' loggato)
            t0 = _server["t_loop"]
            if t0 is not None and time.monotonic() - t0 > ATTESA_LOOP_S:
                break
        if tentativo == TENTATIVI_AVVIO:
            log.error("Handy-compat: il loop asyncio non parte dopo %d tentativi",
                      TENTATIVI_AVVIO)
            return
        log.warning("Handy-compat: loop asyncio fermo da %.0fs, nuovo tentativo %d/%d",
                    ATTESA_LOOP_S, tentativo + 1, TENTATIVI_AVVIO)
        with _server_lock:
            if _server["stop"] is not None:
                return                      # nel frattempo il primo si e' sbloccato
            _server["t_loop"] = None
            _server["thread"] = threading.Thread(target=_corpo, args=(cfg,),
                                                 name="handy-compat", daemon=True)
            _server["thread"].start()


def ferma_compat(timeout: float = 10.0) -> bool:
    """Ferma il server e libera la porta. True se era acceso."""
    with _server_lock:
        t = _server["thread"]
        if t is None or not t.is_alive():
            return False
        # Il thread puo' essere ancora in avvio (pre-load dei motori): si attende
        # che il server sia su, altrimenti non c'e' ancora nulla da fermare.
        fine = time.monotonic() + 120.0
        while _server["stop"] is None and t.is_alive() and time.monotonic() < fine:
            time.sleep(0.05)
        loop, stop = _server["loop"], _server["stop"]
        if loop is not None and stop is not None:
            loop.call_soon_threadsafe(stop.set)
        t.join(timeout)
        return True
