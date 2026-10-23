import pytest
from pathlib import Path
from harness.swarm.orchestrator import SwarmOrchestrator, SwarmPlan


def test_swarm_manifest_loading():
    orch = SwarmOrchestrator()
    manifest = orch.load_manifest()
    assert "_schema" in manifest
    assert len(manifest["swarms"]) >= 3


def test_swarm_create_plan_valid():
    orch = SwarmOrchestrator()
    plan = orch.create_plan("code_review", total_budget_tokens=10000, prefix_anchor="System prompt prefix")

    assert plan.swarm_id == "code_review"
    assert plan.total_budget == 10000
    assert plan.prefix_anchor == "System prompt prefix"
    assert len(plan.members) == 3

    # Check budgets sum to total
    total_tokens = sum(m.token_budget for m in plan.members)
    assert total_tokens == 10000


def test_swarm_create_plan_invalid_share(tmp_path: Path):
    bad_manifest = tmp_path / "bad.json"
    bad_manifest.write_text('{"swarms": [{"id": "bad", "members": [{"role": "a", "share": 0.5}]}]}', encoding="utf-8")
    orch = SwarmOrchestrator(bad_manifest)

    with pytest.raises(ValueError, match="expected 1.0"):
        orch.create_plan("bad")


def test_swarm_execute_mock():
    orch = SwarmOrchestrator()
    plan = orch.create_plan("search_and_synthesize", total_budget_tokens=20000, prefix_anchor="AGENTS.md guidelines")
    res = orch.execute_mock(plan, "Analyze Qwen3.8-Flash-Next architecture")

    assert res["swarm_id"] == "search_and_synthesize"
    assert res["prefix_anchor_len"] > 0
    # planner (1) + searcher (3) + critic (1) = 5
    assert res["members_executed"] == 5
    assert len(res["results"]) == 5
