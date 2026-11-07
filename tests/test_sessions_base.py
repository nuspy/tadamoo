"""The folder that holds the conversations' files: the default, the one chosen in Settings, and the records that stay protected wherever it is."""
import json

import pytest

from serve import userdata
from tadamoo.secrets_broker import PROTECTED_FILES, SecretsBroker


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    return tmp_path / "data"


def test_without_a_choice_the_base_is_the_journal_folder(data):
    assert userdata.chosen_sessions_base() is None
    assert userdata.sessions_base() == data / "journal"


def test_a_chosen_folder_is_kept_in_the_default_data_folder_and_can_be_forgotten(data, tmp_path):
    elsewhere = tmp_path / "elsewhere" / "chats"
    userdata.set_sessions_base(elsewhere)
    assert json.loads((data / "sessions-base.json").read_text(encoding="utf-8")) == {"path": str(elsewhere)}
    assert userdata.sessions_base() == elsewhere and userdata.chosen_sessions_base() == elsewhere
    userdata.set_sessions_base(None)
    assert userdata.sessions_base() == data / "journal"


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"path": ""}', '{"path": 4}', '{"other": "x"}'])
def test_a_broken_or_empty_file_means_no_choice(data, text):
    data.mkdir(parents=True)
    (data / "sessions-base.json").write_text(text, encoding="utf-8")
    assert userdata.chosen_sessions_base() is None


def test_the_conversation_records_are_protected_where_they_really_are(data, tmp_path):
    assert "journal/sessions/agent_sessions.json" in PROTECTED_FILES
    broker = SecretsBroker()
    records = data / "journal" / "sessions"
    records.mkdir(parents=True)
    (records / "agent_sessions.json").write_text("{}", encoding="utf-8")
    broker.protect_in(data, *PROTECTED_FILES)
    assert broker.path_hit(records / "agent_sessions.json")
