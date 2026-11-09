"""Blueprint /analysis/* — analisi di video/audio (file o URL).

Pipeline (job in thread, UNO alla volta):
  downloading  (solo URL: yt-dlp -x -> m4a/opus)
  extracting   (ffmpeg -> wav mono 16k, spezzato in segmenti da N secondi)
  transcribing (ogni segmento -> l'STT del servizio, in-process; percent reale)
  diarizing    (engines.diarize: chi parla quando -> "Speaker 1", "Speaker 2")
  analyzing    (analysis.analyze_transcript: riassunto + salienti + task)
  done | error

Artifacts in {analysis_artifacts_dir}/<id>/:
  source.json     {title, source_type, source, created, duration_s, speakers_found}
  transcript.json {"segments": [{start_s, end_s, text, speaker?}]}
  analysis.json   {summary, highlights, tasks}
  summary.md      render leggibile

Endpoint:
  POST   /analysis/start        multipart "file" OPPURE json {"url", "title"?};
                                "speakers" (0 = automatico) fissa le voci attese;
                                "analyze" = 0 ferma il job dopo la trascrizione
                                (niente riassunto: il testo non passa dal cervello)
  GET    /analysis/list
  GET    /analysis/<id>         stato + progress + analysis (se done)
  GET    /analysis/<id>/transcript
  DELETE /analysis/<id>
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from . import analysis as analysis_mod
from . import models_store
from .engines import diarize
from .util import is_safe_artifact_id

log = logging.getLogger("callbridge.api.analysis")

bp = Blueprint("analysis", __name__)

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".wma"}

# Stato del job corrente (uno alla volta: trascrizione+analisi saturano gia'
# GPU e cervello; la coda la fa l'utente).
_job_lock = threading.Lock()
_current: dict | None = None  # {"id", "state", "progress", "error"}


def _cfg():
    return current_app.config["CB_CONFIG"]


def _root(cfg) -> Path:
    p = Path(cfg.analysis_artifacts_dir)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[1] / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def _set_state(state: str, *, phase: str | None = None, percent: float = 0.0,
               detail: str | None = None, error: str | None = None) -> None:
    # Snapshot: evita la corsa check-then-mutate se _current diventa None (job
    # finito) tra il controllo e la scrittura.
    cur = _current
    if cur is None:
        return
    cur["state"] = state
    cur["progress"] = {"phase": phase or state,
                       "percent": round(percent, 1),
                       "detail": detail}
    if error:
        cur["error"] = error


def _speakers(valore) -> int:
    """Numero di voci chiesto per QUESTO file (0 = lo decide il clustering).

    E' una proprieta' della registrazione, non dell'installazione: un'intervista
    e' a due, una riunione no, e saperlo vale piu' di qualunque soglia."""
    try:
        n = int(str(valore).strip())
    except (TypeError, ValueError):
        return 0
    return n if 0 <= n <= 20 else 0


def _analyze(valore) -> bool:
    """Se dopo la trascrizione va fatta l'analisi (riassunto, salienti, attivita').

    Vero se il campo manca: chi non lo manda ottiene quello di sempre. Solo un
    "no" esplicito (0, false, no, off) la salta."""
    return str(valore).strip().lower() not in ("0", "false", "no", "off")


def _segmenti_del_blocco(body: dict, offset_s: int, seg_s: int) -> list[dict]:
    """Risposta di /transcribe -> segmenti con i tempi ASSOLUTI del file.

    Con `?segments=1` arrivano i tempi di whisper, relativi al blocco: vanno
    riportati sull'intero file, altrimenti ogni blocco ripartirebbe da zero e la
    diarizzazione attribuirebbe le battute alle voci sbagliate. Senza (Handy
    esterno, che risponde solo `{"text"}`) si ricade sul comportamento storico:
    un segmento per blocco, largo quanto il blocco."""
    parti = body.get("segments") or []
    if not parti:
        text = (body.get("text") or "").strip()
        return [{"start_s": offset_s, "end_s": offset_s + seg_s, "text": text}] \
            if text else []
    fuori = []
    for s in parti:
        testo = (s.get("text") or "").strip()
        if not testo:
            continue
        seg = {"start_s": offset_s + float(s.get("start_s") or 0.0),
               "end_s": offset_s + float(s.get("end_s") or 0.0), "text": testo}
        parole = s.get("words") or []
        if parole:
            seg["words"] = [{"start_s": offset_s + float(w.get("start_s") or 0.0),
                             "end_s": offset_s + float(w.get("end_s") or 0.0),
                             "text": w.get("text") or ""} for w in parole]
        fuori.append(seg)
    return fuori


def _diarizza(cfg, job_id: str, seg_files: list[Path], segments: list[dict],
              source: dict) -> None:
    """Attribuisce le battute alle voci. Modifica `segments` sul posto.

    Non solleva: la diarizzazione e' un di piu' e un transcript senza etichette
    vale infinitamente piu' di un'analisi fallita."""
    durata_min = sum(f.stat().st_size for f in seg_files) / (16000 * 2 * 60)
    limite = float(getattr(cfg, "diarization_max_minutes", 240))
    if durata_min > limite:
        log.warning("analisi %s: niente speaker, %.0f min oltre il limite di "
                    "%.0f (l'audio andrebbe tutto in RAM)", job_id, durata_min, limite)
        return
    try:
        _set_state("diarizing", detail="riconosco le voci...")
        audio = diarize.read_wavs(seg_files)
        turni = diarize.get_diarizer(cfg).diarize(
            audio, progress=lambda f: _set_state("diarizing", phase="diarizing",
                                                 percent=100.0 * f,
                                                 detail="riconosco le voci..."),
            num_speakers=int(source.get("speakers") or 0))
        del audio                      # ~230 MB/ora: non deve sopravvivere al job
        if not turni:
            return
        segments[:] = diarize.assign_speakers(segments, turni)
        source["speakers_found"] = len({t["speaker"] for t in turni})
        log.info("analisi %s: %d voci distinte in %d battute", job_id,
                 source["speakers_found"], len(segments))
    except Exception as e:  # noqa: BLE001 - meglio senza etichette che senza analisi
        log.error("analisi %s: diarizzazione fallita (%s): proseguo senza speaker",
                  job_id, e)


_source_lock = threading.Lock()


def _write_source(path: Path, meta: dict) -> None:
    """source.json scritto sotto lock: chi lo legge mentre il job finisce non ne vede mai meta'."""
    with _source_lock:
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def _read_source(path: Path) -> dict:
    with _source_lock:
        return json.loads(path.read_text(encoding="utf-8"))


def _transcribe_block(cfg, wav: Path, words: bool) -> dict:
    """Un blocco della registrazione (wav mono 16 kHz) attraverso l'STT del servizio: {text, segments}.

    Solleva `ModelNotInstalled` se il modello Whisper scelto non e' nella cartella dei modelli."""
    import wave

    import numpy as np

    from . import engines
    with wave.open(str(wav), "rb") as w:
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return engines._get_stt_engine(cfg).transcribe_detailed(
        audio, (getattr(cfg, "stt_language", "") or None), words)


def _stt_mancante(cfg) -> dict | None:
    """Il corpo della risposta 409 se il modello STT scelto non e' installato, altrimenti None."""
    model = "whisper-" + str(getattr(cfg, "stt_model", "") or "")
    if model in models_store.MODELS and not models_store.is_installed(model):
        return {"error": models_store.reason(model), "code": "model_not_installed", "model": model}
    return None


def _run_ffmpeg(cfg, args: list[str], timeout: int = 3600) -> None:
    cmd = [cfg.analysis_ffmpeg_path, "-y", "-hide_banner",
           "-loglevel", "error"] + args
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg fallito: {proc.stderr[-400:]}")


def _job(cfg, job_id: str, jdir: Path, source: dict) -> None:
    """Corpo del job (thread)."""
    global _current
    try:
        media = jdir / "input.media"
        # --- download (solo URL) ----------------------------------------
        if source["source_type"] == "url":
            _set_state("downloading", percent=0,
                       detail="scarico l'audio dal link (yt-dlp)...")
            cmd = [cfg.analysis_ytdlp_path, "-x", "--no-playlist",
                   "-o", str(jdir / "download.%(ext)s"), source["source"]]
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=3600)
            if proc.returncode != 0:
                raise RuntimeError(f"yt-dlp fallito: {proc.stderr[-400:]}")
            dl = sorted(jdir.glob("download.*"))
            if not dl:
                raise RuntimeError("yt-dlp non ha prodotto alcun file")
            media = dl[0]
            # titolo dal video se non fornito
            if not source.get("title"):
                t = subprocess.run([cfg.analysis_ytdlp_path, "--get-title",
                                    "--no-playlist", source["source"]],
                                   capture_output=True, text=True, timeout=120)
                if t.returncode == 0 and t.stdout.strip():
                    source["title"] = t.stdout.strip().splitlines()[0]

        # --- estrazione audio + segmentazione ---------------------------
        _set_state("extracting", detail="estraggo l'audio (ffmpeg)...")
        seg_dir = jdir / "segments"
        seg_dir.mkdir(exist_ok=True)
        seg_s = int(cfg.analysis_segment_s)
        _run_ffmpeg(cfg, ["-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "segment", "-segment_time", str(seg_s),
                          str(seg_dir / "seg%04d.wav")])
        seg_files = sorted(seg_dir.glob("seg*.wav"))
        if not seg_files:
            raise RuntimeError("nessun audio estratto (file senza traccia audio?)")
        source["duration_s"] = sum(
            f.stat().st_size for f in seg_files) // (16000 * 2)

        # --- trascrizione segmento per segmento -------------------------
        # Si decide PRIMA se si diarizzera': i tempi per parola costano ~20-30%
        # di inferenza in piu' e servono solo per attribuire le battute.
        diarizzare, perche_no = diarize.available(cfg)
        segments: list[dict] = []
        for i, f in enumerate(seg_files):
            _set_state("transcribing", phase="transcribing",
                       percent=100.0 * i / len(seg_files),
                       detail=f"segmento {i + 1}/{len(seg_files)}")
            body = _transcribe_block(cfg, f, diarizzare)
            segments += _segmenti_del_blocco(body, i * seg_s, seg_s)

        # --- diarizzazione: chi parla quando ----------------------------
        # Sull'audio INTERO, non blocco per blocco: il clustering delle voci e'
        # per-file, e diarizzare i pezzi separatamente darebbe uno "Speaker 1"
        # diverso in ognuno.
        if not diarizzare:
            log.info("analisi %s: niente speaker (%s)", job_id, perche_no)
        elif segments:
            _diarizza(cfg, job_id, seg_files, segments, source)

        (jdir / "transcript.json").write_text(
            json.dumps({"segments": [{k: v for k, v in s.items() if k != "words"}
                                     for s in segments]},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")
        if not segments:
            raise RuntimeError("trascrizione vuota (nessun parlato rilevato)")

        if not source.get("analyze", True):
            source["state"] = "done"
            _write_source(jdir / "source.json", source)      # prima dello stato: chi vede "done" trova gia' i dati
            _set_state("done", percent=100.0)
            return

        # --- analisi ------------------------------------------------------
        _set_state("analyzing", detail="riassunto, punti salienti e attività...")
        # Le voci riconosciute diventano i "partecipanti": cosi' le attivita'
        # escono intestate a Speaker 1/Speaker 2 invece che a "non specificato".
        voci = sorted({s["speaker"] for s in segments if s.get("speaker")})
        result = analysis_mod.analyze_transcript(
            cfg, segments, participants=voci or None,
            progress=lambda ph, pc: _set_state("analyzing", phase=ph,
                                               percent=pc),
            kind="registrazione")
        (jdir / "analysis.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        (jdir / "summary.md").write_text(
            analysis_mod.analysis_to_markdown(
                source.get("title") or job_id, result),
            encoding="utf-8")
        source["state"] = "done"
        _write_source(jdir / "source.json", source)
        _set_state("done", percent=100.0)
    except Exception as e:  # noqa: BLE001 - il job riporta, non crasha
        log.error("analisi %s fallita: %s", job_id, e)
        source["state"] = "error"
        source["error"] = str(e)
        _write_source(jdir / "source.json", source)
        _set_state("error", error=str(e))
    finally:
        source.setdefault("state", "error")
        _write_source(jdir / "source.json", source)
        # pulizia intermedi pesanti
        shutil.rmtree(jdir / "segments", ignore_errors=True)
        for f in jdir.glob("download.*"):
            f.unlink(missing_ok=True)
        (jdir / "input.media").unlink(missing_ok=True)


@bp.post("/analysis/start")
def analysis_start():
    global _current
    cfg = _cfg()
    if not shutil.which(cfg.analysis_ffmpeg_path):
        return jsonify({"error": "ffmpeg non trovato: installa con "
                                 "'winget install Gyan.FFmpeg'"}), 501
    mancante = _stt_mancante(cfg)
    if mancante is not None:
        return jsonify(mancante), 409
    with _job_lock:
        if _current is not None and _current["state"] not in ("done", "error"):
            return jsonify({"error": "un'analisi è già in corso", "code": "busy"}), 409

        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        jdir = _root(cfg) / job_id
        jdir.mkdir(parents=True)

        source: dict = {"created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "state": "running"}
        file = request.files.get("file")
        if file is not None and file.filename:
            ext = Path(file.filename).suffix.lower()
            if ext not in VIDEO_EXT | AUDIO_EXT:
                shutil.rmtree(jdir, ignore_errors=True)
                return jsonify({"error": f"estensione non supportata: {ext}"}), 400
            file.save(jdir / "input.media")
            source.update(source_type="file", source=Path(file.filename).name,
                          title=(request.form.get("title")
                                 or Path(file.filename).stem),
                          speakers=_speakers(request.form.get("speakers")),
                          analyze=_analyze(request.form.get("analyze", "1")))
        else:
            body = request.get_json(silent=True) or {}
            url = (body.get("url") or "").strip()
            if not re.match(r"^https?://", url):
                shutil.rmtree(jdir, ignore_errors=True)
                return jsonify({"error": "serve un file o un URL http(s)"}), 400
            source.update(source_type="url", source=url,
                          title=(body.get("title") or "").strip() or None,
                          speakers=_speakers(body.get("speakers")),
                          analyze=_analyze(body.get("analyze", "1")))

        _write_source(jdir / "source.json", source)
        _current = {"id": job_id, "state": "queued",
                    "progress": {"phase": "queued", "percent": 0,
                                 "detail": None}, "error": None}
        threading.Thread(target=_job, args=(cfg, job_id, jdir, source),
                         name="analysis-job", daemon=True).start()
    return jsonify({"id": job_id, "state": "queued"}), 202


@bp.get("/analysis/list")
def analysis_list():
    cfg = _cfg()
    out = []
    for d in sorted(_root(cfg).iterdir(), reverse=True):
        src = d / "source.json"
        if not src.is_file():
            continue
        try:
            meta = _read_source(src)
        except (OSError, json.JSONDecodeError):
            continue
        state = meta.get("state", "error")
        if _current is not None and _current["id"] == d.name:
            state = _current["state"]
        out.append({"id": d.name, "title": meta.get("title"),
                    "source_type": meta.get("source_type"),
                    "source": meta.get("source"),
                    "created": meta.get("created"),
                    "duration_s": meta.get("duration_s"),
                    "state": state, "error": meta.get("error")})
    return jsonify({"analyses": out})


@bp.get("/analysis/<job_id>")
def analysis_get(job_id: str):
    cfg = _cfg()
    jdir = _root(cfg) / Path(job_id).name
    src = jdir / "source.json"
    if not src.is_file():
        return jsonify({"error": "analisi non trovata"}), 404
    try:
        meta = _read_source(src)
    except (OSError, json.JSONDecodeError):
        return jsonify({"error": "source analisi illeggibile"}), 500
    payload: dict = {"id": jdir.name, **meta}
    cur = _current
    if cur is not None and cur["id"] == jdir.name:
        payload["state"] = cur["state"]
        payload["progress"] = cur["progress"]
        payload["error"] = cur["error"]
    apath = jdir / "analysis.json"
    if apath.is_file():
        try:
            payload["analysis"] = json.loads(apath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass  # analisi corrotta: restituisci comunque la source
    nomi = _speaker_names(jdir)
    if nomi and "analysis" in payload:
        # L'analisi e' stata scritta prima che le voci avessero un nome: i task
        # sono intestati a "Speaker 2" e il riassunto lo cita.
        payload["analysis"] = _rinomina(payload["analysis"], nomi)
    return jsonify(payload)


@bp.get("/analysis/<job_id>/transcript")
def analysis_transcript(job_id: str):
    cfg = _cfg()
    jdir = _root(cfg) / Path(job_id).name
    tpath = jdir / "transcript.json"
    if not tpath.is_file():
        return jsonify({"error": "trascrizione non disponibile"}), 404
    try:
        data = json.loads(tpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return jsonify({"error": "trascrizione illeggibile"}), 500
    nomi = _speaker_names(jdir)
    for s in data.get("segments") or []:
        etichetta = s.get("speaker")
        if not etichetta:
            continue
        # `speaker_label` resta l'etichetta ORIGINALE: e' la chiave con cui
        # rinominare, e senza di essa la seconda rinomina di una voce (da
        # "Andrea" a "Andrea T.") non troverebbe piu' nulla da sostituire.
        s["speaker_label"] = etichetta
        if etichetta in nomi:
            s["speaker"] = nomi[etichetta]
    return jsonify(data)


def _speaker_names(jdir: Path) -> dict:
    """Mappa "Speaker 1" -> nome dato dall'utente (vuota se non ne ha dati)."""
    try:
        meta = _read_source(jdir / "source.json")
    except (OSError, json.JSONDecodeError):
        return {}
    nomi = meta.get("speaker_names")
    return nomi if isinstance(nomi, dict) else {}


def _rinomina(valore, nomi: dict):
    """Sostituisce le etichette con i nomi in ogni stringa della struttura.

    Applicata in LETTURA: gli artifact conservano "Speaker 1", cosi' la rinomina
    resta reversibile e rifarla una seconda volta continua a funzionare (dopo
    aver riscritto i file, "Speaker 1" non ci sarebbe piu' da cercare)."""
    if isinstance(valore, str):
        for etichetta, nome in nomi.items():
            valore = valore.replace(etichetta, nome)
        return valore
    if isinstance(valore, list):
        return [_rinomina(v, nomi) for v in valore]
    if isinstance(valore, dict):
        return {k: _rinomina(v, nomi) for k, v in valore.items()}
    return valore


@bp.post("/analysis/<job_id>/speakers")
def analysis_speakers(job_id: str):
    """Dai un nome alle voci: {"names": {"Speaker 1": "Andrea"}}.

    La diarizzazione distingue le voci ma non sa di chi sono; chi ascolta si'."""
    cfg = _cfg()
    if not is_safe_artifact_id(job_id):
        return jsonify({"error": "id non valido"}), 400
    jdir = _root(cfg) / job_id
    spath = jdir / "source.json"
    if not spath.is_file():
        return jsonify({"error": "analisi non trovata"}), 404
    try:
        meta = _read_source(spath)
    except (OSError, json.JSONDecodeError):
        return jsonify({"error": "source analisi illeggibile"}), 500
    body = request.get_json(silent=True) or {}
    nomi = body.get("names")
    if not isinstance(nomi, dict):
        return jsonify({"error": "serve {\"names\": {\"Speaker 1\": \"...\"}}"}), 400
    correnti = meta.get("speaker_names")
    correnti = dict(correnti) if isinstance(correnti, dict) else {}
    for etichetta, nome in nomi.items():
        nome = str(nome or "").strip()[:80]
        if nome:
            correnti[str(etichetta)] = nome
        else:
            correnti.pop(str(etichetta), None)   # nome svuotato = torna "Speaker N"
    meta["speaker_names"] = correnti
    _write_source(spath, meta)
    return jsonify({"status": "ok", "speaker_names": correnti})


@bp.delete("/analysis/<job_id>")
def analysis_delete(job_id: str):
    cfg = _cfg()
    if not is_safe_artifact_id(job_id):
        return jsonify({"error": "id non valido"}), 400
    root = _root(cfg)
    jdir = root / job_id
    if jdir.resolve().parent != root.resolve():
        return jsonify({"error": "id non valido"}), 400
    if not jdir.is_dir():
        return jsonify({"error": "analisi non trovata"}), 404
    if _current is not None and _current["id"] == jdir.name \
            and _current["state"] not in ("done", "error"):
        return jsonify({"error": "analisi in corso: attendi la fine"}), 409
    shutil.rmtree(jdir)
    return jsonify({"status": "ok"})
