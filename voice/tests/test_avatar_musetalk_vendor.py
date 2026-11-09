"""The vendored MuseTalk (voice/avatar/musetalk): every file is the one the table of its VENDORED.md names (SHA-256 of the committed bytes, line endings read as LF),
nothing else is there, no weight or media was vendored, the licence is the one MuseTalk ships, and the scripts that build shell commands are not run by the app."""
import ast
import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "voice" / "avatar" / "musetalk"
COMMIT = "0a89dec45a0192b824e3cf4daf96c239440c5ed8"
BINARY = (".onnx", ".pth", ".pt", ".tar", ".bin", ".safetensors", ".mp4", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".npy", ".ckpt")
SHELL_SCRIPTS = ("scripts/inference.py", "scripts/realtime_inference.py")


def digest(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    if path.suffix != ".npz":
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest(), len(data)


@pytest.fixture(scope="module")
def table():
    text = (VENDOR / "VENDORED.md").read_text(encoding="utf-8")
    return text, {m.group(1): (m.group(2), int(m.group(3))) for m in re.finditer(r"^\| (\S+) \| ([0-9a-f]{64}) \| (\d+) \|$", text, re.M)}


def test_every_file_is_the_one_the_table_names_and_nothing_else_is_there(table):
    text, rows = table
    assert rows and f"commit:   {COMMIT}" in text
    on_disk = {p.relative_to(VENDOR).as_posix() for p in VENDOR.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    assert on_disk - {"VENDORED.md"} == set(rows)
    for rel, (sha, size) in rows.items():
        assert digest(VENDOR / rel) == (sha, size), rel
    assert f"Size: {sum(size for _, size in rows.values())} bytes in {len(rows)} files." in text


def test_no_weight_or_media_is_vendored(table):
    _, rows = table
    for rel in rows:
        assert not rel.lower().endswith(BINARY), rel
    assert not (VENDOR / "models").exists() and not (VENDOR / "assets").exists() and not (VENDOR / "data").exists()


def test_the_licence_is_the_mit_one_musetalk_ships():
    text = (VENDOR / "LICENSE").read_text(encoding="utf-8")
    assert text.lstrip().startswith("MIT License") and "Tencent Music Entertainment Group" in text


def test_the_python_files_parse():
    for p in VENDOR.rglob("*.py"):
        ast.parse(p.read_text(encoding="utf-8"), str(p))


def test_the_os_system_calls_are_only_in_the_two_scripts_and_each_says_the_app_does_not_run_it():
    found = {p.relative_to(VENDOR).as_posix() for p in VENDOR.rglob("*.py") if re.search(r"\bos\.system\(|\bos\.popen\(", p.read_text(encoding="utf-8"))}
    assert found == set(SHELL_SCRIPTS)
    for rel in SHELL_SCRIPTS:
        assert (VENDOR / rel).read_text(encoding="utf-8").startswith("# The app never runs this script")


def test_nothing_outside_the_vendored_folder_runs_those_scripts():
    pattern = re.compile(r"realtime_inference|scripts[./]inference|musetalk[./]scripts|from scripts\b|import scripts\b")
    for p in list((ROOT / "voice").rglob("*.py")) + list((ROOT / "serve").rglob("*.py")) + list((ROOT / "tadamoo").rglob("*.py")):
        if VENDOR in p.parents or p == Path(__file__).resolve():
            continue
        assert not pattern.search(p.read_text(encoding="utf-8", errors="replace")), p
