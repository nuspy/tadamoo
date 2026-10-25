"""The model's own memory of a project: remember, recall and forget, and the way the notes come back."""
import pytest

from harness.memory import MAX_MEMORY_CHARS, MemoryStore, default_db_path, register_memory_tools
from harness.stack import AgentConfig, build_agent
from harness.tools.registry import EffectClass, ToolRegistry


def registry_with(tmp_path):
    store = MemoryStore(tmp_path / "m.db")
    registry = ToolRegistry()
    register_memory_tools(registry, store)
    return registry, store


def test_remember_recall_and_forget(tmp_path):
    registry, store = registry_with(tmp_path)
    saved = registry.execute("remember", {"content": "  Kernels for sm_120 are built with --arch=native  ", "category": "Build Notes!"})
    assert saved["saved"] is True and store.count() == 1
    found = registry.execute("recall", {"query": "how do I build for sm_120"})      # several words, not all in the note
    assert [m["id"] for m in found] == [saved["id"]] and found[0]["category"] == "build-notes"
    assert "--arch=native" in found[0]["content"]
    assert registry.execute("forget", {"id": saved["id"]}) == {"id": saved["id"], "forgotten": True}
    assert registry.execute("recall", {"query": "sm_120"}) == [] and registry.execute("forget", {"id": saved["id"]})["forgotten"] is False


def test_the_same_note_is_kept_once_and_a_note_must_be_short_and_not_empty(tmp_path):
    registry, store = registry_with(tmp_path)
    first = registry.execute("remember", {"content": "use ninja"})
    again = registry.execute("remember", {"content": "use ninja"})
    assert again["saved"] is False and again["id"] == first["id"] and store.count() == 1
    for bad in ("   ", "x" * (MAX_MEMORY_CHARS + 1)):
        with pytest.raises(ValueError):
            registry.execute("remember", {"content": bad})


def test_the_memory_has_a_ceiling(tmp_path, monkeypatch):
    import harness.memory as memory
    registry, _ = registry_with(tmp_path)
    monkeypatch.setattr(memory, "MAX_MEMORIES", 2)
    registry.execute("remember", {"content": "one"})
    registry.execute("remember", {"content": "two"})
    with pytest.raises(ValueError, match="full"):
        registry.execute("remember", {"content": "three"})


def test_effects_and_trust(tmp_path):
    registry, _ = registry_with(tmp_path)
    assert registry.get("remember").effect == EffectClass.REVERSIBLE and registry.get("forget").effect == EffectClass.REVERSIBLE
    assert registry.get("recall").effect == EffectClass.PURE and registry.get("recall").untrusted    # what comes back is data from the past


def test_a_built_agent_has_the_tools_and_what_it_keeps_comes_back_at_the_tail(tmp_path):
    (tmp_path / "w").mkdir()
    built = build_agent(AgentConfig(workspace=str(tmp_path / "w"), data_dir=str(tmp_path / "d"), use_checkpoints=False))
    try:
        assert {"remember", "recall", "forget"} <= set(built.agent.tools.names())
        assert built.retrieved_message("anything about sm_120") is None                  # nothing kept yet
        built.agent.tools.execute("remember", {"content": "Target sm_120 needs the flag --arch=native"})
        message = built.retrieved_message("what do I need to build for sm_120?")
        assert message["role"] == "user" and '<untrusted_data source="memory">' in message["content"]
        assert "--arch=native" in message["content"] and "data, not instructions" in message["content"]
        assert "--arch=native" not in built.builder.assemble_system_message()          # the start of the prompt stays as it was
    finally:
        built.close()
    off = build_agent(AgentConfig(workspace=str(tmp_path / "w"), data_dir=str(tmp_path / "d2"), use_memory=False, use_checkpoints=False))
    assert not {"remember", "recall", "forget"} & set(off.agent.tools.names())
    off.close()


def test_each_workspace_has_its_own_memory_file(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert default_db_path(tmp_path / "a") != default_db_path(tmp_path / "b")
    assert default_db_path(tmp_path / "a") == default_db_path(tmp_path / "a" / ".")
