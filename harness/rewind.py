"""Time travel for a workspace: list the checkpoints the agent took, say what went on after each, and go back to one.

Putting the files back is only half of going back. What the agent did after a checkpoint with an irreversible effect
(a command it ran, a call it placed, a tool of an MCP server) is not undone by restoring files, so the list names
those actions and a rewind to a point that has any needs --yes.

    python -m harness.rewind --workspace D:\\proj                  list the checkpoints
    python -m harness.rewind --workspace D:\\proj --to cp_1_2      show what would change
    python -m harness.rewind --workspace D:\\proj --to cp_1_2 --yes
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.audit import AuditLedger
from harness.checkpoints import CheckpointManager
from harness.stack import data_dir_for

SUMMARY_CHARS = 100


def _summary(details: Dict[str, Any]) -> str:
    args = details.get("arguments")
    if isinstance(args, dict):
        key = next((k for k in ("command", "path", "number", "url", "query") if k in args), None)
        text = f"{details.get('tool')} {args[key]}" if key else f"{details.get('tool')} {json.dumps(args, ensure_ascii=False)}"
    else:
        text = str(details.get("tool"))
    return " ".join(text.split())[:SUMMARY_CHARS]


def checkpoint_report(data_dir: Path, workspace: Path) -> List[Dict[str, Any]]:
    """Each checkpoint, oldest first, with the tool calls that ran after it and which of them a rewind cannot undo."""
    checkpoints = CheckpointManager(workspace, storage_dir=data_dir / "checkpoints")
    records = AuditLedger(data_dir / "audit.jsonl").records
    after: Dict[str, List[Dict[str, Any]]] = {}
    marks = [(i, r["details"]["id"]) for i, r in enumerate(records)
             if r.get("action") == "checkpoint" and isinstance(r.get("details"), dict) and "id" in r["details"]]
    for pos, (index, cp_id) in enumerate(marks):
        ran = [r for r in records[index + 1:] if r.get("action") == "tool_executed" and isinstance(r.get("details"), dict)]
        after[cp_id] = [{"tool": r["details"].get("tool"), "effect": r["details"].get("effect"), "what": _summary(r["details"]),
                         "time": r.get("timestamp")} for r in ran if r["details"].get("effect") != "pure"]
    out = []
    for meta in checkpoints.list_checkpoints():
        ran = after.get(meta["checkpoint_id"], [])
        out.append({"id": meta["checkpoint_id"], "time": meta.get("timestamp"), "description": meta.get("description", ""),
                    "files": len(meta.get("files", [])), "after": ran, "not_undone": [a for a in ran if a["effect"] == "irreversible"]})
    return out


def rewind(data_dir: Path, workspace: Path, checkpoint_id: str, delete_new: bool = True) -> Dict[str, Any]:
    """Restores the files and records it; the state just before is kept as a checkpoint, so a rewind can be undone too."""
    checkpoints = CheckpointManager(workspace, storage_dir=data_dir / "checkpoints")
    checkpoints.restore(checkpoint_id, delete_new=delete_new)
    ledger = AuditLedger(data_dir / "audit.jsonl")
    ledger.record("user", "rewind", {"to": checkpoint_id, "safety_checkpoint": checkpoints.last_safety_checkpoint})
    return {"restored": checkpoint_id, "safety_checkpoint": checkpoints.last_safety_checkpoint}


def _when(ts: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "?"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m harness.rewind", description="List and go back to the agent's checkpoints")
    p.add_argument("--workspace", default=".", help="the folder the agent worked in")
    p.add_argument("--data-dir", default=None, help="the agent's data folder (default: the one for this workspace)")
    p.add_argument("--to", default=None, metavar="CHECKPOINT", help="go back to this checkpoint")
    p.add_argument("--keep-new", action="store_true", help="do not delete files created since the checkpoint")
    p.add_argument("--yes", action="store_true", help="go back even though some later actions cannot be undone")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        print(f"Not a folder: {workspace}", file=sys.stderr)
        return 2
    data_dir = Path(args.data_dir) if args.data_dir else data_dir_for(workspace)
    report = checkpoint_report(data_dir, workspace)

    if args.to is None:
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        elif not report:
            print("No checkpoints for this folder: the agent has not changed anything here yet.")
        for cp in report:
            if args.json:
                break
            print(f"{cp['id']}  {_when(cp['time'])}  {cp['description']}  ({cp['files']} files)")
            for a in cp["after"]:
                mark = "cannot be undone" if a["effect"] == "irreversible" else "files restored"
                print(f"    then: {a['what']}  [{a['effect']}: {mark}]")
        return 0

    target = next((c for c in report if c["id"] == args.to), None)
    if target is None:
        print(f"No checkpoint named {args.to}. Run without --to to list them.", file=sys.stderr)
        return 2
    if target["not_undone"] and not args.yes:
        print(f"Going back to {args.to} puts the files back, but these later actions stay done:", file=sys.stderr)
        for a in target["not_undone"]:
            print(f"    {a['what']}", file=sys.stderr)
        print("Run again with --yes to go back anyway.", file=sys.stderr)
        return 3
    try:
        result = rewind(data_dir, workspace, args.to, delete_new=not args.keep_new)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"Could not go back: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({**result, "not_undone": target["not_undone"]}, indent=2, ensure_ascii=False))
    else:
        print(f"Back at {args.to}. To undo this rewind: python -m harness.rewind --workspace \"{workspace}\" --to {result['safety_checkpoint']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
