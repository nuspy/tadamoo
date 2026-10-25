"""Time travel for a workspace: list the checkpoints the agent took, say what went on after each, and go back to one.

The same classes of effect are read for a conversation (`session_impact`) and for a plan (`plan_impact`) before they are rewound.

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

SUMMARY_CHARS = 100


def _summary(details: Dict[str, Any]) -> str:
    args = details.get("arguments")
    if isinstance(args, dict):
        key = next((k for k in ("command", "path", "number", "url", "query") if k in args), None)
        text = f"{details.get('tool')} {args[key]}" if key else f"{details.get('tool')} {json.dumps(args, ensure_ascii=False)}"
    else:
        text = str(details.get("tool"))
    return " ".join(text.split())[:SUMMARY_CHARS]


def describe_call(name: str, arguments: Any) -> str:
    """One short line naming a tool call, for the lists that say what a rewind leaves done."""
    return _summary({"tool": name, "arguments": arguments})


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


# What a conversation's tool calls do, by name, for histories that did not record it. A name not found here is "unknown"
# and is treated like an effect that cannot be taken back.
KNOWN_EFFECTS: Dict[str, str] = {
    **dict.fromkeys(("read_file", "list_directory", "search_web", "browse_url", "list_attachments", "read_attachment", "search_attachment",
                     "browser_open", "browser_read", "find_capability", "load_capability", "ask_council", "wiki_search", "wiki_page",
                     "wiki_context", "diagnostics", "go_to_definition", "find_references", "plan_list", "plan_get", "ask_user", "repo_map", "recall"), "pure"),
    **dict.fromkeys(("write_file", "edit_file", "transcribe_attachment", "write_output_file", "publish_output_file", "plan_create", "plan_set_profile",
                     "plan_check", "plan_mark", "plan_step_start", "plan_propose_change", "plan_archive", "remember", "forget"), "reversible"),
    **dict.fromkeys(("shell_command", "browser_click", "browser_type", "browser_sign_in", "browser_select", "browser_press", "browser_back",
                     "call_capability", "mcp_call", "request_payment", "phone_call", "phone_say", "phone_listen", "phone_hangup", "run_code", "spawn_subagent",
                     "delegate_to_acp_agent", "ask_a2a_agent"), "irreversible"),
}
FILE_TOOLS = ("write_file", "edit_file")
NOT_RUN = ("Permission denied", "Cancelled before it ran", "Loop detected", "Unknown tool", "No checkpoint could be taken", "Tool '", "The arguments of")


def tool_effect(name: str, learned: Optional[Dict[str, str]] = None) -> str:
    """pure, reversible, irreversible or unknown: what the tool does, from what the conversation saw it declare, else the known tools."""
    if learned and learned.get(name) in ("pure", "reversible", "irreversible"):
        return learned[name]
    if name in KNOWN_EFFECTS:
        return KNOWN_EFFECTS[name]
    return "irreversible" if name.startswith("mcp") else "unknown"


def _outside(path: Any, workspace: Optional[str]) -> bool:
    if not workspace or not isinstance(path, str) or not Path(path).is_absolute():
        return False
    try:
        Path(path).resolve().relative_to(Path(workspace).resolve())
        return False
    except ValueError:
        return True


def session_impact(messages: List[Dict[str, Any]], workspace: Optional[str] = None, learned: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """What the tool calls in `messages` (the part of a conversation a rewind would take off) did that going back does not undo.

    `reversible` are the calls that changed files in the conversation's working copy (put back from the Changes panel);
    `not_undone` are those with an effect that stays (commands, calls, payments, messages, a tool of unknown effect, a file outside the
    working copy). A call the agent refused or never ran is not counted."""
    results = {m.get("tool_call_id"): str(m.get("content") or "") for m in messages if isinstance(m, dict) and m.get("role") == "tool"}
    reversible: List[Dict[str, Any]] = []
    stays: List[Dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            raw = fn.get("arguments")
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                args = None
            args = args if isinstance(args, dict) else {}
            effect = tool_effect(name, learned)
            if effect == "pure" or results.get(call.get("id"), "").startswith(NOT_RUN):
                continue
            if effect == "reversible" and name in FILE_TOOLS and _outside(args.get("path"), workspace):
                effect = "irreversible"
            entry = {"tool": name, "effect": effect, "what": _summary({"tool": name, "arguments": args})}
            (reversible if effect == "reversible" else stays).append(entry)
    return {"reversible": reversible, "not_undone": stays}


def _plan_reach(dag: Any, node_id: str) -> List[str]:
    seen, stack = [], [node_id]
    while stack:
        cur = stack.pop()
        if cur not in seen:
            seen.append(cur)
            stack.extend(dag.nodes[cur].outputs)
    return seen


def note_plan_effect(dag: Any, node_id: str, tool: str, effect: str, what: str = "") -> Dict[str, Any]:
    """Records on a step an action done while it ran, so a later rewind of the plan can say what it leaves done."""
    if effect == "pure":
        return {}
    entry = {"tool": str(tool)[:64], "effect": effect if effect in ("reversible", "irreversible") else "unknown", "what": " ".join(str(what or tool).split())[:SUMMARY_CHARS],
             "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    dag.nodes[node_id].state_variables.setdefault("effects", []).append(entry)
    return entry


def plan_impact(dag: Any, node_id: str) -> Dict[str, Any]:
    """Before a plan is rewound from a step: the steps that go back to pending, and the actions they recorded, those that stay done
    (`not_undone`) apart from the reversible ones."""
    if node_id not in dag.nodes:
        raise KeyError(node_id)
    steps = _plan_reach(dag, node_id)
    out: Dict[str, Any] = {"steps": steps, "ran": [s for s in steps if dag.nodes[s].status != "pending"], "reversible": [], "not_undone": []}
    for s in steps:
        for e in dag.nodes[s].state_variables.get("effects") or []:
            out["reversible" if e.get("effect") == "reversible" else "not_undone"].append({**e, "step": s, "title": dag.nodes[s].title})
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
    from harness.stack import data_dir_for                # imported here: the helpers below are used where the whole agent is not
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
