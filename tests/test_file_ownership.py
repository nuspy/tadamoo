"""A shared file has at most one owner: another conversation's write or edit is refused and pointed at session_send, except a file
in 'append' mode, which any conversation may add whole lines to under a cross-process lock (tadamoo/file_owners.py)."""
import json
import threading
import time

import pytest

from serve.file_owners import FileOwners, OwnersError
from tadamoo.file_owners import FileOwnership
from tadamoo.tools.filesystem import WorkspaceSandbox


@pytest.fixture
def sb(tmp_path):
    sandbox = WorkspaceSandbox(tmp_path / "ws")
    sandbox.own_ref = "me"
    return sandbox, tmp_path


def owned(sb, path, owner, mode="owner"):
    sandbox, tmp_path = sb
    store = tmp_path / "owners.json"
    FileOwners(store).set(str((sandbox.root / path).resolve()), owner, mode)
    sandbox.file_owners = FileOwnership(store)
    return store


def test_the_owner_itself_writes_and_edits_freely(sb):
    sandbox, _ = sb
    sandbox.write_file("log.md", "first\n")
    owned(sb, "log.md", "me")
    assert sandbox.write_file("log.md", "second\n")["status"] == "success"
    assert sandbox.edit_file("log.md", "second", "third")["file"] == "log.md"


def test_another_conversation_is_refused_and_pointed_at_session_send(sb):
    sandbox, _ = sb
    sandbox.write_file("log.md", "first\n")
    owned(sb, "log.md", "boss")
    out = sandbox.write_file("log.md", "second\n")
    assert out["status"] == "refused" and "session_send" in out["message"] and out["owner"] == "boss"
    out = sandbox.edit_file("log.md", "first", "changed")
    assert out["status"] == "refused" and out["mode"] == "owner"
    assert (sandbox.root / "log.md").read_text() == "first\n"               # nothing was touched


def test_a_file_nobody_owns_is_written_as_usual(sb):
    sandbox, _ = sb
    assert sandbox.write_file("free.md", "x\n")["status"] == "success"


def test_append_mode_adds_lines_without_a_whole_file_write(sb):
    sandbox, _ = sb
    sandbox.write_file("shared.log", "first\n")
    owned(sb, "shared.log", "boss", mode="append")
    out = sandbox.write_file("shared.log", "second line", append=True)
    assert out["status"] == "success" and out["kind"] == "appended" and out["added"] == 1
    assert (sandbox.root / "shared.log").read_text() == "first\nsecond line\n"
    sandbox.write_file("shared.log", "third\nfourth", append=True)
    assert (sandbox.root / "shared.log").read_text() == "first\nsecond line\nthird\nfourth\n"


def test_append_mode_still_refuses_a_whole_file_write(sb):
    sandbox, _ = sb
    sandbox.write_file("shared.log", "first\n")
    owned(sb, "shared.log", "boss", mode="append")
    out = sandbox.write_file("shared.log", "replaced\n")
    assert out["status"] == "refused" and "append=true" in out["message"]
    assert (sandbox.root / "shared.log").read_text() == "first\n"
    out = sandbox.edit_file("shared.log", "first", "changed")
    assert out["status"] == "refused"


def test_append_on_a_file_in_owner_mode_is_still_refused(sb):
    sandbox, _ = sb
    sandbox.write_file("log.md", "first\n")
    owned(sb, "log.md", "boss", mode="owner")
    out = sandbox.write_file("log.md", "second", append=True)
    assert out["status"] == "refused" and out["mode"] == "owner"


def test_two_conversations_appending_at_once_do_not_interleave_mid_line(tmp_path):
    store = tmp_path / "owners.json"
    target = tmp_path / "ws" / "shared.log"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    FileOwners(store).set(str(target.resolve()), "boss", "append")
    ownership = FileOwnership(store)

    def writer(tag):
        sb = WorkspaceSandbox(tmp_path / "ws")
        sb.own_ref = tag
        sb.file_owners = ownership
        for i in range(20):
            sb.write_file("shared.log", f"{tag}-{i}" + "x" * 50, append=True)

    threads = [threading.Thread(target=writer, args=(tag,)) for tag in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 60 and all(line.startswith(("a-", "b-", "c-")) for line in lines)


def test_server_side_store_resolves_a_path_to_its_owner_and_can_be_changed(tmp_path):
    target = tmp_path / "ws" / "log.md"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    store = FileOwners(tmp_path / "owners.json")
    entry = store.set(str(target), "aa11bb22", "append")
    assert entry["owner"] == "aa11bb22" and entry["mode"] == "append"
    assert FileOwnership(tmp_path / "owners.json").owner_of(target) == ("aa11bb22", "append")
    assert FileOwnership(tmp_path / "owners.json").owner_of(str(target).upper()) == ("aa11bb22", "append")   # case and path form do not matter
    assert store.remove(str(target)) is True and store.remove(str(target)) is False
    assert FileOwnership(tmp_path / "owners.json").owner_of(target) is None


def test_the_server_side_store_validates_what_it_is_given(tmp_path):
    store = FileOwners(tmp_path / "owners.json")
    with pytest.raises(OwnersError) as e:
        store.set("", "boss")
    assert e.value.field == "path"
    with pytest.raises(OwnersError) as e:
        store.set("x", "")
    assert e.value.field == "owner"
    with pytest.raises(OwnersError) as e:
        store.set("x", "boss", "delete")
    assert e.value.field == "mode"


def test_a_missing_or_damaged_owners_file_means_nobody_owns_anything(tmp_path, sb):
    sandbox, _ = sb
    sandbox.file_owners = FileOwnership(tmp_path / "no_such_file.json")
    sandbox.write_file("a.md", "x\n")
    assert sandbox.write_file("a.md", "y\n")["status"] == "success"
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    sandbox.file_owners = FileOwnership(bad)
    assert sandbox.write_file("a.md", "z\n")["status"] == "success"
