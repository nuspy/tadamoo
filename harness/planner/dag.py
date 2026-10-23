"""PlanDAG: Directed Acyclic Graph data model for proactive multi-branch workflows."""
from __future__ import annotations

import enum
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Set


class NodeType(str, enum.Enum):
    ACTION = "action"        # Executable task (agent or user)
    SENSOR = "sensor"        # Environment sensing / event monitoring
    DECISION = "decision"    # Multi-branch routing (N >= 2 exits)
    MERGE = "merge"          # Parallel branch synchronization


class Assignee(str, enum.Enum):
    AGENT = "agent"
    USER = "user"
    THIRD_PARTY = "third_party"


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
    status: str = "pending"  # pending, in_progress, completed, failed, blocked
    retry_policy: Optional[Dict[str, Any]] = None


@dataclass
class PlanDAG:
    plan_id: str
    title: str
    target_type: str  # "constant" or "occasional"
    insight_card: Dict[str, Any] = field(default_factory=dict)
    nodes: Dict[str, PlanNode] = field(default_factory=dict)
    shared_state: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    updated_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    def add_node(self, node: PlanNode) -> None:
        self.nodes[node.id] = node
        self.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def add_edge(self, parent_id: str, child_id: str) -> None:
        if parent_id not in self.nodes:
            raise KeyError(f"Parent node '{parent_id}' not found in DAG")
        if child_id not in self.nodes:
            raise KeyError(f"Child node '{child_id}' not found in DAG")

        if child_id not in self.nodes[parent_id].outputs:
            self.nodes[parent_id].outputs.append(child_id)
        if parent_id not in self.nodes[child_id].dependencies:
            self.nodes[child_id].dependencies.append(parent_id)

        self.validate_acyclic()
        self.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def validate_acyclic(self) -> bool:
        """Validates that the graph has no cycles using Kahn's algorithm."""
        in_degree = {node_id: len(node.dependencies) for node_id, node in self.nodes.items()}
        queue = [node_id for node_id, deg in in_degree.items() if deg == 0]
        visited_count = 0

        while queue:
            curr = queue.pop(0)
            visited_count += 1
            for child_id in self.nodes[curr].outputs:
                in_degree[child_id] -= 1
                if in_degree[child_id] == 0:
                    queue.append(child_id)

        if visited_count != len(self.nodes):
            raise ValueError("Cycle detected in PlanDAG; workflow must be strictly acyclic")
        return True

    def topological_order(self) -> List[str]:
        """Returns node IDs in topological execution order."""
        self.validate_acyclic()
        in_degree = {node_id: len(node.dependencies) for node_id, node in self.nodes.items()}
        queue = [node_id for node_id, deg in in_degree.items() if deg == 0]
        order = []

        while queue:
            curr = queue.pop(0)
            order.append(curr)
            for child_id in self.nodes[curr].outputs:
                in_degree[child_id] -= 1
                if in_degree[child_id] == 0:
                    queue.append(child_id)

        return order

    def reassign_to_user(self, node_id: str, reason: str) -> None:
        """Human fallback: transfers task responsibility to user when beyond software reach."""
        if node_id not in self.nodes:
            raise KeyError(f"Node '{node_id}' not found")
        node = self.nodes[node_id]
        node.assignee = Assignee.USER
        node.state_variables["fallback_reason"] = reason
        self.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "title": self.title,
            "target_type": self.target_type,
            "insight_card": self.insight_card,
            "shared_state": self.shared_state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "nodes": {
                node_id: {
                    "id": n.id,
                    "type": n.type.value if isinstance(n.type, NodeType) else n.type,
                    "title": n.title,
                    "description": n.description,
                    "assignee": n.assignee.value if isinstance(n.assignee, Assignee) else n.assignee,
                    "dependencies": n.dependencies,
                    "outputs": n.outputs,
                    "conditions": n.conditions,
                    "state_variables": n.state_variables,
                    "status": n.status,
                    "retry_policy": n.retry_policy,
                }
                for node_id, n in self.nodes.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PlanDAG:
        nodes = {}
        for nid, nd in data.get("nodes", {}).items():
            nodes[nid] = PlanNode(
                id=nd["id"],
                type=NodeType(nd["type"]),
                title=nd["title"],
                description=nd["description"],
                assignee=Assignee(nd["assignee"]),
                dependencies=nd.get("dependencies", []),
                outputs=nd.get("outputs", []),
                conditions=nd.get("conditions", {}),
                state_variables=nd.get("state_variables", {}),
                status=nd.get("status", "pending"),
                retry_policy=nd.get("retry_policy"),
            )
        dag = cls(
            plan_id=data["plan_id"],
            title=data["title"],
            target_type=data.get("target_type", "occasional"),
            insight_card=data.get("insight_card", {}),
            nodes=nodes,
            shared_state=data.get("shared_state", {}),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )
        dag.validate_acyclic()
        return dag
