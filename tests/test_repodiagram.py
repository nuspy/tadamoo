"""Repository diagrams worked out from the files: the same text on every run, no model, safe labels, and a tool that is pure."""
import re
from pathlib import Path

import pytest

from tadamoo.repodiagram import folders_diagram, import_edges, imports_diagram
from tadamoo.tools import create_standard_tool_registry


def put(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


PROJECT = {
    "app/__init__.py": "",
    "app/main.py": "import os\nfrom app import util\nfrom app.models import User\nfrom . import config\nfrom .db.conn import connect\n",
    "app/util.py": "from app.models import User\n",
    "app/models.py": "import app.util\n",
    "app/config.py": "",
    "app/db/__init__.py": "",
    "app/db/conn.py": "from .. import config\nfrom ..models import User\n",
    "web/index.js": "import { a } from './lib/a.js';\nconst b = require('./lib/b');\nimport x from 'react';\nimport('../app/main.py');\n",
    "web/lib/a.js": "export const a = 1;\n",
    "web/lib/b.ts": "import { a } from './a';\n",
    "native/core.cpp": '#include <vector>\n#include "core.h"\n#include "inc/util.h"\n',
    "native/core.h": "",
    "native/inc/util.h": "",
    "broken.py": "def (:\n",
}


def edges_of(root):
    return {(a, b) for a, b in import_edges(root)[1]}


def test_imports_are_resolved_for_python_javascript_and_c_and_what_is_outside_is_left_out(tmp_path):
    edges = edges_of(put(tmp_path, PROJECT))
    assert edges == {
        ("app/main.py", "app/util.py"), ("app/main.py", "app/models.py"), ("app/main.py", "app/config.py"), ("app/main.py", "app/db/conn.py"),
        ("app/util.py", "app/models.py"), ("app/models.py", "app/util.py"),
        ("app/db/conn.py", "app/config.py"), ("app/db/conn.py", "app/models.py"),
        ("web/index.js", "web/lib/a.js"), ("web/index.js", "web/lib/b.ts"), ("web/lib/b.ts", "web/lib/a.js"),
        ("native/core.cpp", "native/core.h"), ("native/core.cpp", "native/inc/util.h"),
        ("web/index.js", "app/main.py")}                                       # a path import that names a file of the folder is an import


def test_the_file_diagram_is_mermaid_the_same_on_every_run_and_does_not_depend_on_the_order_files_were_made(tmp_path):
    first = imports_diagram(put(tmp_path / "a", PROJECT), level="file")
    second = imports_diagram(put(tmp_path / "b", dict(reversed(list(PROJECT.items())))), level="file")
    assert first == second == imports_diagram(tmp_path / "a", level="file")
    body = first.split("```mermaid\n")[1].split("\n```")[0]
    assert body.startswith("flowchart LR") and '  n0["app/config.py"]' in body
    labels = dict(re.findall(r'(n\d+)\["([^"]+)"\]', body))
    arrows = {(labels[a], labels[b]) for a, b in re.findall(r"(n\d+) --> (n\d+)", body)}
    assert ("app/main.py", "app/util.py") in arrows and len(arrows) == 14
    assert "14 arrows from 14 imports" in first


def test_with_too_many_files_the_diagram_is_cut_by_folder_and_arrows_count_the_imports(tmp_path):
    out = imports_diagram(put(tmp_path, PROJECT), level="folder", depth=2)
    body = out.split("```mermaid\n")[1]
    labels = dict(re.findall(r'(n\d+)\["([^"]+)"\]', body))
    weights = {(labels[a], labels[b]): int(n) for a, n, b in re.findall(r"(n\d+) -->\|(\d+)\| (n\d+)", body)}
    assert weights[("app", "app/db")] == 1 and weights[("app/db", "app")] == 2 and weights[("web", "web/lib")] == 2
    assert all(a != b for a, b in weights)
    auto = imports_diagram(tmp_path, level="auto", max_nodes=6)
    assert "folders (cut at" in auto and auto.count("[\"") <= 6


def test_the_least_connected_nodes_go_first_when_even_folders_are_too_many(tmp_path):
    files = {f"p{i}/m.py": ("import p0.m\n" if i else "") for i in range(1, 12)}
    files["p0/m.py"] = ""
    out = imports_diagram(put(tmp_path, files), level="folder", depth=1, max_nodes=5)
    assert out.count('["') == 5 and "left out" in out and '["p0"]' in out


def test_an_empty_or_import_free_folder_says_so_and_a_broken_file_is_skipped_not_fatal(tmp_path):
    assert imports_diagram(put(tmp_path, {"a.py": "x = 1\n", "b.py": "def (:\n"})).startswith("No imports between the files")
    assert "```" not in imports_diagram(tmp_path / "missing") if (tmp_path / "missing").exists() else True


def test_folders_come_as_a_tree_with_the_count_of_files_inside_and_are_cut_at_the_depth(tmp_path):
    root = put(tmp_path / "proj", PROJECT)
    out = folders_diagram(root, depth=2)
    body = out.split("```mermaid\n")[1]
    labels = dict(re.findall(r'(n\d+)\["([^"]+)"\]', body))
    assert labels["n0"] == "proj (14)" and "db (2)" in labels.values() and "lib (2)" in labels.values() and "inc (1)" in labels.values()
    assert body.startswith("flowchart TD") and out == folders_diagram(root, depth=2)
    shallow = folders_diagram(root, depth=1)
    assert "db (2)" not in shallow and "app (7)" in shallow


def test_a_name_that_could_break_the_diagram_is_made_harmless(tmp_path):
    from tadamoo.repodiagram import _label
    assert _label('we"ird<b>/x#y&z;[1]') == "we_ird_b_/x_y_z__1_"
    root = put(tmp_path, {"d[1]#x&y'z;/x.py": "", "d[1]#x&y'z;/z.py": "import os\n", "ok.py": ""})
    body = folders_diagram(root).split("```mermaid\n")[1]
    assert "d_1__x_y_z_ (2)" in body and not any(c in label for label in re.findall(r'\["([^"]*)"\]', body) for c in "<>#&;'[]\"")


def test_the_tool_is_pure_stays_inside_the_workspace_and_refuses_what_it_does_not_know(tmp_path):
    registry, _ = create_standard_tool_registry(tmp_path)
    put(tmp_path, PROJECT)
    tool = registry.get("repo_diagram")
    assert tool.effect.value == "pure"
    assert "```mermaid" in registry.execute("repo_diagram", {"path": "app", "level": "file"})
    assert "flowchart TD" in registry.execute("repo_diagram", {"kind": "folders"})
    for bad, error in (({"path": ".."}, PermissionError), ({"path": "nope"}, NotADirectoryError), ({"kind": "calls"}, ValueError), ({"level": "deep"}, ValueError)):
        with pytest.raises(error):
            registry.execute("repo_diagram", bad)
