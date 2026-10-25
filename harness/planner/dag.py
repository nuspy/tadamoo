"""PlanDAG: the workflow graph of a plan, with shared state, decisions, merges and human tasks.

Edges live in two lists (a node's `dependencies` and its parents' `outputs`); every change keeps them
symmetric and every method that can break the graph leaves it exactly as it was if it fails.

Decision conditions are small expressions over the plan's shared variables (see safe_eval), never code.
"""
from __future__ import annotations

import ast
import copy
import enum
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
    DECISION = "decision"    # Multi-branch routing (N >= 2 exits)
    MERGE = "merge"          # Parallel branch synchronization


class Assignee(str, enum.Enum):
    AGENT = "agent"
    USER = "user"
    THIRD_PARTY = "third_party"


class PlanError(ValueError):
    """The change would leave the plan invalid; the plan is unchanged."""


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


EDITABLE = ("title", "description", "assignee", "conditions", "state_variables", "retry_policy", "position", "call_request", "wiki_query")

# -- what a node may carry ---------------------------------------------------------------------------------

MAX_ATTEMPTS, MAX_WAIT_SECONDS, MAX_BACKOFF = 10, 7 * 86400, 10
EVENT_TEXT_CHARS = 2000
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


def clean_field(node_type: Any, key: str, value: Any) -> Any:
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
    return value


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

    def _touch(self) -> None:
        self.updated_at = _now()

    # structure ---------------------------------------------------------------------------------------

    def add_node(self, node: PlanNode, replace: bool = False) -> None:
        """Adds a node and the edges it declares; the parents and children it names must already exist.

        With `replace`, a node that exists is swapped for this one and its edges become exactly the declared ones."""
        if node.id in self.nodes and not replace:
            raise PlanError(f"node '{node.id}' already exists")
        if not node.id or not str(node.id).strip():
            raise PlanError("a node needs an id")
        declared_parents, declared_children = list(node.dependencies), list(node.outputs)
        for ref in declared_parents + declared_children:
            if ref not in self.nodes or ref == node.id:
                raise PlanError(f"node '{node.id}' refers to unknown node '{ref}'")
        backup = copy.deepcopy((self.nodes, self.shared_state))
        try:
            if node.id in self.nodes:
                self._detach(node.id)
            node.dependencies, node.outputs = [], []
            self.nodes[node.id] = node
            for p in dict.fromkeys(declared_parents):
                self._link(p, node.id)
            for c in dict.fromkeys(declared_children):
                self._link(node.id, c)
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
        node.dependencies, node.outputs = [], []

    def _link(self, parent_id: str, child_id: str) -> None:
        if child_id not in self.nodes[parent_id].outputs:
            self.nodes[parent_id].outputs.append(child_id)
        if parent_id not in self.nodes[child_id].dependencies:
            self.nodes[child_id].dependencies.append(parent_id)

    def add_edge(self, parent_id: str, child_id: str) -> None:
        for nid in (parent_id, child_id):
            if nid not in self.nodes:
                raise KeyError(f"Node '{nid}' not found in DAG")
        if parent_id == child_id:
            raise PlanError("a node cannot depend on itself")
        backup = copy.deepcopy(self.nodes)
        try:
            self._link(parent_id, child_id)
            self.validate_acyclic()
        except PlanError:
            self.nodes = backup
            raise
        self._touch()

    def remove_edge(self, parent_id: str, child_id: str) -> None:
        if parent_id in self.nodes and child_id in self.nodes[parent_id].outputs:
            self.nodes[parent_id].outputs.remove(child_id)
        if child_id in self.nodes and parent_id in self.nodes[child_id].dependencies:
            self.nodes[child_id].dependencies.remove(parent_id)
        self._touch()

    def remove_node(self, node_id: str) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            raise KeyError(f"Node '{node_id}' not found")
        for p in list(node.dependencies):
            self.remove_edge(p, node_id)
        for c in list(node.outputs):
            self.remove_edge(node_id, c)
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

    def validate_decisions(self) -> List[str]:
        """Problems with decision nodes: fewer than two exits, or an exit without a condition."""
        problems = []
        for nid, n in self.nodes.items():
            if n.type != NodeType.DECISION:
                continue
            if len(n.outputs) < 2:
                problems.append(f"decision '{nid}' needs at least two exits, it has {len(n.outputs)}")
            for c in n.outputs:
                if c not in n.conditions:
                    problems.append(f"decision '{nid}' has no condition for its exit '{c}'")
        return problems

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

    # editing -----------------------------------------------------------------------------------------

    def update_node(self, node_id: str, **changes: Any) -> PlanNode:
        """Edits text, assignee, conditions or variables of a node that has not been completed."""
        node = self.nodes.get(node_id)
        if node is None:
            raise KeyError(f"Node '{node_id}' not found")
        unknown = [k for k in changes if k not in EDITABLE]
        if unknown:
            raise PlanError(f"cannot edit {', '.join(unknown)}; editable: {', '.join(EDITABLE)}")
        if node.status == "completed" and any(k != "position" for k in changes):
            raise PlanError(f"'{node_id}' is already completed; rewind it to change it")
        if "assignee" in changes:
            changes["assignee"] = Assignee(changes["assignee"])
        changes = {k: clean_field(node.type, k, v) for k, v in changes.items()}      # all checked before any is applied
        for key, value in changes.items():
            setattr(node, key, value)
        self.refresh_blocking()
        self._touch()
        return node

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

    def _blockers(self, node: PlanNode) -> List[str]:
        """Unfinished parents that the user must do, which keep this node from starting."""
        return [p for p in node.dependencies
                if self.nodes[p].assignee == Assignee.USER and self.nodes[p].status not in DONE]

    def refresh_blocking(self) -> None:
        """Marks nodes blocked while a user task above them is open, and releases them when it is done."""
        for node in self.nodes.values():
            blockers = self._blockers(node)
            if node.status in ("pending", "blocked"):
                if blockers:
                    node.status = "blocked"
                    node.state_variables["blocked_by"] = blockers
                elif node.status == "blocked":
                    node.status = "pending"
                    node.state_variables.pop("blocked_by", None)

    def ready_nodes(self, now: Optional[float] = None) -> List[str]:
        """Pending nodes whose parents are all finished (a merge needs at least one completed parent), except a
        step that failed and is waiting to try again."""
        out = []
        now = time.time() if now is None else now
        for nid in self.topological_order():
            n = self.nodes[nid]
            if n.status != "pending" or self._blockers(n) or n.state_variables.get("retry_at", 0) > now:
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
        policy = node.retry_policy
        if not policy or attempts >= policy["max_attempts"]:
            node.state_variables.pop("retry_at", None)
            node.status = "failed"
            return None
        wait = policy["wait_seconds"] * policy["backoff"] ** (attempts - 1)
        node.state_variables["retry_at"] = now + wait
        node.status = "pending"
        return {"attempt": attempts, "of": policy["max_attempts"], "wait_seconds": wait, "retry_at": now + wait}

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
        if status == "completed":
            backup = copy.deepcopy((self.nodes, self.shared_state))
            try:
                if outputs:
                    self.shared_state.update(outputs)
                    node.state_variables["outputs"] = dict(outputs)
                node.status = "completed"
                for key in ("attempts", "retry_at", "sensor_error"):
                    node.state_variables.pop(key, None)
                if node.type == NodeType.DECISION:
                    self._route(node)
            except PlanError:
                self.nodes, self.shared_state = backup
                raise
        elif status == "failed":
            retry = self._failed(node, time.time() if now is None else now)
        else:
            node.status = status
            node.state_variables.pop("retry_at", None)
            if status == "skipped":
                node.state_variables.pop("attempts", None)
        self.refresh_blocking()
        self._touch()
        return retry

    def _route(self, node: PlanNode) -> None:
        chosen = [c for c in node.outputs if c in node.conditions and safe_eval(node.conditions[c], self.shared_state)]
        node.state_variables["selected"] = chosen
        for c in node.outputs:
            if c not in chosen:
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
        text = str(event.get("text", ""))[:EVENT_TEXT_CHARS]
        at = event.get("at")
        seen = {"text": text.lower(), "source": str(event.get("source", ""))[:100],
                "at": float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at) else time.time()}
        resolved: List[str] = []
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
            self.mark(nid, "completed", outputs or None)
            resolved.append(nid)
        return resolved

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
            for key in ("attempts", "retry_at", "matched", "sensor_error"):
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
        try:
            for nid in diff.get("remove_nodes", []):
                self.remove_node(nid)
                changed.append(nid)
            declared: List[tuple] = []
            for nd in diff.get("add_nodes", []):
                node = node_from_dict(nd)
                declared.append((node.id, list(node.dependencies), list(node.outputs)))
                node.dependencies, node.outputs = [], []
                self.add_node(node)                      # every new node first, so new nodes can refer to each other
                changed.append(node.id)
            for nid, parents, children in declared:
                for p in parents:
                    self.add_edge(p, nid)
                for c in children:
                    self.add_edge(nid, c)
            for nid, fields in (diff.get("update_nodes") or {}).items():
                self.update_node(nid, **fields)
                changed.append(nid)
            for p, c in diff.get("remove_edges", []):
                self.remove_edge(p, c)
                changed.extend([p, c])
            for p, c in diff.get("add_edges", []):
                self.add_edge(p, c)
                changed.extend([p, c])
            self.validate_acyclic()
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
            "nodes": {nid: node_to_dict(n) for nid, n in self.nodes.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanDAG":
        try:
            nodes = {}
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
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise PlanError(f"the plan is not valid: {type(exc).__name__}: {exc}") from None
        if dag.target_type not in ("constant", "occasional"):
            raise PlanError("target_type must be 'constant' or 'occasional'")
        dag.validate_acyclic()
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
    }


def node_from_dict(nd: Dict[str, Any]) -> PlanNode:
    status = nd.get("status", "pending")
    if status not in STATUSES:
        raise PlanError(f"status must be one of {STATUSES}")
    node_type = NodeType(nd["type"])
    return PlanNode(
        id=str(nd["id"]), type=node_type, title=str(nd["title"]), description=str(nd.get("description", "")),
        assignee=Assignee(nd.get("assignee", "agent")), dependencies=list(nd.get("dependencies", [])),
        outputs=list(nd.get("outputs", [])), conditions=clean_conditions(node_type, nd.get("conditions") or {}),
        state_variables=dict(nd.get("state_variables", {})), status=status,
        retry_policy=clean_field(node_type, "retry_policy", nd.get("retry_policy")),
        position=clean_field(node_type, "position", nd.get("position")),
        call_request=clean_field(node_type, "call_request", nd.get("call_request")),
        wiki_query=clean_field(node_type, "wiki_query", nd.get("wiki_query")),
    )
