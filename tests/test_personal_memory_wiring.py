"""The person's notes in a conversation: the flags that open them, where they come in, and that they do not leave the PC unasked."""
import argparse

from tadamoo.llm.client import RoutedClient
from tadamoo.personal_memory import PersonalMemory
from tadamoo.stack import AgentConfig, add_agent_arguments, agent_settings, build_agent


def built_with(tmp_path, client=None, **settings):
    (tmp_path / "w").mkdir(exist_ok=True)
    notes = PersonalMemory(tmp_path / "pm", key_dir=tmp_path / "keys")
    notes.set_settings(settings or {"use_long": True, "use_short": True})
    notes.add("long", "place", "Lives in Milano")
    notes.add("short", "projects", "Is building the invoice tool with Django")
    cfg = AgentConfig(workspace=str(tmp_path / "w"), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False,
                      personal_memory_dir=str(tmp_path / "pm"), personal_memory_keys=str(tmp_path / "keys"))
    return build_agent(cfg, client=client), notes


def test_the_flags_reach_the_config():
    parser = argparse.ArgumentParser()
    add_agent_arguments(parser)
    args = parser.parse_args(["--personal-memory", "P", "--personal-memory-keys", "K"])
    assert agent_settings(args)["personal_memory_dir"] == "P" and agent_settings(args)["personal_memory_keys"] == "K"
    assert agent_settings(parser.parse_args([]))["personal_memory_dir"] == ""


def test_a_conversation_without_the_folder_has_no_notes(tmp_path):
    (tmp_path / "w").mkdir()
    built = build_agent(AgentConfig(workspace=str(tmp_path / "w"), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False))
    try:
        assert built.personal is None and "recall_about_user" not in built.agent.tools.names() and built.retrieved_message("Milano") is None
    finally:
        built.close()


def test_the_notes_that_matter_come_in_at_the_tail_once(tmp_path):
    built, notes = built_with(tmp_path)
    try:
        assert "recall_about_user" in built.agent.tools.names()
        message = built.retrieved_message("find a restaurant in Milano")
        assert message["role"] == "user" and '<untrusted_data source="personal-memory" id="' in message["content"]
        assert "Lives in Milano" in message["content"] and "invoice" not in message["content"]
        assert "Milano" not in built.builder.assemble_system_message()                  # the start of the prompt stays as it was
        assert built.retrieved_message("and a museum in Milano") is None                # not repeated
        assert "Django" in built.retrieved_message("how is the invoice tool with Django going")["content"]
    finally:
        built.close()


def test_nothing_comes_in_while_the_memories_are_not_in_use(tmp_path):
    built, notes = built_with(tmp_path, use_long=False, use_short=False)
    try:
        assert built.retrieved_message("find a restaurant in Milano") is None
        notes.set_settings({"use_long": True})                                         # the switch works on a running conversation
        assert "Lives in Milano" in built.retrieved_message("find a restaurant in Milano")["content"]
    finally:
        built.close()


class Registry:
    def __init__(self, chain, types):
        self.chain, self.types = chain, types

    def load(self):
        return {}

    def fallback_chain(self, function):
        return self.chain

    def _find(self, data, pid):
        return {"type": self.types[pid]} if pid in self.types else None


def test_a_conversation_that_goes_to_another_provider_gets_no_notes_unless_allowed(tmp_path):
    class Away:
        def stays_local(self):
            return False
    built, notes = built_with(tmp_path, client=Away())
    try:
        assert built.retrieved_message("find a restaurant in Milano") is None
        notes.set_settings({"cloud_ok": True})
        assert built.retrieved_message("find a restaurant in Milano") is not None
    finally:
        built.close()


def test_a_routed_client_says_whether_it_stays_on_this_pc():
    from tadamoo.llm.client import TYPES
    here = next(name for name, spec in TYPES.items() if spec.get("this_server"))
    away = next(name for name, spec in TYPES.items() if not spec.get("this_server"))
    assert RoutedClient("executor", Registry([None], {}), object()).stays_local() is True
    assert RoutedClient("executor", Registry(["p1"], {"p1": here}), object()).stays_local() is True
    assert RoutedClient("executor", Registry(["p1", "p2"], {"p1": here, "p2": away}), object()).stays_local() is False
