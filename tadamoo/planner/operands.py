"""The terms of a decision's comparison: a first term, a comparison and a second term, each an operand with a source.

An operand is `{kind: value, value}`, `{kind: python, expr}` (a condition expression over the plan's values and the node's inputs), `{kind: input, from: name}` (an
input wired to the node, by its name) or `{kind: input, variable: key}` (a value of the plan), or `{kind: agent, describe}` (text telling the agent how to work the
value out; the agent reports it, typed, and it is kept on the node). A comparison branch is `{id, label, left, op, right, right2?}`; `right2` is the upper end of
`between`. An operator that comes out as anything but a known one stops the decision with a clear message, never a guess."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from tadamoo.planner.dag import PlanError, check_expression

KINDS = ("value", "python", "input", "agent")
SLOTS = ("left", "op", "right", "right2")
OPS = ("<", "<=", "=", "!=", ">=", ">", "between", "in", "contains")
ALIASES = {"==": "=", "<>": "!=", "≤": "<=", "≥": ">=", "between": "between", "in": "in", "contains": "contains"}
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
DESCRIBE_MAX = 600


def clean(raw: Any, slot: str) -> Dict[str, Any]:
    """An operand checked and normalised; a bare value (a number, a text) is a value operand."""
    if not isinstance(raw, dict):
        raw = {"kind": "value", "value": raw}
    kind = raw.get("kind", "value")
    if kind not in KINDS:
        raise PlanError(f"the {slot} is a value, a python expression, an input or an agent description")
    if kind == "value":
        value = raw.get("value")
        if value is None or value == "" or isinstance(value, (dict,)) or (isinstance(value, list) and any(isinstance(v, (dict, list)) for v in value)):
            raise PlanError(f"give the {slot} a value")
        if slot == "op" and (not isinstance(value, str) or value.strip() not in OPS):
            raise PlanError(f"the comparison is one of {', '.join(OPS)}")
        return {"kind": "value", "value": value.strip() if isinstance(value, str) and slot == "op" else value}
    if kind == "python":
        check_expression(raw.get("expr"))
        return {"kind": "python", "expr": raw["expr"].strip()}
    if kind == "input":
        source = raw.get("from") or raw.get("variable")
        if not isinstance(source, str) or not NAME.match(source):
            raise PlanError(f"the {slot} names an input of the node or a value of the plan (letters, digits and _)")
        return {"kind": "input", **({"variable": source} if raw.get("variable") and not raw.get("from") else {"from": source})}
    describe = raw.get("describe")
    if not isinstance(describe, str) or not describe.strip() or len(describe) > DESCRIBE_MAX:
        raise PlanError(f"describe how the agent works out the {slot}, in 1 to {DESCRIBE_MAX} characters")
    return {"kind": "agent", "describe": describe.strip()}


def clean_compare(branch: Dict[str, Any]) -> Dict[str, Any]:
    """The operands of a comparison branch, checked. Legacy `op`, `value`, `value2` become value operands (the decision's `input` is the first term)."""
    out = {slot: clean(branch[slot], slot) for slot in ("left", "op", "right") if branch.get(slot) is not None}
    missing = [s for s in ("left", "op", "right") if s not in out]
    if missing:
        raise PlanError("a comparison needs " + ", ".join(missing))
    between = out["op"] == {"kind": "value", "value": "between"}
    if between or branch.get("right2") is not None:
        if branch.get("right2") is None:
            raise PlanError("'between' needs a second value (right2)")
        out["right2"] = clean(branch["right2"], "right2")
    return out


def legacy(branch: Dict[str, Any], subject: Optional[str]) -> Dict[str, Any]:
    """A branch stored as {op, value, value2} with the decision's `input`: the operands it stands for."""
    left = {"kind": "python", "expr": subject} if subject else {"kind": "value", "value": ""}
    out = {"left": left, "op": {"kind": "value", "value": branch["op"]}, "right": {"kind": "value", "value": branch.get("value")}}
    if branch["op"] == "between":
        out["right2"] = {"kind": "value", "value": branch.get("value2")}
    return out


@dataclass
class Result:
    holds: Optional[bool] = None                                   # None: it could not be decided now (see `error` or `waits`)
    error: str = ""
    waits: List[Dict[str, Any]] = field(default_factory=list)      # what is missing: {slot, kind: "agent" | "input", describe | name}
    compared: Dict[str, Any] = field(default_factory=dict)         # what was compared: the term values and the operator


def _number(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            try:
                return float(value.strip())
            except ValueError:
                return value
    return value


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _compare(left: Any, op: str, right: Any, right2: Any) -> bool:
    a, b = _number(left), _number(right)
    if op == "in":
        items = right if isinstance(right, list) else [x.strip() for x in str(right).split(",") if x.strip()]
        return a in [_number(x) for x in items] or left in items
    if op == "contains":
        return str(right) in left if isinstance(left, (list, str)) else str(right) in str(left)
    if op == "=":
        return a == b
    if op == "!=":
        return a != b
    hi = _number(right2) if op == "between" else b
    if not (_is_number(a) and _is_number(b) and _is_number(hi)):
        raise PlanError(f"cannot compare {left!r} {op} {right!r}: they are not all numbers")
    return b <= a <= hi if op == "between" else {"<": a < b, "<=": a <= b, ">=": a >= b, ">": a > b}[op]


def typed(value: Any, slot: str) -> Any:
    """A value an agent reports for an operand: a text, a number, true/false, or (for the second term of `in`) a list of those. Anything else is refused."""
    plain = lambda v: isinstance(v, (str, int, float, bool)) and not (isinstance(v, float) and v != v)
    if plain(value) or (slot in ("right", "right2") and isinstance(value, list) and all(plain(v) for v in value) and value):
        return value
    raise PlanError(f"the value of the {slot} is a text, a number, true/false" + (" or a list of those" if slot in ("right", "right2") else ""))


def evaluate(branch: Dict[str, Any], env: Dict[str, Any], inputs: Dict[str, Any], reported: Dict[str, Any], safe_eval: Callable[[str, Dict[str, Any]], Any]) -> Result:
    """Whether a comparison branch holds. `env` is the plan's values, `inputs` the values of the node's wired inputs by name, `reported` what the agent gave
    for its agent operands, by `<slot>`. Value, python and input operands are worked out at once; an agent operand without its value makes the branch wait."""
    res = Result()
    values: Dict[str, Any] = {}
    names = {**env, **inputs}
    for slot in SLOTS:
        operand = branch.get(slot)
        if operand is None:
            continue
        kind = operand["kind"]
        if kind == "value":
            values[slot] = operand["value"]
        elif kind == "python":
            try:
                values[slot] = safe_eval(operand["expr"], names)
            except PlanError as exc:
                res.error = f"{slot}: {exc}"[:200]
                return res
        elif kind == "input":
            name = operand.get("from") or operand.get("variable")
            source = inputs if operand.get("from") else env
            if name in source:
                values[slot] = source[name]
            else:
                res.waits.append({"slot": slot, "kind": "input", "name": name})
        elif slot in reported:
            values[slot] = reported[slot]
        else:
            res.waits.append({"slot": slot, "kind": "agent", "describe": operand["describe"]})
    if res.waits:
        return res
    op = values["op"]
    op = ALIASES.get(op.strip(), op.strip()) if isinstance(op, str) else op
    res.compared = {"left": values["left"], "op": op, "right": values["right"], **({"right2": values["right2"]} if "right2" in values else {})}
    if op not in OPS:
        res.error = f"unknown operator '{values['op']}': one of {', '.join(OPS)}"
        return res
    try:
        res.holds = _compare(values["left"], op, values["right"], values.get("right2"))
    except (PlanError, TypeError) as exc:
        res.error = str(exc)[:200]
    return res
