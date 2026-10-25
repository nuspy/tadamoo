"""Tool results are bounded by lines and by characters, a masked result can be read again, and skills are found in a stable order."""
import pytest

from harness.context import ContextManager, TokenJuiceCompactor
from harness.skills import SkillCatalog


def compactor(tmp_path, **kw):
    return TokenJuiceCompactor(tmp_path / "art", display_dir=".basalt/artifacts", **kw)


def test_a_single_huge_line_is_cut_and_the_full_text_is_saved(tmp_path):
    c = compactor(tmp_path)
    out = c.compress_output("x" * 1_000_000, "read_file")
    assert len(out) < 3000 and "characters hidden" in out and "full output saved to .basalt/artifacts/" in out
    saved = next((tmp_path / "art").iterdir())
    assert saved.read_text(encoding="utf-8") == "x" * 1_000_000


def test_a_result_with_few_but_long_lines_is_bounded_in_total(tmp_path):
    c = compactor(tmp_path)
    text = "\n".join(f"{i:02d} " + "y" * 1900 for i in range(30))               # 30 lines, 57,000 characters
    out = c.compress_output(text, "shell_command")
    assert len(out) <= c.max_output_chars + 100 and out.rstrip().endswith("]") and "full output saved to" in out
    assert out.startswith("00 ")


def test_a_short_result_is_untouched(tmp_path):
    c = compactor(tmp_path)
    text = "line one\nline two with a traceback\n"
    assert c.compress_output(text, "t") == text
    assert not (tmp_path / "art" / "x").exists() and list((tmp_path / "art").iterdir()) == []


def test_errors_are_kept_and_an_endless_error_line_is_cut_not_the_error_lost(tmp_path):
    c = compactor(tmp_path)
    lines = [f"ok {i}" for i in range(100)]
    lines[50] = "ERROR: build failed: " + "e" * 50_000
    lines[60] = "Traceback (most recent call last):"
    out = c.compress_output("\n".join(lines), "shell_command")
    assert "ERROR: build failed: eee" in out and "Traceback (most recent call last):" in out and len(out) < 6000
    assert "ok 50" not in out and "ok 0" in out and "ok 99" in out


def test_head_and_tail_never_repeat_a_line(tmp_path):
    c = compactor(tmp_path, collapse_threshold_lines=10, keep_head_lines=5, keep_tail_lines=0)
    out = c.compress_output("\n".join(f"n{i}" for i in range(30)), "t").splitlines()
    assert out[:5] == [f"n{i}" for i in range(5)] and sum(1 for ln in out if ln.startswith("n")) == 5
    with pytest.raises(ValueError):
        compactor(tmp_path, collapse_threshold_lines=10, keep_head_lines=6, keep_tail_lines=4)


def test_the_tool_name_cannot_walk_out_of_the_artifact_folder(tmp_path):
    c = compactor(tmp_path)
    for name in ("../../../evil", "..\\..\\evil", "a/b/c", "", "x" * 300):
        path = c.save_artifact("text " + name, name)
        assert path.parent == (tmp_path / "art") and len(path.name) < 80


def test_a_masked_result_names_the_file_that_holds_it(tmp_path):
    c = compactor(tmp_path)
    manager = ContextManager(c, budget_tokens=100, mask_at=0.1, keep_recent=1)
    messages = [{"role": "user", "content": "go"},
                {"role": "tool", "name": "read_file", "tool_call_id": "1", "content": "OLD " * 500},
                {"role": "tool", "name": "read_file", "tool_call_id": "2", "content": "NEW " * 500}]
    out = manager.prepare(messages)
    assert out[1]["masked"] is True and out[2]["content"].startswith("NEW")
    named = out[1]["content"].split("full text in ")[1].rstrip("]")
    assert named.startswith(".basalt/artifacts/")
    assert (tmp_path / "art" / named.split("/")[-1]).read_text(encoding="utf-8") == "OLD " * 500


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_skills_with_the_same_name_resolve_the_same_way_every_time(tmp_path):
    for folder in ("z", "b", "m", "a"):
        write(tmp_path / folder / "SKILL.md", f"---\nname: same\ndescription: from {folder}\n---\nbody of {folder}\n")
    for _ in range(3):
        catalog = SkillCatalog([tmp_path])
        skills = catalog.discover_skills()
        assert skills["same"].description == "from a" and len(catalog.warnings) == 3
        assert all("already defined by" in w for w in catalog.warnings)


def test_a_skill_without_a_body_or_a_header_is_listed_not_lost(tmp_path):
    write(tmp_path / "empty" / "SKILL.md", "")
    write(tmp_path / "header_only" / "SKILL.md", "---\nname: header-only\n---\n")
    write(tmp_path / "plain" / "SKILL.md", "\n\n   \nJust some words\n")
    catalog = SkillCatalog([tmp_path])
    skills = catalog.discover_skills()
    assert set(skills) == {"empty", "header-only", "plain"} and skills["plain"].description == "Just some words"
    assert skills["empty"].content == "" and catalog.body("empty") == ""
    assert "- **empty**" in catalog.format_catalog_prompt() and catalog.warnings == []
