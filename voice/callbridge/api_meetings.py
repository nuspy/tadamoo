"""Blueprint /meetbot/* e /meetings/* — meeting bot e storico riunioni.

Bot (UNA sessione per volta — vincolo del backend Windows-loopback):
  POST /meetbot/join   {"url", "display_name"?, "title"?}  -> 202 {session_id}
  GET  /meetbot/status                                     -> stato live
  POST /meetbot/leave                                      -> avvia l'analisi

Storico (artifacts/meetings/<id>/):
  GET    /meetings/list
  GET    /meetings/<id>        meta + analysis (se pronta)
  GET    /meetings/<id>/transcript
  DELETE /meetings/<id>
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from .calendar_oauth import GoogleCalendar, MicrosoftCalendar
from .meetbot import MeetBotSession
from .meetbot.calendar_watch import AutojoinStore, CalendarWatcher
from .meetbot.summary import analyze_meeting
from .util import is_safe_artifact_id

log = logging.getLogger("callbridge.api.meetings")

bp = Blueprint("meetings", __name__)

_lock = threading.Lock()
_session: MeetBotSession | None = None
_analyzing: str | None = None  # meeting id in analisi post-riunione

# Provider calendario + watcher (lazy, una volta per processo).
_cal_lock = threading.Lock()
_google: GoogleCalendar | None = None
_ms: MicrosoftCalendar | None = None
_watcher: CalendarWatcher | None = None


def _cfg():
    return current_app.config["CB_CONFIG"]


def _ensure_calendar(cfg):
    """Crea provider + watcher al primo uso (il watcher fa anche auto-join)."""
    global _google, _ms, _watcher
    with _cal_lock:
        if _google is None:
            _google = GoogleCalendar(cfg)
            _ms = MicrosoftCalendar(cfg)
            store = AutojoinStore(_root(cfg) / "autojoin.json")

            def _join_cb(url: str, title: str) -> bool:
                ok, detail = _do_join(cfg, url, title=title)
                if not ok:
                    log.warning("auto-join rifiutato: %s", detail)
                return ok

            _watcher = CalendarWatcher(cfg, _google, _ms, store, _join_cb)
            _watcher.start()
    return _google, _ms, _watcher


def _root(cfg) -> Path:
    p = Path(cfg.meetbot_artifacts_dir)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[1] / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def _do_join(cfg, url: str, *, title: str | None = None,
             display_name: str | None = None,
             project_id: str | None = None) -> tuple[bool, str]:
    """Crea e avvia una sessione (riusato da endpoint e auto-join)."""
    global _session
    with _lock:
        if _session is not None and _session.state in (
                "joining", "waiting_admit", "in_call", "leaving"):
            return False, "una riunione è già in corso (una alla volta)"
        session = MeetBotSession(cfg, url, display_name=display_name,
                                 artifacts_dir=_root(cfg))
        meta = {"title": (title or "").strip() or url,
                "url": url,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "project_id": (project_id or "").strip() or None,
                "analysis_state": "pending"}
        (session.dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        session.start()
        _session = session
        return True, session.session_id


@bp.post("/meetbot/join")
def meetbot_join():
    cfg = _cfg()
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url.startswith("http"):
        return jsonify({"error": "campo 'url' mancante o non valido"}), 400
    ok, detail = _do_join(cfg, url, title=body.get("title"),
                          display_name=body.get("display_name"),
                          project_id=body.get("project_id"))
    if not ok:
        return jsonify({"error": detail}), 409
    return jsonify({"session_id": detail,
                    "state": _session.state if _session else "joining"}), 202


@bp.get("/meetbot/status")
def meetbot_status():
    if _session is None:
        return jsonify({"state": "idle"})
    return jsonify(_session.status())


@bp.post("/meetbot/leave")
def meetbot_leave():
    global _analyzing
    cfg = _cfg()
    # Sotto _lock: due POST /leave concorrenti avvierebbero due _finalize (=
    # doppia analisi) sullo stesso meeting. `_analyzing` fa da prenotazione.
    with _lock:
        session = _session
        if session is None:
            return jsonify({"state": "idle"})
        if _analyzing == session.session_id:
            return jsonify({"state": "leaving",
                            "session_id": session.session_id})
        _analyzing = session.session_id
    session.leave()

    def _finalize() -> None:
        global _analyzing
        # attendi la chiusura del thread playwright, poi analizza
        for _ in range(60):
            if session.state in ("ended", "error"):
                break
            time.sleep(1.0)
        mpath = session.dir / "meta.json"
        try:
            meta = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {"title": session.url}
        meta["participants"] = list(session.transcript.participants)
        meta["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        meta["segments"] = len(session.transcript.segments)
        try:
            analyze_meeting(cfg, session.dir, meta)
        finally:
            _analyzing = None

    threading.Thread(target=_finalize, name="meetbot-finalize",
                     daemon=True).start()
    return jsonify({"state": "leaving",
                    "session_id": session.session_id})


# --- storico -----------------------------------------------------------------

@bp.get("/meetings/list")
def meetings_list():
    cfg = _cfg()
    out = []
    for d in sorted(_root(cfg).iterdir(), reverse=True):
        mpath = d / "meta.json"
        if not mpath.is_file():
            continue
        try:
            meta = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        state = meta.get("analysis_state", "pending")
        if _analyzing == d.name:
            state = "analyzing"
        out.append({"id": d.name, "title": meta.get("title"),
                    "created": meta.get("created"), "ended": meta.get("ended"),
                    "participants": meta.get("participants") or [],
                    "segments": meta.get("segments"),
                    "analysis_state": state})
    return jsonify({"meetings": out})


@bp.get("/meetings/<mid>")
def meetings_get(mid: str):
    cfg = _cfg()
    d = _root(cfg) / Path(mid).name
    mpath = d / "meta.json"
    if not mpath.is_file():
        return jsonify({"error": "riunione non trovata"}), 404
    try:
        meta = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return jsonify({"error": "meta riunione illeggibile"}), 500
    payload = {"id": d.name, **meta}
    if _analyzing == d.name:
        payload["analysis_state"] = "analyzing"
    apath = d / "analysis.json"
    if apath.is_file():
        try:
            payload["analysis"] = json.loads(apath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass  # analisi corrotta: restituisci comunque la meta
    return jsonify(payload)


@bp.post("/meetings/<mid>/push-wiki")
def meetings_push_wiki(mid: str):
    """Inserisce (o ri-inserisce) una riunione analizzata nel wiki del
    progetto. Body opzionale {"project_id"}: collega/cambia il progetto."""
    from .meetbot.summary import push_meeting_to_wiki
    cfg = _cfg()
    d = _root(cfg) / Path(mid).name
    mpath = d / "meta.json"
    apath = d / "analysis.json"
    if not mpath.is_file():
        return jsonify({"error": "riunione non trovata"}), 404
    if not apath.is_file():
        return jsonify({"error": "analisi non disponibile (rianalizza prima)"}), 409
    try:
        meta = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return jsonify({"error": "meta riunione illeggibile"}), 500
    body = request.get_json(silent=True) or {}
    if body.get("project_id"):
        meta["project_id"] = body["project_id"]
    if not meta.get("project_id"):
        return jsonify({"error": "riunione senza progetto: indica project_id"}), 400
    try:
        analysis = json.loads(apath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return jsonify({"error": "analisi illeggibile"}), 500
    wiki_path = push_meeting_to_wiki(cfg, d, meta, analysis)
    if not wiki_path:
        return jsonify({"error": "wikibridge non raggiungibile (server :8775 attivo?)"}), 502
    meta["wiki_path"] = wiki_path
    mpath.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    return jsonify({"wiki_path": wiki_path})


@bp.get("/meetings/<mid>/transcript")
def meetings_transcript(mid: str):
    cfg = _cfg()
    tpath = _root(cfg) / Path(mid).name / "transcript.jsonl"
    if not tpath.is_file():
        return jsonify({"error": "trascrizione non disponibile"}), 404
    segments = []
    for line in tpath.read_text(encoding="utf-8").splitlines():
        try:
            segments.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return jsonify({"segments": segments})


# --- calendari (auto-join) -----------------------------------------------------

@bp.get("/calendar/status")
def calendar_status():
    cfg = _cfg()
    google, ms, _ = _ensure_calendar(cfg)
    return jsonify({
        "google": {"configured": google.configured(),
                   "connected": google.connected(),
                   "flow_error": google.flow_error},
        "microsoft": {"configured": ms.configured(),
                      "connected": ms.connected(),
                      "flow_error": ms.flow_error},
    })


@bp.post("/calendar/<provider>/connect")
def calendar_connect(provider: str):
    cfg = _cfg()
    google, ms, _ = _ensure_calendar(cfg)
    if provider == "google":
        return jsonify(google.start_connect())
    if provider == "microsoft":
        return jsonify(ms.start_connect())
    return jsonify({"error": "provider sconosciuto"}), 400


@bp.post("/calendar/<provider>/disconnect")
def calendar_disconnect(provider: str):
    cfg = _cfg()
    google, ms, _ = _ensure_calendar(cfg)
    if provider == "google":
        google.disconnect()
    elif provider == "microsoft":
        ms.disconnect()
    else:
        return jsonify({"error": "provider sconosciuto"}), 400
    return jsonify({"status": "ok"})


@bp.get("/calendar/events")
def calendar_events():
    cfg = _cfg()
    _, _, watcher = _ensure_calendar(cfg)
    days = max(1, min(31, request.args.get("days", default=7, type=int)))
    events = watcher.fetch_events(days=days)
    return jsonify({"events": events})


@bp.post("/calendar/events/<path:event_id>/autojoin")
def calendar_autojoin(event_id: str):
    cfg = _cfg()
    _, _, watcher = _ensure_calendar(cfg)
    body = request.get_json(silent=True) or {}
    enabled = bool(body.get("enabled"))
    watcher.store.set(event_id, enabled)
    return jsonify({"id": event_id, "autojoin": enabled})


@bp.delete("/meetings/<mid>")
def meetings_delete(mid: str):
    cfg = _cfg()
    if not is_safe_artifact_id(mid):
        return jsonify({"error": "id non valido"}), 400
    root = _root(cfg)
    d = root / mid
    if d.resolve().parent != root.resolve():
        return jsonify({"error": "id non valido"}), 400
    if not d.is_dir():
        return jsonify({"error": "riunione non trovata"}), 404
    if _session is not None and _session.session_id == d.name \
            and _session.state in ("joining", "waiting_admit", "in_call"):
        return jsonify({"error": "riunione in corso"}), 409
    shutil.rmtree(d)
    return jsonify({"status": "ok"})
