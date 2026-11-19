"""The folder that holds the conversations' files: the default, the one chosen in Settings for Chat and for Agentic coding
separately, the pre-split choice they both fall back to, and the records that stay protected wherever it is."""
import json

import pytest

from serve import userdata
from tadamoo.secrets_broker import PROTECTED_FILES, SecretsBroker


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.mark.parametrize("kind", ["chat", "agent"])
def test_without_a_choice_the_base_is_the_journal_folder(data, kind):
    assert userdata.chosen_sessions_base(kind) is None
    assert userdata.sessions_base(kind) == data / "journal"


@pytest.mark.parametrize("kind", ["chat", "agent"])
def test_a_chosen_folder_is_kept_in_the_default_data_folder_and_can_be_forgotten(data, tmp_path, kind):
    elsewhere = tmp_path / "elsewhere" / kind
    userdata.set_sessions_base(kind, elsewhere)
    assert json.loads((data / f"sessions-base-{kind}.json").read_text(encoding="utf-8")) == {"path": str(elsewhere)}
    assert userdata.sessions_base(kind) == elsewhere and userdata.chosen_sessions_base(kind) == elsewhere
    userdata.set_sessions_base(kind, None)
    assert userdata.sessions_base(kind) == data / "journal"


def test_choosing_one_kind_never_moves_the_other(data, tmp_path):
    chat_place = tmp_path / "chat-place"
    userdata.set_sessions_base("chat", chat_place)
    assert userdata.sessions_base("chat") == chat_place
    assert userdata.sessions_base("agent") == data / "journal"            # untouched


def test_a_choice_made_before_the_two_were_split_still_applies_to_both(data, tmp_path):
    old_place = tmp_path / "old-shared-place"
    data.mkdir(parents=True)
    (data / "sessions-base.json").write_text(json.dumps({"path": str(old_place)}), encoding="utf-8")
    assert userdata.chosen_sessions_base("chat") == old_place
    assert userdata.chosen_sessions_base("agent") == old_place
    userdata.set_sessions_base("chat", tmp_path / "new-chat-place")       # customizing one kind now...
    assert userdata.chosen_sessions_base("chat") == tmp_path / "new-chat-place"
    assert userdata.chosen_sessions_base("agent") == old_place           # ...leaves the other on the old shared choice
    assert (data / "sessions-base.json").exists()                        # the pre-split file itself is never rewritten


@pytest.mark.parametrize("kind", ["chat", "agent"])
@pytest.mark.parametrize("text", ["", "not json", "[]", '{"path": ""}', '{"path": 4}', '{"other": "x"}'])
def test_a_broken_or_empty_file_means_no_choice(data, text, kind):
    data.mkdir(parents=True)
    (data / f"sessions-base-{kind}.json").write_text(text, encoding="utf-8")
    assert userdata.chosen_sessions_base(kind) is None


def test_an_unknown_kind_is_refused():
    with pytest.raises(AssertionError):
        userdata.chosen_sessions_base("bogus")
    with pytest.raises(AssertionError):
        userdata.set_sessions_base("bogus", None)


def test_the_conversation_records_are_protected_where_they_really_are(data, tmp_path):
    assert "journal/sessions/agent_sessions.json" in PROTECTED_FILES
    broker = SecretsBroker()
    records = data / "journal" / "sessions"
    records.mkdir(parents=True)
    (records / "agent_sessions.json").write_text("{}", encoding="utf-8")
    broker.protect_in(data, *PROTECTED_FILES)
    assert broker.path_hit(records / "agent_sessions.json")
