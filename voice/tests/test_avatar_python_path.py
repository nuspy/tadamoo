"""The engines Python chosen in a page: only a Python interpreter on a disk of this PC is kept, and a kept path that no longer passes is not
started. Files only: nothing is run."""
import json
import os
import sys

import pytest

from voice.avatar_engines import pack, personalised
from voice.avatar_engines.python_path import checked_python


def interpreter(folder, name="python.exe"):
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / name
    f.write_bytes(b"MZ")
    return str(f)


def test_a_local_python_passes_and_empty_forgets(tmp_path):
    for name in ("python.exe", "python3.11", "python3", "py.exe", "PYTHON.EXE"):
        path = interpreter(tmp_path / name.replace(".", "_"), name)
        assert checked_python(f' "{path}" ') == path
    assert checked_python("") == "" and checked_python('  ""  ') == ""
    assert checked_python(sys.executable) == sys.executable


def test_network_paths_other_files_and_missing_files_are_refused(tmp_path):
    other = interpreter(tmp_path / "x", "calc.exe")
    for bad, word in (("\\\\attacker\\share\\python.exe", "network"), ("//attacker/share/python.exe", "network"), ("\\\\?\\C:\\py\\python.exe", "network"),
                      ("relative/python.exe", "full path"), (other, "Python interpreter"), (str(tmp_path / "missing" / "python.exe"), "exists"),
                      (str(tmp_path / "x" / "python.exe.bat"), "Python interpreter")):
        with pytest.raises(ValueError, match=word):
            checked_python(bad)


@pytest.mark.skipif(os.name == "nt", reason="links need a privilege on Windows")
def test_a_link_to_another_program_or_a_loop_is_refused(tmp_path):
    target = interpreter(tmp_path / "bin", "bash")
    (tmp_path / "py").mkdir()
    os.symlink(target, tmp_path / "py" / "python")
    with pytest.raises(ValueError):
        checked_python(str(tmp_path / "py" / "python"))
    os.symlink(tmp_path / "py" / "python3", tmp_path / "py" / "loop")
    os.symlink(tmp_path / "py" / "loop", tmp_path / "py" / "python3")
    with pytest.raises(ValueError, match="exists"):
        checked_python(str(tmp_path / "py" / "python3"))


@pytest.mark.skipif(os.name == "nt", reason="Windows folders are not read for who may write in them")
def test_a_folder_anyone_may_write_in_is_refused(tmp_path):
    shared = tmp_path / "shared"
    path = interpreter(shared, "python3")
    shared.chmod(0o777)
    with pytest.raises(ValueError, match="anyone"):
        checked_python(path)


def test_both_stores_keep_only_a_checked_path_and_drop_one_kept_before(tmp_path):
    base = tmp_path / "models"
    good = interpreter(tmp_path / "env")
    with pytest.raises(ValueError):
        pack.set_python("\\\\attacker\\share\\python.exe", str(base))
    with pytest.raises(ValueError):
        personalised.save_python(interpreter(tmp_path / "x", "evil.exe"), str(base))
    assert pack.set_python(good, str(base)) == good and pack.stored_python(str(base)) == good
    assert personalised.save_python(good, str(base)) == good and personalised.saved_python(str(base)) == good
    planted = interpreter(tmp_path / "x", "payload.exe")                                     # written by hand or by an older version
    (base / "avatar-engines" / "python.json").write_text(json.dumps({"python": planted}), encoding="utf-8")
    assert pack.stored_python(str(base)) == "" and personalised.saved_python(str(base)) == ""
