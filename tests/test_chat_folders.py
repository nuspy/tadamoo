"""Chat's own folders (serve/chat_folders.py): CRUD on the list, and a conversation's own files landing inside its folder's
subfolder instead of the chat base once it names one that exists."""
import pytest

from serve.agent_sessions import SessionManager
from serve.chat_folders import ChatFolderStore, FolderError


@pytest.fixture
def store(tmp_path):
    return ChatFolderStore(tmp_path / "chat-folders.json")


def test_a_fresh_store_is_empty_and_survives_a_reload(store):
    assert store.list() == []
    row = store.add("Work")
    assert store.list() == [row]
    assert ChatFolderStore(store.path).list() == [row]


def test_a_folder_needs_a_name_to_add_or_rename(store):
    with pytest.raises(FolderError):
        store.add("")
    row = store.add("Work")
    with pytest.raises(FolderError):
        store.rename(row["id"], "   ")


def test_renaming_and_removing_an_unknown_folder_is_refused(store):
    with pytest.raises(FolderError):
        store.rename("bogus", "x")
    with pytest.raises(FolderError):
        store.remove("bogus")


def test_rename_and_remove_work_on_a_real_folder(store):
    row = store.add("Work")
    renamed = store.rename(row["id"], "Side projects")
    assert renamed["name"] == "Side projects" and store.list()[0]["name"] == "Side projects"
    assert store.known(row["id"]) is True
    assert store.remove(row["id"]) is True
    assert store.list() == [] and store.known(row["id"]) is False


def test_a_conversation_in_a_known_folder_keeps_its_files_there(tmp_path):
    folders = ChatFolderStore(tmp_path / "chat-folders.json")
    row = folders.add("Work")
    mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", chat_folders=folders, command=None, default_workspace=tmp_path / "chat")
    rec = mgr.create("", folder_id=row["id"])
    assert mgr.history_dir(rec["id"]) == mgr.chat_folder_dir(row["id"]) / "sessions" / rec["id"]
    assert mgr.history_dir(rec["id"]) != mgr.chat_directory / rec["id"]


def test_creating_a_conversation_in_an_unknown_folder_is_refused(tmp_path):
    folders = ChatFolderStore(tmp_path / "chat-folders.json")
    mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", chat_folders=folders, command=None, default_workspace=tmp_path / "chat")
    from serve.agent_runs import RunError
    with pytest.raises(RunError):
        mgr.create("", folder_id="bogus")


def test_a_folder_id_on_an_agent_coding_conversation_is_ignored(tmp_path):
    """folder_id only means something for a Chat conversation (no real workspace): one tied to a real project ignores it."""
    folders = ChatFolderStore(tmp_path / "chat-folders.json")
    row = folders.add("Work")
    mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", chat_folders=folders, command=None, default_workspace=tmp_path / "chat")
    other = tmp_path / "project"
    other.mkdir()
    rec = mgr.create(str(other), folder_id=row["id"])
    assert mgr.history_dir(rec["id"]) == mgr.agent_directory / rec["id"]


def test_deleting_a_folder_deletes_only_its_own_sessions_and_their_folders(tmp_path):
    folders = ChatFolderStore(tmp_path / "chat-folders.json")
    row = folders.add("Work")
    mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", chat_folders=folders, command=None, default_workspace=tmp_path / "chat")
    mine = mgr.create("", folder_id=row["id"])["id"]
    loose = mgr.create("")["id"]
    mine_folder = mgr.history_dir(mine)
    mine_folder.mkdir(parents=True, exist_ok=True)
    (mine_folder / "history.json").write_text("{}", encoding="utf-8")
    deleted = mgr.delete_chat_folder_sessions(row["id"])
    assert deleted == [mine]
    assert mgr.get(loose)["id"] == loose                           # the conversation outside the folder is untouched
    assert not mine_folder.exists()
    folder_dir = mgr.chat_folder_dir(row["id"])
    import shutil
    shutil.rmtree(folder_dir, ignore_errors=True)
    assert not folder_dir.exists()
