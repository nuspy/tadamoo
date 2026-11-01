"""The benchmark registry and the local-model rule (harness/bench/registry.py, localmodel.py). CPU only."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.bench import localmodel, registry
from bench_support import MODEL


def test_the_five_first_benchmarks_are_listed():
    assert set(registry.IDS) == {"terminal-bench-2", "swe-bench-verified", "clawbench", "bfcl-v4", "tau3-bench"}
    for s in registry.SPECS.values():
        assert s.leaderboard in registry.LEADERBOARD_KINDS and s.licence and s.source and s.primary_metric and s.subsets
        json.dumps(s.to_dict())


def test_facts_from_the_study():
    s = registry.SPECS
    assert s["clawbench"].leaderboard == "csv_pr" and s["terminal-bench-2"].leaderboard == "closed"
    assert s["terminal-bench-2"].needs.docker and s["terminal-bench-2"].needs.wsl2
    assert s["swe-bench-verified"].source_kind == registry.HF_DATASET
    assert not s["bfcl-v4"].needs.docker


def test_no_spec_asks_for_a_judge_key_and_each_states_the_local_rule():
    for s in registry.SPECS.values():
        assert not hasattr(s.needs, "judge_key") and not hasattr(s, "judge_env")
        assert "Run only with the local engine and model" in s.notes


def test_cloud_judges_are_replaced_and_the_score_is_provisional():
    assert not registry.SPECS["clawbench"].comparable_with_board and not registry.SPECS["tau3-bench"].comparable_with_board
    assert registry.SPECS["terminal-bench-2"].comparable_with_board
    assert "unofficial judge: not comparable with the board" in registry.SPECS["clawbench"].notes


def test_unknown_details_are_marked():
    assert all(s.to_confirm for s in registry.SPECS.values())


def test_get_unknown_id_names_the_known_ones():
    with pytest.raises(KeyError, match="clawbench"):
        registry.get("nope")


# -- the local-model rule -------------------------------------------------------------------------------------------

M = localmodel.from_info(MODEL)


@pytest.mark.parametrize("settings", [{}, {"trials": 3}, {"model": "Qwen3.8-Flash-Next"}, {"model": "qwen38-iq4_xs.gguf"}, {"provider": "local"},
                                       {"base_url": "http://127.0.0.1:7232/v1"}, {"judge": "local"}, {"nested": {"judge_model": "local"}}])
def test_local_settings_pass(settings):
    assert localmodel.refusal(settings, M) == ""


@pytest.mark.parametrize("settings,word", [({"model": "gpt-5"}, "gpt-5"), ({"provider": "openai"}, "openai"), ({"api_key": "sk-x"}, "API key"),
                                            ({"base_url": "https://api.openai.com/v1"}, "api.openai.com"), ({"judge_model": "claude-opus"}, "claude-opus"),
                                            ({"x": {"simulator_model": "gemini"}}, "gemini"), ({"endpoint": "http://example.org:80"}, "example.org")])
def test_other_models_providers_and_keys_are_refused(settings, word):
    assert word in localmodel.refusal(settings, M)


def test_refusal_never_prints_the_key_value():
    assert "sk-secret" not in localmodel.refusal({"api_key": "sk-secret"}, M)
