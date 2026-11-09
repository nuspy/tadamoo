"""The minutes of a meeting: the transcript goes through the analysis pipeline (summary, highlights, agreements, tasks per person) and the
result is written next to it. The analysis reads through the direct brain, which has no tools; the transcript is data, never instructions."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .. import analysis as analysis_mod
from ..config import Config

log = logging.getLogger("callbridge.meetbot.summary")


def analyze_meeting(cfg: Config, meeting_dir: Path, meta: dict) -> dict:
    """Read `transcript.jsonl`, write `analysis.json` and `summary.md`, and record the outcome in `meta.json` (`analysis_state`: done or error)."""
    segments: list[dict] = []
    tpath = meeting_dir / "transcript.jsonl"
    if tpath.is_file():
        for line in tpath.read_text(encoding="utf-8").splitlines():
            try:
                segments.append(json.loads(line))
            except ValueError:
                continue
    try:
        if not segments:
            raise RuntimeError("the transcript is empty")
        result = analysis_mod.analyze_transcript(cfg, segments, participants=meta.get("participants") or None, kind="meeting")
        (meeting_dir / "analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        (meeting_dir / "summary.md").write_text(analysis_mod.analysis_to_markdown(meta.get("title") or meeting_dir.name, result), encoding="utf-8")
        meta["analysis_state"] = "done"
    except Exception as e:  # noqa: BLE001
        log.error("analysis of meeting %s failed: %s", meeting_dir.name, e)
        meta["analysis_state"], meta["analysis_error"] = "error", str(e)
    (meeting_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta
