"""Blueprint /personas/*: the old callAPIcall personality archive (`<avatars folder>/<name>/manifest.json` and its clips).

  GET    /personas                      list with manifest and clip count
  POST   /personas                      {"name", "voice": {engine, voice}}
  PUT    /personas/<name>               update the manifest (voice and/or name)
  DELETE /personas/<name>               remove it (409 while it is the active avatar)
  POST   /personas/<name>/clips         multipart upload of an mp4
  DELETE /personas/<name>/clips/<file>  remove a clip

The avatar routes (/avatar/*, the poster and the Wav2Lip assets) are in api_avatar.py.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from .util import is_safe_artifact_id

log = logging.getLogger("callbridge.api.personas")

bp = Blueprint("personas", __name__)

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_CLIP_RE = re.compile(r"^[a-zA-Z0-9_. -]+\.mp4$")

# Job di ingest documenti->personalita' (Fase 5), uno per persona. Stato in
# memoria, interrogato via GET /personas/<name>/ingest/state.
_ingest_jobs: dict[str, dict] = {}
_ingest_lock = threading.Lock()


def _cfg():
    return current_app.config["CB_CONFIG"]


def _personas_root() -> Path:
    return Path(_cfg().avatars_dir)


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
