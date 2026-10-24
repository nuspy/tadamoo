"""PlanDAG: the workflow graph of a plan, with shared state, decisions, merges and human tasks.

Edges live in two lists (a node's `dependencies` and its parents' `outputs`); every change keeps them
symmetric and every method that can break the graph leaves it exactly as it was if it fails.

Decision conditions are small expressions over the plan's shared variables (see safe_eval), never code.
"""
from __future__ import annotations

import ast
import copy
import enum
import operator
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
_ARITH = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_FUNCS = {"len": len, "min": min, "max": max, "sum": sum, "abs": abs}


def safe_eval(expression: str, variables: Dict[str, Any]) -> Any:
    """Evaluates a condition such as `len(invitati_confermati) >= 2 and meteo_confermato`.

    Only names, literals, comparisons, and/or/not, + - * /, lists/tuples and len/min/max/sum/abs are allowed."""
    if not isinstance(expression, str) or len(expression) > 300:
        raise PlanError("a condition must be a string of at most 300 characters")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise PlanError(f"invalid condition '{expression}': {exc.msg}") from None

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
                right = walk(right_node)
                if type(op) not in _CMP or not _CMP[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[walk(a) for a in n.args])
        raise PlanError(f"'{expression}' uses something a condition may not use")

    try:
        return walk(tree)
    except (TypeError, ZeroDivisionError) as exc:
        raise PlanError(f"the condition '{expression}' could not be evaluated: {exc}") from None


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
    retry_policy: Optional[Dict[str, Any]] = None


EDITABLE = ("title", "description", "assignee", "conditions", "state_variables", "retry_policy")


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
        """Adds a node and the edges it declares; the parents and children it names must already exist."""
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
        if node.status == "completed":
            raise PlanError(f"'{node_id}' is already completed; rewind it to change it")
        if "assignee" in changes:
            changes["assignee"] = Assignee(changes["assignee"])
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

    def ready_nodes(self) -> List[str]:
        """Pending nodes whose parents are all finished (a merge needs at least one completed parent)."""
        out = []
        for nid in self.topological_order():
            n = self.nodes[nid]
            if n.status != "pending" or self._blockers(n):
                continue
            parents = [self.nodes[p].status for p in n.dependencies]
            if all(s in DONE for s in parents) and (not parents or "completed" in parents):
                if n.type == NodeType.MERGE and not self._merge_inputs_present(n):
                    continue
                out.append(nid)
        return out

    def _merge_inputs_present(self, node: PlanNode) -> bool:
        return all(v in self.shared_state for v in node.conditions.get("requires", []))

    def mark(self, node_id: str, status: str, outputs: Optional[Dict[str, Any]] = None) -> None:
        """Sets a node's status. Completing it writes its outputs into the shared state and, for a
        decision, routes: the exits whose condition does not hold are skipped."""
        node = self.nodes.get(node_id)
        if node is None:
            raise KeyError(f"Node '{node_id}' not found")
        if status not in STATUSES:
            raise PlanError(f"status must be one of {STATUSES}")
        if status == "completed":
            backup = copy.deepcopy((self.nodes, self.shared_state))
            try:
                if outputs:
                    self.shared_state.update(outputs)
                    node.state_variables["outputs"] = dict(outputs)
                node.status = "completed"
                if node.type == NodeType.DECISION:
                    self._route(node)
            except PlanError:
                self.nodes, self.shared_state = backup
                raise
        else:
            node.status = status
        self.refresh_blocking()
        self._touch()

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

    def rewind(self, node_id: str) -> List[str]:
        """Time travel: puts a node and everything after it back to pending, forgetting what they produced.

        Their previous results are kept in each node's `history`. External effects are not undone."""
        if node_id not in self.nodes:
            raise KeyError(f"Node '{node_id}' not found")
        seen, stack = [], [node_id]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.append(cur)
            stack.extend(self.nodes[cur].outputs)
        for nid in seen:
            n = self.nodes[nid]
            produced = n.state_variables.pop("outputs", None)
            if n.status != "pending" or produced:
                n.state_variables.setdefault("history", []).append({"status": n.status, "outputs": produced, "at": _now()})
            for var in (produced or {}):
                self.shared_state.pop(var, None)
            n.state_variables.pop("selected", None)
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
    }


def node_from_dict(nd: Dict[str, Any]) -> PlanNode:
    status = nd.get("status", "pending")
    if status not in STATUSES:
        raise PlanError(f"status must be one of {STATUSES}")
    return PlanNode(
        id=str(nd["id"]), type=NodeType(nd["type"]), title=str(nd["title"]), description=str(nd.get("description", "")),
        assignee=Assignee(nd.get("assignee", "agent")), dependencies=list(nd.get("dependencies", [])),
        outputs=list(nd.get("outputs", [])), conditions=dict(nd.get("conditions", {})),
        state_variables=dict(nd.get("state_variables", {})), status=status, retry_policy=nd.get("retry_policy"),
    )
