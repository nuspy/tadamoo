"""What the agent is told about itself, and the live numbers it can read."""
from tadamoo import awareness
from tadamoo.stack import AgentConfig, build_agent


def test_the_block_says_the_limits_and_what_is_missing_and_never_changes_for_the_same_setup():
    kw = dict(model="", window=24000, mask_at=0.7, compaction_at=0.85, compaction=True, max_turns=-1, max_tokens=None, max_seconds=None,
              absent=awareness.absent_features({"memory": True}), lessons="")
    text = awareness.self_model(**kw)
    assert "24000 tokens" in text and "70%" in text and "85%" in text and "read again" in text
    assert "no browser tools" in text and "no project notes" not in text and "no limit on the steps" in text
    assert awareness.self_model(**kw) == text


def test_a_conversation_without_folding_says_so():
    text = awareness.self_model(model="", window=1000, mask_at=0.5, compaction_at=0.9, compaction=False, max_turns=8, max_tokens=5000, max_seconds=60, absent=[], lessons="")
    assert "not folded" in text and "8 steps" in text and "5000 tokens" in text and "60 seconds" in text


def test_the_built_agent_carries_the_block_and_the_tool_reads_the_live_state(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False, permission_mode="default", max_turns=7),
                        client=object())
    system = built.agent.messages[0]["content"]
    assert "# About yourself" in system and "7 steps" in system
    got = built.agent.tools.get("self_status").handler()
    assert got["context"]["window"] > 0 and got["steps"]["max"] == 7 and got["loop_strikes"] == 0 and got["level"] == "default"
