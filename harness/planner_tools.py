"""The planner's tools for the Chat, and the rule that no change to a plan happens without the person's yes.

Reading a plan is free. Creating, changing, marking and archiving one is always confirmed, in every permission mode,
and the confirmation is one of two things:

* **the person's own words**: the call carries a literal quotation of what they asked for; it counts only if it is in their
  latest message and nothing from outside (a page, a file, a tool's answer) came in after that message. It is a brake for
  the common case, not a proof, so every change is also shown in the conversation with its result;
* **a question from the harness**: the question is made here from the call's real data, never from the model's words, and
  the person answers it with a button.

A plan must have been read (`plan_get`) before it is changed, and the change carries the revision that was read: a plan
that changed meanwhile is refused and has to be read again.
"""
from __future__ import annotations

import json
import re
import secrets
from typing import Any, Callable, Dict, List, Optional, Tuple

from harness.integrations._http import ServiceError
from harness.integrations.planner_client import PlannerClient
from harness.planner.bridge import classify_diff
from harness.tools.registry import EffectClass, ToolRegistry

MIN_QUOTE = 6
STATUSES = ("pending", "in_progress", "completed", "failed", "blocked", "skipped")
KINDS = ("constant", "occasional")
ASSIGNEES = ("agent", "user", "third_party")
MAX_STEPS = 40
MAX_OPTIONS, LABEL_CHARS, VALUE_CHARS, QUESTION_CHARS = 6, 60, 300, 600
SUMMARY_PLANS, SUMMARY_OPEN = 8, 3
CONFIRM_OPTIONS = [{"label": "Confirm", "value": "confirm"}, {"label": "Cancel", "value": "cancel"}]


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


class Confirmer:
    """Decides whether a change to a plan has the person's yes (see the module's text)."""

    def __init__(self, agent: Any, ask: Optional[Callable[[Dict[str, Any]], str]] = None) -> None:
        self.agent = agent
        self.ask = ask                 # shows a question with buttons and waits; returns "confirm", "cancel" or ""

    def check(self, facts: Dict[str, Any], question: str, quote: str = "") -> Tuple[bool, str]:
        """(True, how) when the person said yes; (False, what to tell the model) otherwise."""
        said = _norm(quote or "")
        if len(said) >= MIN_QUOTE and said in _norm(self.agent.last_user_text) and not self.agent.untrusted_since_user:
            return True, "quoted"
        if self.ask is None:
            return False, ("This change needs the user's yes and nobody can give it in this run, so it was not made. "
                           "Do not try again: tell the user what you wanted to change.")
        answer = self.ask({"mode": "confirm", "question": question, "facts": facts, "options": CONFIRM_OPTIONS})
        if answer == "confirm":
            return True, "asked"
        if answer == "cancel":
            return False, "The user said no: nothing was changed. Do not ask again unless they ask for it."
        return False, "The question was not answered, so nothing was changed. Tell the user what is waiting for their yes."


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower().encode("ascii", "ignore").decode()).strip("-")[:40].strip("-")
    return f"{s or 'plan'}-{secrets.token_hex(2)}"


def profile_lines(p: Dict[str, Any]) -> List[str]:
    """What the plan says about time and money, for the model to read."""
    lines = []
    if p.get("goals"):
        lines.append("  aims: " + "; ".join(p["goals"]))
    if p.get("budget"):
        b = p["budget"]
        lines.append(f"  budget: {b['amount']} {b['currency']} {'per month' if b.get('period') != 'total' else 'in all'}")
    for c in p.get("commitments") or []:
        lines.append(f"  already taken: {c['text']} ({c['from']} to {c['to']})")
    if p.get("starts_on") or p.get("ends_on"):
        lines.append(f"  from {p.get('starts_on') or '?'} to {p.get('ends_on') or '?'}")
    if p.get("cost"):
        lines.append(f"  cost: {p['cost']['amount']} {p['cost']['currency']}")
    return lines


def render_alignment(a: Dict[str, Any]) -> str:
    """The findings of the comparison: conflicts are arithmetic on dates and amounts, checks are the model's reading and may be wrong."""
    lines = []
    for c in a.get("conflicts") or []:
        if c["plan_id"] == a.get("plan_id"):
            lines.append(f'CONFLICT with "{c["with_title"]}" ({c["kind"]}): {c["text"]}')
        else:
            lines.append(f'CONFLICT: "{c["plan_title"]}" ({c["kind"]}): {c["text"]}')
    for c in a.get("checks") or []:
        lines.append(f'CHECK against "{c["with_title"]}" (the model\'s reading, may be wrong): {c["text"]}')
    if a.get("out_of_date"):
        lines.append("The model's reading is from before the plan last changed.")
    return "\n".join(lines) if lines else "No conflict found between the dates and amounts of the plans."


def render_plan(snap: Dict[str, Any]) -> str:
    plan, nodes = snap["plan"], snap["plan"].get("nodes", {})
    done = sum(1 for n in nodes.values() if n.get("status") == "completed")
    lines = [f'Plan {plan["plan_id"]}: "{plan["title"]}" [{plan.get("target_type")}], revision {snap.get("rev")}, {len(nodes)} steps, {done} done']
    lines += profile_lines(snap.get("profile") or {})
    for nid, n in nodes.items():
        after = f", after {', '.join(n['dependencies'])}" if n.get("dependencies") else ""
        lines.append(f'  {nid} [{n.get("status")}] {n.get("title")} (by {n.get("assignee")}{after})')
        if n.get("description"):
            lines.append(f'      {n["description"][:200]}')
    if snap.get("ready"):
        lines.append("Ready now: " + ", ".join(snap["ready"]))
    for p in snap.get("proposals", []):
        lines.append(f'Pending proposal {p.get("proposal_id")}: {p.get("title")}')
    for o in snap.get("offers", []):
        lines.append(f'Waiting for the user: {o.get("node_id")}')
    return "\n".join(lines)


def plans_summary(client: PlannerClient) -> str:
    """A short account of the active plans, for the start of a message: the most recently touched first, and how many are
    left out. It is the same text while the plans do not change, so it does not disturb the server's cache."""
    try:
        plans = client.list()
    except (ServiceError, ValueError):
        return ""
    plans = sorted(plans, key=lambda x: (str(x.get("updated_at") or ""), str(x.get("plan_id"))), reverse=True)
    lines = []
    for p in plans[:SUMMARY_PLANS]:
        total, done = p.get("nodes", 0), p.get("completed", 0)
        lines.append(f'- {p.get("plan_id")}: "{p.get("title")}" [{p.get("target_type", "occasional")}], {done}/{total} steps done'
                     + ("; due to be looked at again" if p.get("review_due") else ""))
        for c in (p.get("conflicts") or [])[:3]:
            lines.append(f'    conflict ({c["kind"]}): {c["text"]}')
    if len(plans) > SUMMARY_PLANS:
        lines.append(f"- and {len(plans) - SUMMARY_PLANS} more: plan_list shows all of them")
    return "\n".join(lines)


def register_planner_tools(registry: ToolRegistry, client: PlannerClient, confirmer: Confirmer) -> None:
    seen: Dict[str, int] = {}                       # the revision of each plan as the model last read it

    def read(plan_id: str) -> Dict[str, Any]:
        snap = client.snapshot(plan_id)
        seen[plan_id] = int(snap.get("rev") or 0)
        return snap

    def need_read(plan_id: str) -> Optional[str]:
        if plan_id not in seen:
            return f"Read the plan first with plan_get('{plan_id}'): a change must be made on what you have read."
        return None

    def failure(exc: Exception) -> str:
        if isinstance(exc, ServiceError):
            return f"The planner refused or could not be reached ({exc.kind}): {exc.detail or exc}"
        return f"Error: {exc}"

    # -- reading ------------------------------------------------------------------------------------------

    def plan_list(archived: bool = False) -> str:
        try:
            plans = client.list(archived=bool(archived))
        except Exception as exc:
            return failure(exc)
        if not plans:
            return "There are no plans." if not archived else "There are no archived plans."
        return "\n".join(f'{p["plan_id"]}: "{p["title"]}" [{p.get("target_type", "occasional")}] — {p.get("completed", 0)}/{p.get("nodes", 0)} steps done'
                         + ("; due to be looked at again" if p.get("review_due") else "") + (f"; {len(p['conflicts'])} conflict(s)" if p.get("conflicts") else "") for p in plans)

    def plan_get(plan_id: str) -> str:
        try:
            return render_plan(read(plan_id))
        except Exception as exc:
            return failure(exc)

    # -- changing (always with the person's yes) ----------------------------------------------------------

    def plan_create(title: str, kind: str, steps: List[Dict[str, Any]], quote: str = "", profile: Optional[Dict[str, Any]] = None) -> str:
        title = str(title).strip()
        if not title or len(title) > 200:
            return "The plan needs a title of up to 200 characters."
        if kind not in KINDS:
            return f"kind must be one of: {', '.join(KINDS)}."
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
            return f"A plan has between 1 and {MAX_STEPS} steps."
        nodes: Dict[str, Dict[str, Any]] = {}
        for i, st in enumerate(steps, 1):
            if not isinstance(st, dict) or not str(st.get("title", "")).strip():
                return f"Step {i} needs a title."
            after = st.get("after", [i - 1] if i > 1 else [])
            if not isinstance(after, list) or any(not isinstance(a, int) or isinstance(a, bool) or not 1 <= a < i for a in after):
                return f"Step {i}: 'after' lists earlier step numbers, from 1 to {i - 1}."
            assignee = st.get("assignee", "agent")
            if assignee not in ASSIGNEES:
                return f"Step {i}: assignee must be one of {', '.join(ASSIGNEES)}."
            nodes[f"s{i}"] = {"id": f"s{i}", "type": "action", "title": str(st["title"]).strip()[:200], "description": str(st.get("description", ""))[:1000],
                              "assignee": assignee, "dependencies": [f"s{a}" for a in after], "outputs": [], "status": "pending"}
        for nid, n in nodes.items():
            for d in n["dependencies"]:
                nodes[d]["outputs"].append(nid)
        shown = [n["title"] for n in nodes.values()]
        ok, how = confirmer.check({"kind": "plan_create", "title": title, "plan_kind": kind, "steps": shown},
                                  f'Create the plan "{title}" ({kind}) with {len(shown)} steps: ' + "; ".join(f"{i}. {t}" for i, t in enumerate(shown[:10], 1)) +
                                  ("..." if len(shown) > 10 else ""), quote)
        if not ok:
            return how
        try:
            snap = client.create({"plan_id": _slug(title), "title": title, "target_type": kind, "nodes": nodes}, profile if isinstance(profile, dict) else None)
        except Exception as exc:
            return failure(exc)
        seen[snap["plan"]["plan_id"]] = int(snap.get("rev") or 0)
        found = ""
        try:
            found = render_alignment(client.alignment(snap["plan"]["plan_id"]))
        except Exception:
            pass
        return f"Plan created ({how}).\n" + render_plan(snap) + (f"\n{found}" if found and not found.startswith("No conflict") else "")

    def plan_set_profile(plan_id: str, profile: Dict[str, Any], quote: str = "") -> str:
        """Sets what the plan says about time and money: aims, budget, periods already taken, how often to look again (a long-term plan); dates and cost
        (a short one). The person says yes."""
        if (msg := need_read(plan_id)):
            return msg
        if not isinstance(profile, dict):
            return "profile is an object: goals, review_every_days, last_review, budget, commitments, starts_on, ends_on, cost."
        try:
            snap = client.snapshot(plan_id)
        except Exception as exc:
            return failure(exc)
        ok, how = confirmer.check({"kind": "plan_change", "plan": snap["plan"]["title"], "title": "aims, dates and amounts"},
                                  f'Set what the plan "{snap["plan"]["title"]}" says about time and money: ' + "; ".join(sorted(profile)) + ".", quote)
        if not ok:
            return how
        try:
            client.event(plan_id, {"type": "plan:update_meta", "changes": {"profile": {**(snap.get("profile") or {}), **profile}}, "base_rev": seen[plan_id]})
        except Exception as exc:
            return failure(exc)
        seen[plan_id] = int(client.snapshot(plan_id).get("rev") or seen[plan_id])
        try:
            found = render_alignment(client.alignment(plan_id))
        except Exception:
            found = ""
        return f"Saved ({how}). " + found

    def plan_check(plan_id: str, with_model: bool = False) -> str:
        """The plan against the person's long-term plans: the arithmetic of dates and amounts, and, when asked, the local model's reading of what they mean."""
        try:
            return render_alignment(client.alignment(plan_id, model=bool(with_model)))
        except Exception as exc:
            return failure(exc)

    def plan_mark(plan_id: str, node_id: str, status: str, quote: str = "") -> str:
        if status not in STATUSES:
            return f"status must be one of: {', '.join(STATUSES)}."
        if (msg := need_read(plan_id)):
            return msg
        try:
            snap = client.snapshot(plan_id)
        except Exception as exc:
            return failure(exc)
        node = snap["plan"]["nodes"].get(node_id)
        if node is None:
            return f"The plan has no step '{node_id}'."
        ok, how = confirmer.check({"kind": "plan_mark", "plan": snap["plan"]["title"], "step": node["title"], "from": node.get("status"), "to": status},
                                  f'In the plan "{snap["plan"]["title"]}", mark the step "{node["title"]}" as {status} (now {node.get("status")})?', quote)
        if not ok:
            return how
        try:
            res = client.event(plan_id, {"type": "plan:mark", "node_id": node_id, "status": status, "base_rev": seen[plan_id]})
        except Exception as exc:
            return failure(exc)
        try:
            seen[plan_id] = int(client.snapshot(plan_id).get("rev") or seen[plan_id])     # the next change is made on what this one left
        except Exception:
            seen.pop(plan_id, None)                                                       # unknown now: the plan is read again
        retry = res.get("retry")
        if retry:                                                   # the step has tries left: it is not failed yet
            link_task(plan_id, node, "running")
            return (f"Step {node_id} failed on try {retry['attempt']} of {retry['of']} ({how}); its retry policy makes it ready again "
                    f"in {round(retry['wait_seconds'])} seconds. It is not marked failed. Ready now: {', '.join(res.get('ready', [])) or 'nothing'}.")
        if status in ("completed", "failed", "skipped"):
            link_task(plan_id, node, {"completed": "done", "failed": "failed", "skipped": "closed"}[status])
        return f"Step {node_id} marked {status} ({how}). Ready now: {', '.join(res.get('ready', [])) or 'nothing'}."

    def link_task(plan_id: str, node: Dict[str, Any], status: str, result: str = "") -> None:
        """The Tasks panel shows the step with its plan; a panel that cannot be reached never stops the work."""
        try:
            client.task(f"plan:{plan_id}:{node['id']}", status, result, {"plan": plan_id, "step": node["id"], "title": node.get("title", "")})
        except Exception:
            pass

    def plan_step_start(plan_id: str, node_id: str, quote: str = "") -> str:
        """Starts a step the agent is to do: it must be ready, and the person says yes. The work is then done with the usual tools in
        this conversation, and the step is marked done or failed after the result is read."""
        if (msg := need_read(plan_id)):
            return msg
        try:
            snap = client.snapshot(plan_id)
        except Exception as exc:
            return failure(exc)
        node = snap["plan"]["nodes"].get(node_id)
        if node is None:
            return f"The plan has no step '{node_id}'."
        if node_id not in (snap.get("ready") or []):
            return f"Step {node_id} is not ready: it is {node.get('status')} or waits for an earlier step."
        if node.get("assignee") != "agent" or node.get("type", "action") != "action":
            return f"Step {node_id} is for {node.get('assignee')} and is not an action the agent can do: tell the user."
        ok, how = confirmer.check({"kind": "plan_step_start", "plan": snap["plan"]["title"], "step": node["title"]},
                                  f'In the plan "{snap["plan"]["title"]}", start the step "{node["title"]}" now?', quote)
        if not ok:
            return how
        try:
            client.event(plan_id, {"type": "plan:mark", "node_id": node_id, "status": "in_progress", "base_rev": seen[plan_id]})
            seen[plan_id] = int(client.snapshot(plan_id).get("rev") or seen[plan_id])
        except Exception as exc:
            return failure(exc)
        link_task(plan_id, node, "running")
        return (f'Step {node_id} started ({how}): "{node["title"]}". {node.get("description") or ""}\n'
                "Do it with your tools, read the result, then mark the step completed or failed with plan_mark.")

    def plan_propose_change(plan_id: str, title: str, rationale: str, diff: Dict[str, Any], quote: str = "") -> str:
        if (msg := need_read(plan_id)):
            return msg
        if not isinstance(diff, dict):
            return "diff must be an object with add_nodes, remove_nodes, update_nodes, add_edges and/or remove_edges."
        try:
            snap = client.snapshot(plan_id)
            proposed = client.event(plan_id, {"type": "plan:propose_diff", "title": str(title)[:200], "rationale": str(rationale)[:1000],
                                              "diff": diff, "source": "chat"})
        except Exception as exc:
            return failure(exc)
        pid, changes = proposed.get("proposal_id"), classify_diff(diff)
        parts = [f"{label} {', '.join(map(str, ids))}" for label, ids in (("adds", changes["added"]), ("removes", changes["removed"]), ("changes", changes["updated"])) if ids]
        ok, how = confirmer.check({"kind": "plan_change", "plan": snap["plan"]["title"], "title": str(title), "changes": changes},
                                  f'Change the plan "{snap["plan"]["title"]}": {str(title)[:120]}. This {"; ".join(parts) or "changes nothing"}.', quote)
        try:
            if not ok:
                client.event(plan_id, {"type": "plan:reject", "proposal_id": pid})
                return how
            res = client.event(plan_id, {"type": "plan:approve", "proposal_id": pid})
        except Exception as exc:
            return failure(exc)
        seen[plan_id] = int(res.get("rev") or seen[plan_id])
        return f"Change applied ({how}): {'; '.join(parts)}. Read the plan again before the next change."

    def plan_archive(plan_id: str, quote: str = "") -> str:
        try:
            snap = client.snapshot(plan_id)
        except Exception as exc:
            return failure(exc)
        ok, how = confirmer.check({"kind": "plan_archive", "plan": snap["plan"]["title"]},
                                  f'Archive the plan "{snap["plan"]["title"]}"? It leaves the list and can be restored.', quote)
        if not ok:
            return how
        try:
            client.archive(plan_id)
        except Exception as exc:
            return failure(exc)
        seen.pop(plan_id, None)
        return f'Plan "{snap["plan"]["title"]}" archived ({how}); it can be restored from the Plans view.'

    quote_param = {"type": "string", "description": "The user's own words that ask for this, copied exactly from their latest message. "
                                                    "Leave empty when they did not ask in so many words: they will then be asked."}
    obj = lambda props, req: {"type": "object", "properties": props, "required": req}
    registry.register("plan_list", "Lists the user's plans (id, title, progress).", obj({"archived": {"type": "boolean"}}, []), EffectClass.PURE, plan_list)
    registry.register("plan_get", "Reads one plan: its steps, their status, what is ready, what waits for the user. Read a plan before changing it.",
                      obj({"plan_id": {"type": "string"}}, ["plan_id"]), EffectClass.PURE, plan_get, untrusted=True)
    registry.register("plan_create", "Creates a plan from a title and its steps. The user must say yes: quote their words, or they are asked.",
                      obj({"title": {"type": "string"}, "kind": {"type": "string", "enum": list(KINDS), "description": "occasional (a trip, an event) or constant (a routine)"},
                           "steps": {"type": "array", "items": obj({"title": {"type": "string"}, "description": {"type": "string"},
                                                                    "assignee": {"type": "string", "enum": list(ASSIGNEES)},
                                                                    "after": {"type": "array", "items": {"type": "integer"}, "description": "numbers of the earlier steps this one waits for; default the previous step"}}, ["title"])},
                           "quote": quote_param,
                           "profile": {"type": "object", "description": "what the plan says about time and money. A short plan: starts_on and ends_on (2026-11-24), "
                                                                       "cost {amount, currency}. A long-term plan: goals [text], review_every_days (1-30), budget {amount, currency, "
                                                                       "period month|total}, commitments [{text, from, to}]"}}, ["title", "kind", "steps"]), EffectClass.REVERSIBLE, plan_create)
    registry.register("plan_set_profile", "Sets what a plan says about time and money (aims, budget, periods already taken, dates, cost), so that it can be compared with the others. Needs the user's yes.",
                      obj({"plan_id": {"type": "string"}, "profile": {"type": "object"}, "quote": quote_param}, ["plan_id", "profile"]), EffectClass.REVERSIBLE, plan_set_profile)
    registry.register("plan_check", "Compares a plan with the user's long-term plans. Conflicts are arithmetic on dates and amounts; with with_model the local model also reads what the plans mean together (a check, not a fact). Use it before a new plan is proposed and when something in the plan changes.",
                      obj({"plan_id": {"type": "string"}, "with_model": {"type": "boolean"}}, ["plan_id"]), EffectClass.REVERSIBLE, plan_check)
    registry.register("plan_mark", "Marks a step of a plan (pending, in_progress, completed, failed, blocked, skipped). Needs the user's yes.",
                      obj({"plan_id": {"type": "string"}, "node_id": {"type": "string"}, "status": {"type": "string", "enum": list(STATUSES)}, "quote": quote_param},
                          ["plan_id", "node_id", "status"]), EffectClass.REVERSIBLE, plan_mark)
    registry.register("plan_step_start", "Starts a step that is ready and is for the agent; the user must say yes. Then do the step with your tools and mark it.",
                      obj({"plan_id": {"type": "string"}, "node_id": {"type": "string"}, "quote": quote_param}, ["plan_id", "node_id"]), EffectClass.REVERSIBLE, plan_step_start)
    registry.register("plan_propose_change", "Changes the structure of a plan (add, remove or change steps and links) with a diff. Needs the user's yes.",
                      obj({"plan_id": {"type": "string"}, "title": {"type": "string"}, "rationale": {"type": "string"},
                           "diff": {"type": "object", "description": "{add_nodes: [node], remove_nodes: [id], update_nodes: {id: fields}, add_edges: [[parent, child]], remove_edges: [[parent, child]]}"},
                           "quote": quote_param}, ["plan_id", "title", "diff"]), EffectClass.REVERSIBLE, plan_propose_change)
    registry.register("plan_archive", "Archives a plan (it can be restored). Needs the user's yes.",
                      obj({"plan_id": {"type": "string"}, "quote": quote_param}, ["plan_id"]), EffectClass.REVERSIBLE, plan_archive)


def register_ask_tool(registry: ToolRegistry, agent: Any, question: Callable[[Dict[str, Any]], bool]) -> None:
    """`ask_user`: a question with buttons. The turn ends; the button the person presses is their next message."""

    def ask_user(question_text: str, options: Optional[List[Dict[str, str]]] = None) -> str:
        q = str(question_text).strip()
        if not q:
            return "Write the question."
        opts = []
        for o in (options or [])[:MAX_OPTIONS]:
            if not isinstance(o, dict) or not str(o.get("label", "")).strip():
                return "Each option needs a label and a value."
            label, value = str(o["label"]).strip(), str(o.get("value", o["label"])).strip()
            if len(label) > LABEL_CHARS or len(value) > VALUE_CHARS or not value:
                return f"A label is at most {LABEL_CHARS} characters and a value at most {VALUE_CHARS}."
            opts.append({"label": label, "value": value})
        if not question({"mode": "question", "question": q[:QUESTION_CHARS], "options": opts}):
            return "Nobody can answer in this run: decide with what you know, or say what is missing."
        agent.request_end_turn()
        return "The question is on the user's screen with its buttons; whatever they press or write is their next message. Say nothing more now."

    registry.register("ask_user", "Asks the user a question, with buttons for the likely answers (they can always write instead). "
                                  "Use it when you need a choice or a missing fact; the answer comes as their next message.",
                      {"type": "object", "properties": {"question_text": {"type": "string"},
                                                        "options": {"type": "array", "items": {"type": "object", "properties": {
                                                            "label": {"type": "string", "description": "what the button says"},
                                                            "value": {"type": "string", "description": "what is sent as the user's message when it is pressed"}},
                                                            "required": ["label"]}}},
                       "required": ["question_text"]}, EffectClass.PURE, ask_user)
