"""Blueprint /personas/* e /avatar/* — identita' video e worker MuseTalk.

Gestione dell'archivio persone (`avatar/persone/<nome>/manifest.json` +
clip video) e del worker avatar dalla GUI:

  GET    /personas                      elenco con manifest e conteggio clip
  POST   /personas                      {"name", "voice": {engine, voice}}
  PUT    /personas/<name>               aggiorna il manifest (voice e/o name)
  DELETE /personas/<name>               rimuove la persona (409 se attiva)
  POST   /personas/<name>/clips         upload multipart di un mp4
  DELETE /personas/<name>/clips/<file>  rimuove un clip
  POST   /avatar/start                  {"persona"} (bloccante, anche minuti)
  POST   /avatar/stop
  GET    /avatar/state                  -> {"running", "persona"}
  POST   /avatar/speak                  {"text", "voice"?, "lang"?} — sintesi
                                        via Handy POST /tts/test + lipsync

NB: il copilota crea un proprio AvatarClient quando si attiva con una persona
configurata; evitare di tenere l'avatar della GUI avviato durante una sessione
copilota con avatar (due worker = doppia VRAM).
"""
from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import tempfile
import threading
from pathlib import Path

import requests
from flask import Blueprint, current_app, jsonify, request, send_file

from . import poster
from .avatar_client import AvatarClient
from .util import is_safe_artifact_id

log = logging.getLogger("callbridge.api.personas")

bp = Blueprint("personas", __name__)

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_CLIP_RE = re.compile(r"^[a-zA-Z0-9_. -]+\.mp4$")

# AvatarClient della GUI: uno per processo, creato pigramente e protetto da
# lock (start/stop/speak arrivano da thread Flask diversi).
_avatar_lock = threading.Lock()

# Job di ingest documenti->personalita' (Fase 5), uno per persona. Stato in
# memoria, interrogato via GET /personas/<name>/ingest/state.
_ingest_jobs: dict[str, dict] = {}
_ingest_lock = threading.Lock()


def _cfg():
    return current_app.config["CB_CONFIG"]


def _avatar() -> AvatarClient:
    app = current_app
    with _avatar_lock:
        client = app.config.get("CB_AVATAR")
        if client is None:
            client = AvatarClient(_cfg())
            app.config["CB_AVATAR"] = client
        return client


def _prune_old(directory: Path, pattern: str, max_age_s: float = 300.0) -> None:
    """Cancella i file `pattern` in `directory` piu' vecchi di `max_age_s`.
    Serve a non far crescere all'infinito la cache dei wav di sintesi, senza
    racare con una riproduzione asincrona ancora in corso."""
    import time
    cutoff = time.time() - max_age_s
    for p in directory.glob(pattern):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def _personas_root() -> Path:
    return Path(_cfg().avatar_dir) / "persone"


def _persona_dir(name: str) -> Path | None:
    # Gli spazi (es. "Andrea Taini") vengono normalizzati a underscore per il
    # nome cartella; restano leciti lettere/numeri/-/_. Il nome visualizzato
    # (con spazi) resta nel manifest.
    safe = re.sub(r"\s+", "_", (name or "").strip())
    if not safe or not _NAME_RE.match(safe):
        return None
    return _personas_root() / safe


def _read_manifest(pdir: Path) -> dict:
    return json.loads((pdir / "manifest.json").read_text(encoding="utf-8"))


def _rag_count(pdir: Path) -> int:
    """Numero di chunk nell'indice RAG della persona (0 se non ne ha)."""
    try:
        meta = json.loads((pdir / "rag" / "meta.json").read_text(encoding="utf-8"))
        return int(meta.get("count") or 0)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return 0


def _write_manifest(pdir: Path, manifest: dict) -> None:
    (pdir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


# Campi "identità" per la modalità LM Studio (Ctrl+Shift+F4/F5): personalità e
# system prompt compongono il prompt di `LmStudioClient.begin_session`;
# knowledge_project collega la persona a un progetto wikibridge (memoria/sapere).
def _apply_identity_fields(manifest: dict, body: dict) -> str | None:
    """Copia personality/system_prompt/knowledge_project dal body al manifest
    (solo le chiavi presenti). Ritorna un messaggio d'errore se
    knowledge_project non è uno slug sicuro, altrimenti None."""
    for key in ("personality", "system_prompt"):
        if key in body:
            manifest[key] = (body.get(key) or "").strip()
    if "knowledge_project" in body:
        kp = (body.get("knowledge_project") or "").strip()
        if kp and not is_safe_artifact_id(kp):
            return "knowledge_project non valido (slug: lettere, numeri, - e _)"
        manifest["knowledge_project"] = kp
    return None


@bp.get("/personas")
def personas_list():
    out = []
    root = _personas_root()
    if root.is_dir():
        for p in sorted(root.iterdir()):
            mpath = p / "manifest.json"
            if not mpath.is_file():
                continue
            try:
                m = json.loads(mpath.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                log.warning("Persona %s: manifest illeggibile: %s", p.name, e)
                continue
            clips = m.get("clips") or []
            out.append({
                "name": p.name,
                "voice": m.get("voice") or {},
                "clips": clips,
                "clip_count": len(clips),
                "personality": m.get("personality") or "",
                "system_prompt": m.get("system_prompt") or "",
                "knowledge_project": m.get("knowledge_project") or "",
                "has_personality_docs": bool(m.get("personality_from_docs")),
                "knowledge_chunks": _rag_count(p),
            })
    return jsonify({"personas": out})


@bp.post("/personas")
def personas_create():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    pdir = _persona_dir(name)
    if pdir is None:
        return jsonify({"error": "nome non valido (lettere, numeri, - e _)"}), 400
    if pdir.exists():
        return jsonify({"error": f"persona '{name}' gia' esistente"}), 409
    voice = body.get("voice") or {}
    manifest = {"name": name, "voice": voice, "clips": []}
    err = _apply_identity_fields(manifest, body)
    if err:
        return jsonify({"error": err}), 400
    (pdir / "clips").mkdir(parents=True)
    _write_manifest(pdir, manifest)
    if "voice" in body:
        _allinea_motore_tts(name, manifest.get("voice") or {})
    log.info("Persona creata: %s", name)
    return jsonify({"status": "ok", "name": name}), 201


def allinea_motore_tts_persona(persona: str) -> None:
    """Come `_allinea_motore_tts`, ma legge la voce dal manifest.

    La usa il cambio di `avatar_persona` da /config: li' si sa solo il nome
    della nuova identita', non la sua voce."""
    persona = (persona or "").strip()
    if not persona:
        return
    pdir = _persona_dir(persona)
    if pdir is None or not (pdir / "manifest.json").is_file():
        return
    _allinea_motore_tts(persona, _read_manifest(pdir).get("voice") or {})


def _allinea_motore_tts(persona: str, voce: dict) -> None:
    """Se `persona` e' l'avatar ATTIVO, rende caldo il motore della sua voce.

    Cambiando voce dall'identita' il motore in memoria restava quello di
    prima: si allineava da solo solo alla PRIMA sintesi successiva, e fino
    a quel momento in VRAM c'era il motore sbagliato (con XTTS sono 2,3 GB
    occupati per niente, e il primo turno pagava lo swap: 60+ s misurati).
    Qui lo swap si fa SUBITO, che e' anche il momento in cui l'utente se lo
    aspetta.

    Solo per l'avatar attivo: cambiare la voce di un'altra identita' non deve
    sfrattare il motore di quella in uso.
    """
    cfg = _cfg()
    attiva = (getattr(cfg, "avatar_persona", "") or "").strip()
    if not attiva or attiva != persona:
        return
    engine = (voce.get("engine") or "").strip().lower()
    if not engine:
        return
    try:
        from .engines import _get_tts_engine
        router = _get_tts_engine(cfg)
        if hasattr(router, "attiva"):
            router.attiva(engine)
            log.info("TTS: voce di '%s' cambiata -> motore attivo '%s'",
                     persona, engine)
    except Exception as e:  # noqa: BLE001 - lo swap non deve far fallire il salvataggio
        log.warning("TTS: allineamento al motore '%s' non riuscito: %s", engine, e)


@bp.put("/personas/<name>")
def personas_update(name: str):
    pdir = _persona_dir(name)
    if pdir is None or not (pdir / "manifest.json").is_file():
        return jsonify({"error": "persona non trovata"}), 404
    body = request.get_json(silent=True) or {}
    manifest = _read_manifest(pdir)
    if "voice" in body:
        manifest["voice"] = body["voice"] or {}
    err = _apply_identity_fields(manifest, body)
    if err:
        return jsonify({"error": err}), 400
    _write_manifest(pdir, manifest)
    if "voice" in body:
        _allinea_motore_tts(name, manifest.get("voice") or {})
    return jsonify({"status": "ok", "manifest": manifest})


@bp.delete("/personas/<name>")
def personas_delete(name: str):
    pdir = _persona_dir(name)
    if pdir is None or not pdir.is_dir():
        return jsonify({"error": "persona non trovata"}), 404
    avatar = current_app.config.get("CB_AVATAR")
    active = getattr(avatar, "_active_persona", None) if avatar else None
    if active == name:
        return jsonify({"error": "persona attiva sull'avatar: fermalo prima"}), 409
    shutil.rmtree(pdir)
    log.info("Persona eliminata: %s", name)
    return jsonify({"status": "ok"})


def _read_memory(pdir: Path) -> dict:
    try:
        return json.loads((pdir / "memory.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_memory(pdir: Path, mem: dict) -> None:
    (pdir / "memory.json").write_text(
        json.dumps(mem, indent=2, ensure_ascii=False), encoding="utf-8")


@bp.get("/personas/<name>/memory")
def persona_memory(name: str):
    """Memoria della persona in forma leggibile: profilo + sessioni ricordate.

    `rolling_profile` è il ricordo DUREVOLE, rigenerato a fine sessione fondendo
    il profilo precedente col riassunto nuovo: un fatto entrato lì ci resta
    anche cancellando la sessione da cui proveniva. Per questo i due pezzi si
    modificano separatamente.
    """
    pdir = _persona_dir(name)
    if pdir is None or not pdir.is_dir():
        return jsonify({"error": "persona non trovata"}), 404
    mem = _read_memory(pdir)
    sessioni = []
    for i, s in enumerate(mem.get("recent_sessions") or []):
        sessioni.append({"index": i, "ts": s.get("ts"),
                         "title": s.get("title") or "",
                         "summary": s.get("summary") or ""})
    return jsonify({
        "profile": mem.get("rolling_profile") or "",
        "sessions": sessioni,
        "last_seen_ts": mem.get("last_seen_ts"),
        "sessions_count": int((mem.get("stats") or {}).get("sessions", 0)),
    })


@bp.put("/personas/<name>/memory")
def persona_memory_update(name: str):
    """Riscrive il profilo durevole (campo `profile`)."""
    pdir = _persona_dir(name)
    if pdir is None or not pdir.is_dir():
        return jsonify({"error": "persona non trovata"}), 404
    body = request.get_json(silent=True) or {}
    if "profile" not in body:
        return jsonify({"error": "campo 'profile' mancante"}), 400
    mem = _read_memory(pdir)
    mem["version"] = mem.get("version", 1)
    mem["rolling_profile"] = (body.get("profile") or "").strip()
    _write_memory(pdir, mem)
    log.info("Memoria di '%s': profilo riscritto (%d caratteri)",
             name, len(mem["rolling_profile"]))
    return jsonify({"status": "ok"})


@bp.delete("/personas/<name>/memory/sessions/<int:index>")
def persona_memory_del_session(name: str, index: int):
    """Dimentica UNA conversazione ricordata."""
    pdir = _persona_dir(name)
    if pdir is None or not pdir.is_dir():
        return jsonify({"error": "persona non trovata"}), 404
    mem = _read_memory(pdir)
    sessioni = list(mem.get("recent_sessions") or [])
    if not 0 <= index < len(sessioni):
        return jsonify({"error": "indice fuori intervallo"}), 404
    tolta = sessioni.pop(index)
    mem["recent_sessions"] = sessioni
    _write_memory(pdir, mem)
    log.info("Memoria di '%s': dimenticata la sessione del %s",
             name, tolta.get("title") or tolta.get("ts"))
    return jsonify({"status": "ok", "remaining": len(sessioni)})


@bp.delete("/personas/<name>/memory")
def persona_memory_clear(name: str):
    """Azzera TUTTA la memoria: profilo, sessioni e conteggi.

    Il file resta, vuoto: cancellarlo funzionerebbe uguale, ma un file assente
    e uno svuotato si distinguono solo guardando il disco."""
    pdir = _persona_dir(name)
    if pdir is None or not pdir.is_dir():
        return jsonify({"error": "persona non trovata"}), 404
    _write_memory(pdir, {"version": 1, "rolling_profile": "",
                         "recent_sessions": [], "stats": {"sessions": 0}})
    log.warning("Memoria di '%s' azzerata dalla GUI", name)
    return jsonify({"status": "ok"})


@bp.post("/personas/<name>/clips")
def personas_clip_upload(name: str):
    pdir = _persona_dir(name)
    if pdir is None or not (pdir / "manifest.json").is_file():
        return jsonify({"error": "persona non trovata"}), 404
    file = request.files.get("clip") or request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "campo multipart 'clip' mancante"}), 400
    fname = Path(file.filename).name
    if not _CLIP_RE.match(fname):
        return jsonify({"error": "solo file .mp4 con nome semplice"}), 400
    (pdir / "clips").mkdir(exist_ok=True)
    dest = pdir / "clips" / fname
    file.save(dest)
    manifest = _read_manifest(pdir)
    rel = f"clips/{fname}"
    clips = manifest.setdefault("clips", [])
    if rel not in clips:
        clips.append(rel)
    _write_manifest(pdir, manifest)
    log.info("Persona %s: clip aggiunto %s (%d byte)", name, fname, dest.stat().st_size)
    return jsonify({"status": "ok", "clips": clips}), 201


@bp.delete("/personas/<name>/clips/<path:clip>")
def personas_clip_delete(name: str, clip: str):
    pdir = _persona_dir(name)
    if pdir is None or not (pdir / "manifest.json").is_file():
        return jsonify({"error": "persona non trovata"}), 404
    fname = Path(clip).name  # niente traversal
    target = pdir / "clips" / fname
    if not target.is_file():
        return jsonify({"error": "clip non trovato"}), 404
    target.unlink()
    manifest = _read_manifest(pdir)
    rel = f"clips/{fname}"
    manifest["clips"] = [c for c in manifest.get("clips", []) if c != rel]
    _write_manifest(pdir, manifest)
    return jsonify({"status": "ok", "clips": manifest["clips"]})


# --- ingest documenti -> personalita'/sapere (Fase 5) ------------------------

def _run_ingest_job(cfg, name: str, path: Path) -> None:
    """Esegue la pipeline in background aggiornando il job in `_ingest_jobs`."""
    job = _ingest_jobs.get(name)
    if job is None:
        return

    def progress(phase: str, pct: float, detail: str = "") -> None:
        job.update(phase=phase, percent=round(float(pct), 1), detail=detail)

    try:
        from . import persona_ingest
        job.update(state="running")
        result = persona_ingest.run_ingest(cfg, name, path, progress)
        job.update(state="done", phase="done", percent=100.0, result=result)
    except Exception as e:  # noqa: BLE001 - superficie d'errore per la GUI
        log.exception("ingest persona %s fallito", name)
        job.update(state="error", error=str(e))


@bp.post("/personas/<name>/ingest")
def persona_ingest_start(name: str):
    """Carica un documento e avvia l'estrazione personalita'+sapere (ASINCRONO).
    La GUI segue con GET /personas/<name>/ingest/state."""
    from . import persona_ingest
    pdir = _persona_dir(name)
    if pdir is None or not (pdir / "manifest.json").is_file():
        return jsonify({"error": "persona non trovata"}), 404
    file = request.files.get("file") or request.files.get("doc")
    if file is None or not file.filename:
        return jsonify({"error": "campo multipart 'file' mancante"}), 400
    fname = Path(file.filename).name
    ext = Path(fname).suffix.lower()
    if ext not in persona_ingest.SUPPORTED_EXTS:
        allowed = ", ".join(sorted(persona_ingest.SUPPORTED_EXTS))
        return jsonify({"error": f"formato non supportato ({ext}); ammessi: {allowed}"}), 400
    with _ingest_lock:
        cur = _ingest_jobs.get(name)
        if cur and cur.get("state") == "running":
            return jsonify({"error": "importazione già in corso per questa persona"}), 409
        _ingest_jobs[name] = {"state": "running", "phase": "queued",
                              "percent": 0.0, "detail": fname,
                              "error": None, "result": None}
    docs = pdir / "docs"
    docs.mkdir(exist_ok=True)
    dest = docs / fname
    file.save(dest)
    log.info("Persona %s: ingest avviato (%s, %d byte)", name, fname, dest.stat().st_size)
    threading.Thread(target=_run_ingest_job, args=(_cfg(), name, dest),
                     name=f"persona-ingest-{name}", daemon=True).start()
    return jsonify({"status": "started", "file": fname}), 202


@bp.get("/personas/<name>/ingest/state")
def persona_ingest_state(name: str):
    job = _ingest_jobs.get(name)
    if not job:
        return jsonify({"state": "idle"})
    return jsonify(job)


# --- worker avatar ----------------------------------------------------------

@bp.post("/avatar/start")
def avatar_start():
    """Avvio ASINCRONO: risponde subito, la preparazione (anche minuti la
    prima volta per clip) prosegue in background. La GUI fa polling su
    /avatar/state finche' running/errore."""
    body = request.get_json(silent=True) or {}
    persona = (body.get("persona") or "").strip()
    pdir = _persona_dir(persona)
    if pdir is None or not (pdir / "manifest.json").is_file():
        return jsonify({"error": "persona non trovata"}), 404
    avatar = _avatar()
    running = (avatar._proc is not None and avatar._proc.poll() is None)
    if running and avatar._active_persona == persona and not avatar.preparing:
        return jsonify({"status": "ok", "persona": persona})
    if not avatar.start_async(persona):
        return jsonify({"error": f"preparazione di '{avatar.preparing}' "
                                 "gia' in corso"}), 409
    return jsonify({"status": "preparing", "persona": persona}), 202


@bp.post("/avatar/stream")
def avatar_stream():
    """Manda (o smette di mandare) il video dell'avatar a un consumatore remoto.

    Lo usa il GATEWAY, che gira in un processo suo e non può parlare col worker:
    il worker appartiene al bridge, ed è uno solo per vincolo di VRAM. Il
    gateway passa la porta del proprio server fotogrammi e il bridge gira il
    comando al worker.
    """
    body = request.get_json(silent=True) or {}
    avatar = _avatar()
    on = bool(body.get("on", True))
    port = int(body.get("port") or 0)
    if not avatar.set_stream(on, port):
        return jsonify({"error": "worker avatar non attivo"}), 409
    log.info("Avatar: stream verso il telefono %s%s",
             "attivo" if on else "chiuso", f" (porta {port})" if on and port else "")
    return jsonify({"status": "ok", "streaming": on})


@bp.post("/avatar/interrupt")
def avatar_interrupt():
    """Tronca la battuta in corso (lo streaming frame-by-frame la rende
    interrompibile): il video torna in idle, l'audio si ferma."""
    avatar = current_app.config.get("CB_AVATAR")
    if avatar is not None:
        # `svuota`: butta anche le battute gia' accodate nel worker, non solo
        # quella in corso. Serve a chi abbandona una risposta intera.
        body = request.get_json(silent=True) or {}
        avatar.interrupt(svuota=bool(body.get("svuota")))
    return jsonify({"status": "ok"})


@bp.post("/avatar/stop")
def avatar_stop():
    avatar = current_app.config.get("CB_AVATAR")
    if avatar is not None:
        avatar.stop()
        avatar._active_persona = None
    return jsonify({"status": "ok"})


@bp.get("/avatar/state")
def avatar_state():
    avatar = current_app.config.get("CB_AVATAR")
    worker_alive = bool(avatar is not None and avatar._proc is not None
                        and avatar._proc.poll() is None)
    preparing = getattr(avatar, "preparing", None) if avatar else None
    active = getattr(avatar, "_active_persona", None) if worker_alive else None
    return jsonify({
        # running = persona pronta e operativa (worker vivo, niente prepare)
        "running": bool(worker_alive and active and not preparing),
        "persona": active,
        "preparing": preparing,
        "progress": getattr(avatar, "progress", None) if avatar else None,
        "error": getattr(avatar, "last_error", None) if avatar else None,
    })


@bp.post("/avatar/lipsync")
def avatar_lipsync():
    """Anima le labbra su un WAV gia' sintetizzato, senza suonarlo qui.

    Serve al voice gateway: la battuta e' gia' stata sintetizzata una volta e
    inviata al telefono. Risintetizzarla con /avatar/speak significherebbe due
    sintesi della stessa frase (lente, e non identiche fra loro) piu' l'audio
    che esce dalle casse del PC. Qui si riusa lo stesso file: una sola voce,
    labbra sincronizzate.
    """
    avatar = current_app.config.get("CB_AVATAR")
    if avatar is None or getattr(avatar, "_active_persona", None) is None:
        return jsonify({"error": "avatar non attivo: POST /avatar/start prima"}), 409
    body = request.get_json(silent=True) or {}
    wav = (body.get("wav") or "").strip()
    if not wav:
        return jsonify({"error": "campo 'wav' mancante"}), 400
    if not Path(wav).is_file():
        return jsonify({"error": f"wav non trovato: {wav}"}), 400
    avatar.speak_wav(wav, mute=bool(body.get("mute", True)))
    # Si risponde quando il video comincia DAVVERO a scorrere, non appena il
    # comando e' accodato: chi chiama manda la voce subito dopo, e senza questa
    # attesa la sentirebbe partire prima che la bocca si muova (il worker deve
    # ancora generare i fotogrammi). Attesa limitata: se il worker non parte, la
    # conversazione deve andare avanti lo stesso, sfasata ma viva.
    partito = True
    if body.get("attendi_avvio", True):
        partito = avatar.wait_until_playing(float(body.get("attesa_max_s", 15.0)))
    # `preparazione_s` e' il tempo che il WORKER ha impiegato a preparare la
    # battuta, misurato da lui. Non coincide con la durata di questa richiesta:
    # quella include anche l'attesa in coda dietro le battute gia' accodate.
    # Chi tara la lunghezza dei chunk deve usare questo, non il proprio
    # cronometro (vedi chunk_policy.osserva_labiale).
    return jsonify({"status": "ok", "partito": partito,
                    "preparazione_s": getattr(avatar, "preparazione_s", 0.0)})


@bp.post("/avatar/speak")
def avatar_speak():
    """Sintetizza il testo con Handy (/tts/test) e lo fa pronunciare
    all'avatar (lipsync). E' il backend dello Studio TTS+video."""
    avatar = current_app.config.get("CB_AVATAR")
    persona = getattr(avatar, "_active_persona", None) if avatar else None
    if avatar is None or persona is None:
        return jsonify({"error": "avatar non attivo: POST /avatar/start prima"}), 409
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "campo 'text' mancante"}), 400
    cfg = _cfg()
    # Anche il MOTORE della persona, non il solo nome: senza, il router lo
    # deduce dal nome e le voci che non gli appartengono finiscono al motore
    # sbagliato — uno speaker XTTS come "Chandra MacFarland" veniva mandato a
    # Kokoro, che rispondeva "Voice not found".
    voce_persona = avatar.voice_for(persona) or {}
    voice = body.get("voice") or voce_persona.get("voice")
    engine = (body.get("engine") or voce_persona.get("engine") or "").strip()
    lang = body.get("lang") or cfg.default_lang

    try:
        resp = requests.post(
            f"{cfg.handy_base_url()}/tts/test",
            json={"text": text, "voice": voice, "lang": lang, "engine": engine},
            timeout=300,
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        return jsonify({"error": f"Handy /tts/test non raggiungibile: {e}"}), 502
    if data.get("status") != "ok":
        return jsonify({"error": data.get("message", "sintesi fallita")}), 502
    audio_b64 = data.get("audio_b64")
    if not audio_b64:
        return jsonify({"error": "Handy /tts/test: risposta senza audio"}), 502

    wav_dir = Path(cfg.avatar_dir) / "cache"
    wav_dir.mkdir(parents=True, exist_ok=True)
    # Il worker legge il wav in modo asincrono: non lo cancelliamo subito (romperemmo
    # la riproduzione), ma potiamo i vecchi per non farli accumulare all'infinito.
    _prune_old(wav_dir, "speak-*.wav")
    with tempfile.NamedTemporaryFile(
        dir=wav_dir, prefix="speak-", suffix=".wav", delete=False
    ) as f:
        f.write(base64.b64decode(audio_b64))
        wav_path = f.name
    avatar.speak_wav(wav_path)
    return jsonify({
        "status": "ok",
        "duration_ms": data.get("duration_ms"),
        "voice": data.get("voice", voice),
    })


# ---------------------------------------------------------------------------
#  Poster: il primo fotogramma del clip, come immagine fissa
# ---------------------------------------------------------------------------
# La generazione sta in `poster.py` perche' la serve anche il media server
# (:8772), che e' quello che l'app raggiunge. Qui c'e' solo la rotta per la
# GUI del PC.


def _assicura_wav2lip(pdir):
    """Genera gli asset del modo A se mancano, dal poster della persona.

    Serve agli avatar che NON sono passati dal wizard (importati, creati prima,
    o senza gli asset per qualsiasi motivo: era il caso di Shan, che dava
    fileNotFoundException sul telefono). Con un poster gli asset si ricavano al
    volo; senza, non c'è nulla da fare. Idempotente: non rigenera se ci sono.
    """
    face = pdir / "wav2lip" / "face.png"
    box = pdir / "wav2lip" / "box.json"
    if face.is_file() and box.is_file():
        return True
    if not (pdir / "poster.jpg").is_file():
        return False
    from . import avatar_prep
    try:
        avatar_prep.prepara_wav2lip(pdir / "poster.jpg", pdir)
        return True
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("wav2lip on-demand per %s: %s", pdir.name, e)
        return False


@bp.get("/personas/<name>/wav2lip/box")
def persona_box(name: str):
    """Box del volto sul poster (modo A): dove reincollare il volto rigenerato."""
    pdir = _persona_dir(name)
    if pdir is None:
        return jsonify({"error": "persona non trovata"}), 404
    _assicura_wav2lip(pdir)
    bf = pdir / "wav2lip" / "box.json"
    if not bf.is_file():
        return jsonify({"error": "asset non presente"}), 404
    resp = jsonify(json.loads(bf.read_text(encoding="utf-8")))
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@bp.get("/personas/<name>/wav2lip/face")
def persona_face(name: str):
    """Il volto ritagliato 96x96 (modo A), per il runtime Wav2Lip sul client."""
    pdir = _persona_dir(name)
    if pdir is None:
        return jsonify({"error": "persona non trovata"}), 404
    _assicura_wav2lip(pdir)
    f = pdir / "wav2lip" / "face.png"
    if not f.is_file():
        return jsonify({"error": "asset non presente"}), 404
    resp = send_file(f, mimetype="image/png", conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@bp.post("/personas/<name>/poster")
def persona_imposta_poster(name: str):
    """Sostituisce la foto (poster) di un avatar esistente, tenendo il resto
    (voce, memoria, nome). Rigenera un default della bocca: l'utente poi la
    risegna a mano con l'editor."""
    pdir = _persona_dir(name)
    if pdir is None:
        return jsonify({"error": "persona non trovata"}), 404
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "manca il file"}), 400
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(f.read(), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jsonify({"error": "immagine non leggibile"}), 400
    cv2.imwrite(str(pdir / "poster.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    from . import avatar_prep
    try:
        avatar_prep.prepara_wav2lip(pdir / "poster.jpg", pdir)
    except Exception as e:  # noqa: BLE001 - il default non deve far fallire il cambio foto
        current_app.logger.warning("poster %s: default bocca non generato (%s)", name, e)
    return jsonify({"ok": True})


@bp.post("/personas/<name>/wav2lip/bocca")
def persona_imposta_bocca(name: str):
    """Segna a mano i due angoli della bocca (dall'editor sul PC) e rigenera gli
    asset del modo A. `p1`/`p2` sono [x, y] in pixel del poster."""
    pdir = _persona_dir(name)
    if pdir is None:
        return jsonify({"error": "persona non trovata"}), 404
    body = request.get_json(silent=True) or {}
    p1, p2 = body.get("p1"), body.get("p2")
    if not (isinstance(p1, list) and isinstance(p2, list) and len(p1) == 2 and len(p2) == 2):
        return jsonify({"error": "servono p1 e p2 come [x, y]"}), 400
    poster = pdir / "poster.jpg"
    if not poster.is_file():
        return jsonify({"error": "la persona non ha un poster"}), 404
    from . import avatar_prep
    try:
        r = avatar_prep.prepara_da_bocca(poster, pdir, p1, p2)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "box": r["box"]})


@bp.get("/personas/<name>/mouthbox")
def persona_mouthbox(name: str):
    """Posizione della bocca sul poster, in frazioni 0-1, per il renderer del
    modo C: sovrappone la bocca DENTRO il volto invece che in un riquadro a
    parte. Ricavata dal box del volto (wav2lip/box.json): la bocca sta circa a
    tre quarti dell'altezza del volto, centrata, larga poco meno di metà."""
    pdir = _persona_dir(name)
    if pdir is None:
        return jsonify({"error": "persona non trovata"}), 404
    bf = pdir / "wav2lip" / "box.json"
    if not bf.is_file():
        return jsonify({"error": "asset non presente"}), 404
    b = json.loads(bf.read_text(encoding="utf-8"))
    pw, ph = b.get("poster_w", 1) or 1, b.get("poster_h", 1) or 1
    bocca = b.get("bocca")
    if bocca and bocca.get("p1") and bocca.get("p2"):
        # Posizione REALE della bocca, dai due angoli segnati a mano: centro,
        # e larghezza un po' oltre gli angoli (la bocca aperta è più larga).
        import math
        p1, p2 = bocca["p1"], bocca["p2"]
        cx = (p1[0] + p2[0]) / 2 / pw
        cy = (p1[1] + p2[1]) / 2 / ph
        w = math.hypot(p2[0] - p1[0], p2[1] - p1[1]) * 1.3 / pw
    else:
        # Ripiego per i box vecchi: bocca a tre quarti dell'altezza del volto.
        cx = (b["x"] + b["w"] * 0.5) / pw
        cy = (b["y"] + b["h"] * 0.78) / ph
        w = (b["w"] * 0.42) / pw
    resp = jsonify({"cx": cx, "cy": cy, "w": w})
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@bp.get("/personas/<name>/poster")
def persona_poster(name: str):
    jpg = poster.per_nome(name, _personas_root())
    if jpg is None:
        return jsonify({"error": "nessun clip da cui ricavare il poster"}), 404
    resp = send_file(jpg, mimetype="image/jpeg", conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


# ======================================================================
#  Wizard di creazione avatar (da mobile o PC)
#
#  Una bozza raccoglie i materiali passo per passo; all'`elabora` diventa una
#  persona vera, con gli asset per i modi A (Wav2Lip) e C (visemi) generati sul
#  PC. Aperto: chiunque puo' crearne uno (scelta dell'utente). Lo spazio
#  per-utente e la moderazione sono una fase successiva, prima della
#  pubblicazione pubblica.
# ======================================================================
import uuid as _uuid

_WIZARD_STEPS = ("foto", "pose", "video", "voce")
_STEP_EXT = {"foto": ".jpg", "pose": ".jpg", "video": ".mp4", "voce": ".wav"}


def _wizard_root() -> Path:
    d = _personas_root().parent / "wizard-draft"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _draft_dir(draft_id: str) -> Path | None:
    safe = re.sub(r"[^a-f0-9]", "", (draft_id or "").lower())
    if len(safe) != 32:
        return None
    d = _wizard_root() / safe
    return d if d.is_dir() else None


@bp.post("/avatar/wizard/start")
def wizard_start():
    draft_id = _uuid.uuid4().hex
    (_wizard_root() / draft_id).mkdir(parents=True, exist_ok=True)
    log.info("Wizard avatar: bozza %s aperta", draft_id)
    return jsonify({"draft_id": draft_id, "steps": list(_WIZARD_STEPS)})


@bp.post("/avatar/wizard/<draft_id>/<step>")
def wizard_upload(draft_id: str, step: str):
    d = _draft_dir(draft_id)
    if d is None:
        return jsonify({"error": "bozza non trovata"}), 404
    if step not in _WIZARD_STEPS:
        return jsonify({"error": f"passo sconosciuto: {step}"}), 400
    file = request.files.get("file") or request.files.get(step)
    if file is None or not file.filename:
        return jsonify({"error": "campo multipart 'file' mancante"}), 400
    # Un nome fisso per passo: la bozza tiene un materiale per tipo (le 'pose'
    # si numerano). Il nome del client non si usa — non ci si fida di un
    # percorso che arriva da fuori.
    if step == "pose":
        n = len(list(d.glob("pose-*"))) + 1
        dest = d / f"pose-{n}.jpg"
    else:
        dest = d / f"{step}{_STEP_EXT[step]}"
    file.save(dest)
    log.info("Wizard %s: %s -> %s (%d byte)", draft_id, step, dest.name,
             dest.stat().st_size)
    return jsonify({"ok": True, "file": dest.name})


@bp.post("/avatar/wizard/<draft_id>/elabora")
def wizard_elabora(draft_id: str):
    d = _draft_dir(draft_id)
    if d is None:
        return jsonify({"error": "bozza non trovata"}), 404
    body = request.get_json(silent=True) or {}
    nome = re.sub(r"[^A-Za-z0-9_]", "_", (body.get("name") or "").strip())
    if not nome:
        return jsonify({"error": "nome mancante"}), 400
    foto = d / "foto.jpg"
    if not foto.is_file():
        return jsonify({"error": "la foto e' obbligatoria"}), 400

    pdir = _personas_root() / nome
    if pdir.exists():
        return jsonify({"error": f"esiste gia' un avatar '{nome}'"}), 409
    pdir.mkdir(parents=True)
    shutil.copyfile(foto, pdir / "poster.jpg")

    # Voce clonata (se c'e' il campione): la fase CA-3 la aggancera' a XTTS.
    voce = d / "voce.wav"
    manifest = {"name": nome, "voice": {"engine": "xtts", "voice": nome}}
    if voce.is_file():
        (pdir / "voce-campione.wav").write_bytes(voce.read_bytes())
    else:
        manifest["voice"] = {"engine": "kokoro", "voice": "im_nicola"}
    _write_manifest(pdir, manifest)

    # Asset per i modi A e C, sul PC.
    from . import avatar_prep
    asset = avatar_prep.prepara(pdir)

    shutil.rmtree(d, ignore_errors=True)
    log.info("Wizard %s: avatar '%s' creato (asset: %s)", draft_id, nome, asset)
    return jsonify({"ok": True, "persona": nome, "asset": asset})


@bp.delete("/avatar/wizard/<draft_id>")
def wizard_annulla(draft_id: str):
    d = _draft_dir(draft_id)
    if d is not None:
        shutil.rmtree(d, ignore_errors=True)
    return jsonify({"ok": True})
