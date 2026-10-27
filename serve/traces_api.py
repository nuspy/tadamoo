"""The traces of the Chat and Agent conversations for the LOGS page: the list, a replay, a masked export and the audit ledger.

A trace is read from the conversation's saved history (harness/replay.py); the audit ledger is the hash-chained record
the conversation's folder keeps (harness/audit.py). Nothing here runs a tool or calls the model: a replay answers from
what was recorded, with stand-ins for the tools.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from harness import replay as replay_mod
from harness.audit import AuditLedger
from harness.stack import data_dir_for
from serve.agent_runs import RunError

REPLAY_MODES = ("read_only", "default", "auto")
LEDGER_ROWS = 200                      # rows of the ledger shown at most (the newest)
LEDGER_ROWS_MAX = 1000
PROBLEMS_SHOWN = 10
MAX_TRACE_BYTES = 64 << 20             # a history larger than this is not replayed or exported by a request


def _history_file(sessions: Any, sid: str) -> Path:
    return sessions.history_dir(sid) / "history.json"


def _record(sessions: Any, sid: str) -> Dict[str, Any]:
    return sessions.get(sid)


def listing(sessions: Any) -> Dict[str, Any]:
    """The conversations that have a saved history, newest first, with what the page needs to offer them."""
    rows: List[Dict[str, Any]] = []
    for rec in sessions.view()["sessions"]:
        file = _history_file(sessions, rec["id"])
        try:
            info = file.stat()
        except OSError:
            continue
        rows.append({"id": rec["id"], "title": rec.get("title", ""), "workspace": rec.get("workspace", ""), "mode": rec.get("mode", ""),
                     "state": rec.get("state", ""), "turns": rec.get("turns", 0), "updated": rec.get("updated", 0),
                     "bytes": info.st_size, "too_large": info.st_size > MAX_TRACE_BYTES})
    return {"sessions": rows}


def _checked(sessions: Any, sid: str) -> Path:
    _record(sessions, sid)                                      # raises for a conversation that is not in the list
    file = _history_file(sessions, sid)
    if not file.is_file():
        raise RunError("This conversation has no saved history yet.", "no_trace")
    if file.stat().st_size > MAX_TRACE_BYTES:
        raise RunError("This conversation is too large to be read in one request.", "trace_too_large")
    return file.parent


def export(sessions: Any, sid: str) -> Tuple[bytes, str]:
    """The trace as JSONL, credential-looking values masked, and the file name to offer."""
    folder = _checked(sessions, sid)
    rec = _record(sessions, sid)
    text = replay_mod.export_history(folder, meta={"session": sid, "title": rec.get("title", ""), "mode": rec.get("mode", "")})
    return text.encode("utf-8"), f"basalt-trace-{sid}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"


def replay(sessions: Any, sid: str, mode: Any = "default", compare_results: bool = False) -> Dict[str, Any]:
    """Runs the conversation again with the recorded answers and the tools' recorded results, under the permission level
    asked for, and says whether the same tools were called with the same arguments and got the same decisions."""
    from harness.permissions import PermissionLevel, PermissionManager
    if mode not in REPLAY_MODES:
        raise ValueError("the permission level of a replay is read_only, default or auto")
    folder = _checked(sessions, sid)
    trace = replay_mod.load_history(folder)
    permissions = PermissionManager(level=PermissionLevel(mode), confirm_callback=replay_mod.confirm_from_trace(trace))
    report = replay_mod.replay(trace, permissions=permissions, compare_results=bool(compare_results))
    out: Dict[str, Any] = {"ok": report.ok, "mode": mode, "recorded_calls": report.recorded_calls, "replayed_calls": report.replayed_calls,
                           "steps": sum(len(seg.steps) for seg in trace.segments), "requests": len(trace.segments),
                           "stop_reason": report.stop_reason, "summary": report.summary(), "divergence": None}
    if report.divergence:
        d = report.divergence
        out["divergence"] = {"index": d.index, "kind": d.kind, "message": d.message, "recorded": d.recorded, "replayed": d.replayed}
    return out


def _ledger(sessions: Any, sid: str) -> Path:
    rec = _record(sessions, sid)
    folder = rec.get("workdir") or rec.get("workspace")
    if not folder:
        raise RunError("This conversation has no folder.", "no_trace")
    return data_dir_for(Path(folder).resolve()) / "audit.jsonl"


def _row(rec: Dict[str, Any]) -> Dict[str, Any]:
    details = rec.get("details") if isinstance(rec.get("details"), dict) else {}
    row = {"index": rec.get("index"), "time": rec.get("timestamp"), "actor": rec.get("actor"), "action": rec.get("action")}
    for key in ("tool", "permission", "outcome", "duration_ms", "effect", "reason"):
        if key in details:
            row[key] = details[key]
    if "arguments" in details:
        row["arguments"] = details["arguments"]
    return row


def audit(sessions: Any, sid: str, limit: Any = LEDGER_ROWS) -> Dict[str, Any]:
    """The audit ledger of the conversation's folder: whether its hash chain holds, and its newest rows. The ledger belongs to
    the folder, so it also holds what other conversations in the same folder did."""
    try:
        limit = min(LEDGER_ROWS_MAX, max(1, int(limit)))
    except (TypeError, ValueError):
        limit = LEDGER_ROWS
    path = _ledger(sessions, sid)
    if not path.is_file():
        return {"status": "missing", "count": 0, "problems": [], "rows": [], "folder_ledger": str(path)}
    ledger = AuditLedger(path)
    problems = ledger.problems()
    if problems:                                                # a record being written while the file was read looks broken for a moment
        time.sleep(0.25)
        ledger = AuditLedger(path)
        problems = ledger.problems()
    rows = [_row(r) for r in ledger.records[-limit:] if "_corrupt" not in r][::-1]
    status = "broken" if problems else ("empty" if not ledger.records else "intact")
    return {"status": status, "count": len(ledger.records), "problems": problems[:PROBLEMS_SHOWN], "problems_total": len(problems),
            "rows": rows, "folder_ledger": str(path), "shown": len(rows)}
