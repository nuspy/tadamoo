"""Swarm orchestrator: coordinates multi-agent teams with shared prefix caching.

Each swarm member receives a fraction of the token/time budget while sharing
the common system prefix (prefix anchor) to maximize cache hits on the inference engine.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class SwarmMember:
    role: str
    count: int
    share: float
    token_budget: int = 0


@dataclass
class SwarmPlan:
    swarm_id: str
    description: str
    members: List[SwarmMember]
    total_budget: int
    prefix_anchor: str = ""


class SwarmOrchestrator:
    def __init__(self, manifest_path: Optional[str | Path] = None) -> None:
        if manifest_path is None:
            manifest_path = Path(__file__).with_name("swarm.json")
        self.manifest_path = Path(manifest_path)
        self._manifest: Optional[Dict[str, Any]] = None

    def load_manifest(self) -> Dict[str, Any]:
        if self._manifest is None:
            if not self.manifest_path.is_file():
                raise FileNotFoundError(f"Swarm manifest not found: {self.manifest_path}")
            self._manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        return self._manifest

    def get_swarm(self, swarm_id: str) -> Dict[str, Any]:
        manifest = self.load_manifest()
        for item in manifest.get("swarms", []):
            if item.get("id") == swarm_id:
                return item
        raise ValueError(f"Swarm ID '{swarm_id}' not found in manifest")

    def create_plan(
        self,
        swarm_id: str,
        total_budget_tokens: int = 16384,
        prefix_anchor: str = "",
    ) -> SwarmPlan:
        cfg = self.get_swarm(swarm_id)
        members_cfg = cfg.get("members", [])
        if not members_cfg:
            raise ValueError(f"Swarm '{swarm_id}' has no members configured")

        total_share = sum(float(m.get("share", 0.0)) for m in members_cfg)
        if abs(total_share - 1.0) > 0.01:
            raise ValueError(
                f"Member shares for swarm '{swarm_id}' sum to {total_share:.3f}, expected 1.0"
            )

        members = []
        for m in members_cfg:
            role = m["role"]
            count = int(m.get("count", 1))
            share = float(m["share"])
            allocated_tokens = int(total_budget_tokens * share)
            members.append(
                SwarmMember(
                    role=role,
                    count=count,
                    share=share,
                    token_budget=allocated_tokens,
                )
            )

        return SwarmPlan(
            swarm_id=swarm_id,
            description=cfg.get("description", ""),
            members=members,
            total_budget=total_budget_tokens,
            prefix_anchor=prefix_anchor,
        )

    def execute_mock(self, plan: SwarmPlan, prompt: str) -> Dict[str, Any]:
        """Executes a swarm plan deterministically for testing and offline runs."""
        results = []
        for m in plan.members:
            for idx in range(m.count):
                member_id = f"{m.role}_{idx + 1}" if m.count > 1 else m.role
                results.append({
                    "member": member_id,
                    "role": m.role,
                    "tokens_used": int(m.token_budget / m.count),
                    "status": "completed",
                    "output": f"Analysis from {member_id} on: {prompt[:40]}...",
                })

        return {
            "swarm_id": plan.swarm_id,
            "prefix_anchor_len": len(plan.prefix_anchor),
            "members_executed": len(results),
            "total_budget": plan.total_budget,
            "results": results,
        }
