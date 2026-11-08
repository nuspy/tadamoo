"""Turning a plan that ran well into a personal skill.

A plan already says what was done and in what order; the conversation that ran it holds the tool calls the model decided on for each step. This
module finds, for each step, the calls made between its start and its completion (the failed ones are marked and left out of the script by the
same code as for a conversation), lists the steps that have none and must be approved by the person, and gives the rest of the machinery of
`distill` what it expects: numbered requests (one for each step) and recorded steps. The script is written by `distill.write_script`, with the
plan's structure added to its rules."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from . import distill
from .values import NAME

MAX_PROPOSED = 6
AUTO_KINDS = ("wait_duration", "wait_until", "wait_condition", "set_var", "get_var", "output", "log", "loop", "merge", "decision", "sensor")


def _args(call: Dict[str, Any]) -> Dict[str, Any]:
    try:
        value = json.loads((call.get("function") or {}).get("arguments") or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def runs(messages: List[Dict[str, Any]], plan_id: str) -> Dict[str, Tuple[int, int]]:
    """For each step the agent started and completed in this conversation: where it started and where it was completed, as message indexes."""
    start: Dict[str, int] = {}
    end: Dict[str, int] = {}
    for i, m in enumerate(messages):
        if m.get("role") != "assistant":
            continue
        for call in m.get("tool_calls") or []:
            args = _args(call)
            if args.get("plan_id") != plan_id or not isinstance(args.get("node_id"), str):
                continue
            name = (call.get("function") or {}).get("name")
            if name == "plan_step_start":
                start[args["node_id"]] = i
            elif name == "plan_mark" and args.get("status") == "completed":
                end[args["node_id"]] = i
    return {n: (start[n], end[n]) for n in start if n in end and end[n] >= start[n]}


def has_run(messages: List[Dict[str, Any]], plan_id: str) -> bool:
    """Whether this conversation ran steps of the plan."""
    return bool(runs(messages, plan_id))


def finished(plan: Dict[str, Any]) -> bool:
    """A plan can become a script when every step ended well: completed or skipped, none failed or pending."""
    nodes = plan.get("nodes") or {}
    return bool(nodes) and all(n.get("status") in ("completed", "skipped") for n in nodes.values()) and any(n.get("status") == "completed" for n in nodes.values())


def _produces(node: Dict[str, Any]) -> str:
    return ", ".join(f"{p['name']} ({p['type']})" for p in node.get("produces") or [])


def step_text(node: Dict[str, Any]) -> str:
    """What the step was, in words: its title, its description and what it produces."""
    parts = [f"Step {node['id']}: {node.get('title', '')}."]
    if node.get("description"):
        parts.append(node["description"])
    if node.get("produces"):
        parts.append("It produces " + _produces(node) + ".")
    return " ".join(parts)


def outline(plan: Dict[str, Any], order: List[str]) -> str:
    """The plan's structure for the model that writes the script: every step with what it reads, what it produces and what it is made of."""
    nodes = plan.get("nodes") or {}
    lines: List[str] = []
    for nid in order:
        n = nodes[nid]
        head = f"- {nid} \"{n.get('title', '')}\" [{n.get('type')}, for {n.get('assignee')}]"
        if n.get("dependencies"):
            head += " after " + ", ".join(n["dependencies"])
        if n.get("status") == "skipped":
            head += " (skipped in this run)"
        lines.append(head)
        if n.get("description"):
            lines.append(f"    description: {n['description']}")
        if n.get("produces"):
            lines.append(f"    produces: {_produces(n)}")
        if n.get("spec"):
            lines.append(f"    made of: {json.dumps(n['spec'], ensure_ascii=False)}")
        if n.get("type") == "decision" and n.get("conditions"):
            lines.append("    exits: " + "; ".join(f"{k} when {v}" for k, v in n["conditions"].items()))
        out = (n.get("state_variables") or {}).get("outputs")
        if out:
            lines.append(f"    wrote in this run: {json.dumps(out, ensure_ascii=False)[:300]}")
    shared = plan.get("shared_state") or {}
    if shared:
        lines.append(f"The plan's values at the end: {json.dumps(shared, ensure_ascii=False)[:600]}")
    return "\n".join(lines)


def build(plan: Dict[str, Any], messages: List[Dict[str, Any]], plan_id: str, order: Optional[List[str]] = None) -> Dict[str, Any]:
    """{"requests", "steps", "coverage", "outline"} for the plan. `coverage` says, for each step, where its work comes from: "recorded" (tool calls were
    found), "words" (it was answered in words only), "asks" (it is for the person), "plan" (the plan itself works it out) or "missing" (an action the
    agent did with nothing recorded: the person must approve what it is to do)."""
    nodes = plan.get("nodes") or {}
    order = [i for i in (order or plan.get("order") or list(nodes)) if i in nodes]
    found = runs(messages, plan_id)
    requests: List[Dict[str, Any]] = []
    steps: List[Dict[str, Any]] = []
    coverage: List[Dict[str, Any]] = []
    for ordinal, nid in enumerate(order, 1):
        n = nodes[nid]
        text = step_text(n)
        requests.append({"n": ordinal, "index": found.get(nid, (0, 0))[0], "raw": text, "text": text, "node": nid})
        kind, who = n.get("type"), n.get("assignee")
        row = {"id": nid, "title": n.get("title", ""), "type": kind, "assignee": who, "status": n.get("status"), "tools": []}
        if n.get("status") == "skipped":
            row["how"] = "plan"
        elif kind == "input" or who in ("user", "third_party"):
            row["how"] = "asks"
        elif kind in AUTO_KINDS:
            row["how"] = "plan"
        elif nid in found:
            lo, hi = found[nid]
            own = [s for s in distill.steps_in(messages, lo + 1, hi, ordinal) if not (s["kind"] == "tool" and str(s["name"]).startswith("plan_"))]
            steps.extend(own)
            row["tools"] = [s["name"] for s in own if s["kind"] == "tool" and s["ok"]]
            row["how"] = "recorded" if row["tools"] else "words" if any(s["kind"] == "answer" for s in own) else "missing"
        else:
            row["how"] = "missing"
        coverage.append(row)
    return {"requests": requests, "steps": steps, "coverage": coverage, "outline": outline(plan, order), "order": order}


def input_params(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The values the plan asked the person for are parameters of the script as they are."""
    out: List[Dict[str, Any]] = []
    shared = plan.get("shared_state") or {}
    for n in (plan.get("nodes") or {}).values():
        spec = n.get("spec") or {}
        if n.get("type") != "input" or not NAME.match(str(spec.get("into", ""))):
            continue
        seen = shared.get(spec["into"])
        out.append({"name": spec["into"], "label": str(spec.get("prompt", spec["into"]))[:60], "explanation": str(spec.get("prompt", ""))[:300],
                    "type": "number" if spec.get("kind") == "number" else "text", "value_seen": seen if isinstance(seen, (str, int, float)) and not isinstance(seen, bool) else None,
                    "unverified": False})
    return out


def merge_params(proposed: List[Dict[str, Any]], mandatory: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The proposed parameters, with the plan's own questions added when the model did not name them."""
    names = {p["name"] for p in proposed}
    return [*proposed, *[p for p in mandatory if p["name"] not in names]]


# -- the steps with nothing recorded -----------------------------------------------------------------------------------

def missing_prompt(node: Dict[str, Any], plan_outline: str, tool_names: List[str]) -> List[Dict[str, str]]:
    system = ("You propose the tool calls that would do one step of a plan, for a person to approve. Answer with one JSON object only: "
              "{\"steps\": [{\"name\": tool name, \"arguments\": {...}, \"why\": one short sentence}]}, at most %d calls. Use only the tools named." % MAX_PROPOSED)
    user = (f"The plan:\n{plan_outline}\n\nThe step with no recorded work: {step_text(node)}\n\nTools you may use: {', '.join(tool_names) or '(none known)'}.\n"
            "If the step needs no tool because it is only words, answer {\"steps\": []}.")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_missing(text: str, tool_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """The calls the model proposed, kept when well formed (and, when the tools are known, only those)."""
    try:
        data = distill.parse_json_object(text)
    except ValueError:
        return []
    out: List[Dict[str, Any]] = []
    for item in data.get("steps") if isinstance(data.get("steps"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("arguments", {}), dict):
            continue
        if tool_names and item["name"] not in tool_names:
            continue
        out.append({"name": item["name"][:80], "arguments": item.get("arguments") or {}, "why": str(item.get("why") or "")[:200]})
    return out[:MAX_PROPOSED]


def add_approved(steps: List[Dict[str, Any]], coverage: List[Dict[str, Any]], approved: Any) -> List[Dict[str, Any]]:
    """The steps with the calls the person approved for the steps that had none, in the plan's order: each one marked `proposed` (never run)."""
    if not isinstance(approved, dict):
        return steps
    turn_of = {row["id"]: i for i, row in enumerate(coverage, 1)}
    extra: List[Dict[str, Any]] = []
    for nid, calls in approved.items():
        if nid not in turn_of or not isinstance(calls, list):
            continue
        for call in calls[:MAX_PROPOSED]:
            if isinstance(call, dict) and isinstance(call.get("name"), str) and isinstance(call.get("arguments", {}), dict):
                extra.append({"turn": turn_of[nid], "kind": "tool", "name": call["name"][:80], "arguments": call.get("arguments") or {}, "result": "(approved, not run)",
                              "ok": True, "proposed": True})
    return sorted([*steps, *extra], key=lambda s: s["turn"])


# -- changing the parameters by talking ----------------------------------------------------------------------------------

def chat_prompt(params: List[Dict[str, Any]], message: str, plan_outline: str) -> List[Dict[str, str]]:
    system = ("You help a person decide which values of a repeated job should be parameters of a program. They tell you what to add, remove or change. "
              "Answer with one JSON object only: {\"reply\": one or two sentences to the person, \"params\": the whole list of parameters after the change}. "
              "Each parameter: {name (lower case, letters, digits and _), label, explanation, type (text, number, date, datetime, choice, list, path or bool), "
              "value_seen (the value in this run, or null), default (optional)}. Change only what was asked.")
    user = f"The plan:\n{plan_outline}\n\nThe parameters now:\n{json.dumps(params, ensure_ascii=False)}\n\nThe person says: {message[:1500]}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def apply_chat(client: Any, params: List[Dict[str, Any]], message: str, plan_outline: str, evidence: str) -> Dict[str, Any]:
    """One turn of the talk about the parameters: the model's reply and the list after the change. A reply that cannot be read leaves the list as it was."""
    try:
        resp = client.chat_completion(messages=chat_prompt(params, message, plan_outline), temperature=0.0, extra={"reasoning_effort": "none"}, timeout=300.0)
        content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        data = distill.parse_json_object(content)
    except Exception as exc:                                     # the list stays; the person can use the form
        return {"reply": f"I could not follow that ({exc}). Use the form below.", "params": params, "changed": False}
    changed = distill.clean_params(data.get("params"), evidence)
    if not changed and data.get("params") not in ([], None):
        return {"reply": "That did not make a valid list of parameters; nothing changed.", "params": params, "changed": False}
    return {"reply": str(data.get("reply") or "")[:500], "params": changed, "changed": changed != params}
