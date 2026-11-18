"""The first-run wizard's own state: a step is pending until answered, once, and the answer is kept between sessions."""
import pytest

from tadamoo.setup_wizard import Wizard


def test_a_step_starts_unseen_and_the_answer_is_kept_between_sessions(tmp_path):
    f = tmp_path / "wizard.json"
    w = Wizard(f)
    assert w.seen("embed_env") is False
    w.answer("embed_env", "skipped")
    assert w.seen("embed_env") is True
    assert Wizard(f).seen("embed_env") is True                 # a fresh Wizard reads the same file


def test_installed_is_also_a_valid_answer_and_a_bad_step_or_choice_is_refused(tmp_path):
    w = Wizard(tmp_path / "wizard.json")
    w.answer("embed_env", "installed")
    assert w.seen("embed_env") is True
    with pytest.raises(ValueError):
        w.answer("no_such_step", "installed")
    with pytest.raises(ValueError):
        w.answer("embed_env", "maybe")
    with pytest.raises(ValueError):
        w.seen("no_such_step")


def test_a_missing_or_damaged_file_is_simply_nothing_seen_yet(tmp_path):
    w = Wizard(tmp_path / "missing" / "wizard.json")
    assert w.seen("embed_env") is False
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert Wizard(bad).seen("embed_env") is False
