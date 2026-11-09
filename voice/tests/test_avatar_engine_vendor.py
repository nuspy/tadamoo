"""The vendored upstream code of Ultralight-Digital-Human and FeatherTalk: every file is the one the table of its VENDORED.md names, with that SHA-256 (the blob of the
pinned commit, unedited except for the patches that file lists, each of which undoes to the upstream blob), nothing else is there, no weight or media was vendored, the
licence files are what the decision says, and the scripts parse."""
import ast
import hashlib
import re
from pathlib import Path

import pytest

from voice.avatar_engines.personalised import VENDOR

COMMITS = {"ultralight": "5aa8b5a8aae4b003b7de5750abeac5343a7b38e0", "feathertalk": "7a8bb0439eb5da5e10601168df329a7052b7edf3"}
ORIGINS = {"ultralight": "https://github.com/anliyuan/Ultralight-Digital-Human", "feathertalk": "https://github.com/anliyuan/FeatherTalk"}
NOTES = {"VENDORED.md", "LICENSE-NOTE.md"}
BINARY = (".onnx", ".pth", ".pt", ".tar", ".npy", ".bin", ".safetensors", ".mnn", ".mp4", ".jpg", ".jpeg", ".png", ".gif", ".zip")
SIZE_LIMIT = 400_000
# the patched files: {engine: {path: (SHA-256 and size of the upstream blob, the line removed and the line it followed)}}
PATCHES = {"ultralight": {"data_utils/get_landmark.py": ("cbac181a5f49a3b7c4efc62374ab463f3e724bfcfcd8e0135435e6617c9b350e", 3848, b"import argparse\n", b"from os import wait3\n")}}


def table(name):
    text = (VENDOR / name / "VENDORED.md").read_text(encoding="utf-8")
    return text, {m.group(1): (m.group(2), int(m.group(3))) for m in re.finditer(r"^\| (\S+) \| ([0-9a-f]{64}) \| (\d+) \|$", text, re.M)}


@pytest.mark.parametrize("name", ["ultralight", "feathertalk"])
def test_every_file_is_the_one_the_table_names_and_nothing_else_is_there(name):
    text, rows = table(name)
    patched = PATCHES.get(name, {})
    assert rows and f"- Commit: {COMMITS[name]}" in text and f"- Origin: {ORIGINS[name]}" in text
    assert ("- Patches: none." in text) == (not patched) and ("## Patches" in text) == bool(patched)
    on_disk = {p.relative_to(VENDOR / name).as_posix() for p in (VENDOR / name).rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    assert on_disk - NOTES == set(rows)
    for rel, (sha, size) in rows.items():
        data = (VENDOR / name / rel).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha and len(data) == size and b"\r" not in data, rel
    assert sum(size for _, size in rows.values()) < SIZE_LIMIT
    assert f"- Size: {sum(size for _, size in rows.values())} bytes in {len(rows)} files." in text


@pytest.mark.parametrize("name", ["ultralight"])
def test_a_patched_file_says_what_changed_and_undoes_to_the_upstream_blob(name):
    text, rows = table(name)
    for rel, (upstream, upstream_size, after, removed) in PATCHES[name].items():
        assert f"`{rel}`" in text and upstream in text and f"{upstream_size} bytes" in text and upstream != rows[rel][0]
        here = (VENDOR / name / rel).read_bytes()
        assert removed.strip().decode() in text and removed not in here
        assert here.count(after) == 1
        undone = here.replace(after, after + removed, 1)
        assert hashlib.sha256(undone).hexdigest() == upstream and len(undone) == upstream_size


def test_nothing_vendored_imports_a_name_only_linux_has():
    """os.wait3 and its kin do not exist on Windows: no vendored script may import them from os or call them."""
    names = r"wait3|wait4|fork|forkpty|killpg|getpgid|setsid|geteuid|getuid"
    posix_only = re.compile(rf"\bfrom os import [^\n]*\b({names})\b|\bos\.({names})\b|\bimport (fcntl|termios|pwd|grp)\b|\bsignal\.SIG(KILL|STOP|HUP)\b")
    for p in VENDOR.rglob("*.py"):
        assert not posix_only.search(p.read_text(encoding="utf-8")), p


@pytest.mark.parametrize("name", ["ultralight", "feathertalk"])
def test_no_weight_or_media_is_vendored_and_no_agpl_code_is(name):
    for p in (VENDOR / name).rglob("*"):
        assert p.suffix.lower() not in BINARY, p
    for p in (VENDOR / name).rglob("*.py"):
        source = p.read_text(encoding="utf-8")
        ast.parse(source, str(p))
        assert "ultralytics" not in source, p


def test_feathertalk_has_its_apache_licence_file_unchanged():
    text = (VENDOR / "feathertalk" / "LICENSE").read_bytes()
    assert hashlib.sha256(text).hexdigest() == "5c9817c129b98e7bb966bca028c43c19107102ef8e03fe799bffb4354f4ef015" and text.startswith(b"                                 Apache License")


def test_ultralight_has_no_licence_file_only_the_note_the_decision_asks_for():
    assert not (VENDOR / "ultralight" / "LICENSE").exists()
    note = (VENDOR / "ultralight" / "LICENSE-NOTE.md").read_text(encoding="utf-8")
    assert ("Licence declared by the README badge (Apache 2.0), no LICENSE file in the repository: to be confirmed with the author before distribution to others." in note)


def test_the_weights_the_scripts_open_are_not_here_and_are_catalog_entries():
    from serve import voice_catalog as vc
    for rel in ("data_utils/scrfd_2.5g_kps.onnx", "data_utils/checkpoint_epoch_335.pth.tar", "data_utils/encoder.onnx"):
        assert not (VENDOR / "ultralight" / rel).exists() and not (VENDOR / "feathertalk" / rel).exists()
    assert not (VENDOR / "feathertalk" / "feather_hubert.pth").exists()
    assert vc.find("ultralight-code") is None and vc.find("feathertalk-code") is None                            # the code is vendored: no entry for it
    landmarks = {s["name"]: s["sha256"] for s in vc.find("engine-landmarks")["sources"]}
    assert landmarks == {"scrfd_2.5g_kps.onnx": "32d20c77b9e2dc1d07e94c2ab9d25bdd5cd05eddbe0b46e7b38e7a1eca22e99a",
                         "checkpoint_epoch_335.pth.tar": "bada866661ad5fa1080a085f51fe9c016c69958c406951afa4afc7840f856de0"}
