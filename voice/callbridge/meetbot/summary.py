"""Analisi di fine riunione: wrapper su callbridge.analysis con i
partecipanti osservati (per i task per-partecipante) + scrittura artifacts.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .. import analysis as analysis_mod
from .. import knowledge_client
from ..config import Config

log = logging.getLogger("callbridge.meetbot.summary")


def analyze_meeting(cfg: Config, meeting_dir: Path, meta: dict) -> dict:
    """Legge transcript.jsonl, produce analysis.json + summary.md.
    Aggiorna e riscrive meta.json (stato analyzed|analysis_error).
    Se la riunione è collegata a un progetto (meta.project_id), l'analisi
    riceve la conoscenza del progetto (knowledge_client.project_context) e a
    fine corsa la riunione viene inserita nella sezione meetings/ del wiki."""
    tpath = meeting_dir / "transcript.jsonl"
    segments: list[dict] = []
    if tpath.is_file():
        for line in tpath.read_text(encoding="utf-8").splitlines():
            try:
                segments.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    project_id = meta.get("project_id") or None
    try:
        if not segments:
            raise RuntimeError("trascrizione vuota")
        # conoscenza = intersezione dei permessi di chi salva e dei
        # partecipanti riconosciuti: sono loro a vedere la pagina della
        # riunione; chi non è riconosciuto non la vede e non restringe
        result = analysis_mod.analyze_transcript(
            cfg, segments,
            participants=meta.get("participants") or None,
            kind="riunione",
            preamble=knowledge_client.project_context(
                cfg, project_id, knowledge_client.transcript_query(segments),
                participants=meta.get("participants") or None,
                unresolved="ignore") or None)
        (meeting_dir / "analysis.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        (meeting_dir / "summary.md").write_text(
            analysis_mod.analysis_to_markdown(
                meta.get("title") or meeting_dir.name, result),
            encoding="utf-8")
        meta["analysis_state"] = "done"
        if project_id:
            wiki_path = push_meeting_to_wiki(cfg, meeting_dir, meta, result)
            if wiki_path:
                meta["wiki_path"] = wiki_path
    except Exception as e:  # noqa: BLE001
        log.error("analisi riunione %s fallita: %s", meeting_dir.name, e)
        meta["analysis_state"] = "error"
        meta["analysis_error"] = str(e)
    (meeting_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def push_meeting_to_wiki(cfg: Config, meeting_dir: Path, meta: dict,
                         analysis: dict, *, source: str = "meetbot") -> str | None:
    """Push (o ri-push) di una riunione analizzata nel wiki del progetto."""
    project_id = meta.get("project_id")
    if not project_id:
        return None
    return knowledge_client.push_meeting(
        cfg, project_id,
        meeting_id=meeting_dir.name,
        title=meta.get("title") or meeting_dir.name,
        date=(meta.get("created") or "")[:10],
        source=source,
        participants=meta.get("participants") or [],
        analysis=analysis,
        transcript_path=str(meeting_dir / "transcript.jsonl"))
