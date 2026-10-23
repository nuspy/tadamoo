from pathlib import Path

import pytest

from harness.tools.filesystem import MAX_LISTED, WorkspaceSandbox


@pytest.fixture
def sb(tmp_path):
    return WorkspaceSandbox(tmp_path / "ws")


def test_line_endings_are_preserved_on_write_and_edit(sb):
    sb.write_file("crlf.txt", "a\r\nb\r\nc\r\n")
    assert (sb.root / "crlf.txt").read_bytes() == b"a\r\nb\r\nc\r\n"
    sb.edit_file("crlf.txt", "b\nc", "B\nC")                         # the model writes LF
    assert (sb.root / "crlf.txt").read_bytes() == b"a\r\nB\r\nC\r\n"
    sb.write_file("lf.txt", "x\ny\n")
    assert (sb.root / "lf.txt").read_bytes() == b"x\ny\n"
    assert sb.read_file("crlf.txt") == "a\r\nB\r\nC\r\n"


def test_edit_rejects_an_ambiguous_or_empty_target(sb):
    sb.write_file("a.py", "x = 1\nx = 1\ny = 2\n")
    with pytest.raises(ValueError, match="appears 2 times"):
        sb.edit_file("a.py", "x = 1", "x = 2")
    with pytest.raises(ValueError, match="must not be empty"):
        sb.edit_file("a.py", "", "z")
    with pytest.raises(ValueError, match="not found"):
        sb.edit_file("a.py", "nope", "z")
    assert (sb.root / "a.py").read_text(encoding="utf-8") == "x = 1\nx = 1\ny = 2\n"
    sb.edit_file("a.py", "x = 1\ny", "x = 1\nY")
    assert "Y = 2" in (sb.root / "a.py").read_text(encoding="utf-8")


def test_binary_and_large_files_are_handled(sb):
    (sb.root / "bin.dat").write_bytes(b"\x00\x01\x02" * 100)
    with pytest.raises(ValueError, match="binary"):
        sb.read_file("bin.dat")
    (sb.root / "big.txt").write_text("é" * 5000, encoding="utf-8")
    out = sb.read_file("big.txt", max_bytes=1001)
    assert out.startswith("é") and "only the first 1001 bytes" in out and "�" not in out


def test_a_failed_write_leaves_the_old_file_and_no_temp_files(sb, monkeypatch):
    sb.write_file("keep.txt", "original")
    import harness.tools.filesystem as fsmod
    monkeypatch.setattr(fsmod.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        sb.write_file("keep.txt", "new")
    monkeypatch.undo()
    assert (sb.root / "keep.txt").read_text(encoding="utf-8") == "original"
    assert not list(sb.root.glob(".basalt-*"))


def test_changes_keep_the_text_before_and_after_for_diff_views(sb):
    sb.write_file("a.txt", "one\n")
    sb.write_file("a.txt", "two\n")
    first, second = sb.changes
    assert (first["kind"], first["old_text"], first["new_text"]) == ("added", "", "one\n")
    assert (second["kind"], second["old_text"], second["new_text"]) == ("modified", "one\n", "two\n")
    assert "-one" in second["diff"] and "+two" in second["diff"]
    sb.write_file("huge.txt", "x" * 300_000)
    assert sb.changes[-1]["new_text"] is None and sb.changes[-1]["added"] == 1


def test_directories_and_escapes(sb, tmp_path):
    sb.write_file("sub/dir/f.txt", "x")
    with pytest.raises(IsADirectoryError):
        sb.write_file("sub", "x")
    with pytest.raises(PermissionError):
        sb.write_file("..\\escape.txt", "x")
    for i in range(MAX_LISTED + 5):
        (sb.root / f"f{i:04}.txt").write_text("", encoding="utf-8")
    listing = sb.list_directory(".")
    assert len(listing) == MAX_LISTED + 1 and "more than" in listing[-1]["name"]


def test_symlinks_cannot_lead_out(sb, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = sb.root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this system")
    with pytest.raises(PermissionError):
        sb.read_file("link/secret.txt")
