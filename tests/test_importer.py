"""Import, re-import and delete of skills, plugins, MCP servers and plans: always on copies in the global space, never on the source."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from tadamoo import capabilities as cap
from tadamoo.importer import Importer, Problem, Spaces
from tadamoo.importer.common import scan_tree
from tadamoo.personal_skills.store import PersonalSkills
from tadamoo.planner import PlanStore


def digest_dir(path: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(Path(path).rglob("*")):
        h.update(str(p.relative_to(path)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_skill(base: Path, name: str, body: str = "Do the thing.", description: str = "Reads a thing.") -> Path:
    write(base / name / "SKILL.md", f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")
    write(base / name / "scripts" / "run.py", "print('never run at import')\n")
    return base / name


PERSONAL_SPEC = {"name": "Daily note", "description": "Writes a note.", "params": [{"name": "topic", "label": "Topic", "type": "text", "required": True}]}
PERSONAL_SCRIPT = "def main(params):\n    return params.topic\n"


def make_personal(base: Path, name: str = "daily-note", script: str = PERSONAL_SCRIPT) -> Path:
    write(base / name / "skill.json", json.dumps({**PERSONAL_SPEC, "name": name}))
    write(base / name / "skill.py", script)
    return base / name


PLAN = {"plan_id": "newscast", "title": "Newscast", "target_type": "occasional",
        "nodes": {"ask": {"id": "ask", "type": "action", "title": "Ask", "description": "", "assignee": "user", "dependencies": [], "outputs": ["done"]},
                  "done": {"id": "done", "type": "merge", "title": "Done", "description": "", "assignee": "agent", "dependencies": ["ask"], "outputs": []}}}


def make_plan(base: Path, name: str = "newscast.plan.json", **change: object) -> Path:
    plan = {**PLAN, **change}
    return write(base / name, json.dumps(plan))


def make_plugin(base: Path, name: str = "news", with_dep: bool = True, mcp: bool = True) -> Path:
    p = base / "plugins" / name
    make_skill(p / "skills", "news-skill")
    make_plan(p / "plans", "news.plan.json")
    spec = {"name": name, "version": "1.2.0", "description": "A plugin.", "skills": ["skills/news-skill"], "plans": [{"file": "plans/news.plan.json"}],
            "mcp_servers": [{"name": "news-feed", "command": "python", "args": ["-m", "feed"], "env": {"FEED_REGION": "eu", "FEED_API_KEY": "sk-secret-value-1234567890abcdef"}}] if mcp else [],
            "dependencies": {"skills": [{"name": "shared-research", "path": "../../shared-research", "why": "research"}]} if with_dep else {}}
    write(p / "plugin.json", json.dumps(spec))
    write(p / "README.md", "Readme.\n")
    return p


@pytest.fixture
def imp(tmp_path):
    spaces = Spaces.from_data(tmp_path / "data")
    return Importer(spaces, PlanStore(spaces.plans_dir))


@pytest.fixture
def src(tmp_path):
    base = tmp_path / "repo"
    base.mkdir()
    return base


def names(imp, kind):
    return [r["name"] for r in imp.list(kind)]


# -- skills ------------------------------------------------------------------------------------------------------------

def test_a_skill_is_copied_and_the_harness_reads_the_copy(imp, src, tmp_path):
    folder = make_skill(src, "news-research")
    before = digest_dir(src)
    row = imp.import_("skill", folder)["imported"]
    assert row["name"] == "news-research" and row["source"] == str(folder) and len(row["hash"]) == 64 and row["imported_at"]
    copy = imp.spaces.root / "skills" / "news-research"
    assert (copy / "SKILL.md").is_file() and (copy / "scripts" / "run.py").is_file()
    index = cap.CapabilityIndex(imp.spaces.cap_dir, tmp_path / "ws", home=tmp_path / "nohome", appdata=tmp_path / "noapp")
    entry = index.get("skill:news-research")
    assert entry["origin"] == "imported" and Path(entry["path"]).is_relative_to(imp.spaces.root)
    assert digest_dir(src) == before                                               # the source was only read


def test_the_copy_is_what_is_used_even_when_the_source_changes_or_goes(imp, src, tmp_path):
    folder = make_skill(src, "news-research", body="First text.")
    imp.import_("skill", folder)
    write(folder / "SKILL.md", "---\nname: news-research\ndescription: Changed.\n---\n\nSecond text.\n")
    index = cap.CapabilityIndex(imp.spaces.cap_dir, tmp_path / "ws", home=tmp_path / "nohome", appdata=tmp_path / "noapp")
    assert index.body("skill:news-research") == "First text."
    import shutil
    shutil.rmtree(folder)
    assert cap.CapabilityIndex(imp.spaces.cap_dir, tmp_path / "ws", home=tmp_path / "nohome", appdata=tmp_path / "noapp").body("skill:news-research") == "First text."


def test_importing_twice_is_refused_not_overwritten(imp, src):
    folder = make_skill(src, "news-research")
    imp.import_("skill", folder)
    with pytest.raises(Problem) as e:
        imp.import_("skill", folder)
    assert e.value.code == "exists"


def test_reimport_says_what_changed_and_replaces_only_on_confirmation(imp, src):
    folder = make_skill(src, "news-research", body="One.\n")
    imp.import_("skill", folder)
    assert imp.reimport("skill", "news-research")["source_changed"] is False
    assert imp.reimport("skill", "news-research", confirm=True)["status"] == "unchanged"
    write(folder / "SKILL.md", "---\nname: news-research\ndescription: Reads a thing.\n---\n\nOne.\nTwo.\n")
    write(folder / "extra.md", "new file\n")
    before = digest_dir(src)
    preview = imp.reimport("skill", "news-research")
    assert preview["status"] == "preview" and preview["source_changed"] and preview["diff"]["added"] == ["extra.md"]
    assert preview["diff"]["changed"][0]["file"] == "SKILL.md" and preview["diff"]["changed"][0]["plus"] == 1
    copy = imp.spaces.root / "skills" / "news-research"
    assert "Two." not in (copy / "SKILL.md").read_text(encoding="utf-8") and not (copy / "extra.md").exists()      # a preview writes nothing
    assert imp.reimport("skill", "news-research", confirm=True)["status"] == "replaced"
    assert "Two." in (copy / "SKILL.md").read_text(encoding="utf-8") and (copy / "extra.md").is_file()
    assert digest_dir(src) == before


def test_a_copy_edited_here_is_not_overwritten_without_saying_so(imp, src):
    folder = make_skill(src, "news-research", body="One.\n")
    imp.import_("skill", folder)
    copy = imp.spaces.root / "skills" / "news-research"
    write(copy / "SKILL.md", "---\nname: news-research\ndescription: Mine.\n---\n\nMy own edit.\n")
    assert imp.list("skill")[0]["local_edited"] is True
    assert imp.reimport("skill", "news-research")["local_edited"] is True
    write(folder / "SKILL.md", "---\nname: news-research\ndescription: Reads a thing.\n---\n\nSource two.\n")
    with pytest.raises(Problem) as e:
        imp.reimport("skill", "news-research", confirm=True)
    assert e.value.code == "local_edits" and "My own edit." in (copy / "SKILL.md").read_text(encoding="utf-8")
    imp.reimport("skill", "news-research", confirm=True, overwrite_local=True)
    assert "Source two." in (copy / "SKILL.md").read_text(encoding="utf-8") and imp.list("skill")[0]["local_edited"] is False


def test_delete_removes_the_copy_and_never_the_source(imp, src):
    folder = make_skill(src, "news-research")
    imp.import_("skill", folder)
    before = digest_dir(src)
    out = imp.delete("skill", "news-research")
    assert out["deleted"] == "skill:news-research" and not (imp.spaces.root / "skills" / "news-research").exists() and imp.list("skill") == []
    assert digest_dir(src) == before and folder.is_dir()
    with pytest.raises(Problem):
        imp.delete("skill", "news-research")


def test_a_personal_skill_is_saved_among_the_personal_skills_and_reimport_keeps_the_old_version(imp, src):
    folder = make_personal(src)
    imp.import_("skill", folder)
    store = PersonalSkills(imp.spaces.personal_dir)
    assert store.get("daily-note")["script"] == PERSONAL_SCRIPT
    row = imp.list("skill")[0]
    assert row["forms"] == ["personal"] and row["local_edited"] is False
    store.save({**PERSONAL_SPEC, "name": "daily-note"}, PERSONAL_SCRIPT + "# mine\n", slug="daily-note")
    assert imp.list("skill")[0]["local_edited"] is True
    write(folder / "skill.py", "def main(params):\n    return params.topic.upper()\n")
    preview = imp.reimport("skill", "daily-note")
    assert preview["source_changed"] and preview["local_edited"] and preview["diff"]["changed"][0]["file"] == "skill.py"
    with pytest.raises(Problem):
        imp.reimport("skill", "daily-note", confirm=True)
    imp.reimport("skill", "daily-note", confirm=True, overwrite_local=True)
    assert "upper" in store.get("daily-note")["script"] and len(store.versions("daily-note")) >= 1
    imp.delete("skill", "daily-note")
    assert not (imp.spaces.personal_dir / "daily-note").exists() and (folder / "skill.py").is_file()


def test_a_personal_skill_of_the_same_name_that_was_not_imported_blocks_the_import(imp, src):
    PersonalSkills(imp.spaces.personal_dir).save({**PERSONAL_SPEC, "name": "daily-note"}, PERSONAL_SCRIPT, slug="daily-note")
    with pytest.raises(Problem) as e:
        imp.import_("skill", make_personal(src))
    assert e.value.code == "blocked" and "not imported" in str(e.value)


def test_a_folder_with_both_forms_is_one_skill_in_both_places(imp, src):
    folder = make_skill(src, "daily-note")
    write(folder / "skill.json", json.dumps({**PERSONAL_SPEC, "name": "daily-note"}))
    write(folder / "skill.py", PERSONAL_SCRIPT)
    imp.import_("skill", folder)
    assert (imp.spaces.root / "skills" / "daily-note" / "SKILL.md").is_file() and PersonalSkills(imp.spaces.personal_dir).get("daily-note")
    imp.delete("skill", "daily-note")
    assert not (imp.spaces.root / "skills" / "daily-note").exists() and not (imp.spaces.personal_dir / "daily-note").exists()


def test_a_folder_that_is_not_a_skill_and_a_script_that_the_store_refuses_are_reported(imp, src):
    write(src / "nothing" / "README.md", "x")
    with pytest.raises(Problem) as e:
        imp.import_("skill", src / "nothing")
    assert e.value.code == "not_a_skill"
    with pytest.raises(Problem) as e:
        imp.import_("skill", make_personal(src, "bad-one", script="import os\nos.system('x')\n"))
    assert e.value.code == "bad_skill" and imp.list() == []


def test_imported_code_is_never_run(imp, src):
    folder = make_skill(src, "danger")
    write(folder / "scripts" / "run.py", f"open({str(src / 'RAN')!r}, 'w').write('x')\n")
    write(src / "plugins" / "p" / "plugin.json", json.dumps({"name": "p", "skills": []}))
    imp.import_("skill", folder)
    imp.import_("plugin", src / "plugins" / "p")
    assert not (src / "RAN").exists()


# -- safety: paths, links, half-written ----------------------------------------------------------------------------

def test_a_plugin_that_points_outside_itself_is_refused_and_nothing_is_written(imp, src):
    p = make_plugin(src, with_dep=False, mcp=False)
    spec = json.loads((p / "plugin.json").read_text(encoding="utf-8"))
    spec["skills"] = ["../../elsewhere"]
    write(p / "plugin.json", json.dumps(spec))
    make_skill(src, "elsewhere")
    with pytest.raises(Problem) as e:
        imp.import_("plugin", p)
    assert e.value.code == "outside" and imp.list() == []


@pytest.mark.parametrize("bad", ["../x", "..", "a/b", "C:evil", "a b", "x" * 80])
def test_a_name_that_could_climb_out_is_never_used_for_a_folder(imp, src, bad):
    folder = src / "plain"
    write(folder / "SKILL.md", f"---\nname: {bad}\ndescription: d\n---\nbody\n")
    row = imp.import_("skill", folder)["imported"]
    assert row["name"] == "plain"                                                      # the folder's own name is used instead
    assert (imp.spaces.root / "skills" / "plain" / "SKILL.md").is_file()


def test_a_folder_whose_own_name_is_unusable_is_refused(imp, src):
    folder = src / "a b"
    write(folder / "SKILL.md", "---\ndescription: d\n---\nbody\n")
    with pytest.raises(Problem) as e:
        imp.import_("skill", folder)
    assert e.value.code == "bad_name" and imp.list() == []


def test_links_in_a_source_are_not_followed(imp, src, tmp_path):
    folder = make_skill(src, "linked")
    secret = write(tmp_path / "outside-secret.txt", "TOP SECRET")
    try:
        os.symlink(secret, folder / "leak.txt")
    except (OSError, NotImplementedError):
        pytest.skip("no permission to make a symbolic link here")
    imp.import_("skill", folder)
    assert not (imp.spaces.root / "skills" / "linked" / "leak.txt").exists()


def test_a_failure_in_the_middle_leaves_nothing_behind(imp, src, monkeypatch):
    p = make_plugin(src, with_dep=False)
    before_manifest = imp.manifest.path.read_bytes() if imp.manifest.path.is_file() else None
    from tadamoo.importer import plans as plans_mod
    real = plans_mod.PlanKind.add

    def boom(self, *a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(plans_mod.PlanKind, "add", boom)
    with pytest.raises(RuntimeError):
        imp.import_("plugin", p)
    monkeypatch.setattr(plans_mod.PlanKind, "add", real)
    assert (imp.manifest.path.read_bytes() if imp.manifest.path.is_file() else None) == before_manifest
    assert imp.list() == [] and not (imp.spaces.root / "skills" / "news-skill").exists() and not (imp.spaces.root / "plugins").exists()
    assert not (imp.spaces.root / "mcp").exists() or not list((imp.spaces.root / "mcp").glob("*.json"))
    assert not (imp.spaces.root / ".work").exists()
    assert not (imp.spaces.cap_dir / "settings.json").exists() or "news-feed" not in (imp.spaces.cap_dir / "settings.json").read_text(encoding="utf-8")
    assert imp.plans().list() == []


def test_a_manifest_that_cannot_be_written_undoes_the_copy(imp, src, monkeypatch):
    folder = make_skill(src, "news-research")
    from tadamoo.importer import common
    real = common.Txn.write_file

    def fail_on_manifest(self, path, data):
        if Path(path).name == "manifest.json":
            raise OSError("no space")
        return real(self, path, data)
    monkeypatch.setattr(common.Txn, "write_file", fail_on_manifest)
    with pytest.raises(OSError):
        imp.import_("skill", folder)
    assert not (imp.spaces.root / "skills" / "news-research").exists()


# -- MCP servers -------------------------------------------------------------------------------------------------------

MCP_CONFIG = {"mcpServers": {"stocks": {"command": "node", "args": ["server.js", "--api-key", "abcdefabcdefabcdefabcdefabcdef12"], "env": {"REGION": "eu", "STOCK_TOKEN": "tok_1234567890abcdef1234567890abcdef"}},
                             "remote": {"url": "https://example.com/mcp?x=1"}}}


def test_an_mcp_server_is_imported_turned_off_and_keeps_no_secret(imp, src, tmp_path):
    cfg = write(src / ".mcp.json", json.dumps(MCP_CONFIG))
    with pytest.raises(Problem) as e:
        imp.verify("mcp", cfg)
    assert e.value.code == "choose_server" and e.value.data["servers"] == ["remote", "stocks"]
    check = imp.verify("mcp", cfg, server="stocks")
    assert check["can_import"] and check["snapshot"]["runs"].startswith("node server.js") and check["snapshot"]["needs_keys"] == [{"name": "STOCK_TOKEN", "entry": "STOCK_TOKEN"}]
    assert "abcdefabcdef" not in json.dumps(check["snapshot"]["runs"])
    row = imp.import_("mcp", cfg, server="stocks")["imported"]
    assert row["enabled"] is False
    saved = json.loads((imp.spaces.root / "mcp" / "stocks.json").read_text(encoding="utf-8"))
    assert "tok_1234" not in json.dumps(saved) and {"name": "REGION", "value": "eu"}.items() <= next(e for e in saved["env"] if e["name"] == "REGION").items()
    index = cap.CapabilityIndex(imp.spaces.cap_dir, tmp_path / "ws", home=tmp_path / "nohome", appdata=tmp_path / "noapp")
    entry = index.get("mcp:stocks")
    assert entry["origin"] == "imported" and entry["state"] == "off"
    with pytest.raises(PermissionError):
        index.command("mcp:stocks")
    imp.set_enabled("stocks", True)
    index = cap.CapabilityIndex(imp.spaces.cap_dir, tmp_path / "ws", home=tmp_path / "nohome", appdata=tmp_path / "noapp")
    assert index.get("mcp:stocks")["state"] == "approved" and index.command("mcp:stocks")[0] == "node"
    assert imp.list("mcp")[0]["enabled"] is True


def test_the_variables_of_an_imported_server_reach_it_without_the_secret_being_stored(imp, src, tmp_path):
    cfg = write(src / ".mcp.json", json.dumps(MCP_CONFIG))
    imp.import_("mcp", cfg, server="stocks")
    imp.set_enabled("stocks", True)
    index = cap.CapabilityIndex(imp.spaces.cap_dir, tmp_path / "ws", home=tmp_path / "nohome", appdata=tmp_path / "noapp")
    from tadamoo.discovery.installed import MissingKey
    with pytest.raises(MissingKey):
        index.target("mcp:stocks")                                                     # the key is not in the vault: the message names the entry to create


def test_a_remote_server_and_a_changed_command(imp, src):
    cfg = write(src / ".mcp.json", json.dumps(MCP_CONFIG))
    imp.import_("mcp", cfg, server="remote")
    assert imp.list("mcp")[0]["runs"] == "https://example.com/mcp"
    imp.import_("mcp", cfg, server="stocks")
    imp.set_enabled("stocks", True)
    changed = json.loads(json.dumps(MCP_CONFIG))
    changed["mcpServers"]["stocks"]["command"] = "python"
    write(cfg, json.dumps(changed))
    preview = imp.reimport("mcp", "stocks")
    assert preview["turns_off"] is True and preview["diff"]["changed"][0]["file"] == "command"
    imp.reimport("mcp", "stocks", confirm=True)
    assert imp.list("mcp")[1]["enabled"] is False                                      # a different program: it is the person's call again
    imp.delete("mcp", "stocks")
    assert not (imp.spaces.root / "mcp" / "stocks.json").exists() and "mcp:stocks" not in json.loads((imp.spaces.cap_dir / "settings.json").read_text(encoding="utf-8"))["off"]


# -- plans -------------------------------------------------------------------------------------------------------------

def test_a_plan_becomes_a_plan_of_the_planner_and_is_not_read_from_the_file_again(imp, src):
    f = make_plan(src)
    before = digest_dir(src)
    row = imp.import_("plan", f)["imported"]
    assert row["name"] == "newscast" and row["ran"] is False and row["local_edited"] is False
    assert [p["plan_id"] for p in imp.plans().list()] == ["newscast"]
    write(f, json.dumps({**PLAN, "title": "Changed in the repo"}))
    assert imp.plans().get("newscast").dag.title == "Newscast"
    assert digest_dir(src) != before                                                  # (the test changed the repo itself)
    imp.delete("plan", "newscast")
    assert imp.plans().list() == [] and f.is_file()


def test_a_plan_that_already_ran_keeps_its_history_and_asks_first(imp, src):
    f = make_plan(src)
    imp.import_("plan", f)
    store = imp.plans()
    bridge = store.get("newscast")
    bridge.dag.nodes["ask"].status = "completed"
    bridge.dag.shared_state["answer"] = 42
    bridge.record("user", "mark", "answered")
    store.save("newscast")
    assert imp.list("plan")[0]["ran"] is True
    changed = json.loads(f.read_text(encoding="utf-8"))
    changed["nodes"]["done"]["title"] = "Finished"
    changed["nodes"]["extra"] = {"id": "extra", "type": "action", "title": "Extra", "description": "", "assignee": "agent", "dependencies": ["done"], "outputs": []}
    changed["nodes"]["done"]["outputs"] = ["extra"]
    write(f, json.dumps(changed))
    preview = imp.reimport("plan", "newscast")
    assert preview["ran"] is True and preview["diff"]["added"] == ["extra"] and [c["file"] for c in preview["diff"]["changed"]] == ["done"]
    assert store.get("newscast").dag.nodes["done"].title == "Done"                      # nothing changed yet
    out = imp.reimport("plan", "newscast", confirm=True)
    bridge = store.get("newscast")
    assert out["status"] == "replaced" and bridge.dag.nodes["done"].title == "Finished" and "extra" in bridge.dag.nodes
    assert bridge.dag.nodes["ask"].status == "completed" and bridge.dag.shared_state == {"answer": 42}      # the run is kept
    assert any(h["summary"] == "answered" for h in bridge.history) and out["imported"]["last_reimport"] == {"kept": 1, "reset": 2}


def test_a_plan_edited_in_the_app_is_not_replaced_without_saying_so(imp, src):
    f = make_plan(src)
    imp.import_("plan", f)
    bridge = imp.plans().get("newscast")
    bridge.dag.nodes["ask"].title = "Edited in the app"
    imp.plans().save("newscast")
    assert imp.list("plan")[0]["local_edited"] is True
    write(f, json.dumps({**PLAN, "title": "Newscast 2"}))
    with pytest.raises(Problem) as e:
        imp.reimport("plan", "newscast", confirm=True)
    assert e.value.code == "local_edits" and imp.plans().get("newscast").dag.nodes["ask"].title == "Edited in the app"
    imp.reimport("plan", "newscast", confirm=True, overwrite_local=True)
    assert imp.plans().get("newscast").dag.nodes["ask"].title == "Ask"


def test_a_plan_made_in_the_app_with_the_same_id_is_not_taken_over(imp, src):
    f = make_plan(src)
    from tadamoo.planner import PlanDAG
    imp.plans().create(PlanDAG.from_dict(PLAN))
    with pytest.raises(Problem) as e:
        imp.import_("plan", f)
    assert e.value.code == "blocked" and "was not imported" in str(e.value)
    assert imp.import_("plan", f, adopt=True)["imported"]["name"] == "newscast"           # the same definition may be adopted
    imp.delete("plan", "newscast")


def test_a_broken_plan_is_refused(imp, src):
    for text in ("not json", json.dumps({"title": "x"}), json.dumps({**PLAN, "nodes": {"a": {"id": "a", "type": "nope", "title": "t"}}})):
        with pytest.raises(Problem):
            imp.import_("plan", write(src / "bad.plan.json", text))
    assert imp.list() == []


# -- plugins -----------------------------------------------------------------------------------------------------------

def test_a_plugin_brings_its_parts_as_copies_and_reports_its_dependencies(imp, src):
    p = make_plugin(src)
    check = imp.verify("plugin", p)
    assert {(x["kind"], x["name"], x["state"]) for x in check["parts"]} == {("skill", "news-skill", "new"), ("plan", "newscast", "new"), ("mcp", "news-feed", "new")}
    dep = check["snapshot"]["dependencies"][0]
    assert dep["name"] == "shared-research" and dep["imported"] is False and dep["exists"] is False
    shared = make_skill(src, "shared-research")
    assert imp.verify("plugin", p)["snapshot"]["dependencies"][0]["exists"] is True
    before = digest_dir(src)
    imp.import_("plugin", p, with_dependencies=True)
    assert digest_dir(src) == before
    assert names(imp, "plugin") == ["news"] and set(names(imp, "skill")) == {"news-skill", "shared-research"} and names(imp, "plan") == ["newscast"] and names(imp, "mcp") == ["news-feed"]
    plugin = imp.list("plugin")[0]
    assert set(plugin["parts"]) == {"skill:news-skill", "plan:newscast", "mcp:news-feed"} and plugin["dependencies"] == ["skill:shared-research"]
    assert (imp.spaces.root / "plugins" / "news" / "README.md").is_file()
    assert {r["name"]: r["parent"] for r in imp.list() if r["kind"] != "plugin"} == {"news-skill": "plugin:news", "newscast": "plugin:news", "news-feed": "plugin:news", "shared-research": ""}
    assert imp.list("mcp")[0]["enabled"] is False
    with pytest.raises(Problem) as e:
        imp.delete("skill", "news-skill")
    assert e.value.code == "part"
    out = imp.delete("plugin", "news")
    assert set(out["removed"]) == {"plugin:news", "skill:news-skill", "plan:newscast", "mcp:news-feed"}
    assert names(imp, "skill") == ["shared-research"] and imp.list("plan") == [] and imp.list("mcp") == [] and imp.plans().list() == []
    assert not (imp.spaces.root / "plugins" / "news").exists() and shared.is_dir() and p.is_dir()


def test_a_plugin_without_its_dependencies_imports_and_says_so(imp, src):
    out = imp.import_("plugin", make_plugin(src))
    assert out["snapshot"]["dependencies"][0]["imported"] is False and out["imported"]["dependencies"] == [] and names(imp, "skill") == ["news-skill"]


def test_a_plugin_part_that_is_already_imported_differently_blocks_and_the_same_is_reused(imp, src):
    p = make_plugin(src, with_dep=False, mcp=False)
    imp.import_("skill", p / "skills" / "news-skill")
    out = imp.import_("plugin", p)
    assert out["imported"]["parts"] == ["plan:newscast"] and out["imported"]["reused"] == ["skill:news-skill"]
    imp.delete("plugin", "news")
    assert names(imp, "skill") == ["news-skill"]                                       # imported on its own: the plugin's delete leaves it
    imp.delete("skill", "news-skill")
    imp.import_("skill", p / "skills" / "news-skill")
    write(p / "skills" / "news-skill" / "SKILL.md", "---\nname: news-skill\ndescription: Different.\n---\nx\n")
    with pytest.raises(Problem) as e:
        imp.import_("plugin", p)
    assert e.value.code == "blocked" and imp.list("plan") == []


def test_reimporting_a_plugin_updates_adds_and_removes_its_parts(imp, src):
    p = make_plugin(src, with_dep=False, mcp=False)
    imp.import_("plugin", p)
    write(p / "skills" / "news-skill" / "SKILL.md", "---\nname: news-skill\ndescription: Reads a thing.\n---\n\nNew body.\n")
    make_skill(p / "skills", "second-skill")
    spec = json.loads((p / "plugin.json").read_text(encoding="utf-8"))
    spec["skills"] = ["skills/news-skill", "skills/second-skill"]
    spec["plans"] = []
    spec["version"] = "1.3.0"
    write(p / "plugin.json", json.dumps(spec))
    preview = imp.reimport("plugin", "news")
    states = {x["key"]: x["state"] for x in preview["parts"]}
    assert states == {"skill:news-skill": "changed", "skill:second-skill": "new"} and preview["parts_removed"] == ["plan:newscast"]
    imp.reimport("plugin", "news", confirm=True)
    assert set(names(imp, "skill")) == {"news-skill", "second-skill"} and imp.list("plan") == [] and imp.list("plugin")[0]["version"] == "1.3.0"
    assert "New body." in (imp.spaces.root / "skills" / "news-skill" / "SKILL.md").read_text(encoding="utf-8")
    write(imp.spaces.root / "skills" / "news-skill" / "SKILL.md", "---\nname: news-skill\ndescription: x\n---\nlocal\n")
    assert imp.list("plugin")[0]["local_edited"] is True
    with pytest.raises(Problem):
        imp.reimport("plugin", "news", confirm=True)


def test_a_source_that_is_gone_is_reported_on_reimport_and_the_copy_stays(imp, src):
    folder = make_skill(src, "news-research")
    imp.import_("skill", folder)
    import shutil
    shutil.rmtree(folder)
    assert imp.list("skill")[0]["source_exists"] is False
    with pytest.raises(Problem) as e:
        imp.reimport("skill", "news-research")
    assert e.value.code == "source_missing" and (imp.spaces.root / "skills" / "news-research" / "SKILL.md").is_file()


def test_the_scan_skips_tool_folders_and_counts_what_it_copies(tmp_path):
    base = tmp_path / "s"
    write(base / "a.txt", "a")
    write(base / ".git" / "config", "x")
    write(base / "__pycache__" / "m.pyc", "x")
    assert sorted(scan_tree(base).files) == ["a.txt"]
