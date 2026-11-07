"""What each kind of step is: one table, read by the checks of a plan, the tools that offer the kinds to the model, and the page that lists them.

A step has the common fields of tadamoo/planner/dag.py (title, assignee, parents) and, for the kinds that need more, a `spec`: waits, variables,
loops, questions to the person, results and log lines. The kinds marked `auto` are worked out by the plan itself (`PlanDAG.advance`), not given to
the agent or the person: waiting out a time, setting a value, writing a line."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from tadamoo.planner.dag import PlanError, _json_value, check_expression

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MIN_WAIT_S, MAX_WAIT_S = 1, 366 * 86400
MIN_POLL_S, MAX_POLL_S, DEFAULT_POLL_S = 60, 86400, 300
TEXT_MAX = 600
MAX_ITER = 10_000

# kind of field -> what a page draws for it
FIELD_KINDS = ("number", "integer", "text", "textarea", "expression", "name", "names", "datetime", "choice", "json")

NODE_SPECS: Dict[str, Dict[str, Any]] = {
    "action": {"family": "action", "auto": False, "summary": "A task for the agent or the person", "fields": []},
    "sensor": {"family": "action", "auto": False, "summary": "Listens for an event and resolves when its condition holds", "fields": []},
    "decision": {"family": "flow", "auto": False, "summary": "Chooses the exits whose condition holds", "fields": []},
    "merge": {"family": "flow", "auto": False, "summary": "Waits for the parallel branches above it", "fields": []},
    "wait_duration": {"family": "wait", "auto": True, "summary": "Waits a fixed time", "fields": [
        {"name": "seconds", "kind": "number", "required": True, "min": MIN_WAIT_S, "max": MAX_WAIT_S}]},
    "wait_until": {"family": "wait", "auto": True, "summary": "Waits for a date and time", "fields": [
        {"name": "at", "kind": "datetime", "required": True}]},
    "wait_condition": {"family": "wait", "auto": True, "summary": "Waits until something holds: an expression, or a description in words that is judged when what it watches changes", "fields": [
        {"name": "expr", "kind": "expression"}, {"name": "describe", "kind": "textarea", "max": TEXT_MAX},
        {"name": "watch", "kind": "names", "max": 20}, {"name": "timeout_s", "kind": "number", "min": MIN_POLL_S, "max": MAX_WAIT_S},
        {"name": "poll_s", "kind": "number", "min": MIN_POLL_S, "max": MAX_POLL_S, "default": DEFAULT_POLL_S},
        {"name": "on_timeout", "kind": "choice", "choices": ["fail", "skip"], "default": "fail"}]},
    "loop": {"family": "flow", "auto": False, "summary": "Does its work again and again while a condition holds", "fields": [
        {"name": "while", "kind": "expression", "required": True}, {"name": "every", "kind": "number", "min": 60, "max": 30 * 86400, "default": 300},
        {"name": "max_iter", "kind": "integer", "min": 1, "max": MAX_ITER, "default": 1000}]},
    "set_var": {"family": "data", "auto": True, "summary": "Sets a value of the plan", "fields": [
        {"name": "name", "kind": "name", "required": True}, {"name": "value", "kind": "json"}, {"name": "expr", "kind": "expression"}]},
    "get_var": {"family": "data", "auto": True, "summary": "Reads a value of the plan, optionally into another name", "fields": [
        {"name": "name", "kind": "name", "required": True}, {"name": "into", "kind": "name"}]},
    "input": {"family": "io", "auto": False, "summary": "Asks the person for a value", "fields": [
        {"name": "prompt", "kind": "textarea", "required": True, "max": 300}, {"name": "into", "kind": "name", "required": True},
        {"name": "kind", "kind": "choice", "choices": ["text", "number", "choice"], "default": "text"}, {"name": "choices", "kind": "names", "max": 10}]},
    "output": {"family": "io", "auto": True, "summary": "Shows a result of the plan", "fields": [
        {"name": "name", "kind": "name", "required": True}, {"name": "value", "kind": "json"}, {"name": "expr", "kind": "expression"},
        {"name": "template", "kind": "textarea", "max": TEXT_MAX}]},
    "log": {"family": "io", "auto": True, "summary": "Writes a line in the plan's log", "fields": [
        {"name": "message", "kind": "textarea", "required": True, "max": 500}]},
}
AUTO = tuple(k for k, v in NODE_SPECS.items() if v["auto"])
FAMILIES = ("action", "wait", "data", "flow", "io")


def _name(spec: Dict[str, Any], key: str, required: bool = False) -> Optional[str]:
    value = spec.get(key)
    if value is None or value == "":
        if required:
            raise PlanError(f"'{key}' is needed: a name of letters, digits and _")
        return None
    if not isinstance(value, str) or not NAME.match(value):
        raise PlanError(f"'{key}' is a name of letters, digits and _, starting with a letter")
    return value


def _number(spec: Dict[str, Any], key: str, low: float, high: float, required: bool = False, default: Optional[float] = None) -> Optional[float]:
    value = spec.get(key, default)
    if value is None:
        if required:
            raise PlanError(f"'{key}' is needed")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise PlanError(f"'{key}' is a number from {low:g} to {high:g}")
    return value


def _text(spec: Dict[str, Any], key: str, limit: int, required: bool = False) -> Optional[str]:
    value = spec.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise PlanError(f"'{key}' is needed")
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise PlanError(f"'{key}' is a text of at most {limit} characters")
    return value.strip()


def _names(spec: Dict[str, Any], key: str, limit: int) -> Optional[List[str]]:
    value = spec.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > limit or any(not isinstance(v, str) or not v.strip() for v in value):
        raise PlanError(f"'{key}' is a list of at most {limit} texts")
    return [v.strip() for v in value]


def parse_at(text: Any) -> float:
    """The moment a date and time in the plan means, as seconds since the epoch; no zone means this PC's local time."""
    if not isinstance(text, str) or not text.strip():
        raise PlanError("'at' is a date and time, like 2026-11-09 14:50")
    try:
        moment = datetime.fromisoformat(text.strip().replace(" ", "T", 1))
    except ValueError:
        raise PlanError("'at' is a date and time, like 2026-11-09 14:50") from None
    return (moment if moment.tzinfo else moment.astimezone()).timestamp()


def _one_of(spec: Dict[str, Any], keys: tuple) -> None:
    given = [k for k in keys if spec.get(k) is not None]
    if len(given) != 1:
        raise PlanError(f"give exactly one of {', '.join(keys)}")


def _value(spec: Dict[str, Any]) -> None:
    if spec.get("value") is not None and not _json_value(spec["value"]):
        raise PlanError("'value' is a text, a number, true/false, or a small list or object of those")


def clean_spec(node_type: Any, spec: Any) -> Dict[str, Any]:
    """The spec a kind of step may carry, checked and filled with its defaults; raises PlanError naming what is wrong."""
    kind = getattr(node_type, "value", node_type)
    if kind not in NODE_SPECS:
        raise PlanError(f"unknown kind of step '{kind}' (a plan written by a newer version?)")
    spec = {} if spec is None else spec
    if not isinstance(spec, dict):
        raise PlanError("spec is an object")
    allowed = {f["name"] for f in NODE_SPECS[kind]["fields"]}
    if set(spec) - allowed:
        raise PlanError(f"a {kind} step has {', '.join(sorted(allowed)) or 'no spec'}; not {', '.join(sorted(set(spec) - allowed))}")
    out: Dict[str, Any] = {}
    if kind == "wait_duration":
        out["seconds"] = _number(spec, "seconds", MIN_WAIT_S, MAX_WAIT_S, required=True)
    elif kind == "wait_until":
        parse_at(spec.get("at"))
        out["at"] = spec["at"].strip()
    elif kind == "wait_condition":
        expr, describe = spec.get("expr"), _text(spec, "describe", TEXT_MAX)
        if expr is not None:
            check_expression(expr)
        if not expr and not describe:
            raise PlanError("a wait for a condition needs 'expr' (a condition) or 'describe' (what to wait for, in words)")
        watch, timeout = _names(spec, "watch", 20), _number(spec, "timeout_s", MIN_POLL_S, MAX_WAIT_S)
        on_timeout = spec.get("on_timeout", "fail")
        if on_timeout not in ("fail", "skip"):
            raise PlanError("'on_timeout' is fail or skip")
        out.update({k: v for k, v in (("expr", expr), ("describe", describe), ("watch", watch), ("timeout_s", timeout)) if v})
        out.update({"poll_s": _number(spec, "poll_s", MIN_POLL_S, MAX_POLL_S, default=DEFAULT_POLL_S), "on_timeout": on_timeout})
    elif kind == "loop":
        check_expression(spec.get("while"))
        out.update({"while": spec["while"], "every": _number(spec, "every", 60, 30 * 86400, default=300),
                    "max_iter": int(_number(spec, "max_iter", 1, MAX_ITER, default=1000))})
    elif kind == "set_var":
        out["name"] = _name(spec, "name", True)
        _one_of(spec, ("value", "expr"))
        if spec.get("expr") is not None:
            check_expression(spec["expr"])
        _value(spec)
        out.update({k: spec[k] for k in ("value", "expr") if spec.get(k) is not None})
    elif kind == "get_var":
        out["name"] = _name(spec, "name", True)
        if _name(spec, "into"):
            out["into"] = spec["into"]
    elif kind == "input":
        ask = _text(spec, "prompt", 300, True)
        into, form = _name(spec, "into", True), spec.get("kind", "text")
        if form not in ("text", "number", "choice"):
            raise PlanError("'kind' is text, number or choice")
        choices = _names(spec, "choices", 10)
        if form == "choice" and (not choices or len(choices) < 2):
            raise PlanError("a question with choices needs at least two")
        out.update({"prompt": ask, "into": into, "kind": form, **({"choices": choices} if choices and form == "choice" else {})})
    elif kind == "output":
        out["name"] = _name(spec, "name", True)
        _one_of(spec, ("value", "expr", "template"))
        if spec.get("expr") is not None:
            check_expression(spec["expr"])
        _value(spec)
        template = _text(spec, "template", TEXT_MAX)
        out.update({k: spec[k] for k in ("value", "expr") if spec.get(k) is not None})
        if template:
            out["template"] = template
    elif kind == "log":
        out["message"] = _text(spec, "message", 500, True)
    return out


def catalogue() -> List[Dict[str, Any]]:
    """The kinds in the order a page lists them, with their family, summary and fields."""
    return [{"type": k, **{f: v[f] for f in ("family", "auto", "summary", "fields")}} for k, v in NODE_SPECS.items()]


# -- the steps the plan works out itself ------------------------------------------------------------------------

JOURNAL_MAX = 200


def repeat_of(node: Any) -> Optional[Dict[str, Any]]:
    """What re-arms a step when it completes: its own `repeat`, or for a loop the one its spec describes (works, then again while the condition holds)."""
    if getattr(node.type, "value", node.type) == "loop":
        spec = node.spec
        return {"every": float(spec["every"]), "max_runs": int(spec["max_iter"]), "stop_when": f"not ({spec['while']})"}
    return node.repeat


def _template_context(dag: Any, now: float) -> Dict[str, Any]:
    return {**{k: v for k, v in dag.shared_state.items() if isinstance(k, str)}, "plan_id": dag.plan_id, "plan_title": dag.title,
            "date": datetime.fromtimestamp(now).astimezone().strftime("%Y-%m-%d %H:%M")}


def _brief(value: Any) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= 80 else text[:79] + "…"


def _startable(dag: Any, node: Any) -> bool:
    if node.status != "pending" or dag._blockers(node):
        return False
    parents = [dag.nodes[p].status for p in node.dependencies]
    return all(s in ("completed", "skipped") for s in parents) and (not parents or "completed" in parents)


def _complete(dag: Any, node: Any, now: float, outputs: Optional[Dict[str, Any]], line: str, kind: str = "auto") -> bool:
    try:
        dag.mark(node.id, "completed", outputs or None, now)
    except PlanError as exc:                       # it cannot finish now (a value too large, a name refused): it stays and says why
        node.state_variables["auto_error"] = str(exc)[:200]
        return False
    node.state_variables.pop("auto_error", None)
    dag.note(node.id, kind, line, now)
    return True


def _value_of(dag: Any, node: Any) -> Any:
    from tadamoo.planner.dag import safe_eval
    spec = node.spec
    return safe_eval(spec["expr"], dag.shared_state) if spec.get("expr") is not None else spec.get("value")


def _resolve(dag: Any, node: Any, now: float, due: List[float], judge: List[str]) -> bool:
    """One step the plan resolves by itself, if its time or its condition has come. True when it completed."""
    from tadamoo.cron_templates import render_template
    from tadamoo.planner.dag import safe_eval
    kind, spec, sv = node.type.value, node.spec, node.state_variables
    if kind == "wait_duration":
        started = sv.setdefault("wait_started", now)
        if now >= started + spec["seconds"]:
            return _complete(dag, node, now, None, f"{node.title}: waited {spec['seconds']:g} s")
        sv["wake_at"] = started + spec["seconds"]
        due.append(sv["wake_at"])
    elif kind == "wait_until":
        sv.setdefault("wait_started", now)
        at = parse_at(spec["at"])
        if now >= at:
            return _complete(dag, node, now, None, f"{node.title}: reached {spec['at']}")
        sv["wake_at"] = at
        due.append(at)
    elif kind == "wait_condition":
        started = sv.setdefault("wait_started", now)
        held = False
        if spec.get("expr"):
            try:
                held = bool(safe_eval(spec["expr"], dag.shared_state))
            except PlanError:
                held = False                                 # a value it needs is not there yet: not held
        if held:
            return _complete(dag, node, now, None, f"{node.title}: the condition holds")
        if spec.get("timeout_s") and now >= started + spec["timeout_s"]:
            if spec["on_timeout"] == "skip":
                dag.mark(node.id, "skipped")
                dag.note(node.id, "auto", f"{node.title}: gave up waiting, skipped", now)
            else:
                dag.timed_out(node.id, now)
                dag.note(node.id, "auto", f"{node.title}: timed out", now)
            return True
        if spec.get("timeout_s"):
            due.append(started + spec["timeout_s"])
        if spec.get("describe") and not spec.get("expr"):
            judge.append(node.id)
        due.append(now + spec["poll_s"])
    elif kind == "set_var":
        try:
            value = _value_of(dag, node)
        except PlanError as exc:
            sv["auto_error"] = str(exc)[:200]
            return False
        return _complete(dag, node, now, {spec["name"]: value}, f"{spec['name']} = {_brief(value)}")
    elif kind == "get_var":
        if spec["name"] not in dag.shared_state:
            sv["auto_error"] = f"'{spec['name']}' has no value yet"
            return False
        value = dag.shared_state[spec["name"]]
        sv["value"] = value
        return _complete(dag, node, now, {spec["into"]: value} if spec.get("into") else None, f"{spec['name']} is {_brief(value)}")
    elif kind == "output":
        try:
            value = render_template(spec["template"], _template_context(dag, now)) if spec.get("template") else _value_of(dag, node)
        except PlanError as exc:
            sv["auto_error"] = str(exc)[:200]
            return False
        return _complete(dag, node, now, {spec["name"]: value}, f"{spec['name']}: {_brief(value)}", "output")
    elif kind == "log":
        return _complete(dag, node, now, None, render_template(spec["message"], _template_context(dag, now)), "log")
    return False


def advance(dag: Any, now: float) -> Dict[str, Any]:
    """Resolves every step the plan can work out by itself at `now`, over and over until nothing more moves (a wait that ends lets what is after it
    start). Returns {"done": ids, "judge": ids of waits that only a reading of their description can settle, "next_due": the next moment
    something here comes due, or None}."""
    from tadamoo.planner.dag import safe_eval
    done: List[str] = []
    due: List[float] = []
    judge: List[str] = []
    moved = True
    while moved:
        moved, due, judge = False, [], []
        for nid in dag.topological_order():
            node = dag.nodes[nid]
            kind = node.type.value
            if not _startable(dag, node):
                continue
            if kind == "loop" and "runs" not in node.state_variables:       # a loop that does not hold on arrival is not entered
                try:
                    enter = bool(safe_eval(node.spec["while"], dag.shared_state))
                except PlanError:
                    enter = True
                if not enter and _complete(dag, node, now, None, f"{node.title}: not entered, its condition does not hold"):
                    node.state_variables["runs"] = 0
                    done.append(nid)
                    moved = True
                continue
            if kind in AUTO and _resolve(dag, node, now, due, judge):
                done.append(nid)
                moved = True
    return {"done": list(dict.fromkeys(done)), "judge": judge, "next_due": min(due) if due else None}
