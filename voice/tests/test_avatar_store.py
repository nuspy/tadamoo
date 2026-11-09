"""The avatars store: safe names, listing, sizes, the prepared index, deleting only what is inside the root."""
import json
import os

import pytest

from callbridge.avatar_store import AvatarStore, StoreError, slug_of, valid_clip, valid_name


@pytest.fixture
def store(tmp_path):
    return AvatarStore(tmp_path / "avatars")


def test_names_are_plain_words():
    for good in ("ada", "Ada_2", "a-b", "x" * 60):
        assert valid_name(good), good
    for bad in ("", ".", "..", "../x", "a/b", "a\b", "-a", "_a", "a b", "a.b", "x" * 61, None, 5, "ada\n", "C:evil", "a\x00"):
        assert not valid_name(bad), repr(bad)
    for route in ("wizard", "State", "list", "preview"):                       # the names of the service's routes
        assert not valid_name(route), route
    assert valid_clip("walk 1.mp4") and not valid_clip("../x.mp4") and not valid_clip("a.mp4.exe") and not valid_clip("a..b.mp4") and not valid_clip(".mp4")


def test_slug_of_a_display_name():
    assert slug_of("Ada Lovelace") == "Ada-Lovelace"
    assert slug_of("Pietro Perù") == "Pietro-Peru"
    assert slug_of("***") == ""
    assert len(slug_of("a" * 100)) == 60


def test_create_list_info_and_unique_names(store, tmp_path):
    assert store.names() == [] and store.listing() == {"avatars": [], "size_bytes": 0}
    folder = store.create("ada", "Ada L.")
    (folder / "clips" / "a.mp4").write_bytes(b"1234")
    (folder / "poster.jpg").write_bytes(b"12")
    info = store.info("ada")
    assert info["display_name"] == "Ada L." and info["poster"] and info["clips"] == ["a.mp4"] and not info["wav2lip"]
    assert info["size_bytes"] >= 6 and store.listing()["size_bytes"] == sum(r["size_bytes"] for r in store.listing()["avatars"])
    with pytest.raises(StoreError):
        store.create("ada")
    assert store.unique_name("Ada") == "Ada-2" or store.unique_name("ada") == "ada-2"
    with pytest.raises(StoreError):
        store.unique_name("???")


def test_a_name_that_is_not_plain_never_builds_a_path(store):
    for bad in ("../x", "a/b", "", "a b"):
        with pytest.raises(StoreError):
            store.path(bad)
        with pytest.raises(StoreError):
            store.delete(bad)
    store.create("ada")
    for bad in ("../x.mp4", "a/b.mp4", "x.txt"):
        with pytest.raises(StoreError):
            store.clip_path("ada", bad)


def test_prepared_index_and_voice_binding(store):
    store.create("ada")
    store.write_metadata("ada", voice={"engine": "xtts", "voice": "ada"})
    assert store.voice_of("ada") == {"engine": "xtts", "voice": "ada"}
    store.write_metadata("ada", voice={"engine": 5})
    assert store.voice_of("ada") == {}
    (store.path("ada") / "clips" / "a.mp4").write_bytes(b"1")
    store.record_prepared("ada", "a.mp4", 30)
    assert store.prepared("ada")["a.mp4"]["frames"] == 30 and store.info("ada")["prepared"] == ["a.mp4"]
    store.forget_prepared("ada", "a.mp4")
    assert store.prepared("ada") == {}
    assert store.prepared_dir("ada", "a.mp4").name == "a__h480" and store.prepared_dir("ada", "a.mp4").parent.name == "prepared"


def test_delete_removes_only_the_avatar_and_reports_the_size(store, tmp_path):
    store.create("ada")
    store.create("bob")
    (store.path("ada") / "poster.jpg").write_bytes(b"x" * 100)
    freed = store.delete("ada")
    assert freed >= 100 and store.names() == ["bob"]
    with pytest.raises(StoreError):
        store.delete("ada")
    assert (tmp_path / "avatars" / "bob").is_dir()


def test_a_link_out_of_the_root_is_not_followed(store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("keep", encoding="utf-8")
    store.root.mkdir(parents=True)
    link = store.root / "evil"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symbolic links here")
    assert store.names() == []
    with pytest.raises(StoreError):
        store.delete("evil")
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "keep"


def test_a_damaged_metadata_file_reads_as_empty(store):
    store.create("ada")
    (store.path("ada") / "avatar.json").write_text("{not json", encoding="utf-8")
    assert store.metadata("ada") == {} and store.info("ada")["display_name"] == "ada"
    (store.path("ada") / "avatar.json").write_text(json.dumps([1]), encoding="utf-8")
    assert store.metadata("ada") == {}
