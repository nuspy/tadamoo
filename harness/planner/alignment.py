"""Whether the plans agree with one another: short plans against the long-term ones.

Two kinds of finding, kept apart because they are not equally sure. A **conflict** is arithmetic on what the plans say: a short plan whose days
fall in a period a long-term plan has already taken, or whose cost, with the others', is over the budget a long-term plan allows. A **check**
is the local model's reading of what the words mean (a weekend of skiing against "heal the knee"): it may be wrong, so it is offered to be
looked at, never stated as a fact. The model is asked only when the person asks, and sees only the plans' titles, aims, dates, costs and step names.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

MODEL_MAX = 5
STEPS_SHOWN = 25


def view(plan: Dict[str, Any], profile: Dict[str, Any], card: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A plan as the comparison reads it."""
    return {"plan_id": plan["plan_id"], "title": plan["title"], "target_type": plan.get("target_type", "occasional"), "profile": profile, "card": card,
            "steps": [n.get("title", "") for n in (plan.get("nodes") or {}).values()][:STEPS_SHOWN]}


def _finding(kind: str, severity: str, short: Dict[str, Any], long: Dict[str, Any], goal: str, text: str, source: str,
             data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`text` is for the model and the Chat; `data` holds the same facts as values, so that a page can say them in its own language."""
    return {"kind": kind, "severity": severity, "plan_id": short["plan_id"], "plan_title": short["title"], "with_plan_id": long["plan_id"],
            "with_title": long["title"], "goal": goal, "text": text, "source": source, "data": data or {}}


def rule_conflicts(views: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The conflicts the dates and amounts show, between every long-term plan and every short one."""
    longs = [v for v in views if v["target_type"] == "constant"]
    shorts = [v for v in views if v["target_type"] == "occasional"]
    out: List[Dict[str, Any]] = []
    for lp in longs:
        prof = lp["profile"]
        for sp in shorts:
            start, end = sp["profile"].get("starts_on", ""), sp["profile"].get("ends_on", "")
            if not (start and end):
                continue
            for c in prof.get("commitments", []):
                if start <= c["to"] and c["from"] <= end:
                    out.append(_finding("time", "conflict", sp, lp, c["text"],
                                        f"\"{sp['title']}\" runs from {start} to {end}, and \"{lp['title']}\" has \"{c['text']}\" from {c['from']} to {c['to']}.", "rules",
                                        {"start": start, "end": end, "commitment": c["text"], "from": c["from"], "to": c["to"]}))
        budget = prof.get("budget")
        if not budget:
            continue
        limit = Decimal(budget["amount"])
        priced = [sp for sp in shorts if (sp["profile"].get("cost") or {}).get("currency") == budget["currency"]]
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for sp in priced:
            if budget.get("period") == "total":
                key = "all"
            else:
                starts = sp["profile"].get("starts_on", "")
                key = starts[:7] if starts else f"undated:{sp['plan_id']}"          # without a date a plan cannot be put in a month with the others
            groups.setdefault(key, []).append(sp)
        for key, group in groups.items():
            total = sum(Decimal(sp["profile"]["cost"]["amount"]) for sp in group)
            if total > limit:
                span = "in all" if key == "all" else (f"in {key}" if not key.startswith("undated") else "on its own")
                where = "all" if key == "all" else ("own" if key.startswith("undated") else key)
                for sp in group:
                    out.append(_finding("budget", "conflict", sp, lp, f"{budget['amount']} {budget['currency']} {'per month' if budget.get('period') != 'total' else 'in all'}",
                                        f"The costs of the short plans come to {total:.2f} {budget['currency']} {span}, over the {limit:.2f} {budget['currency']} "
                                        f"that \"{lp['title']}\" allows{' per month' if budget.get('period') != 'total' else ''}.", "rules",
                                        {"total": f"{total:.2f}", "limit": f"{limit:.2f}", "currency": budget["currency"], "period": budget.get("period", "month"), "where": where}))
    return out


def _describe_long(v: Dict[str, Any]) -> str:
    p = v["profile"]
    bits = [f"[{v['plan_id']}] \"{v['title']}\""]
    if p.get("goals"):
        bits.append("aims: " + "; ".join(p["goals"]))
    if p.get("budget"):
        b = p["budget"]
        bits.append(f"budget: {b['amount']} {b['currency']} {'per month' if b.get('period') != 'total' else 'in all'}")
    if p.get("commitments"):
        bits.append("already taken: " + "; ".join(f"{c['text']} ({c['from']} to {c['to']})" for c in p["commitments"]))
    return ", ".join(bits)


def _describe_short(v: Dict[str, Any]) -> str:
    p = v["profile"]
    card = v.get("card") or {}
    bits = [f"[{v['plan_id']}] \"{v['title']}\" ({v['target_type']})"]
    if p.get("starts_on") or p.get("ends_on"):
        bits.append(f"from {p.get('starts_on') or '?'} to {p.get('ends_on') or '?'}")
    if p.get("cost"):
        bits.append(f"cost {p['cost']['amount']} {p['cost']['currency']}")
    if p.get("goals"):
        bits.append("aims: " + "; ".join(p["goals"]))
    for key in ("destinazione", "date"):
        if card.get(key):
            bits.append(f"{key}: {card[key]}")
    if v.get("steps"):
        bits.append("steps: " + "; ".join(v["steps"]))
    return ", ".join(bits)


SYSTEM = ("You compare a person's plans. You are given their long-term plans (what they want to keep true) and one other plan. Say which long-term "
          "plans the other plan contradicts or puts at risk. A plan that only touches the same subject is not a conflict; do not invent one. "
          "Answer with JSON only: {\"conflicts\": [{\"with_plan_id\": \"<id of the long-term plan>\", \"goal\": \"<the aim or commitment it goes against>\", "
          "\"text\": \"<why, in one sentence>\"}]}, and {\"conflicts\": []} when there is none.")


def meaning_conflicts(subject: Dict[str, Any], views: List[Dict[str, Any]], ask: Callable[[List[Dict[str, str]]], str]) -> List[Dict[str, Any]]:
    """The model's reading of what the plans mean together. `ask` takes chat messages and returns the model's text."""
    longs = [v for v in views if v["target_type"] == "constant" and v["plan_id"] != subject["plan_id"]]
    if not longs:
        return []
    body = "LONG-TERM PLANS:\n" + "\n".join(_describe_long(v) for v in longs) + "\n\nTHE OTHER PLAN:\n" + _describe_short(subject)
    text = ask([{"role": "system", "content": SYSTEM}, {"role": "user", "content": body}]) or ""
    match = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        return []
    by_id = {v["plan_id"]: v for v in longs}
    out = []
    for item in (data.get("conflicts") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict) or item.get("with_plan_id") not in by_id or not str(item.get("text", "")).strip():
            continue
        out.append(_finding("meaning", "check", subject, by_id[item["with_plan_id"]], str(item.get("goal", ""))[:300], str(item["text"]).strip()[:300], "model"))
        if len(out) >= MODEL_MAX:
            break
    return out


def client_ask(client: Any, timeout: float = 120.0) -> Callable[[List[Dict[str, str]]], str]:
    """The local model as the comparison's reader."""
    from harness.client import extract_reasoning_and_content

    def ask(messages: List[Dict[str, str]]) -> str:
        resp = client.chat_completion(messages=messages, temperature=0.0, max_tokens=700, timeout=timeout, extra={"reasoning_effort": "none"})
        _, content = extract_reasoning_and_content(resp["choices"][0]["message"])
        return content
    return ask
