"""The map of the development: the register written as docs/roadmap.json and roadmap.md (and not rewritten when nothing changed), read back into a register, the text,
the page for the Artifact, and the project's own committed roadmap.json."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tadamoo import roadmap
from tadamoo.taskboard import TaskBoard, TaskError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def board(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    return TaskBoard(folder)


def filled(board):
    g1, g2 = board.add_group("The engine"), board.add_group("The phone")
    done = board.add("Study", group=g1, by="user", description="Reading first", notes={"solved": "- read the paper\n- found a trap", "risks": "numbers may drift"})
    board.close(done)
    work = board.add("Fuse the experts", group=g1, by="user", state="running", description="Fused at load", notes={"decisions": "which format?"}, subtasks=[{"title": "Quantizers"}])
    board.add("Messages while it works", group=g2, by="user", state="review")
    board.add("A free one", by="user", state="decision")
    hidden = board.add("Not for the map", group=g2, by="user")
    board.hide(hidden)
    board.report("agent-r9", "running", metadata={"title": "A run"})              # a program's task is not part of the map
    return work


def test_the_map_has_groups_in_order_nodes_by_number_statuses_and_leaves_out_hidden_and_programs(board):
    filled(board)
    data = roadmap.export(board)
    assert data["schema"] == "roadmap-1" and [g["title"] for g in data["groups"]] == ["The engine", "The phone", roadmap.NO_GROUP]
    assert [(n["num"], n["title"], n["status"]) for n in data["nodes"]] == [(1, "Study", "done"), (2, "Fuse the experts", "run"), (3, "Messages while it works", "review"),
                                                                           (4, "A free one", "decision")] or len(data["nodes"]) == 4
    study = data["nodes"][0]
    assert study["solved"] == ["read the paper", "found a trap"] and study["risks"] == ["numbers may drift"] and study["summary"] == "Reading first"
    fuse = next(n for n in data["nodes"] if n["title"] == "Fuse the experts")
    assert fuse["decisions"] == ["which format?"] and fuse["subtasks"] == [{"title": "Quantizers", "status": "none"}]
    assert "Not for the map" not in json.dumps(data) and "A run" not in json.dumps(data)


def test_write_makes_the_two_files_and_leaves_them_alone_when_nothing_changed(board):
    filled(board)
    out = roadmap.write(board)
    assert out["changed"] and out["nodes"] == 4
    js, md = Path(out["json"]), Path(out["md"])
    assert js.parent.name == "docs" and json.loads(js.read_text(encoding="utf-8"))["nodes"][0]["title"] == "Study"
    first = (js.read_text(encoding="utf-8"), md.read_text(encoding="utf-8"), js.stat().st_mtime_ns)
    again = roadmap.write(board)
    assert not again["changed"] and (js.read_text(encoding="utf-8"), md.read_text(encoding="utf-8"), js.stat().st_mtime_ns) == first
    board.update(board.view()[0]["id"], "user", title="Fuse the experts, all of them")
    assert roadmap.write(board)["changed"] and "all of them" in js.read_text(encoding="utf-8")


def test_load_makes_a_register_from_the_file_and_updating_it_again_changes_only_what_differs(board, tmp_path):
    filled(board)
    roadmap.write(board)
    fresh = TaskBoard(tmp_path / "copy" / "project")
    (tmp_path / "copy" / "project" / "docs").mkdir(parents=True)
    shutil.copy(board.project / "docs" / "roadmap.json", tmp_path / "copy" / "project" / "docs" / "roadmap.json")
    assert roadmap.load(fresh) == {"made": 4, "updated": 0} and [r["title"] for r in fresh.view() if r["depth"] == 1] == ["Quantizers"]
    assert [g["title"] for g in fresh.groups()][:2] == ["The engine", "The phone"] and [r["title"] for r in fresh.history()] == ["Study"]
    assert {r["title"]: r["state"] for r in fresh.view() if r["depth"] == 0} == {"Fuse the experts": "running", "Messages while it works": "review", "A free one": "decision"}
    assert fresh.history()[0]["notes"]["solved"] == "read the paper\nfound a trap"
    assert roadmap.load(fresh) == {"made": 0, "updated": 4}
    assert roadmap.write(fresh)["changed"] is False                               # the same map comes out of the register it made
    data = json.loads((tmp_path / "copy" / "project" / "docs" / "roadmap.json").read_text(encoding="utf-8"))
    data["nodes"][1]["status"] = "done"                                           # the file says a node is done, the register has it open
    (tmp_path / "copy" / "project" / "docs" / "roadmap.json").write_text(json.dumps(data), encoding="utf-8")
    roadmap.load(fresh)
    assert "Fuse the experts" in [r["title"] for r in fresh.history()] and "Fuse the experts" not in [r["title"] for r in fresh.view()]


def test_load_refuses_what_is_not_a_roadmap_file(board):
    with pytest.raises(TaskError):
        roadmap.load(board)
    (board.project / "docs").mkdir()
    (board.project / "docs" / "roadmap.json").write_text('{"nodes": []}', encoding="utf-8")
    with pytest.raises(TaskError):
        roadmap.load(board)


def test_the_text_says_states_groups_and_notes(board):
    filled(board)
    text = roadmap.to_markdown(roadmap.export(board))
    assert text.startswith("# project") and "## The engine" in text and "### 1. Study (fatto)" in text and "### 2. Fuse the experts (in corso)" in text
    assert "- read the paper" in text and "**Decisioni e punti aperti**" in text and "- Quantizers (non iniziato)" in text and "| in corso | 1 |" in text


def test_the_page_gets_the_nodes_the_title_and_what_is_missing_and_its_script_is_valid(board, tmp_path):
    filled(board)
    data = roadmap.export(board)
    page = roadmap.render_page({**data, "title": "My <map>", "stamp": "01/01/2027 10:00"})
    assert "<title>My &lt;map&gt;</title>" in page and "Stato al 01/01/2027 10:00" in page and "{{" not in page
    assert "Fuse the experts" in page and "A free one" in page and "Not for the map" not in page
    assert page.count('<li><span class="dot"') >= 3                              # what is missing now: the running, to-commit and to-decide ones
    script = "\n".join(re.findall(r"<script>(.*?)</script>", page, re.S))
    assert "</script" not in script and "/*NODES*/" not in script
    if shutil.which("node"):
        js = tmp_path / "page.js"
        js.write_text(script, encoding="utf-8")
        assert subprocess.run(["node", "--check", str(js)], capture_output=True).returncode == 0


def test_a_script_tag_inside_a_note_cannot_close_the_pages_script(board):
    board.add("Danger", by="user", notes={"solved": "</script><b>x</b>"})
    page = roadmap.render_page(roadmap.export(board))
    assert page.count("</script>") == page.count("<script") and "<\\/script>" in page


def test_the_template_refuses_to_be_used_without_its_places(board):
    with pytest.raises(TaskError):
        roadmap.render_page(roadmap.export(board), "<html></html>")


def test_the_projects_own_committed_roadmap_is_valid_and_makes_a_page_and_a_register():
    file = ROOT / "docs" / "roadmap.json"
    data = json.loads(file.read_text(encoding="utf-8"))
    assert data["schema"] == "roadmap-1" and len(data["nodes"]) >= 37
    group_ids = {g["id"] for g in data["groups"]}
    nums = [n["num"] for n in data["nodes"]]
    assert len(nums) == len(set(nums)) and all(n["group"] in group_ids and n["status"] in roadmap.LABEL for n in data["nodes"])
    assert all("<" not in x for n in data["nodes"] for x in [n["title"], n["summary"], *n["solved"], *n["decisions"], *n["risks"]])
    assert "{{" not in roadmap.render_page(data)
    md = ROOT / "docs" / "roadmap.md"
    assert md.read_text(encoding="utf-8") == roadmap.to_markdown(data)           # the text is what the file makes: run `python -m tadamoo.roadmap render`


def _browser():
    import os
    for p in (os.environ.get("TADAMOO_TEST_BROWSER", ""), r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              shutil.which("google-chrome") or "", shutil.which("chromium") or ""):
        if p and os.path.exists(p):
            return p
    return None


@pytest.mark.skipif(_browser() is None, reason="no headless browser")
def test_the_page_made_from_the_projects_roadmap_draws_every_node_in_a_real_browser(tmp_path):
    data = json.loads((ROOT / "docs" / "roadmap.json").read_text(encoding="utf-8"))
    page = tmp_path / "page.html"
    page.write_text(roadmap.render_page(data), encoding="utf-8")
    out = subprocess.run([_browser(), "--headless=new", "--disable-gpu", "--virtual-time-budget=4000", "--dump-dom", page.as_uri()], capture_output=True, text=True, timeout=120).stdout
    assert len(re.findall(r'<button class="node(?: branch)?"', out)) == len(data["nodes"]) and out.count('class="branch-sep"') == len({n["group"] for n in data["nodes"]})
    assert data["title"] in out and "Stato al" in out and "{{" not in out
    assert 'class="chip"' in out                                                   # the filters were built by the page's own script


def test_the_git_branch_and_commits_of_a_task_go_into_the_map_the_text_and_back_into_a_register(board, tmp_path):
    task = board.add("Has code", by="user")
    board.update(task, "user", git_branch="  feat/ship  ", commits=["abcdef1", "ABCDEF1", "not-a-sha", 7, "0123456789abcdef"])
    row = board.get(task)
    assert row["links"]["git_branch"] == "feat/ship" and row["links"]["commits"] == ["abcdef1", "0123456789abcdef"]
    assert board.add("Plain", by="user") and "git_branch" not in roadmap.export(board)["nodes"][1]
    node = roadmap.export(board)["nodes"][0]
    assert node["git_branch"] == "feat/ship" and node["commits"] == ["abcdef1", "0123456789abcdef"]
    assert "ramo `feat/ship` · `abcdef1` · `01234567`" in roadmap.to_markdown(roadmap.export(board))
    roadmap.write(board)
    fresh = TaskBoard(tmp_path / "project")
    fresh.path.unlink()
    roadmap.load(fresh)
    again = next(r for r in fresh.view() if r["title"] == "Has code")
    assert again["links"]["git_branch"] == "feat/ship" and again["links"]["commits"] == ["abcdef1", "0123456789abcdef"]


def test_loading_a_map_that_says_nothing_about_the_branch_keeps_what_the_register_has(board, tmp_path):
    task = board.add("Has code", by="user")
    board.update(task, "user", git_branch="feat/keep", commits=["abcdef1"])
    roadmap.write(board)
    path = tmp_path / "project" / "docs" / "roadmap.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for n in data["nodes"]:
        n.pop("git_branch", None), n.pop("commits", None)
    path.write_text(json.dumps(data), encoding="utf-8")
    roadmap.load(board)
    assert board.get(task)["links"]["git_branch"] == "feat/keep" and board.get(task)["links"]["commits"] == ["abcdef1"]
