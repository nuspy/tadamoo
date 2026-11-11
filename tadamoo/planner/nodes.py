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
FIELD_KINDS = ("number", "integer", "text", "textarea", "expression", "name", "names", "datetime", "choice", "json", "archives", "branches")
ARCHIVE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_ARCHIVES, MAX_BRANCHES, LABEL_MAX = 50, 12, 40
OPS = ("<", "<=", "=", "!=", ">=", ">", "between", "in", "contains", "else")      # what the helper of a branch can build; `else` takes what no other branch took
ON_NO_MATCH = ("stop", "ask")                                                    # no branch holds and there is no else: that path stops, or the decision goes to the person
MODES = ("first", "all")                                                         # a decision: the first branch that holds wins, or every branch that holds fires

NODE_SPECS: Dict[str, Dict[str, Any]] = {
    "action": {"family": "action", "auto": False, "summary": "A task for the agent or the person", "fields": [
        {"name": "rag_archives", "kind": "archives", "max": MAX_ARCHIVES}]},
    "sensor": {"family": "action", "auto": False, "summary": "Listens for an event and resolves when its condition holds", "fields": []},
    "decision": {"family": "flow", "auto": False, "summary": "Has several outcomes, each its own connector: the branches whose condition holds are taken", "fields": [
        {"name": "input", "kind": "expression", "max": 300}, {"name": "mode", "kind": "choice", "choices": list(MODES), "default": "first"},
        {"name": "on_no_match", "kind": "choice", "choices": list(ON_NO_MATCH), "default": "stop"},
        {"name": "branches", "kind": "branches", "required": True, "max": MAX_BRANCHES}]},
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
    "set_var": {"family": "data", "auto": True, "summary": "Sets a value, in the plan or in the values shared by all plans", "fields": [
        {"name": "name", "kind": "name", "required": True}, {"name": "value", "kind": "json"}, {"name": "expr", "kind": "expression"},
        {"name": "target", "kind": "choice", "choices": ["plan", "global"], "default": "plan"}]},
    "get_var": {"family": "data", "auto": True, "summary": "Reads a value: a value of the plan, the clock, a value shared by all plans or a value another step wrote", "fields": [
        {"name": "name", "kind": "name", "required": True}, {"name": "into", "kind": "name"},
        {"name": "source", "kind": "choice", "choices": ["variable", "clock", "global", "step"], "default": "variable"}, {"name": "step", "kind": "name"}]},
    "input": {"family": "io", "auto": False, "summary": "Asks the person for a value", "fields": [
        {"name": "prompt", "kind": "textarea", "required": True, "max": 300}, {"name": "into", "kind": "name", "required": True},
        {"name": "kind", "kind": "choice", "choices": ["text", "number", "choice"], "default": "text"}, {"name": "choices", "kind": "names", "max": 10}]},
    "output": {"family": "io", "auto": True, "summary": "Shows a result of the plan", "fields": [
        {"name": "name", "kind": "name", "required": True}, {"name": "value", "kind": "json"}, {"name": "expr", "kind": "expression"},
        {"name": "template", "kind": "textarea", "max": TEXT_MAX}]},
    "rag": {"family": "data", "auto": True, "summary": "A source of knowledge archives: connected to the steps that may use them; it never waits and nothing waits for it", "fields": [
        {"name": "archives", "kind": "archives", "required": True, "max": MAX_ARCHIVES}]},
    "log": {"family": "io", "auto": True, "summary": "Writes a line in the plan's log", "fields": [
        {"name": "message", "kind": "textarea", "required": True, "max": 500}]},
}
for _row in NODE_SPECS.values():                    # the numbers that are lengths of time, so a page can offer minutes, hours and days
    for _field in _row["fields"]:
        if _field["name"] in ("seconds", "every", "timeout_s", "poll_s"):
            _field["unit"] = "seconds"
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


def _archives(spec: Dict[str, Any], key: str, required: bool = False) -> Optional[List[str]]:
    value = spec.get(key)
    if value is None or value == []:
        if required:
            raise PlanError(f"'{key}' needs at least one archive")
        return None
    if not isinstance(value, list) or len(value) > MAX_ARCHIVES or any(not isinstance(v, str) or not ARCHIVE_ID.match(v) for v in value):
        raise PlanError(f"'{key}' is a list of at most {MAX_ARCHIVES} knowledge archive ids")
    return list(dict.fromkeys(value))


# -- the connectors of a step: one table for the checks, the tools and the page ----------------------------------------------------
# A port has an id, a kind (`flow`: the step below waits for this one; `input`: data given to a step, which neither waits nor is waited for) and a label.
# An edge goes from an output port of one step to an input port of another of the same kind. A decision has one output port for each of its branches.

PORT_IN = {"default": [{"id": "in", "kind": "flow"}], "action": [{"id": "in", "kind": "flow"}, {"id": "rag", "kind": "input"}], "rag": []}
PORT_OUT = {"default": [{"id": "out", "kind": "flow"}], "decision": "branches", "rag": [{"id": "rag", "kind": "input"}]}
BRANCH_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def in_ports(kind: str) -> List[Dict[str, str]]:
    return [dict(p) for p in PORT_IN.get(kind, PORT_IN["default"])]


def out_ports(kind: str, spec: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """The output connectors of a step of this kind: for a decision its branches in the order shown, with their labels."""
    ports = PORT_OUT.get(kind, PORT_OUT["default"])
    if ports == "branches":
        return [{"id": b["id"], "kind": "flow", "label": b.get("label", b["id"])} for b in (spec or {}).get("branches") or []]
    return [dict(p) for p in ports]


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).strip()
    try:
        return repr(int(text))
    except ValueError:
        pass
    try:
        number = float(text)
        if number == number and abs(number) != float("inf"):
            return repr(number)
    except ValueError:
        pass
    if not text or len(text) > 100 or any(c in text for c in ("\\", "\n", "\r")):
        raise PlanError("a value to compare is a number or a short text")
    return '"' + text.replace('"', "'") + '"'


def build_condition(subject: Any, op: str, value: Any = None, value2: Any = None) -> str:
    """The expression a branch's helper stands for: `input op value` (between: both ends; in: a list; contains: the value is in the input)."""
    if not isinstance(subject, str) or not subject.strip():
        raise PlanError("set the value the branches compare (the decision's 'input') to build a condition from an operator")
    s = f"({subject.strip()})"
    if op in ("<", "<=", ">=", ">", "!="):
        text = f"{s} {op} {_literal(value)}"
    elif op == "=":
        text = f"{s} == {_literal(value)}"
    elif op == "between":
        text = f"({s} >= {_literal(value)} and {s} <= {_literal(value2)})"
    elif op == "in":
        items = value if isinstance(value, list) else [x for x in str(value).split(",") if x.strip()]
        if not items:
            raise PlanError("'in list' needs at least one value")
        text = f"{s} in [{', '.join(_literal(x) for x in items)}]"
    elif op == "contains":
        text = f"{_literal(value)} in {s}"
    else:
        raise PlanError(f"an operator is one of {', '.join(OPS)}")
    check_expression(text)
    return text


def clean_branches(raw: Any, subject: Optional[str]) -> List[Dict[str, Any]]:
    """The ordered branches of a decision, each {id, label, condition} (and the helper's op, value, value2 when it was built from them). New branches get
    an id; the labels are unique; `else` is at most one and last."""
    if not isinstance(raw, list) or not raw:
        raise PlanError("a decision needs at least one branch")
    if len(raw) > MAX_BRANCHES:
        raise PlanError(f"a decision has at most {MAX_BRANCHES} branches")
    taken = {b["id"] for b in raw if isinstance(b, dict) and isinstance(b.get("id"), str)}
    out: List[Dict[str, Any]] = []
    labels, ids, n = set(), set(), 0
    for i, b in enumerate(raw):
        if not isinstance(b, dict):
            raise PlanError("a branch is an object with a label and a condition")
        label = b.get("label")
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > LABEL_MAX:
            raise PlanError(f"a branch has a label of 1 to {LABEL_MAX} characters")
        label = label.strip()
        if label.lower() in labels:
            raise PlanError(f"two branches are labelled '{label}': labels are unique")
        labels.add(label.lower())
        bid = b.get("id")
        if bid is None or bid == "":
            while f"b{n + 1}" in taken or f"b{n + 1}" in ids:
                n += 1
            n += 1
            bid = f"b{n}"
        if not isinstance(bid, str) or not BRANCH_ID.match(bid) or bid in ids:
            raise PlanError("a branch id is letters, digits and - _ . and is not used twice")
        ids.add(bid)
        op = b.get("op")
        entry: Dict[str, Any] = {"id": bid, "label": label}
        if op == "else":
            if i != len(raw) - 1:
                raise PlanError("the 'else' branch is the last one")
            entry["op"] = "else"
        elif op:
            if op not in OPS:
                raise PlanError(f"an operator is one of {', '.join(OPS)}")
            entry.update(op=op, value=b.get("value"), condition=build_condition(subject, op, b.get("value"), b.get("value2")))
            if op == "between":
                entry["value2"] = b.get("value2")
        else:
            cond = b.get("condition")
            check_expression(cond)
            entry["condition"] = cond.strip()
        out.append(entry)
    return out


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


def clean_spec(node_type: Any, spec: Any, draft: bool = False) -> Dict[str, Any]:
    """The spec a kind of step may carry, checked and filled with its defaults; raises PlanError naming what is wrong. A `draft` step (put on the
    plan before it is set up) keeps what it was given when that is not yet complete."""
    if draft:
        try:
            return clean_spec(node_type, spec)
        except PlanError:
            kind = getattr(node_type, "value", node_type)
            allowed = {f["name"] for f in NODE_SPECS.get(kind, {"fields": []})["fields"]}
            return {k: v for k, v in (spec if isinstance(spec, dict) else {}).items() if k in allowed}
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
    if kind == "action":
        if _archives(spec, "rag_archives"):
            out["rag_archives"] = _archives(spec, "rag_archives")
    elif kind == "rag":
        out["archives"] = _archives(spec, "archives", True)
    elif kind == "decision":
        subject = spec.get("input")
        if subject is not None and subject != "":
            check_expression(subject)
            out["input"] = subject.strip()
        mode = spec.get("mode", "first")
        if mode not in MODES:
            raise PlanError("'mode' is first or all")
        out["mode"] = mode
        on_no_match = spec.get("on_no_match", "stop")
        if on_no_match not in ON_NO_MATCH:
            raise PlanError("'on_no_match' is stop or ask")
        out["on_no_match"] = on_no_match
        out["branches"] = clean_branches(spec.get("branches"), out.get("input"))
    elif kind == "wait_duration":
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
        target = spec.get("target", "plan")
        if target not in ("plan", "global"):
            raise PlanError("'target' is plan or global")
        out["target"] = target
    elif kind == "get_var":
        out["name"] = _name(spec, "name", True)
        if _name(spec, "into"):
            out["into"] = spec["into"]
        source = spec.get("source", "variable")
        if source not in ("variable", "clock", "global", "step"):
            raise PlanError("'source' is variable, clock, global or step")
        out["source"] = source
        if source == "clock" and spec["name"] not in CLOCK_NAMES:
            raise PlanError(f"the clock gives {', '.join(CLOCK_NAMES)}")
        if source == "step":
            out["step"] = _name(spec, "step", True)
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


PORTS_HELP = ("A decision has an ordered list of branches in spec.branches, each {label, op (< <= = != >= > between in contains, with value and value2, compared with spec.input), "
              "or condition (an expression over the plan's values); else for the one taken when no other is}, mode first (the first that holds) or all; each branch is an output "
              "connector named by its id: connect it with [decision, child, branch_id]. A rag node ({archives: [knowledge archive ids]}) is an input to the actions it is connected to "
              "([rag_node, action]): it neither waits nor is waited for. An action's own archives are spec.rag_archives; the whole plan's are profile.rag_archives (plan_set_profile). "
              "Use the ids knowledge_list shows.")


def kinds_line() -> str:
    """The kinds beyond the four basic ones, each with the fields of its spec, in a line a tool description can carry."""
    return "; ".join(f"{k} {{{', '.join(f['name'] + ('' if f.get('required') else '?') for f in v['fields'])}}}" for k, v in NODE_SPECS.items() if v["fields"])


def catalogue() -> List[Dict[str, Any]]:
    """The kinds in the order a page lists them, with their family, summary and fields."""
    return [{"type": k, **{f: v[f] for f in ("family", "auto", "summary", "fields")}, "ports": {"in": in_ports(k), "out": out_ports(k) if k != "decision" else "branches"}}
            for k, v in NODE_SPECS.items()]


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


# -- values: where a step reads them from and where it sets them ----------------------------------------------------------

CLOCK_NAMES = ("now", "date", "time", "weekday", "hour", "timestamp")


class GlobalValues:
    """The values every plan can read and set (`target: global`, `source: global`): small plain data, kept in one JSON file when it has a path."""
    LIMIT = 200

    def __init__(self, path: Any = None) -> None:
        self.path, self._data = path, {}

    def _load(self) -> Dict[str, Any]:
        if self.path is not None:
            try:
                import json
                data = json.loads(open(self.path, encoding="utf-8").read())
                if isinstance(data, dict):
                    self._data = data
            except (OSError, ValueError):
                pass
        return self._data

    def get(self, name: str, default: Any = None) -> Any:
        return self._load().get(name, default)

    def has(self, name: str) -> bool:
        return name in self._load()

    def set(self, name: str, value: Any) -> None:
        data = self._load()
        if len(data) >= self.LIMIT and name not in data:
            raise PlanError(f"the shared values hold at most {self.LIMIT} names")
        data[name] = value
        if self.path is not None:
            import json
            import os
            import tempfile
            directory = os.path.dirname(os.fspath(self.path)) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix="globals.", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)


GLOBALS = GlobalValues()


def clock_value(name: str, now: float) -> Any:
    moment = datetime.fromtimestamp(now).astimezone()
    return {"now": moment.strftime("%Y-%m-%d %H:%M"), "date": moment.strftime("%Y-%m-%d"), "time": moment.strftime("%H:%M"),
            "weekday": moment.strftime("%A").lower(), "hour": moment.hour, "timestamp": int(now)}[name]


def read_value(dag: Any, spec: Dict[str, Any], now: float) -> Any:
    """(found, value) for a get_var: from the plan's values, the clock, the values shared by all plans, or the output another step wrote."""
    source, name = spec.get("source", "variable"), spec["name"]
    if source == "clock":
        return True, clock_value(name, now)
    if source == "global":
        return GLOBALS.has(name), GLOBALS.get(name)
    if source == "step":
        step = dag.nodes.get(spec.get("step"))
        outputs = (step.state_variables.get("outputs") if step is not None else None) or {}
        return name in outputs, outputs.get(name)
    return name in dag.shared_state, dag.shared_state.get(name)


PRODUCE_TYPES = ("number", "text", "bool", "list", "object", "any")
MAX_PRODUCES = 12


def clean_produces(value: Any) -> List[Dict[str, str]]:
    """The values a step must write when it completes: a list of {"name", "type"} (or just names, which are of any type)."""
    if value is None or value == []:
        return []
    if not isinstance(value, list) or len(value) > MAX_PRODUCES:
        raise PlanError(f"'produces' is a list of at most {MAX_PRODUCES} values, each with a name and a type")
    out: List[Dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].isidentifier():
            raise PlanError("a value a step produces has a name of letters, digits and _, starting with a letter")
        kind = item.get("type", "any")
        if kind not in PRODUCE_TYPES:
            raise PlanError(f"the type of '{item['name']}' is one of {', '.join(PRODUCE_TYPES)}")
        if any(o["name"] == item["name"] for o in out):
            raise PlanError(f"'{item['name']}' is listed twice")
        out.append({"name": item["name"], "type": kind})
    return out


def _is_type(value: Any, kind: str) -> bool:
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return {"text": isinstance(value, str), "bool": isinstance(value, bool), "list": isinstance(value, list), "object": isinstance(value, dict)}.get(kind, True)


def check_produced(produces: List[Dict[str, str]], outputs: Optional[Dict[str, Any]]) -> None:
    """A step that says what it produces completes with exactly those values, of those types."""
    given = outputs or {}
    missing = [p["name"] for p in produces if p["name"] not in given]
    if missing:
        raise PlanError("this step must produce " + ", ".join(f"{p['name']} ({p['type']})" for p in produces if p["name"] in missing)
                        + ": complete it with outputs {" + ", ".join(f"{m}: ..." for m in missing) + "}")
    wrong = [p["name"] for p in produces if not _is_type(given[p["name"]], p["type"])]
    if wrong:
        raise PlanError("these values are not of the type the step says: " + ", ".join(f"{w} should be {next(p['type'] for p in produces if p['name'] == w)}" for w in wrong))


def _brief(value: Any) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= 80 else text[:79] + "…"


def _startable(dag: Any, node: Any) -> bool:
    if node.status != "pending" or node.state_variables.get("draft") or dag._blockers(node):
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
        if spec.get("target") == "global":
            GLOBALS.set(spec["name"], value)
            sv["value"] = value
            return _complete(dag, node, now, None, f"{spec['name']} = {_brief(value)} (shared by all plans)")
        return _complete(dag, node, now, {spec["name"]: value}, f"{spec['name']} = {_brief(value)}")
    elif kind == "get_var":
        found, value = read_value(dag, spec, now)
        if not found:
            sv["auto_error"] = f"'{spec['name']}' has no value yet"
            return False
        sv["value"] = value
        return _complete(dag, node, now, {spec.get("into") or spec["name"]: value} if spec.get("into") or spec.get("source", "variable") != "variable" else None,
                         f"{spec['name']} is {_brief(value)}")
    elif kind == "output":
        try:
            value = render_template(spec["template"], _template_context(dag, now)) if spec.get("template") else _value_of(dag, node)
        except PlanError as exc:
            sv["auto_error"] = str(exc)[:200]
            return False
        return _complete(dag, node, now, {spec["name"]: value}, f"{spec['name']}: {_brief(value)}", "output")
    elif kind == "log":
        return _complete(dag, node, now, None, render_template(spec["message"], _template_context(dag, now)), "log")
    elif kind == "rag":
        return _complete(dag, node, now, None, f"{node.title}: {len(spec['archives'])} knowledge archive(s) for {len(node.routes.get('rag') or [])} step(s)")
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
