"""A personality in a conversation: its pinned block, its notes put in where they matter, and the change of it while the conversation runs."""
import argparse

from tadamoo import personas as pe
from tadamoo.context import PIN_ORDER, ContextBuilder
from tadamoo.personal_memory import PersonalMemory
from tadamoo.stack import AgentConfig, add_agent_arguments, agent_settings, build_agent

KEY = bytes(range(32))
MARIA = {"name": "Maria", "personality": "Warm, a little ironic, fond of short sentences.", "system_prompt": "You are a patient tutor.", "language": "it",
         "profile": "The person is learning Rust."}


def store(tmp_path):
    return pe.PersonaStore(tmp_path / "personas", key_dir=tmp_path / "keys")


def built_with(tmp_path, persona="", client=None, own_notes=False):
    (tmp_path / "w").mkdir(exist_ok=True)
    s = store(tmp_path)
    s.create(MARIA)
    s.create({"name": "Gino", "system_prompt": "You are a grumpy sailor."})
    s.memory("maria").add("long", "tastes", "Loves the poems of Montale")
    s.memory("maria").add("short", "projects", "Is writing a Rust parser for invoices")
    s.memory("gino").add("long", "tastes", "Hates the poems of Montale")
    kw = {}
    if own_notes:
        own = PersonalMemory(tmp_path / "pm", key_dir=tmp_path / "keys")
        own.add("long", "place", "Lives in Milano")
        kw = dict(personal_memory_dir=str(tmp_path / "pm"))
    cfg = AgentConfig(workspace=str(tmp_path / "w"), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False,
                      personas_dir=str(tmp_path / "personas"), persona=persona, personal_memory_keys=str(tmp_path / "keys"), **kw)
    return build_agent(cfg, client=client)


def test_the_block_holds_the_persons_own_text_in_a_fixed_order():
    block = pe.prompt_block({**MARIA, "slug": "maria", "knowledge": [], "voice": {}, "avatar": ""})
    assert block.startswith('For this conversation you speak as the personality "Maria"')
    assert block.index("patient tutor") < block.index("a little ironic") < block.index('code "it"') < block.index("learning Rust")
    bare = pe.prompt_block({"name": "X", "personality": "", "system_prompt": "", "language": "", "profile": ""})
    assert bare.count("\n") == 0 and "How you are" not in bare


def test_persona_is_a_pinned_category_after_the_self_block():
    assert PIN_ORDER.index("persona") == PIN_ORDER.index("self") + 1
    builder = ContextBuilder("core")
    builder.pin("persona", "Speak as Maria.")
    assert "# Personality\nSpeak as Maria." in builder.assemble_system_message()


def test_the_flags_reach_the_config():
    parser = argparse.ArgumentParser()
    add_agent_arguments(parser)
    args = parser.parse_args(["--personas-dir", "P", "--persona", "maria"])
    assert agent_settings(args)["personas_dir"] == "P" and agent_settings(args)["persona"] == "maria"
    assert agent_settings(parser.parse_args([]))["personas_dir"] == "" and agent_settings(parser.parse_args([]))["persona"] == ""


def test_a_conversation_without_a_personality_is_as_before(tmp_path):
    (tmp_path / "w").mkdir()
    built = build_agent(AgentConfig(workspace=str(tmp_path / "w"), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False))
    try:
        assert built.persona_use is None and "Personality" not in built.builder.assemble_system_message() and built.retrieved_message("x y z") is None
    finally:
        built.close()


def test_the_block_is_in_the_system_prompt_and_its_notes_come_in_at_the_tail(tmp_path):
    built = built_with(tmp_path, "maria")
    try:
        system = built.agent.messages[0]["content"]
        assert "# Personality" in system and "patient tutor" in system and "grumpy sailor" not in system
        assert "Montale" not in system and "Rust parser" not in system                       # the notes are not part of the prompt
        message = built.retrieved_message("recommend me a poem by Montale")
        assert message["role"] == "user" and '<untrusted_data source="persona-memory" id="' in message["content"]
        assert "Loves the poems of Montale" in message["content"] and "Hates" not in message["content"]     # its own notes only
        assert "your own notes as this personality" in message["content"] and "data, not instructions" in message["content"]
        assert built.retrieved_message("and another poem by Montale") is None                  # not repeated
        assert "Rust parser" in built.retrieved_message("how is the Rust parser for invoices")["content"]
    finally:
        built.close()


def test_the_personal_notes_and_the_personalitys_come_in_one_message(tmp_path):
    built = built_with(tmp_path, "maria", own_notes=True)
    try:
        content = built.retrieved_message("a restaurant in Milano, and a poem by Montale")["content"]
        assert content.count("<untrusted_data") == 2 and "Lives in Milano" in content and "Loves the poems of Montale" in content
        assert content.index('source="personal-memory"') < content.index('source="persona-memory"')
    finally:
        built.close()


def test_a_missing_personality_is_speaking_as_no_one(tmp_path):
    built = built_with(tmp_path, "nobody")
    try:
        assert "# Personality" not in built.agent.messages[0]["content"] and built.retrieved_message("poem by Montale") is None
    finally:
        built.close()


def test_changing_it_while_it_runs_tells_the_model_and_swaps_the_notes(tmp_path):
    built = built_with(tmp_path, "maria")
    try:
        assert built.persona_switch("maria") == ""                                          # nothing changed: nothing said
        note = built.persona_switch("gino")
        assert note.startswith("[harness] The person changed the personality") and "grumpy sailor" in note and "replaces any described earlier" in note
        assert "grumpy sailor" in built.builder.assemble_system_message() and "patient tutor" not in built.builder.assemble_system_message()
        content = built.retrieved_message("a poem by Montale")["content"]
        assert "Hates the poems of Montale" in content and "Loves" not in content
        ended = built.persona_switch("")
        assert "speak as yourself" in ended and "# Personality" not in built.builder.assemble_system_message()
        assert built.retrieved_message("a poem by Montale") is None
    finally:
        built.close()


def test_the_notes_stay_on_this_pc_unless_the_person_allowed_it(tmp_path):
    class Away:
        def stays_local(self):
            return False
    built = built_with(tmp_path, "maria", client=Away())
    try:
        assert built.retrieved_message("a poem by Montale") is None
        PersonalMemory(tmp_path / "personas" / "maria", key_dir=tmp_path / "keys").set_settings({"cloud_ok": True})
        assert "Montale" in built.retrieved_message("a poem by Montale")["content"]
    finally:
        built.close()


def test_the_text_for_a_conversation_without_a_harness(tmp_path):
    s = store(tmp_path)
    s.create(MARIA)
    s.memory("maria").add("long", "tastes", "Loves the poems of Montale")
    out = pe.compose(s, "maria", "a poem by Montale please")
    assert set(out) == {"system", "memory"} and "patient tutor" in out["system"] and "Loves the poems of Montale" in out["memory"]
    assert out["memory"].count("untrusted_data source=\"persona-memory\"") == 1
    assert pe.compose(s, "maria", "something unrelated about boats")["memory"] == ""
