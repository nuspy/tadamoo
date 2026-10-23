import pytest
from pathlib import Path

from harness.context import TokenJuiceCompactor, ContextBuilder
from harness.memory import MemoryStore
from harness.skills import SkillCatalog, parse_frontmatter, load_project_guidelines


def test_tokenjuice_compactor(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    compactor = TokenJuiceCompactor(
        artifacts,
        collapse_threshold_lines=20,
        keep_head_lines=5,
        keep_tail_lines=5,
    )

    short_text = "line\n" * 10
    assert compactor.compress_output(short_text) == short_text

    long_text = "".join(f"line {i}\n" for i in range(100))
    compressed = compactor.compress_output(long_text, tool_name="shell")

    assert "line 0" in compressed
    assert "line 4" in compressed
    assert "line 99" in compressed
    assert "lines collapsed; saved to" in compressed

    # Artifact file actually created
    art_files = list(artifacts.glob("shell_*.txt"))
    assert len(art_files) == 1
    assert art_files[0].read_text(encoding="utf-8") == long_text


def test_context_builder_stability():
    builder = ContextBuilder(system_core="Core instructions.")
    builder.set_project_rules("Follow AGENTS.md conventions.")
    builder.set_skills_catalog("- git: Version control helper")
    builder.set_memories(["User prefers concise python code"])

    msg = builder.assemble_system_message()
    assert "Core instructions." in msg
    assert "# Project Rules & Guidelines" in msg
    assert "# Available Skills" in msg
    assert "# Project Memory & Context" in msg

    # Assembled message is identical and deterministic
    msg2 = builder.assemble_system_message()
    assert msg == msg2


def test_memory_store_fts5(tmp_path: Path):
    db_file = tmp_path / "memory.db"
    store = MemoryStore(db_file)

    id1 = store.add_memory("The user prefers pytest over unittest for testing", "testing")
    id2 = store.add_memory("Postgres database runs on port 5432", "infra")
    id3 = store.add_memory("Blackwell GPU requires sm_120 architecture", "hardware")

    # Search matches
    results = store.search("pytest")
    assert len(results) == 1
    assert results[0]["id"] == id1
    assert "pytest" in results[0]["content"]

    # Prefix match
    results_prefix = store.search("Black")
    assert len(results_prefix) == 1
    assert results_prefix[0]["id"] == id3

    # Delete
    assert store.delete_memory(id2)
    assert len(store.search("Postgres")) == 0
    store.close()


def test_skill_discovery(tmp_path: Path):
    skills_root = tmp_path / "skills"
    my_skill = skills_root / "code_analyzer"
    my_skill.mkdir(parents=True)

    (my_skill / "SKILL.md").write_text(
        "---\nname: code_analyzer\ndescription: Static code analysis tool\n---\nDetailed instructions.",
        encoding="utf-8",
    )

    catalog = SkillCatalog([skills_root])
    catalog.discover_skills()
    assert "code_analyzer" in catalog.skills
    assert catalog.skills["code_analyzer"].description == "Static code analysis tool"

    formatted = catalog.format_catalog_prompt()
    assert "- **code_analyzer**: Static code analysis tool" in formatted


def test_load_project_guidelines(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("# Agents Rules\nBe precise.", encoding="utf-8")

    loaded = load_project_guidelines(ws)
    assert "# Agents Rules" in loaded
