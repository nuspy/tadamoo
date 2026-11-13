"""PlanDAG: the workflow graph of a plan, with shared state, decisions, merges and human tasks.

Edges live in two lists (a node's `dependencies` and its parents' `outputs`); every change keeps them
symmetric and every method that can break the graph leaves it exactly as it was if it fails.

Decision conditions are small expressions over the plan's shared variables (see safe_eval), never code.
"""
from __future__ import annotations

import ast
import copy
import enum
import heapq
import json
import keyword
import math
import operator
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

STATUSES = ("pending", "in_progress", "completed", "failed", "blocked", "skipped")
DONE = ("completed", "skipped")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class NodeType(str, enum.Enum):
    ACTION = "action"        # Executable task (agent or user)
    SENSOR = "sensor"        # Environment sensing / event monitoring
    DECISION = "decision"    # Several outcomes: an ordered list of branches, each its own output connector
    MERGE = "merge"          # Parallel branch synchronization
    WAIT_DURATION = "wait_duration"      # the kinds below carry a `spec`; tadamoo/planner/nodes.py says what each one is
    WAIT_UNTIL = "wait_until"
    WAIT_CONDITION = "wait_condition"
    LOOP = "loop"
    SET_VAR = "set_var"
    GET_VAR = "get_var"
    INPUT = "input"
    OUTPUT = "output"
    LOG = "log"
    RAG = "rag"              # a source of knowledge archives, connected as an input to the steps that may use them
    START = "start"          # where a plan begins; it has a number and says how it is started (the person, an event, a time schedule, another plan)
    START_PLAN = "start_plan"    # starts another plan at the start node whose number it names


class Assignee(str, enum.Enum):
    AGENT = "agent"
    USER = "user"
    THIRD_PARTY = "third_party"


class PlanError(ValueError):
    """The change would leave the plan invalid; the plan is unchanged."""


class NeedValue(PlanError):
    """A decision cannot be taken yet: a value it compares is for the agent to work out, or an input has no value. `facts` says which."""

    def __init__(self, message: str, facts: Dict[str, Any]) -> None:
        super().__init__(message)
        self.facts = facts


class NoRoute(PlanError):
    """A decision was completed and none of its exits holds; `facts` says which conditions were read and with what values."""

    def __init__(self, message: str, facts: Dict[str, Any]) -> None:
        super().__init__(message)
        self.facts = facts


# -- condition expressions -----------------------------------------------------------------------------

_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
        ast.GtE: operator.ge, ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}
_FUNCS = {"len": len, "min": min, "max": max, "sum": sum, "abs": abs}
MAX_SEQUENCE = 10_000        # longest list or text a condition may build
MAX_NUMBER_BITS = 256        # largest whole number a condition may build
MAX_POWER = 64


def _number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _check_size(value: Any) -> Any:
    if isinstance(value, (list, str)) and len(value) > MAX_SEQUENCE:
        raise PlanError(f"a condition may not build a list or text longer than {MAX_SEQUENCE}")
    if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > MAX_NUMBER_BITS:
        raise PlanError("a condition may not build a number that large")
    return value


def _add(a: Any, b: Any) -> Any:
    return _check_size(a + b)


def _mul(a: Any, b: Any) -> Any:
    for seq, count in ((a, b), (b, a)):
        if isinstance(seq, (list, str)) and isinstance(count, int) and not isinstance(count, bool):
            if len(seq) * max(count, 0) > MAX_SEQUENCE:
                raise PlanError(f"a condition may not build a list or text longer than {MAX_SEQUENCE}")
    if isinstance(a, int) and isinstance(b, int) and not isinstance(a, bool) and not isinstance(b, bool)             and a.bit_length() + b.bit_length() > MAX_NUMBER_BITS:
        raise PlanError("a condition may not build a number that large")
    return _check_size(a * b)


def _pow(a: Any, b: Any) -> Any:
    if not (_number(a) and _number(b)) or abs(b) > MAX_POWER:
        raise PlanError(f"a power needs numbers and an exponent of at most {MAX_POWER}")
    if isinstance(a, int) and isinstance(b, int) and b > 0 and a.bit_length() * b > MAX_NUMBER_BITS:
        raise PlanError("a condition may not build a number that large")
    return _check_size(a ** b)


_ARITH = {ast.Add: _add, ast.Sub: operator.sub, ast.Mult: _mul, ast.Div: operator.truediv, ast.Pow: _pow}


def safe_eval(expression: str, variables: Dict[str, Any]) -> Any:
    """Evaluates a condition such as `len(invitati_confermati) >= 2 and meteo_confermato`.

    Only names, literals, comparisons, and/or/not, + - * / **, lists/tuples and len/min/max/sum/abs are allowed.
    What it builds is bounded (lists, texts, numbers), and whatever goes wrong is a PlanError, never another exception."""
    if not isinstance(expression, str) or len(expression) > 300:
        raise PlanError("a condition must be a string of at most 300 characters")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise PlanError(f"invalid condition '{expression}': {exc.msg}") from None
    except (ValueError, RecursionError, MemoryError):
        raise PlanError(f"invalid condition '{expression}'") from None

    def walk(n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return walk(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float, str, bool, type(None))):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in ("True", "False", "None"):
                return {"True": True, "False": False, "None": None}[n.id]
            if n.id not in variables:
                raise PlanError(f"the condition uses '{n.id}', which is not set yet")
            return variables[n.id]
        if isinstance(n, (ast.List, ast.Tuple)):
            return [walk(e) for e in n.elts]
        if isinstance(n, ast.BoolOp):
            vals = [walk(v) for v in n.values]
            return all(vals) if isinstance(n.op, ast.And) else any(vals)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            return not walk(n.operand)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -walk(n.operand)
        if isinstance(n, ast.BinOp) and type(n.op) in _ARITH:
            return _ARITH[type(n.op)](walk(n.left), walk(n.right))
        if isinstance(n, ast.Compare):
            left = walk(n.left)
            for op, right_node in zip(n.ops, n.comparators):
                if type(op) not in _CMP:
                    raise PlanError(f"'{expression}' uses a comparison a condition may not use")
                right = walk(right_node)
                if not _CMP[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[walk(a) for a in n.args])
        raise PlanError(f"'{expression}' uses something a condition may not use")

    try:
        return walk(tree)
    except PlanError:
        raise
    except (TypeError, ValueError, ArithmeticError, RecursionError, MemoryError) as exc:
        raise PlanError(f"the condition '{expression}' could not be evaluated: {type(exc).__name__}: {exc}") from None


# -- nodes and the graph -------------------------------------------------------------------------------

@dataclass
class PlanNode:
    id: str
    type: NodeType
    title: str
    description: str
    assignee: Assignee
    dependencies: List[str] = field(default_factory=list)  # Parent node IDs
    outputs: List[str] = field(default_factory=list)       # Child node IDs
    conditions: Dict[str, Any] = field(default_factory=dict)
    state_variables: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending, in_progress, completed, failed, blocked, skipped
    retry_policy: Optional[Dict[str, Any]] = None        # {"max_attempts", "wait_seconds", "backoff"}: tries before the step counts as failed
    position: Optional[Dict[str, float]] = None          # where the canvas draws the node, {"x", "y"}; None: the automatic layout
    call_request: Optional[Dict[str, str]] = None        # a call the plan prepared, {"number", "reason", "text"}; only the Chat's phone_call places it
    wiki_query: Optional[str] = None                     # what this step wants from the knowledge wiki, when there is one
    repeat: Optional[Dict[str, Any]] = None              # {"every", "max_runs", "reset_from", "stop_when"}: completing the step re-arms it instead of finishing it
    spec: Dict[str, Any] = field(default_factory=dict)   # what the kinds beyond action/sensor/decision/merge need (tadamoo/planner/nodes.py)
    produces: List[Dict[str, str]] = field(default_factory=list)   # the values this step must write when it completes: [{"name", "type"}]
    routes: Dict[str, List[str]] = field(default_factory=dict)     # output port id -> the steps it is connected to; kept for the nodes with named ports (a decision's branches, a RAG node)
    inputs: Dict[str, Dict[str, str]] = field(default_factory=dict)  # the values wired to this step, by the name it refers to them with: {"node", "port"} of the step they come from


EDITABLE = ("title", "description", "assignee", "conditions", "state_variables", "retry_policy", "position", "call_request", "wiki_query", "repeat", "spec", "produces", "inputs")

# -- what a node may carry ---------------------------------------------------------------------------------

MAX_ATTEMPTS, MAX_WAIT_SECONDS, MAX_BACKOFF = 10, 7 * 86400, 10
DEFAULT_RETRY = {"max_attempts": 3, "wait_seconds": 30, "backoff": 2}      # a step without its own policy: tries again, waiting longer each time
POSITION_LIMIT = 1_000_000
_NUMBER = re.compile(r"^\+?[0-9][0-9 ()\-]{5,19}$")
CALL_REASON_CHARS, CALL_TEXT_CHARS, WIKI_QUERY_CHARS = 300, 500, 300
SENSOR_VARIABLES = ("text", "source", "at")      # what a sensor's condition can read of an event, besides the plan's values
_CONDITION_NODES = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.List, ast.Tuple, ast.BoolOp, ast.And, ast.Or,
                    ast.UnaryOp, ast.Not, ast.USub, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Compare,
                    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Call)


def _whole(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def clean_retry_policy(value: Any) -> Optional[Dict[str, Any]]:
    """{"max_attempts": 1-10 (the first try counts), "wait_seconds": 0-a week, "backoff": 1-10 (each wait is this much longer)}."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"max_attempts", "wait_seconds", "backoff"}:
        raise PlanError("a retry policy is {max_attempts, wait_seconds, backoff}")
    attempts = value.get("max_attempts", 1)
    wait, backoff = value.get("wait_seconds", 0), value.get("backoff", 1)
    if not _whole(attempts) or not 1 <= attempts <= MAX_ATTEMPTS:
        raise PlanError(f"max_attempts is a whole number from 1 to {MAX_ATTEMPTS}")
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 <= wait <= MAX_WAIT_SECONDS:
        raise PlanError(f"wait_seconds is a number from 0 to {MAX_WAIT_SECONDS}")
    if isinstance(backoff, bool) or not isinstance(backoff, (int, float)) or not 1 <= backoff <= MAX_BACKOFF:
        raise PlanError(f"backoff is a number from 1 to {MAX_BACKOFF}")
    return {"max_attempts": attempts, "wait_seconds": wait, "backoff": backoff}


def clean_position(value: Any) -> Optional[Dict[str, float]]:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"x", "y"}:
        raise PlanError("a position is {x, y}")
    out = {}
    for axis in ("x", "y"):
        v = value[axis]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > POSITION_LIMIT:
            raise PlanError(f"position {axis} must be a number between -{POSITION_LIMIT} and {POSITION_LIMIT}")
        out[axis] = float(v)
    return out


def clean_call_request(value: Any) -> Optional[Dict[str, str]]:
    """A call the plan prepared: the number, why to call and, optionally, what to say. Nothing here places a call."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"number", "reason", "text"}:
        raise PlanError("a call request is {number, reason, text}")
    number, reason, text = value.get("number"), value.get("reason"), value.get("text", "")
    if not isinstance(number, str) or not _NUMBER.match(number.strip()):
        raise PlanError("the call request needs a phone number with the country code")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > CALL_REASON_CHARS:
        raise PlanError(f"the call request needs a reason of 1 to {CALL_REASON_CHARS} characters")
    if not isinstance(text, str) or len(text) > CALL_TEXT_CHARS:
        raise PlanError(f"the text of a call request is at most {CALL_TEXT_CHARS} characters")
    return {"number": number.strip(), "reason": reason.strip(), "text": text.strip()}


def clean_wiki_query(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > WIKI_QUERY_CHARS:
        raise PlanError(f"the wiki query is a text of at most {WIKI_QUERY_CHARS} characters")
    return value.strip() or None


MIN_REPEAT_S, MAX_REPEAT_S, MAX_REPEAT_RUNS = 60, 30 * 86400, 10_000
RESET_FROM_RE = re.compile(r"^(?:value|session|skill):[A-Za-z0-9_.:-]{1,80}$")


def clean_repeat(value: Any) -> Optional[Dict[str, Any]]:
    """A step that re-arms itself on completion instead of finishing: {"every" (seconds, 60 to 30 days), "max_runs" (1-10000, or
    absent for no limit), "reset_from" ("value:<name>", "session:<ref>" or "skill:<name>": where the next run's fresh input comes
    from, a label only — the value itself is already in the plan's shared state once something writes it), "stop_when" (a condition
    over the plan's values, tadamoo/planner/dag.py's own expression language: true ends the repetition for good)}."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"every", "max_runs", "reset_from", "stop_when"}:
        raise PlanError("repeat is {every, max_runs, reset_from, stop_when}")
    every = value.get("every")
    if isinstance(every, bool) or not isinstance(every, (int, float)) or not MIN_REPEAT_S <= every <= MAX_REPEAT_S:
        raise PlanError(f"repeat.every is seconds, from {MIN_REPEAT_S} to {MAX_REPEAT_S}")
    max_runs = value.get("max_runs")
    if max_runs is not None and (not _whole(max_runs) or not 1 <= max_runs <= MAX_REPEAT_RUNS):
        raise PlanError(f"repeat.max_runs is a whole number from 1 to {MAX_REPEAT_RUNS}, or absent for no limit")
    reset_from = value.get("reset_from")
    if reset_from is not None and (not isinstance(reset_from, str) or not RESET_FROM_RE.match(reset_from)):
        raise PlanError("repeat.reset_from is 'value:<name>', 'session:<ref>' or 'skill:<name>'")
    stop_when = value.get("stop_when")
    if stop_when is not None:
        check_expression(stop_when)
    return {"every": float(every), **({"max_runs": max_runs} if max_runs is not None else {}),
            **({"reset_from": reset_from} if reset_from else {}), **({"stop_when": stop_when} if stop_when else {})}


def check_expression(expression: Any) -> None:
    """Syntax and vocabulary of a condition, without any values: raises PlanError if safe_eval would never accept it."""
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 300:
        raise PlanError("a condition must be a text of 1 to 300 characters")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise PlanError(f"invalid condition '{expression[:60]}'") from None
    for n in ast.walk(tree):
        if (not isinstance(n, _CONDITION_NODES)
                or (isinstance(n, ast.Constant) and not isinstance(n.value, (int, float, str, bool, type(None))))
                or (isinstance(n, ast.BinOp) and type(n.op) not in _ARITH)
                or (isinstance(n, ast.Call) and not (isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords))):
            raise PlanError(f"'{expression[:60]}' uses something a condition may not use")


def clean_conditions(node_type: Any, conditions: Any) -> Dict[str, Any]:
    """A sensor's conditions are `when` (an expression over the event: text, source, at, and the plan's values) and `set`
    (values to write into the plan when it holds; a text that starts with = is an expression). Other keys are left alone."""
    if not isinstance(conditions, dict):
        raise PlanError("conditions is an object")
    if NodeType(node_type) != NodeType.SENSOR:
        return dict(conditions)
    if "when" in conditions:
        check_expression(conditions["when"])
    sets = conditions.get("set", {})
    if not isinstance(sets, dict) or any(not isinstance(k, str) or not k.isidentifier() for k in sets):
        raise PlanError("'set' of a sensor is an object whose keys are variable names")
    for value in sets.values():
        if isinstance(value, str) and value.startswith("="):
            check_expression(value[1:])
    return dict(conditions)


def clean_field(node_type: Any, key: str, value: Any, draft: bool = False) -> Any:
    """A field of a step, checked. `draft`: the step is not set up yet (state_variables.draft), so its spec may be incomplete."""
    if key == "retry_policy":
        return clean_retry_policy(value)
    if key == "position":
        return clean_position(value)
    if key == "wiki_query":
        return clean_wiki_query(value)
    if key == "call_request":
        cleaned = clean_call_request(value)
        if cleaned is not None and NodeType(node_type) != NodeType.ACTION:
            raise PlanError("only an action step can carry a call request")
        return cleaned
    if key == "conditions":
        return clean_conditions(node_type, value)
    if key == "repeat":
        return clean_repeat(value)
    if key == "inputs":
        if value is None:
            return {}
        if not isinstance(value, dict) or len(value) > 20:
            raise PlanError("inputs is an object: name -> {node, port} (at most 20)")
        out = {}
        for name, ref in value.items():
            if not isinstance(name, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", name) or keyword.iskeyword(name):
                raise PlanError(f"'{str(name)[:40]}' is not a name an input can have: letters, digits and _, starting with a letter")
            if not isinstance(ref, dict) or not isinstance(ref.get("node"), str):
                raise PlanError(f"the input '{name}' is {{node, port}}")
            out[name] = {"node": ref["node"], "port": str(ref.get("port") or "result")}
        return out
    if key == "spec":
        from tadamoo.planner import nodes
        return nodes.clean_spec(node_type, value, draft)
    if key == "produces":
        from tadamoo.planner import nodes
        return nodes.clean_produces(value)
    return value


MAX_OUTPUTS, MAX_VALUE_BYTES, MAX_STATE_BYTES, MAX_NAME = 30, 4000, 50_000, 64
OPERAND_KEY = re.compile(r"^[A-Za-z0-9_.-]+\.(left|op|right|right2)$")


def _json_value(value: Any, depth: int = 0) -> bool:
    if value is None or isinstance(value, (bool, str)):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if depth >= 3:
        return False
    if isinstance(value, list):
        return all(_json_value(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _json_value(v, depth + 1) for k, v in value.items())
    return False


def _brief(value: Any, limit: int = 120) -> Any:
    text = json.dumps(value, ensure_ascii=False)
    return value if len(text) <= limit else text[:limit - 1] + "…"


def clean_outputs(outputs: Any) -> Dict[str, Any]:
    """The plan values a step writes when it completes: names a condition can read, values that are plain data of bounded size."""
    if not isinstance(outputs, dict):
        raise PlanError("outputs is an object: variable name -> value")
    if len(outputs) > MAX_OUTPUTS:
        raise PlanError(f"a step writes at most {MAX_OUTPUTS} values")
    for name, value in outputs.items():
        if (not isinstance(name, str) or not name.isidentifier() or len(name) > MAX_NAME or keyword.iskeyword(name)
                or name in SENSOR_VARIABLES):
            raise PlanError(f"'{str(name)[:40]}' is not a name a condition can read: letters, digits and _, not one of {', '.join(SENSOR_VARIABLES)}")
        if not _json_value(value) or len(json.dumps(value, ensure_ascii=False)) > MAX_VALUE_BYTES:
            raise PlanError(f"the value of '{name}' must be plain data (text, number, true/false, list, object) of at most {MAX_VALUE_BYTES} characters")
    return dict(outputs)


def edge_parts(edge: Any) -> tuple:
    """(parent, child, port) of an edge as a diff gives it: [parent, child], [parent, child, port], or {"from": {"node", "port"}, "to": {"node"}}."""
    if isinstance(edge, dict):
        src, dst = edge.get("from"), edge.get("to")
        if not isinstance(src, dict) or not isinstance(dst, dict) or not isinstance(src.get("node"), str) or not isinstance(dst.get("node"), str):
            raise PlanError("an edge is {from: {node, port}, to: {node}}")
        return src["node"], dst["node"], src.get("port")
    if isinstance(edge, (list, tuple)) and len(edge) in (2, 3):
        return edge[0], edge[1], edge[2] if len(edge) == 3 else None
    raise PlanError("an edge is [parent, child], [parent, child, port] or {from: {node, port}, to: {node}}")


def migrate_decision(node: PlanNode) -> None:
    """A decision stored with a condition for each exit (before branches) becomes branches: one for each condition, named by the exit, every branch that holds
    taken (`all`). One that has no condition to make branches from is left for the person to set up. Run when a plan is loaded or a node is put on it."""
    if node.type != NodeType.DECISION or node.spec.get("branches"):
        return
    legacy = {k: v for k, v in node.conditions.items() if isinstance(k, str) and isinstance(v, str)}
    if not legacy:
        if not node.spec and node.outputs:
            node.state_variables["draft"] = True
        return
    node.spec = {"mode": "all", "on_no_match": "ask", "branches": [{"id": key, "label": key, "condition": expr} for key, expr in legacy.items()]}
    node.conditions = {}
    for key in legacy:
        if key in node.outputs:
            node.routes.setdefault(key, [key])


def _ports(node: PlanNode) -> Dict[str, Dict[str, Any]]:
    from tadamoo.planner import nodes
    return {p["id"]: p for p in nodes.out_ports(node.type.value, node.spec)}


@dataclass
class PlanDAG:
    plan_id: str
    title: str
    target_type: str  # "constant" or "occasional"
    insight_card: Dict[str, Any] = field(default_factory=dict)
    nodes: Dict[str, PlanNode] = field(default_factory=dict)
    shared_state: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    order: List[str] = field(default_factory=list)       # the list the person arranged; list_order() keeps it valid
    confirmations: bool = True          # false: the person chose that nothing about this plan is asked (except what involves other people), and its text is trusted
    journal: List[Dict[str, Any]] = field(default_factory=list)      # what the plan did by itself and what its log and result steps wrote, newest last
    note_count: int = field(default=0, repr=False, compare=False)    # lines ever written in this run of the program: how many are new since a look

    def _touch(self) -> None:
        self.updated_at = _now()

    def note(self, node_id: str, kind: str, text: str, now: Optional[float] = None) -> None:
        """A line in the plan's journal: {"t", "node", "kind" (auto, log or output), "text"}; only the latest few hundred are kept."""
        from tadamoo.planner import nodes
        self.journal.append({"t": time.time() if now is None else now, "node": node_id, "kind": kind, "text": " ".join(str(text).split())[:500]})
        del self.journal[:-nodes.JOURNAL_MAX]
        self.note_count += 1

    def advance(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Resolves the steps the plan works out by itself (waits, values, log and result lines); see tadamoo/planner/nodes.py."""
        from tadamoo.planner import nodes
        return nodes.advance(self, time.time() if now is None else now)

    # structure ---------------------------------------------------------------------------------------

    def add_node(self, node: PlanNode, replace: bool = False) -> None:
        """Adds a node and the edges it declares; the parents and children it names must already exist.

        With `replace`, a node that exists is swapped for this one and its edges become exactly the declared ones."""
        if node.id in self.nodes and not replace:
            raise PlanError(f"node '{node.id}' already exists")
        if not node.id or not str(node.id).strip():
            raise PlanError("a node needs an id")
        migrate_decision(node)
        if node.type == NodeType.DECISION and node.spec.get("branches"):
            from tadamoo.planner import nodes
            node.spec = nodes.clean_spec("decision", node.spec, bool(node.state_variables.get("draft")))
        if node.type == NodeType.START:                                  # a start added with a number that is taken gets the next free one
            taken = {n.spec.get("number", 1) for n in self.start_nodes() if n.id != node.id}
            wanted = node.spec.get("number", 1)
            if wanted in taken:
                node.spec = {**node.spec, "number": next(i for i in range(1, 100 + len(taken)) if i not in taken)}
        declared_parents, declared_children = list(node.dependencies), list(node.outputs)
        routed = {port: list(kids) for port, kids in node.routes.items()}
        for ref in declared_parents + declared_children + [k for kids in routed.values() for k in kids]:
            if ref not in self.nodes or ref == node.id:
                raise PlanError(f"node '{node.id}' refers to unknown node '{ref}'")
        backup = copy.deepcopy((self.nodes, self.shared_state))
        try:
            if node.id in self.nodes:
                self._detach(node.id)
            node.dependencies, node.outputs, node.routes = [], [], {}
            self.nodes[node.id] = node
            for p in dict.fromkeys(declared_parents):
                self.add_edge(p, node.id)
            for port, kids in routed.items():
                for c in dict.fromkeys(kids):
                    self.add_edge(node.id, c, port)
            for c in dict.fromkeys(k for k in declared_children if not any(k in kids for kids in routed.values())):
                self.add_edge(node.id, c)
            self.validate_acyclic()
        except PlanError:
            self.nodes, self.shared_state = backup
            raise
        self._touch()

    def _detach(self, node_id: str) -> None:
        """Takes a node's edges out of both lists on both sides, leaving the node itself in place."""
        node = self.nodes[node_id]
        for p in node.dependencies:
            if p in self.nodes and node_id in self.nodes[p].outputs:
                self.nodes[p].outputs.remove(node_id)
        for c in node.outputs:
            if c in self.nodes and node_id in self.nodes[c].dependencies:
                self.nodes[c].dependencies.remove(node_id)
        for other in self.nodes.values():                        # an input edge from or to this step goes too
            other.inputs = {k: r for k, r in other.inputs.items() if r.get("node") != node_id}
            other.routes = {port: [k for k in kids if k != node_id] for port, kids in other.routes.items() if other.id != node_id}
            other.routes = {port: kids for port, kids in other.routes.items() if kids}
        node.dependencies, node.outputs, node.routes = [], [], {}

    def _link(self, parent_id: str, child_id: str) -> None:
        if child_id not in self.nodes[parent_id].outputs:
            self.nodes[parent_id].outputs.append(child_id)
        if parent_id not in self.nodes[child_id].dependencies:
            self.nodes[child_id].dependencies.append(parent_id)

    def _port_of(self, parent: PlanNode, child: PlanNode, port: Optional[str]) -> Dict[str, str]:
        """The output port of `parent` an edge to `child` leaves from: the one named, or the only one it has; a decision's exit that is named after the step
        it goes to (a decision stored with a condition for each exit) is that exit's branch."""
        ports = _ports(parent)
        if port is None:
            flow = [p for p in ports.values() if p["kind"] == "flow"]
            if parent.type == NodeType.DECISION and child.id in ports:
                return ports[child.id]
            if len(flow) == 1 or (not flow and len(ports) == 1):
                return flow[0] if flow else next(iter(ports.values()))
            raise PlanError(f"'{parent.id}' has several output connectors: say which one the edge leaves from ({', '.join(ports) or 'none yet'})")
        if port not in ports:
            raise PlanError(f"'{parent.id}' has no output connector '{port}' ({', '.join(ports) or 'none'})")
        return ports[port]

    def add_edge(self, parent_id: str, child_id: str, port: Optional[str] = None) -> None:
        """Connects an output port of `parent_id` to `child_id`. A `flow` port makes the child wait for the parent. A `data` port that carries archives (a RAG node)
        or a value (a step's result, a sensor's reading, a variable's value, an answer) gives the child that data and nobody waits for it: a value is kept in the
        child's `inputs` under a name, and the child does not run before it has one."""
        from tadamoo.planner import nodes
        for nid in (parent_id, child_id):
            if nid not in self.nodes:
                raise KeyError(f"Node '{nid}' not found in DAG")
        if parent_id == child_id:
            raise PlanError("a node cannot depend on itself")
        parent, child = self.nodes[parent_id], self.nodes[child_id]
        out = self._port_of(parent, child, port)
        accepts = [p for p in nodes.in_ports(child.type.value) if p["kind"] == out["kind"] and (out["kind"] == "flow" or out.get("carries") in p.get("accepts", []))]
        if not accepts:
            what = {"archives": "archives", "value": "a value", "reading": "a value"}.get(out.get("carries"), "this")
            raise PlanError(f"'{child_id}' ({child.type.value}) cannot take {what} from '{parent_id}'" + (": a RAG node feeds actions and decisions" if out.get("carries") == "archives" else ""))
        backup = copy.deepcopy(self.nodes)
        try:
            if out["kind"] == "flow":
                self._link(parent_id, child_id)
            if out["kind"] == "data" and out.get("carries") != "archives":
                if self._depends(parent_id, child_id):
                    raise PlanError(f"'{parent_id}' depends on '{child_id}': a value cannot come back round")
                if not any(r.get("node") == parent_id and r.get("port") == out["id"] for r in child.inputs.values()):
                    child.inputs[self._input_name(child, parent)] = {"node": parent_id, "port": out["id"]}
            elif parent.type in (NodeType.DECISION, NodeType.RAG):
                kids = parent.routes.setdefault(out["id"], [])
                if child_id not in kids:
                    kids.append(child_id)
            self.validate_acyclic()
        except PlanError:
            self.nodes = backup
            raise
        if out["kind"] == "data":
            self.refresh_blocking()
        self._touch()

    def _input_name(self, child: PlanNode, parent: PlanNode) -> str:
        """The name a step refers to a wired value by: the title of the step it comes from as an identifier, unique among the step's inputs."""
        base = re.sub(r"[^A-Za-z0-9_]+", "_", parent.title.strip()).strip("_") or parent.id
        base = (base if re.match(r"[A-Za-z_]", base) else "v_" + base)[:40]
        name, n = base, 1
        while name in child.inputs:
            n += 1
            name = f"{base}_{n}"
        return name

    def _depends(self, a: str, b: str) -> bool:
        """Whether step `a` waits for step `b`, through the steps it waits for and the values it takes."""
        seen, stack = set(), [a]
        while stack:
            cur = stack.pop()
            if cur == b:
                return True
            if cur in seen or cur not in self.nodes:
                continue
            seen.add(cur)
            node = self.nodes[cur]
            stack.extend(node.dependencies)
            stack.extend(r["node"] for r in node.inputs.values() if isinstance(r, dict) and "node" in r)
        return False

    def remove_edge(self, parent_id: str, child_id: str, port: Optional[str] = None) -> None:
        child_node = self.nodes.get(child_id)
        if child_node is not None:                                       # a value wired from the parent
            for name in [n for n, r in child_node.inputs.items() if r.get("node") == parent_id and (not port or r.get("port") == port)]:
                del child_node.inputs[name]
        parent = self.nodes.get(parent_id)
        if parent is not None:
            for name in [port] if port else list(parent.routes):
                kids = parent.routes.get(name) or []
                if child_id in kids:
                    kids.remove(child_id)
                if not kids:
                    parent.routes.pop(name, None)
        still = parent is not None and any(child_id in kids for name, kids in parent.routes.items() if _ports(parent).get(name, {}).get("kind") == "flow")
        if not still:
            if parent is not None and child_id in parent.outputs:
                parent.outputs.remove(child_id)
            if child_id in self.nodes and parent_id in self.nodes[child_id].dependencies:
                self.nodes[child_id].dependencies.remove(parent_id)
        self._touch()

    def edges(self) -> List[Dict[str, Any]]:
        """Every edge as {"from": {"node", "port"}, "to": {"node", "port"}, "kind": "flow" | "data", "carries"?}, for the page and the tools. A value edge names
        the input it makes (`to.name`)."""
        out: List[Dict[str, Any]] = []
        for nid, n in self.nodes.items():
            ports = _ports(n)
            for port, kids in n.routes.items():
                for c in kids:
                    kind = ports.get(port, {}).get("kind", "flow")
                    out.append({"from": {"node": nid, "port": port}, "to": {"node": c, "port": "in" if kind == "flow" else "rag"}, "kind": kind,
                                **({"carries": "archives"} if kind == "data" else {})})
            default = next((p["id"] for p in ports.values() if p["kind"] == "flow"), "out")
            out.extend({"from": {"node": nid, "port": default}, "to": {"node": c, "port": "in"}, "kind": "flow"} for c in n.outputs if not any(c in kids for kids in n.routes.values()))
            out.extend({"from": {"node": ref["node"], "port": ref["port"]}, "to": {"node": nid, "port": "data", "name": name}, "kind": "data", "carries": "value"}
                       for name, ref in n.inputs.items() if isinstance(ref, dict) and "node" in ref)
        return out

    def remove_node(self, node_id: str) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            raise KeyError(f"Node '{node_id}' not found")
        for p in list(node.dependencies):
            self.remove_edge(p, node_id)
        for c in list(node.outputs):
            self.remove_edge(node_id, c)
        for other in self.nodes.values():                        # input edges are not in the dependency lists
            other.inputs = {k: r for k, r in other.inputs.items() if r.get("node") != node_id}
            for port in list(other.routes):
                other.routes[port] = [k for k in other.routes[port] if k != node_id]
                if not other.routes[port]:
                    del other.routes[port]
        del self.nodes[node_id]
        self._touch()

    def validate_acyclic(self) -> bool:
        """Checks that both edge lists agree, name only existing nodes, and form no cycle."""
        edges: Set[tuple] = set()
        for nid, n in self.nodes.items():
            for c in n.outputs:
                if c not in self.nodes:
                    raise PlanError(f"node '{nid}' has an output to unknown node '{c}'")
                if nid not in self.nodes[c].dependencies:
                    raise PlanError(f"edge {nid} -> {c} is listed in outputs but not in the dependencies of '{c}'")
                edges.add((nid, c))
            for p in n.dependencies:
                if p not in self.nodes:
                    raise PlanError(f"node '{nid}' depends on unknown node '{p}'")
                if nid not in self.nodes[p].outputs:
                    raise PlanError(f"edge {p} -> {nid} is listed in dependencies but not in the outputs of '{p}'")
                edges.add((p, nid))
            if len(set(n.outputs)) != len(n.outputs) or len(set(n.dependencies)) != len(n.dependencies):
                raise PlanError(f"node '{nid}' lists an edge twice")
            ports = _ports(n)
            for port, kids in n.routes.items():
                if port not in ports:
                    raise PlanError(f"node '{nid}' has an edge from a connector that does not exist: '{port}'")
                for c in kids:
                    if c not in self.nodes:
                        raise PlanError(f"node '{nid}' is connected to unknown node '{c}'")
                    if ports[port]["kind"] == "flow" and c not in n.outputs:
                        raise PlanError(f"edge {nid}.{port} -> {c} is not in the outputs of '{nid}'")
                    if ports[port]["kind"] == "data" and c in n.outputs:
                        raise PlanError(f"a data edge {nid} -> {c} must not be a dependency")
            for name, ref in n.inputs.items():
                if not isinstance(ref, dict) or ref.get("node") not in self.nodes:
                    raise PlanError(f"node '{nid}' takes its input '{name}' from a step that does not exist")
                if ref.get("port") not in _ports(self.nodes[ref["node"]]):
                    raise PlanError(f"node '{nid}' takes its input '{name}' from a connector that does not exist")
            if nid in n.outputs:
                raise PlanError(f"node '{nid}' depends on itself")
        in_degree = {nid: 0 for nid in self.nodes}
        for _, c in edges:
            in_degree[c] += 1
        queue = [nid for nid, d in in_degree.items() if d == 0]
        visited = 0
        while queue:
            cur = queue.pop(0)
            visited += 1
            for c in self.nodes[cur].outputs:
                in_degree[c] -= 1
                if in_degree[c] == 0:
                    queue.append(c)
        if visited != len(self.nodes):
            raise PlanError("Cycle detected in PlanDAG; workflow must be strictly acyclic")
        return True

    def _decision_problems(self, node: PlanNode) -> List[str]:
        nid, problems = node.id, []
        if node.state_variables.get("draft"):
            return problems                                 # a step put on the plan and not set up yet is checked when it is
        from tadamoo.planner import nodes
        try:
            nodes.clean_spec("decision", node.spec)
        except PlanError as exc:
            problems.append(f"decision '{nid}': {exc}")
        return problems

    def validate_decisions(self) -> List[str]:
        """Problems with decision nodes: no branch, a condition that is not an expression, labels used twice."""
        return [p for n in self.nodes.values() if n.type == NodeType.DECISION for p in self._decision_problems(n)]

    def _decision_signature(self) -> Dict[str, Any]:
        return {nid: (tuple(n.outputs), repr(n.spec), repr(sorted(n.routes.items())))
                for nid, n in self.nodes.items() if n.type == NodeType.DECISION}

    def enforce_decisions(self, before: Optional[Dict[str, Any]] = None) -> None:
        """Raises PlanError when a decision is not valid. With `before` (a _decision_signature), only the decisions that are new
        or whose branches or exits changed since are checked, so a change elsewhere is not held up by an old plan's decision."""
        problems, now = [], self._decision_signature()
        for nid, sig in now.items():
            if before is None or before.get(nid) != sig:
                problems += self._decision_problems(self.nodes[nid])
        if problems:
            raise PlanError("; ".join(problems))

    def topological_order(self) -> List[str]:
        self.validate_acyclic()
        in_degree = {nid: len(n.dependencies) for nid, n in self.nodes.items()}
        queue = [nid for nid, d in in_degree.items() if d == 0]
        order: List[str] = []
        while queue:
            cur = queue.pop(0)
            order.append(cur)
            for c in self.nodes[cur].outputs:
                in_degree[c] -= 1
                if in_degree[c] == 0:
                    queue.append(c)
        return order

    def layers(self) -> List[List[str]]:
        """Nodes grouped by depth (longest path from a root), for drawing."""
        depth: Dict[str, int] = {}
        for nid in self.topological_order():
            depth[nid] = 1 + max((depth[p] for p in self.nodes[nid].dependencies), default=-1)
        out: List[List[str]] = [[] for _ in range(max(depth.values(), default=-1) + 1)]
        for nid, d in depth.items():
            out[d].append(nid)
        return out

    def list_order(self) -> List[str]:
        """The steps as a list: the order the person gave them, adjusted so no step comes before one it waits for."""
        rank = {nid: i for i, nid in enumerate(i for i in self.order if i in self.nodes)}
        for nid in (n for layer in self.layers() for n in layer):
            rank.setdefault(nid, len(rank))
        in_degree = {nid: len(n.dependencies) for nid, n in self.nodes.items()}
        ready = [(rank[nid], nid) for nid, d in in_degree.items() if d == 0]
        heapq.heapify(ready)
        out: List[str] = []
        while ready:
            _, cur = heapq.heappop(ready)
            out.append(cur)
            for c in self.nodes[cur].outputs:
                in_degree[c] -= 1
                if in_degree[c] == 0:
                    heapq.heappush(ready, (rank[c], c))
        return out

    def move_node(self, node_id: str, direction: int) -> None:
        """Moves a step one place up (-1) or down (1) in the list. Neighbours that do not wait for each other change places. A step that
        waits for its neighbour changes places only in a plain chain, and then the links are redrawn so the work follows the list."""
        if node_id not in self.nodes:
            raise KeyError(f"Node '{node_id}' not found")
        if direction not in (-1, 1) or isinstance(direction, bool):
            raise PlanError("direction is -1 (up) or 1 (down)")
        order = self.list_order()
        i = order.index(node_id)
        j = i + direction
        if self.nodes[node_id].type == NodeType.START or not 0 <= j < len(order) or (direction < 0 and self.nodes[order[j]].type == NodeType.START):
            raise PlanError("the step is already at the " + ("top" if direction < 0 or self.nodes[node_id].type == NodeType.START else "bottom") + " of the list")
        first, second = self.nodes[order[min(i, j)]], self.nodes[order[max(i, j)]]
        if second.id in first.outputs:
            self._swap_chain(first, second)
        order[i], order[j] = order[j], order[i]
        self.order = order
        self._touch()

    def _swap_chain(self, a: PlanNode, b: PlanNode) -> None:
        """a -> b becomes b -> a in a plain chain: before -> a -> b -> after turns into before -> b -> a -> after."""
        plain = (NodeType.ACTION,)
        if a.outputs != [b.id] or b.dependencies != [a.id] or a.type not in plain or b.type not in plain:
            raise PlanError("these two steps are linked to others or are not plain actions; change what the step waits for instead")
        if a.status not in ("pending", "blocked") or b.status not in ("pending", "blocked"):
            raise PlanError("a step that has started or finished cannot change places")
        before, after = list(a.dependencies), list(b.outputs)
        backup = copy.deepcopy(self.nodes)
        try:
            for p in before:
                self.remove_edge(p, a.id)
            self.remove_edge(a.id, b.id)
            for x in after:
                self.remove_edge(b.id, x)
            for p in before:
                self._link(p, b.id)
            self._link(b.id, a.id)
            for x in after:
                self._link(a.id, x)
            self.validate_acyclic()
        except PlanError:
            self.nodes = backup
            raise
        self.refresh_blocking()

    # editing -----------------------------------------------------------------------------------------

    def update_node(self, node_id: str, **changes: Any) -> PlanNode:
        """Edits text, assignee, conditions or variables of a node that has not been completed."""
        return self._update_node(node_id, changes, True)

    def _update_node(self, node_id: str, changes: Dict[str, Any], enforce: bool) -> PlanNode:
        """`enforce` checks a decision after its conditions change; a diff turns it off and checks the whole result at its end."""
        node = self.nodes.get(node_id)
        if node is None:
            raise KeyError(f"Node '{node_id}' not found")
        unknown = [k for k in changes if k not in EDITABLE]
        if unknown:
            raise PlanError(f"cannot edit {', '.join(unknown)}; editable: {', '.join(EDITABLE)}")
        if node.status == "completed" and node.type != NodeType.RAG and any(k != "position" for k in changes):      # a RAG node completes by itself at once and stays editable
            raise PlanError(f"'{node_id}' is already completed; rewind it to change it")
        if "assignee" in changes:
            changes["assignee"] = Assignee(changes["assignee"])
        draft = bool((changes["state_variables"] if isinstance(changes.get("state_variables"), dict) else node.state_variables).get("draft"))
        changes = {k: clean_field(node.type, k, v, draft) for k, v in changes.items()}      # all checked before any is applied
        if node.type == NodeType.START and "spec" in changes:
            mine = changes["spec"].get("number", 1)
            if any(o.spec.get("number", 1) == mine for o in self.start_nodes() if o.id != node.id):
                raise PlanError(f"another start of this plan is already number {mine}: every start has its own number")
        for key, value in changes.items():
            setattr(node, key, value)
        if "spec" in changes and node.type == NodeType.DECISION:
            self._follow_branches(node)
        self.refresh_blocking()
        self._touch()
        return node

    def _follow_branches(self, node: PlanNode) -> None:
        """A decision whose branches changed: an exit whose branch is gone is disconnected, so no step is left waiting for a branch that does not exist."""
        ports = _ports(node)
        for port in [p for p in node.routes if p not in ports]:
            for c in list(node.routes[port]):
                self.remove_edge(node.id, c, port)

    def reassign_to_user(self, node_id: str, reason: str) -> None:
        """Human fallback: the user takes a task the software cannot do; what depends on it waits."""
        node = self.nodes.get(node_id)
        if node is None:
            raise KeyError(f"Node '{node_id}' not found")
        node.assignee = Assignee.USER
        node.state_variables["fallback_reason"] = reason
        self.refresh_blocking()
        self._touch()

    # execution state ---------------------------------------------------------------------------------

    def input_values(self, node: PlanNode) -> Dict[str, Any]:
        """The values of the node's wired inputs that exist now, by name. A step's result is its one value, or all of them when it wrote several; a sensor gives
        the text it matched, a variable step its value, an input step the answer."""
        out: Dict[str, Any] = {}
        for name, ref in node.inputs.items():
            src = self.nodes.get(ref.get("node")) if isinstance(ref, dict) else None
            if src is None or src.status != "completed":
                continue
            sv, kind = src.state_variables, src.type
            if kind == NodeType.SENSOR:
                found = (sv.get("matched") or {}).get("text")
            elif kind in (NodeType.GET_VAR, NodeType.SET_VAR):
                found = sv.get("value")
            else:
                wrote = sv.get("outputs") or {}
                found = next(iter(wrote.values())) if len(wrote) == 1 else (dict(wrote) if wrote else None)
            if found is not None:
                out[name] = found
        return out

    def waiting_inputs(self, node: PlanNode) -> List[str]:
        """The names of the node's inputs that have no value yet: what the step waits for."""
        have = self.input_values(node)
        return [name for name in node.inputs if name not in have]

    def _blockers(self, node: PlanNode) -> List[str]:
        """Unfinished parents that the user must do, which keep this node from starting."""
        return [p for p in node.dependencies
                if self.nodes[p].assignee == Assignee.USER and self.nodes[p].status not in DONE]

    def _halted_parents(self, node: PlanNode) -> List[str]:
        """Parents that stopped for good (failed, timed out, or themselves parked): what is below them waits, the rest of the plan goes on."""
        out = []
        for p in node.dependencies:
            parent = self.nodes[p]
            if parent.status == "failed" or (parent.status not in DONE and parent.state_variables.get("timed_out")) or parent.state_variables.get("parked_by"):
                out.append(p)
        return out

    def refresh_blocking(self) -> None:
        """Marks nodes blocked while a user task above them is open, and releases them when it is done. A step below a failed or
        timed-out one is parked with the reason in `parked_by` until that step is dealt with; independent branches are not touched."""
        for n in self.nodes.values():                           # a step whose wired values are not all there yet says which it waits for
            missing = self.waiting_inputs(n) if n.status == "pending" else []
            if missing:
                n.state_variables["waiting_inputs"] = missing
            else:
                n.state_variables.pop("waiting_inputs", None)
        changed = True
        while changed:                                          # a parked step parks what is below it, until nothing moves
            changed = False
            for node in self.nodes.values():
                if node.status not in ("pending", "blocked") or node.state_variables.get("timed_out"):
                    continue
                blockers, halted = self._blockers(node), self._halted_parents(node)
                if blockers or halted:
                    if node.status != "blocked" or node.state_variables.get("blocked_by") != (blockers or None) or node.state_variables.get("parked_by") != (halted or None):
                        node.status = "blocked"
                        changed = True
                    for key, value in (("blocked_by", blockers), ("parked_by", halted)):
                        if value:
                            node.state_variables[key] = value
                        else:
                            node.state_variables.pop(key, None)
                elif node.status == "blocked":
                    node.status = "pending"
                    for key in ("blocked_by", "parked_by"):
                        node.state_variables.pop(key, None)
                    changed = True
                elif "parked_by" in node.state_variables:
                    node.state_variables.pop("parked_by")

    def ready_nodes(self, now: Optional[float] = None) -> List[str]:
        """Pending nodes whose parents are all finished (a merge needs at least one completed parent), except a
        step that failed and is waiting to try again and the kinds the plan resolves itself (see `advance`)."""
        from tadamoo.planner import nodes
        out = []
        now = time.time() if now is None else now
        for nid in self.topological_order():
            n = self.nodes[nid]
            if (n.status != "pending" or n.state_variables.get("draft") or n.type.value in nodes.AUTO or self._blockers(n) or self.waiting_inputs(n) or n.state_variables.get("retry_at", 0) > now
                    or n.state_variables.get("repeat_at", 0) > now):
                continue
            parents = [self.nodes[p].status for p in n.dependencies]
            if all(s in DONE for s in parents) and (not parents or "completed" in parents):
                if n.type == NodeType.MERGE and not self._merge_inputs_present(n):
                    continue
                out.append(nid)
        return out

    def _merge_inputs_present(self, node: PlanNode) -> bool:
        return all(v in self.shared_state for v in node.conditions.get("requires", []))

    def _failed(self, node: PlanNode, now: float) -> Optional[Dict[str, Any]]:
        """A step failed. With a retry policy it goes back to pending, to be ready again after the wait, until the tries
        are used up; then it is failed. Returns the retry that was scheduled, if any."""
        attempts = int(node.state_variables.get("attempts", 0)) + 1
        node.state_variables["attempts"] = attempts
        policy = node.retry_policy or DEFAULT_RETRY
        if attempts >= policy["max_attempts"]:
            node.state_variables.pop("retry_at", None)
            node.status = "failed"
            return None
        wait = policy["wait_seconds"] * policy["backoff"] ** (attempts - 1)
        node.state_variables["retry_at"] = now + wait
        node.status = "pending"
        return {"attempt": attempts, "of": policy["max_attempts"], "wait_seconds": wait, "retry_at": now + wait}

    def _rearm(self, node: PlanNode, now: float) -> Optional[Dict[str, Any]]:
        """After a repeating step completes: ends it for good (its `stop_when` holds, or it used up its `max_runs`), leaving it
        `completed` like any other step, or sends it back to `pending` for another run once `every` seconds have passed."""
        from tadamoo.planner import nodes
        repeat = nodes.repeat_of(node) or {}
        runs = int(node.state_variables.get("runs", 0)) + 1
        node.state_variables["runs"] = runs
        stop = False
        if repeat.get("stop_when"):
            try:
                stop = bool(safe_eval(repeat["stop_when"], self.shared_state))
            except PlanError:
                stop = False                              # a value the condition needs is not there yet: not held, it repeats again
        if stop or (repeat.get("max_runs") and runs >= repeat["max_runs"]):
            node.state_variables.pop("repeat_at", None)
            return None
        node.state_variables["repeat_at"] = now + repeat["every"]
        node.status = "pending"
        return {"repeated": True, "run": runs, "repeat_at": node.state_variables["repeat_at"]}

    def mark(self, node_id: str, status: str, outputs: Optional[Dict[str, Any]] = None, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Sets a node's status. Completing it writes its outputs into the shared state and, for a
        decision, routes: the exits whose condition does not hold are skipped. Failing a step that has a
        retry policy schedules another try instead (the returned {"attempt", "of", "retry_at"}) until the tries are used up."""
        node = self.nodes.get(node_id)
        if node is None:
            raise KeyError(f"Node '{node_id}' not found")
        if status not in STATUSES:
            raise PlanError(f"status must be one of {STATUSES}")
        retry = None
        if outputs is not None and status != "completed":
            raise PlanError("a step writes plan values when it is completed")
        if status == "completed":
            backup = copy.deepcopy((self.nodes, self.shared_state))
            kept_operands: Dict[str, Any] = {}
            try:
                if node.type == NodeType.DECISION:
                    problems = self._decision_problems(node)
                    if problems:
                        raise PlanError("; ".join(problems))
                    if outputs:                                          # the values the agent worked out for the terms that are its to work out: "<branch>.<slot>" -> value
                        from tadamoo.planner import operands
                        outputs = dict(outputs)
                        for key in [k for k in outputs if OPERAND_KEY.match(k)]:
                            bid, slot = key.rsplit(".", 1)
                            branch = next((b for b in node.spec.get("branches") or [] if b["id"] == bid), None)
                            if branch is None or (branch.get(slot) or {}).get("kind") != "agent":
                                raise PlanError(f"'{key}' is not a term of this decision that the agent works out")
                            node.state_variables.setdefault("operands", {})[key] = operands.typed(outputs.pop(key), slot)
                        outputs = outputs or None
                    kept_operands = dict(node.state_variables.get("operands") or {})
                if node.type == NodeType.INPUT and node.spec.get("ask", "value") == "value" and (node.spec["into"] not in (outputs or {}) and node.spec["into"] not in self.shared_state):
                    raise PlanError(f"this step asks the person for '{node.spec['into']}': complete it with outputs {{{node.spec['into']}: their answer}}")
                if node.produces:
                    from tadamoo.planner import nodes
                    nodes.check_produced(node.produces, outputs)
                if outputs:
                    outputs = clean_outputs(outputs)
                    merged = {**self.shared_state, **outputs}
                    if len(json.dumps(merged, ensure_ascii=False)) > MAX_STATE_BYTES:
                        raise PlanError(f"the plan's values may not be larger than {MAX_STATE_BYTES} characters in all")
                    self.shared_state.update(outputs)
                    node.state_variables["outputs"] = dict(outputs)
                node.status = "completed"
                for key in ("attempts", "retry_at", "sensor_error", "timed_out"):
                    node.state_variables.pop(key, None)
                if node.type == NodeType.DECISION:
                    node.state_variables.pop("no_route", None)
                    node.state_variables.pop("no_branch", None)
                    self._route(node)
                from tadamoo.planner import nodes
                if nodes.repeat_of(node):
                    retry = self._rearm(node, time.time() if now is None else now)
            except NeedValue as wait:
                self.nodes, self.shared_state = backup           # not taken yet: it waits for a value, and what the agent already reported is kept
                node = self.nodes[node_id]
                node.state_variables["waiting_for"] = wait.facts
                if kept_operands:
                    node.state_variables["operands"] = kept_operands
                node.status = "pending"
                self.refresh_blocking()
                self._touch()
                return {"waiting": wait.facts, "message": str(wait)}
            except NoRoute as stop:
                self.nodes, self.shared_state = backup           # the decision is not taken: it goes to the person with the facts
                node = self.nodes[node_id]
                node.state_variables["no_route"] = stop.facts
                node.state_variables["no_route"]["was"] = node.assignee.value
                node.assignee = Assignee.USER
                node.status = "pending"
                self.refresh_blocking()
                self._touch()
                return {"no_route": stop.facts, "message": str(stop)}
            except PlanError:
                self.nodes, self.shared_state = backup
                raise
        elif status == "failed":
            retry = self._failed(node, time.time() if now is None else now)
        else:
            node.status = status
            node.state_variables.pop("retry_at", None)
            node.state_variables.pop("timed_out", None)
            if status == "skipped":
                node.state_variables.pop("attempts", None)
        self.refresh_blocking()
        self._touch()
        return retry

    def timed_out(self, node_id: str, now: Optional[float] = None) -> None:
        """A step ran out of time. It stays as it is (blocked, with `timed_out`) until the person or the planner marks it again;
        the steps below it are parked, the independent branches go on."""
        node = self.nodes.get(node_id)
        if node is None:
            raise KeyError(f"Node '{node_id}' not found")
        if node.status in DONE:
            return
        node.status = "blocked"
        node.state_variables["timed_out"] = {"at": time.time() if now is None else now}
        self.refresh_blocking()
        self._touch()

    def _route(self, node: PlanNode) -> None:
        """Takes the branches whose comparison holds and skips the steps only the others lead to. In mode `first` the first branch that holds is the one, in the
        order shown; in mode `all` every branch that holds. The `else` branch is taken when no other is. A term that is for the agent to work out, or an input
        with no value yet, makes the decision wait (NeedValue). When no branch holds the path stops (`on_no_match` stop) or the decision goes to the person
        (ask), and an operator that is not a known one or terms that cannot be compared stop it with the reason: NoRoute says what was read."""
        from tadamoo.planner import operands
        read: List[Dict[str, Any]] = []
        used: Set[str] = set()
        chosen: List[str] = []
        waits: List[Dict[str, Any]] = []
        first = node.spec.get("mode", "first") == "first"
        inputs, reported = self.input_values(node), node.state_variables.get("operands") or {}
        env = {**self.shared_state, **inputs}
        for b in node.spec.get("branches") or []:
            if b.get("op") == "else":
                continue
            if first and chosen:
                break
            if "condition" in b:
                expr = b["condition"]
                used.update(n.id for n in ast.walk(ast.parse(expr.strip(), mode="eval")) if isinstance(n, ast.Name) and n.id not in _FUNCS)
                try:
                    holds = bool(safe_eval(expr, env))
                    read.append({"to": b["id"], "title": b["label"], "condition": expr, "holds": holds})
                    if holds:
                        chosen.append(b["id"])
                except PlanError as exc:
                    read.append({"to": b["id"], "title": b["label"], "condition": expr, "holds": None, "error": str(exc)[:200]})
                continue
            got = operands.evaluate(b, self.shared_state, inputs, {k.split(".", 1)[1]: v for k, v in reported.items() if k.startswith(b["id"] + ".")}, safe_eval)
            for slot in operands.SLOTS:
                if (b.get(slot) or {}).get("kind") == "python":
                    used.update(n.id for n in ast.walk(ast.parse(b[slot]["expr"].strip(), mode="eval")) if isinstance(n, ast.Name) and n.id not in _FUNCS)
            waits.extend({**w, "branch": b["id"], "label": b["label"]} for w in got.waits)
            text = " ".join(str(got.compared.get(k, "?")) for k in ("left", "op", "right") if got.compared) or b["label"]
            entry = {"to": b["id"], "title": b["label"], "condition": text, "compared": got.compared}
            read.append({**entry, "holds": got.holds, **({"error": got.error} if got.error else {}), **({"waiting": True} if got.waits else {})})
            if got.holds:
                chosen.append(b["id"])
        if waits:
            node.state_variables.pop("compared", None)
            facts = {"waiting": waits, "inputs": {k: _brief(v) for k, v in inputs.items()}, "missing_inputs": self.waiting_inputs(node), "at": _now()}
            raise NeedValue("the decision '" + node.id + "' waits for " + "; ".join(
                (f"the {w['slot']} of '{w['label']}' (to be worked out: {w['describe'][:120]})" if w["kind"] == "agent" else f"the input '{w['name']}'") for w in waits), facts)
        other = next((b for b in node.spec.get("branches") or [] if b.get("op") == "else"), None)
        if not chosen and other is not None and not any(r["holds"] is None for r in read):
            chosen.append(other["id"])
        unreadable = any(r["holds"] is None for r in read)
        node.state_variables["compared"] = {r["to"]: {**r["compared"], "holds": r["holds"]} for r in read if r.get("compared")}
        if not chosen and not unreadable and node.spec.get("on_no_match", "stop") == "stop":
            values = {v: _brief(self.shared_state[v]) if v in self.shared_state else None for v in sorted(used - {"True", "False", "None"})}
            node.state_variables["no_branch"] = {"exits": read, "values": values, "at": _now()}      # no branch holds: this path stops here, the rest of the plan goes on
            node.state_variables["selected"], node.state_variables["selected_branches"] = [], []
            for c in node.outputs:
                self._skip(c, because=node.id)
            return
        if not chosen or unreadable:
            values = {v: _brief(self.shared_state[v]) if v in self.shared_state else None for v in sorted(used - {"True", "False", "None"})}
            facts = {"exits": read, "values": values, "unset": sorted(v for v in values if v not in self.shared_state), "at": _now()}
            raise NoRoute(f"no branch of '{node.id}' can be taken: " + "; ".join(
                f"'{r['condition']}' " + ("holds" if r["holds"] else "does not hold" if r["holds"] is False else f"could not be read ({r.get('error', '')})")
                for r in read) + f"; values: {json.dumps(values, ensure_ascii=False)}", facts)
        kept = [c for port in chosen for c in node.routes.get(port, [])]
        node.state_variables["selected"] = list(dict.fromkeys(kept))
        node.state_variables["selected_branches"] = chosen
        node.state_variables.pop("waiting_for", None)
        for c in node.outputs:
            if c not in kept:
                self._skip(c, because=node.id)

    def _skip(self, node_id: str, because: str) -> None:
        n = self.nodes[node_id]
        if n.status not in ("pending", "blocked"):
            return
        # a node (a merge above all) stays alive while any other parent is not skipped: only when every
        # route into it is gone does it go too
        if any(self.nodes[p].status != "skipped" and p != because for p in n.dependencies):
            return
        n.status = "skipped"
        for c in n.outputs:
            self._skip(c, because=node_id)

    def _listening(self, node: PlanNode) -> bool:
        """A sensor listens from the moment its turn comes (every parent done) until it has resolved."""
        if node.type != NodeType.SENSOR or node.status not in ("pending", "in_progress") or self._blockers(node):
            return False
        parents = [self.nodes[p].status for p in node.dependencies]
        return all(s in DONE for s in parents) and (not parents or "completed" in parents)

    def feed_event(self, event: Dict[str, Any]) -> List[str]:
        """Offers an event ({"text", "source", "at"}) to the sensors that are listening. A sensor whose `when` condition
        holds is completed, and what its `set` names is written into the plan's values; its condition reads `text` (in lower
        case), `source`, `at` and the plan's values. Returns the ids of the sensors that resolved."""
        text = str(event.get("text", ""))
        at = event.get("at")
        seen = {"text": text.lower(), "source": str(event.get("source", ""))[:100],
                "at": float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at) else time.time()}
        resolved: List[str] = []
        if self.run_state() in ("idle", "finished"):             # an event may start a plan that is not running: the first start node whose condition holds
            for start in self.start_nodes():
                if start.spec.get("launch") != "event":
                    continue
                try:
                    holds = bool(safe_eval(start.spec["when"], {**self.shared_state, **seen}))
                except PlanError as exc:
                    start.state_variables["event_error"] = str(exc)[:200]
                    continue
                start.state_variables.pop("event_error", None)
                if holds:
                    resolved.append(self.begin(start.spec.get("number", 1), "event", seen["at"], matched={"text": text[:200], "source": seen["source"]}))
                    break
        for nid in self.topological_order():
            node = self.nodes[nid]
            when = node.conditions.get("when")
            if not when or not self._listening(node):
                continue
            variables = {**self.shared_state, **seen}
            try:
                if not safe_eval(when, variables):
                    node.state_variables.pop("sensor_error", None)
                    continue
                outputs = {var: safe_eval(value[1:], variables) if isinstance(value, str) and value.startswith("=") else value
                           for var, value in (node.conditions.get("set") or {}).items()}
            except PlanError as exc:
                node.state_variables["sensor_error"] = str(exc)[:200]         # a sensor that cannot judge waits for the next event
                continue
            node.state_variables["matched"] = {"text": text[:200], "source": seen["source"], "at": seen["at"]}
            try:
                self.mark(nid, "completed", outputs or None)
            except PlanError as exc:                       # a failed mark restores copies of the nodes: take the sensor again
                kept = self.nodes[nid]
                kept.state_variables.pop("matched", None)
                kept.state_variables["sensor_error"] = str(exc)[:200]
                continue
            resolved.append(nid)
        return resolved

    # starting a plan ----------------------------------------------------------------------------------

    def start_nodes(self) -> List[PlanNode]:
        """The start nodes, by number."""
        return sorted((n for n in self.nodes.values() if n.type == NodeType.START), key=lambda n: (n.spec.get("number", 1), n.id))

    def validate_starts(self) -> None:
        numbers = [n.spec.get("number", 1) for n in self.start_nodes()]
        if len(set(numbers)) != len(numbers):
            raise PlanError("two starts of this plan have the same number: every start has its own number")

    def ensure_start(self) -> Optional[str]:
        """A plan without a start gets one, already taken: it is the start the plan always had, so the steps it leads to are where they were. It leads to every
        step that waits for nothing. Returns the id it made, or None when the plan had one."""
        if self.start_nodes() or not self.nodes:
            return None
        nid, n = "start", 1
        while nid in self.nodes:
            n += 1
            nid = f"start_{n}"
        node = PlanNode(id=nid, type=NodeType.START, title="Start", description="", assignee=Assignee.AGENT, spec={"number": 1, "launch": "manual"}, status="completed",
                        state_variables={"implicit": True, "started": {"at": _now(), "cause": "implicit"}})
        roots = [r.id for r in self.nodes.values() if not r.dependencies and r.type != NodeType.RAG]
        self.nodes[nid] = node
        for r in roots:
            self._link(nid, r)
        return nid

    def run_state(self) -> str:
        """`idle` (nothing has started), `running`, or `finished` (every step is done or skipped, and a start has been taken)."""
        starts = self.start_nodes()
        others = [n for n in self.nodes.values() if n.type != NodeType.START]
        if not any(s.status == "completed" for s in starts) and all(n.status in ("pending", "blocked") for n in others):
            return "idle"
        if self.nodes and all(n.status in DONE for n in self.nodes.values()) and (not starts or any(s.status == "completed" for s in starts)):
            return "finished"
        return "running"

    def begin(self, number: Any, cause: str, now: Optional[float] = None, caller: Optional[Dict[str, str]] = None, matched: Optional[Dict[str, Any]] = None) -> str:
        """Starts the plan at the start node with this number: it is taken, the other starts are skipped with what only they lead to. A plan that has finished
        starts again from the beginning. Raises PlanError for a number the plan has not, or a plan that is running. Returns the id of the start."""
        starts = self.start_nodes()
        node = next((s for s in starts if s.spec.get("number", 1) == number), None)
        if node is None:
            raise PlanError(f"the plan has no start number {number}" + (f" (it has {', '.join(str(s.spec.get('number', 1)) for s in starts)})" if starts else ""))
        state = self.run_state()
        if state == "running":
            raise PlanError("the plan is already running")
        moment = time.time() if now is None else now
        backup = copy.deepcopy((self.nodes, self.shared_state))
        try:
            if state == "finished":
                for s in starts:
                    self.rewind(s.id, True)
            for s in starts:
                if s is not node:
                    s.status = "skipped"
            node.state_variables["started"] = {"at": _now(), "cause": cause, **({"caller": caller} if caller else {}), **({"matched": matched} if matched else {})}
            node.state_variables["runs"] = int(node.state_variables.get("runs", 0)) + 1
            node.status = "completed"
            for s in starts:
                if s is not node:
                    for c in s.outputs:
                        self._skip(c, because=s.id)
        except PlanError:
            self.nodes, self.shared_state = backup
            raise
        self.note(node.id, "auto", f"{node.title}: started by {cause}" + (f" from {caller['plan']}" if caller else ""), moment)
        self.refresh_blocking()
        self._touch()
        return node.id

    # the person's requests ----------------------------------------------------------------------------

    def human_requests(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """The steps that ask the person for something and are asking now (their turn has come), oldest first: what to approve or what value to give."""
        from tadamoo.cron_templates import render_template
        from tadamoo.planner import nodes
        moment = time.time() if now is None else now
        out = []
        for nid in self.ready_nodes(moment):
            n = self.nodes[nid]
            if n.type != NodeType.INPUT:
                continue
            asked = n.state_variables.setdefault("asked_at", moment)
            context = {**self.shared_state, **self.input_values(n), "plan_title": self.title, "plan_id": self.plan_id}
            spec = n.spec
            try:
                content = render_template(spec["content"], context) if spec.get("content") else ""
            except PlanError:
                content = spec.get("content", "")
            out.append({"node_id": nid, "title": n.title, "ask": spec.get("ask", "value"), "prompt": spec["prompt"], "content": content, "kind": spec.get("kind", "text"),
                        "choices": list(spec.get("choices") or []), "into": nodes.answer_name(n), "since": asked})
        return sorted(out, key=lambda r: r["since"])

    def answer(self, node_id: str, value: Any, now: Optional[float] = None, by: str = "user") -> Dict[str, Any]:
        """The person's answer to a request that is asking now: it is checked against what it asks for, written to the plan's value, and the step is done."""
        from tadamoo.planner import nodes
        node = self.nodes.get(node_id)
        if node is None:
            raise KeyError(f"Node '{node_id}' not found")
        if node.type != NodeType.INPUT:
            raise PlanError(f"'{node_id}' does not ask the person for anything")
        if node_id not in self.ready_nodes(time.time() if now is None else now):
            raise PlanError(f"'{node_id}' is not asking now" + (": it was answered already" if node.status == "completed" else ""))
        cleaned = nodes.clean_answer(node.spec, value)
        name = nodes.answer_name(node)
        self.mark(node_id, "completed", {name: cleaned}, now)
        node = self.nodes[node_id]
        node.state_variables["answer"] = {"value": cleaned, "at": _now(), "by": by}
        self.note(node_id, "answer", f"{node.title}: {nodes.shown_answer(node.spec, cleaned)}", now)
        return {"value": cleaned, "name": name}

    def rewind(self, node_id: str, accept_irreversible: bool = False) -> List[str]:
        """Time travel: puts a node and everything after it back to pending, forgetting what they produced.

        Their previous results are kept in each node's `history`. External effects are not undone: a step that recorded
        an effect that cannot be taken back (`state_variables["effects"]`) stops the rewind until it is accepted, and the
        effect stays listed in that step's history entry as still done."""
        if node_id not in self.nodes:
            raise KeyError(f"Node '{node_id}' not found")
        seen, stack = [], [node_id]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.append(cur)
            stack.extend(self.nodes[cur].outputs)
        stays = {nid: [e for e in self.nodes[nid].state_variables.get("effects") or [] if e.get("effect") != "reversible"] for nid in seen}
        if any(stays.values()) and not accept_irreversible:
            raise PlanError("rewinding would not undo: " + "; ".join(f"{self.nodes[nid].title}: {e.get('what') or e.get('tool')}"
                                                                       for nid, es in stays.items() for e in es) + " (confirm to rewind anyway)")
        for nid in seen:
            n = self.nodes[nid]
            produced = n.state_variables.pop("outputs", None)
            effects = n.state_variables.pop("effects", None)
            if n.status != "pending" or produced or effects:
                n.state_variables.setdefault("history", []).append({"status": n.status, "outputs": produced, "at": _now(), **({"effects": effects} if effects else {}),
                                                                    **({"still_done": stays[nid]} if stays[nid] else {})})
            for var in (produced or {}):
                self.shared_state.pop(var, None)
            n.state_variables.pop("selected", None)
            n.state_variables.pop("selected_branches", None)
            n.state_variables.pop("no_branch", None)
            for key in ("attempts", "retry_at", "matched", "sensor_error", "timed_out", "parked_by"):
                n.state_variables.pop(key, None)
            n.status = "pending"
        self.refresh_blocking()
        self._touch()
        return seen

    # diffs -------------------------------------------------------------------------------------------

    def apply_diff(self, diff: Dict[str, Any]) -> List[str]:
        """Applies a proposed restructuring all at once, or not at all. Returns the ids of changed nodes.

        diff = {"add_nodes": [node dict], "remove_nodes": [id], "update_nodes": {id: fields},
                "add_edges": [[parent, child]], "remove_edges": [[parent, child]]}"""
        backup = copy.deepcopy((self.nodes, self.shared_state, self.updated_at))
        changed: List[str] = []
        decisions = self._decision_signature()
        try:
            for nid in diff.get("remove_nodes", []):
                self.remove_node(nid)
                changed.append(nid)
            declared: List[tuple] = []
            for nd in diff.get("add_nodes", []):
                node = node_from_dict(nd)
                declared.append((node.id, list(node.dependencies), list(node.outputs), {port: list(kids) for port, kids in node.routes.items()}))
                node.dependencies, node.outputs, node.routes = [], [], {}
                self.add_node(node)                      # every new node first, so new nodes can refer to each other
                changed.append(node.id)
            for nid, parents, children, routed in declared:
                for p in parents:
                    self.add_edge(p, nid)
                for port, kids in routed.items():
                    for c in kids:
                        self.add_edge(nid, c, port)
                for c in children:
                    if not any(c in kids for kids in routed.values()):
                        self.add_edge(nid, c)
            for nid, fields in (diff.get("update_nodes") or {}).items():
                if not isinstance(fields, dict):
                    raise PlanError(f"the changes to '{nid}' are an object")
                self._update_node(nid, dict(fields), False)
                changed.append(nid)
            for edge in diff.get("remove_edges", []):
                p, c, port = edge_parts(edge)
                self.remove_edge(p, c, port)
                changed.extend([p, c])
            for edge in diff.get("add_edges", []):
                p, c, port = edge_parts(edge)
                self.add_edge(p, c, port)
                changed.extend([p, c])
            self.validate_acyclic()
            self.enforce_decisions(decisions)
            self.validate_starts()
        except (PlanError, KeyError, ValueError, TypeError) as exc:
            self.nodes, self.shared_state, self.updated_at = backup
            raise PlanError(f"the change was not applied: {exc}") from None
        self.refresh_blocking()
        return list(dict.fromkeys(changed))

    def preview_diff(self, diff: Dict[str, Any]) -> List[str]:
        """The ids a diff would change, checked on a copy; raises PlanError if it would not apply."""
        return copy.deepcopy(self).apply_diff(diff)

    # serialization -----------------------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "title": self.title,
            "target_type": self.target_type,
            "insight_card": self.insight_card,
            "shared_state": self.shared_state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "order": [i for i in self.order if i in self.nodes],
            "nodes": {nid: node_to_dict(n) for nid, n in self.nodes.items()},
            **({"confirmations": False} if not self.confirmations else {}),
            **({"journal": self.journal} if self.journal else {}),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanDAG":
        try:
            nodes = {}
            legacy = {nid for nid, nd in data.get("nodes", {}).items() if isinstance(nd, dict) and nd.get("type") == "decision" and not (nd.get("spec") or {}).get("branches")}
            for nid, nd in data.get("nodes", {}).items():
                if nd.get("id", nid) != nid:
                    raise PlanError(f"node key '{nid}' does not match its id '{nd.get('id')}'")
                nodes[nid] = node_from_dict({**nd, "id": nid})
            dag = cls(
                plan_id=str(data["plan_id"]), title=str(data["title"]),
                target_type=data.get("target_type", "occasional"),
                insight_card=data.get("insight_card") or {}, nodes=nodes,
                shared_state=data.get("shared_state") or {},
                created_at=data.get("created_at") or _now(), updated_at=data.get("updated_at") or _now(),
                order=[str(i) for i in (data.get("order") or [])],
                confirmations=data.get("confirmations") is not False,
                journal=[dict(j) for j in (data.get("journal") or []) if isinstance(j, dict)],
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise PlanError(f"the plan is not valid: {type(exc).__name__}: {exc}") from None
        if dag.target_type not in ("constant", "occasional"):
            raise PlanError("target_type must be 'constant' or 'occasional'")
        for n in (dag.nodes[i] for i in legacy if i in dag.nodes):        # a decision made from its exits is labelled with the step each one leads to
            if n.type == NodeType.DECISION and n.spec.get("branches"):
                titles = {b["id"]: dag.nodes[n.routes[b["id"]][0]].title for b in n.spec.get("branches") or [] if len(n.routes.get(b["id"], [])) == 1 and b["label"] == b["id"]}
                if len(set(titles.values())) == len(titles) and not {t.lower() for t in titles.values()} & {b["label"].lower() for b in n.spec.get("branches") or [] if b["id"] not in titles}:
                    n.spec = {**n.spec, "branches": [{**b, "label": titles[b["id"]][:40]} if b["id"] in titles else b for b in n.spec["branches"]]}
        dag.validate_acyclic()
        dag.ensure_start()
        return dag


def node_to_dict(n: PlanNode) -> Dict[str, Any]:
    return {
        "id": n.id,
        "type": n.type.value if isinstance(n.type, NodeType) else n.type,
        "title": n.title,
        "description": n.description,
        "assignee": n.assignee.value if isinstance(n.assignee, Assignee) else n.assignee,
        "dependencies": list(n.dependencies),
        "outputs": list(n.outputs),
        "conditions": dict(n.conditions),
        "state_variables": dict(n.state_variables),
        "status": n.status,
        "retry_policy": n.retry_policy,
        "position": n.position,
        "call_request": n.call_request,
        "wiki_query": n.wiki_query,
        "repeat": n.repeat,
        **({"spec": n.spec} if n.spec else {}),
        **({"produces": n.produces} if n.produces else {}),
        **({"routes": {port: list(kids) for port, kids in n.routes.items()}} if n.routes else {}),
        **({"inputs": {name: dict(ref) for name, ref in n.inputs.items()}} if n.inputs else {}),
    }


def node_from_dict(nd: Dict[str, Any]) -> PlanNode:
    status = nd.get("status", "pending")
    if status not in STATUSES:
        raise PlanError(f"status must be one of {STATUSES}")
    try:
        node_type = NodeType(nd["type"])
    except ValueError:
        raise PlanError(f"unknown kind of step '{nd['type']}' (a plan written by a newer version?)") from None
    node = PlanNode(
        id=str(nd["id"]), type=node_type, title=str(nd["title"]), description=str(nd.get("description", "")),
        assignee=Assignee(nd.get("assignee", "agent")), dependencies=list(nd.get("dependencies", [])),
        outputs=list(nd.get("outputs", [])), conditions=clean_conditions(node_type, nd.get("conditions") or {}),
        state_variables=dict(nd.get("state_variables", {})), status=status,
        retry_policy=clean_field(node_type, "retry_policy", nd.get("retry_policy")),
        position=clean_field(node_type, "position", nd.get("position")),
        call_request=clean_field(node_type, "call_request", nd.get("call_request")),
        wiki_query=clean_field(node_type, "wiki_query", nd.get("wiki_query")),
        repeat=clean_field(node_type, "repeat", nd.get("repeat")),
        spec=({} if node_type == NodeType.DECISION and not (nd.get("spec") or {}).get("branches") else clean_field(node_type, "spec", nd.get("spec"), bool((nd.get("state_variables") or {}).get("draft")))),
        produces=clean_field(node_type, "produces", nd.get("produces")),
    )
    node.routes = {str(port): [str(k) for k in kids] for port, kids in (nd.get("routes") or {}).items() if isinstance(kids, list)}
    node.inputs = {str(name): {"node": str(ref["node"]), "port": str(ref.get("port") or "result")} for name, ref in (nd.get("inputs") or {}).items() if isinstance(ref, dict) and ref.get("node")}
    if node_type == NodeType.INPUT:
        node.assignee = Assignee.USER                       # a question is answered by the person
    migrate_decision(node)
    if node.type == NodeType.DECISION and node.spec.get("branches"):
        node.spec = clean_field(node_type, "spec", node.spec, bool(node.state_variables.get("draft")))
    return node
