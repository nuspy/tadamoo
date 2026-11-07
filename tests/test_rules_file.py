"""--rules-file: a file of operating rules, pinned whole in the system message, read once at construction (tadamoo/stack.py)."""
from pathlib import Path

from tadamoo.stack import AgentConfig, RULES_MAX, build_agent
from test_session import Scripted, say


def test_a_rules_file_is_pinned_in_the_system_message(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    rules = tmp_path / "rules.md"
    rules.write_text("Never write the shared log file directly: send it a message instead.", encoding="utf-8")
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False,
                                    rules_file=str(rules)), client=Scripted(say("ok")))
    assert "# Operating rules" in built.agent.messages[0]["content"] and "send it a message instead" in built.agent.messages[0]["content"]
    built.close()


def test_a_long_rules_file_is_cut_and_a_missing_one_is_quietly_skipped(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    rules = tmp_path / "rules.md"
    rules.write_text("x" * (RULES_MAX + 500), encoding="utf-8")
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False,
                                    rules_file=str(rules)), client=Scripted(say("ok")))
    text = built.agent.messages[0]["content"]
    section = text[text.index("# Operating rules"):]
    assert len(section) - len("# Operating rules (they hold for the whole task and are never folded away)\n") <= RULES_MAX
    built.close()

    gone = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d2"), use_memory=False, use_checkpoints=False,
                                   rules_file=str(tmp_path / "no_such_file.md")), client=Scripted(say("ok")))
    assert "# Operating rules" not in gone.agent.messages[0]["content"]
    gone.close()


def test_without_a_rules_file_nothing_changes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False),
                        client=Scripted(say("ok")))
    assert "# Operating rules" not in built.agent.messages[0]["content"]
    built.close()
