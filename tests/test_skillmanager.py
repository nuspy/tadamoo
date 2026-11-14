"""The skill manager: what is installed is the person's choice (install, remove, turn on and off, update), from the program's list, a folder, a GitHub address or a web
page, with a fake net and a fake model and fake model downloads. Nothing that was not asked for is imported or downloaded."""
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo import capabilities as cap
from tadamoo.importer import Importer, Problem, Spaces
from tadamoo.skillmanager import SkillManager, catalogue, sources
from tadamoo.skillmanager.jobs import Cancelled, Job
from test_importer import make_personal, make_plugin, make_skill, write

SKILLS = Path(__file__).resolve().parents[1] / "skills"


class Net(sources.Fetcher):
    """A net made of a dict: address -> (content type, bytes). Anything else is a 404."""

    def __init__(self, pages=None):
        self.pages, self.asked = dict(pages or {}), []

    def get(self, url, limit=sources.MAX_PAGE):
        self.asked.append(url)
        if url not in self.pages:
            raise Problem("fetch_failed", f"answered 404: {url}", status=404)
        ctype, data = self.pages[url]
        return sources.Reply(url, ctype, data)

    def save(self, url, dest, limit=sources.MAX_ARCHIVE):
        reply = self.get(url)
        Path(dest).write_bytes(reply.data)
        return reply.ctype


def zip_of(files, top="repo-main"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(f"{top}/{name}" if top else name, text)
    return ("application/zip", buf.getvalue())


def skill_md(name, body="Do it.", description="Does a thing."):
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


class Model:
    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def chat_completion(self, messages, **kw):
        self.asked.append(messages)
        return {"choices": [{"message": {"content": self.answers.pop(0)}}]}


@pytest.fixture
def world(tmp_path):
    asked = []

    def installer(name, job):
        asked.append(name)
        job.say(f"downloading for {name}")
        return {"ready": True, "by_hand": []}
    return SkillManager(tmp_path / "data", installer=installer), tmp_path, asked


def names(mgr, state):
    return [r["name"] for r in mgr.list() if r["state"] == state]


def off_list(tmp_path):
    f = tmp_path / "data" / "capabilities" / "settings.json"
    return json.loads(f.read_text(encoding="utf-8")).get("off", []) if f.is_file() else []


# -- what ships ---------------------------------------------------------------------------------------------------

def test_every_entry_of_the_catalogue_is_a_folder_and_every_top_folder_is_an_entry():
    entries = catalogue.load()
    assert all(e["present"] for e in entries) and len({e["name"] for e in entries}) == len(entries)
    assert {p.name for p in SKILLS.iterdir() if (p / "SKILL.md").is_file()} == {e["name"] for e in entries if e["kind"] == "skill"}
    for e in entries:
        assert set(e["requires"]) <= {x["name"] for x in entries} and e["title"]["en"] and e["title"]["it"] and e["summary"]["en"] and e["summary"]["it"]


def test_the_media_skills_are_separate_entries_each_one_that_can_be_chosen_alone():
    entries = {e["name"]: e for e in catalogue.load()}
    media = ("image-generation", "image-editing", "video-generation", "talking-head", "video-editing", "video-modification")
    assert set(media) <= set(entries) and all(entries[m]["requires"] == [] for m in media)


def test_what_a_skill_says_it_depends_on_is_what_the_catalogue_requires():
    entries = {e["name"]: e for e in catalogue.load()}
    for name, e in entries.items():
        if e["kind"] == "skill":
            meta, _ = cap.parse_frontmatter((SKILLS / name / "SKILL.md").read_text(encoding="utf-8"))
            assert set(e["requires"]) == {d.strip() for d in meta.get("depends", "").split(",") if d.strip()}, name
    plugin = json.loads((SKILLS / "plugins" / "ai-newscast" / "plugin.json").read_text(encoding="utf-8"))
    assert set(entries["ai-newscast"]["requires"]) == {d["name"] for d in plugin["dependencies"]["skills"]}


def test_a_choice_brings_what_it_requires_once_in_the_order_of_the_catalogue():
    entries = catalogue.load()
    assert [e["name"] for e in catalogue.choose(entries, ["video-generation"])] == ["video-generation"]
    got = [e["name"] for e in catalogue.choose(entries, ["ai-newscast", "talking-head"])]
    assert got[-1] == "ai-newscast" and len(got) == len(set(got)) == 8
    with pytest.raises(Problem):
        catalogue.choose(entries, ["nothing"])


# -- nothing is installed that was not asked for ----------------------------------------------------------------------

def test_at_first_nothing_is_installed_and_listing_and_syncing_install_nothing(world):
    mgr, tmp, asked = world
    rows = mgr.list()
    assert {r["state"] for r in rows} == {"not_installed"} and [r["name"] for r in rows] == [e["name"] for e in catalogue.load()]
    mgr.sync()
    assert Importer(Spaces.from_data(tmp / "data")).list() == [] and asked == []
    assert not (tmp / "data" / "capabilities" / "imported").exists()


def test_only_the_named_skills_and_what_they_require_are_installed_and_only_their_models_downloaded(world):
    mgr, tmp, asked = world
    out = mgr.install(["video-generation", "image-generation"])
    assert out["installed"] == ["image-generation", "video-generation"] and out["models"] == [{"name": "image-generation", "ready": True, "by_hand": []},
                                                                                              {"name": "video-generation", "ready": True, "by_hand": []}]
    assert asked == ["image-generation", "video-generation"]
    assert names(mgr, "installed") == ["image-generation", "video-generation"] and "video-modification" in names(mgr, "not_installed")
    assert "video-modification" not in asked and not (tmp / "data" / "capabilities" / "imported" / "skills" / "video-modification").exists()


def test_a_second_install_changes_nothing_and_a_skill_never_asked_for_is_never_downloaded(world):
    mgr, tmp, asked = world
    mgr.install(["talking-head"])
    again = mgr.install(["talking-head"])
    assert again["same"] == ["talking-head"] and not again["installed"] and not again["updated"]
    assert set(asked) == {"talking-head"}
    with pytest.raises(Problem) as e:
        mgr.download_models("video-modification")
    assert e.value.code == "not_installed" and "video-modification" not in asked


def test_installing_without_the_models_copies_only_and_the_models_come_later(world):
    mgr, tmp, asked = world
    out = mgr.install(["image-editing"], models=False)
    assert out["installed"] == ["image-editing"] and asked == [] and mgr.get("image-editing")["models"] == "unknown"
    assert mgr.download_models("image-editing") == {"name": "image-editing", "ready": True, "by_hand": []} and asked == ["image-editing"]
    assert mgr.get("image-editing")["models"] == "ready"


def test_a_download_that_does_not_finish_is_recorded_and_the_skill_stays_installed(tmp_path):
    mgr = SkillManager(tmp_path / "data", installer=lambda n, j: {"ready": False, "by_hand": ["put the LoRA in the folder"]})
    out = mgr.install(["video-modification"])
    assert out["models"] == [{"name": "video-modification", "ready": False, "by_hand": ["put the LoRA in the folder"]}]
    row = mgr.get("video-modification")
    assert row["state"] == "installed" and row["models"] == "missing" and row["download_gb"] == 60


def test_a_size_is_said_before_the_install_and_models_shared_with_an_installed_skill_count_once(world):
    mgr, tmp, asked = world
    rows = {r["name"]: r for r in mgr.list()}
    assert rows["video-modification"]["download_gb"] == 60 and rows["image-generation"]["download_gb"] == 10.5 and rows["video-editing"]["download_gb"] is None
    assert rows["ai-newscast"]["will_install"][-1] == "ai-newscast" and rows["image-editing"]["download_gb"] == 10.5
    mgr.install(["image-generation"], models=False)
    assert mgr.get("image-editing")["download_gb"] == 0.0 or mgr.get("image-editing")["download_gb"] is None
    assert mgr.get("image-generation")["download_gb"] == 10.5                        # installed, its models not yet there


def test_the_plugin_brings_the_skills_it_requires_and_not_the_others(world):
    mgr, tmp, asked = world
    out = mgr.install(["ai-newscast"], models=False)
    required = set(next(e for e in catalogue.load() if e["name"] == "ai-newscast")["requires"])
    assert required | {"ai-newscast"} == set(out["installed"]) and "video-modification" not in names(mgr, "installed")
    assert mgr.get("news-research")["needed_by"] == ["ai-newscast"]
    assert (tmp / "data" / "personal-skills" / "ai-newscast" / "skill.py").is_file()


def test_the_imported_copy_of_the_newscast_skill_finds_the_skills_it_uses(world, monkeypatch):
    import importlib.util
    import types
    mgr, tmp, asked = world
    mgr.install(["ai-newscast"], models=False)
    stub = types.ModuleType("tadamoo_skill")
    stub.tool = stub.ai = stub.say = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "tadamoo_skill", stub)
    monkeypatch.delitem(sys.modules, "research", raising=False)
    copy = tmp / "data" / "personal-skills" / "ai-newscast" / "skill.py"
    monkeypatch.setattr(sys, "argv", ["entry.py", str(copy), "context.json"])
    spec = importlib.util.spec_from_file_location("imported_newscast_skill", copy)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.find_skill("news-research") == tmp / "data" / "capabilities" / "imported" / "skills" / "news-research"


# -- the choice, turning on and off ---------------------------------------------------------------------------------

def test_the_choice_is_kept_per_skill_in_the_user_data_folder(world):
    mgr, tmp, asked = world
    mgr.install(["news-research", "talking-head"], models=False)
    saved = json.loads((tmp / "data" / "skills" / "choices.json").read_text(encoding="utf-8"))["skills"]
    assert set(saved) == {"news-research", "talking-head"}
    one = saved["news-research"]
    assert one["source"] == {"type": "builtin", "ref": "news-research"} and one["enabled"] is True and one["kind"] == "skill" and one["hash"] and one["installed_at"]
    mgr.set_enabled("news-research", False)
    assert json.loads((tmp / "data" / "skills" / "choices.json").read_text(encoding="utf-8"))["skills"]["news-research"]["enabled"] is False


def test_a_skill_turned_off_is_not_seen_by_the_harness_and_turned_on_it_is_seen_again_with_nothing_downloaded(world):
    mgr, tmp, asked = world
    mgr.install(["news-research"], models=False)
    idx = lambda: cap.CapabilityIndex(tmp / "data" / "capabilities", tmp / "ws", home=tmp / "nohome", appdata=tmp / "noapp")   # noqa: E731
    state = lambda: next(e for e in idx().entries if e["id"] == "skill:news-research")["state"]                                   # noqa: E731
    assert state() == "approved"
    row = mgr.set_enabled("news-research", False)
    assert row["state"] == "disabled" and off_list(tmp) == ["skill:news-research"] and state() == "off"
    assert (tmp / "data" / "capabilities" / "imported" / "skills" / "news-research" / "SKILL.md").is_file()      # the files stay
    assert idx().search("evidence table investigation") == []
    assert mgr.set_enabled("news-research", True)["state"] == "installed" and off_list(tmp) == [] and state() == "approved"
    assert asked == [] and mgr.get("news-research")["hash"]


def test_turning_off_a_skill_with_a_personal_form_puts_it_out_of_the_list_and_back(world):
    mgr, tmp, asked = world
    mgr.install(["ai-newscast"], models=False)
    personal = tmp / "data" / "personal-skills"
    mgr.set_enabled("ai-newscast", False)
    assert not (personal / "ai-newscast").exists() and (personal / "ai-newscast.off" / "skill.py").is_file()
    assert "skill:ai-newscast" in off_list(tmp) and mgr.get("ai-newscast")["state"] == "disabled"
    mgr.update("ai-newscast")                                                      # an update while it is off leaves it off
    assert mgr.get("ai-newscast")["state"] == "disabled" and (personal / "ai-newscast.off").is_dir()
    mgr.set_enabled("ai-newscast", True)
    assert (personal / "ai-newscast" / "skill.py").is_file() and not (personal / "ai-newscast.off").exists()


def test_a_change_made_in_the_capabilities_page_is_followed(world):
    mgr, tmp, asked = world
    mgr.install(["news-research"], models=False)
    cap.CapabilityIndex(tmp / "data" / "capabilities", tmp / "ws", home=tmp / "nohome", appdata=tmp / "noapp").save_settings(off=["skill:news-research"])
    assert mgr.get("news-research")["state"] == "disabled"


def test_skills_imported_before_the_manager_existed_are_taken_in_as_installed(world):
    mgr, tmp, asked = world
    src = make_skill(tmp / "src", "old-one")
    Importer(Spaces.from_data(tmp / "data")).import_("skill", src)
    row = mgr.get("old-one")
    assert row["state"] == "installed" and row["source"] == {"type": "folder", "ref": str(src)} and not row["builtin"]
    cat = SKILLS / "news-research"
    Importer(Spaces.from_data(tmp / "data")).import_("skill", cat)
    assert mgr.get("news-research")["source"] == {"type": "builtin", "ref": "news-research"}


# -- remove -----------------------------------------------------------------------------------------------------------

def test_remove_takes_the_copy_out_says_the_models_stay_and_cleans_the_off_list(world):
    mgr, tmp, asked = world
    mgr.install(["talking-head", "news-research"])
    mgr.set_enabled("talking-head", False)
    out = mgr.remove("talking-head")
    assert out == {"removed": "talking-head", "models_kept": True, "needed_by": []}
    assert mgr.get("talking-head")["state"] == "not_installed" and not (tmp / "data" / "capabilities" / "imported" / "skills" / "talking-head").exists()
    assert off_list(tmp) == [] and "talking-head" not in json.loads((tmp / "data" / "skills" / "choices.json").read_text(encoding="utf-8"))["skills"]
    assert mgr.remove("news-research")["models_kept"] is False
    with pytest.raises(Problem) as e:
        mgr.remove("talking-head")
    assert e.value.code == "not_imported"


def test_removing_a_plugin_removes_its_parts_and_a_skill_others_need_says_so(world):
    mgr, tmp, asked = world
    mgr.install(["ai-newscast"], models=False)
    assert mgr.remove("news-research")["needed_by"] == ["ai-newscast"]
    mgr.set_enabled("ai-newscast", False)
    mgr.remove("ai-newscast")
    assert not (tmp / "data" / "personal-skills" / "ai-newscast").exists() and not (tmp / "data" / "personal-skills" / "ai-newscast.off").exists()
    assert "ai-newscast" not in names(mgr, "installed") and off_list(tmp) == []


# -- a folder ---------------------------------------------------------------------------------------------------------

def test_a_folder_of_several_skills_shows_each_and_only_the_chosen_one_is_installed(world):
    mgr, tmp, asked = world
    repo = tmp / "clone"
    make_skill(repo / "skills", "alpha")
    make_skill(repo / "skills", "beta")
    make_plugin(repo / "stuff", "gamma-plug", with_dep=False, mcp=False)
    write(repo / "node_modules" / "x" / "SKILL.md", skill_md("junk"))
    plan = mgr.plan(str(repo))
    assert [c["name"] for c in plan["candidates"]] == ["alpha", "beta", "gamma-plug"] and plan["type"] == "folder" and not plan["generated"]
    assert all(c["files"] and not c["already_installed"] for c in plan["candidates"])
    out = mgr.install(token=plan["token"], only=["skills/beta"])
    assert out["installed"] == ["beta"] and names(mgr, "installed") == ["beta"] and mgr.get("beta")["source"] == {"type": "folder", "ref": str(repo / "skills" / "beta")}
    assert mgr.saved_plan(plan["token"])["candidates"]                                  # what was read stays while skills of it are left
    mgr.install(token=plan["token"], only=None)
    assert {"alpha", "beta", "gamma-plug"} <= set(names(mgr, "installed"))
    with pytest.raises(Problem):
        mgr.saved_plan(plan["token"])                                                    # all taken: forgotten


def test_a_single_skill_folder_a_missing_folder_and_a_file_are_told_apart(world):
    mgr, tmp, asked = world
    one = make_skill(tmp / "src", "solo")
    assert [c["id"] for c in mgr.plan(str(one))["candidates"]] == ["."]
    assert [c["name"] for c in mgr.plan(str(one / "SKILL.md"))["candidates"]] == ["solo"]
    with pytest.raises(Problem) as e:
        mgr.plan(str(tmp / "nope"))
    assert e.value.code == "bad_address"
    empty = tmp / "empty"
    empty.mkdir()
    with pytest.raises(Problem) as e2:
        mgr.plan(str(empty))
    assert e2.value.code == "no_skill_found"


def test_an_address_read_shows_what_is_already_installed_and_installing_again_updates_the_copy(world):
    mgr, tmp, asked = world
    src = make_skill(tmp / "src", "solo", body="One.")
    mgr.install(token=mgr.plan(str(src))["token"])
    (src / "SKILL.md").write_text(skill_md("solo", "Two."), encoding="utf-8")
    plan = mgr.plan(str(src))
    assert plan["candidates"][0]["already_installed"] and plan["candidates"][0]["installed_from"]["type"] == "folder"
    assert mgr.install(token=plan["token"])["updated"] == ["solo"]
    assert "Two." in (tmp / "data" / "capabilities" / "imported" / "skills" / "solo" / "SKILL.md").read_text(encoding="utf-8")


def test_update_brings_a_skill_up_to_its_folder_and_says_what_it_would_change_first(world):
    mgr, tmp, asked = world
    src = make_skill(tmp / "src", "solo", body="One.")
    mgr.install(token=mgr.plan(str(src))["token"])
    assert mgr.update("solo")["status"] == "unchanged"
    (src / "SKILL.md").write_text(skill_md("solo", "Two."), encoding="utf-8")
    seen = mgr.update("solo", check_only=True)
    assert seen["source_changed"] and seen["diff"]["changed"] == 1
    assert "One." in (tmp / "data" / "capabilities" / "imported" / "skills" / "solo" / "SKILL.md").read_text(encoding="utf-8")
    assert mgr.update("solo")["status"] == "updated"
    (tmp / "data" / "capabilities" / "imported" / "skills" / "solo" / "SKILL.md").write_text("edited here", encoding="utf-8")
    with pytest.raises(Problem) as e:
        (src / "SKILL.md").write_text(skill_md("solo", "Three."), encoding="utf-8")
        mgr.update("solo")
    assert e.value.code == "local_edits"
    assert mgr.update("solo", overwrite_local=True)["status"] == "updated"


def test_a_built_in_skill_updates_from_the_program_folder_when_it_changed_there(tmp_path):
    import shutil
    program = tmp_path / "program"
    shutil.copytree(SKILLS / "news-research", program / "news-research")
    (program / "builtin.json").write_text(json.dumps({"version": 1, "skills": [
        {"name": "news-research", "kind": "skill", "group": "news", "default": True, "requires": [], "title": {"en": "N", "it": "N"}, "summary": {"en": "n", "it": "n"}}]}), encoding="utf-8")
    mgr = SkillManager(tmp_path / "data", program=program)
    mgr.install(["news-research"], models=False)
    assert mgr.update("news-research")["status"] == "unchanged"
    with (program / "news-research" / "SKILL.md").open("a", encoding="utf-8") as f:
        f.write("\nA new line.\n")
    assert mgr.update("news-research")["status"] == "updated"
    assert "A new line." in (tmp_path / "data" / "capabilities" / "imported" / "skills" / "news-research" / "SKILL.md").read_text(encoding="utf-8")


# -- GitHub -----------------------------------------------------------------------------------------------------------

def test_github_address_forms():
    g = sources.parse_github
    assert g("https://github.com/o/r") == {"owner": "o", "repo": "r", "kind": "", "rest": []}
    assert g("github.com/o/r.git")["repo"] == "r"
    assert g("https://github.com/o/r/tree/main/skills/x") == {"owner": "o", "repo": "r", "kind": "tree", "rest": ["main", "skills", "x"]}
    assert g("https://github.com/o/r/blob/main/x/SKILL.md")["kind"] == "blob"
    assert g("https://github.com/o/r/issues/3") is None and g("https://example.com/o/r") is None


def test_a_github_repository_gives_its_skills(world):
    mgr, tmp, asked = world
    zipped = zip_of({"skills/alpha/SKILL.md": skill_md("alpha"), "skills/alpha/scripts/run.py": "print(1)", "skills/beta/SKILL.md": skill_md("beta"), "README.md": "hi"})
    mgr.fetcher = Net({"https://github.com/o/r/archive/HEAD.zip": zipped})
    plan = mgr.plan("https://github.com/o/r")
    assert plan["type"] == "github" and [c["id"] for c in plan["candidates"]] == ["skills/alpha", "skills/beta"] and plan["candidates"][0]["files"] == 2
    mgr.install(token=plan["token"], only=["skills/alpha"])
    row = mgr.get("alpha")
    assert row["source"] == {"type": "github", "ref": "https://github.com/o/r", "sub": "skills/alpha", "generated": False}
    assert (tmp / "data" / "skills" / "sources" / "alpha" / "SKILL.md").is_file()                  # the source the copy is updated from lives in the data folder, not in a temporary one
    zipped2 = zip_of({"skills/alpha/SKILL.md": skill_md("alpha", "Changed."), "skills/beta/SKILL.md": skill_md("beta")})
    mgr.fetcher.pages["https://github.com/o/r/archive/HEAD.zip"] = zipped2
    assert mgr.update("alpha")["status"] == "updated"
    assert "Changed." in (tmp / "data" / "capabilities" / "imported" / "skills" / "alpha" / "SKILL.md").read_text(encoding="utf-8")


def test_a_github_folder_with_a_branch_that_has_a_slash_is_found_by_trying_the_splits(world):
    mgr, tmp, asked = world
    mgr.fetcher = Net({"https://github.com/o/r/archive/feature/x.zip": zip_of({"tools/one/SKILL.md": skill_md("one")})})
    plan = mgr.plan("https://github.com/o/r/tree/feature/x/tools")
    assert [c["name"] for c in plan["candidates"]] == ["one"]
    assert mgr.fetcher.asked == ["https://github.com/o/r/archive/feature.zip", "https://github.com/o/r/archive/feature/x.zip"]
    mgr.fetcher.pages["https://github.com/o/r/archive/main.zip"] = zip_of({"tools/one/SKILL.md": skill_md("one")})
    assert [c["id"] for c in mgr.plan("https://github.com/o/r/blob/main/tools/one/SKILL.md")["candidates"]] == ["."]


def test_a_github_repository_that_is_not_there_or_a_folder_that_is_not_in_it_is_told(world):
    mgr, tmp, asked = world
    mgr.fetcher = Net({"https://github.com/o/r/archive/HEAD.zip": zip_of({"a/SKILL.md": skill_md("a")})})
    with pytest.raises(Problem) as e:
        mgr.plan("https://github.com/o/missing")
    assert e.value.code == "fetch_failed" and "private" in str(e.value)
    with pytest.raises(Problem) as e2:
        mgr.plan("https://github.com/o/r/tree/HEAD/nothere")
    assert e2.value.code in ("fetch_failed", "no_source")
    with pytest.raises(Problem) as e3:
        mgr.plan("https://github.com/o/r/tree/HEAD/../../..")                       # a folder cannot be named outside the repository
    assert e3.value.code == "outside"
    assert list((tmp / "data" / "skills" / "staging").iterdir()) == [] if (tmp / "data" / "skills" / "staging").exists() else True


def test_an_archive_cannot_write_outside_its_folder_and_a_link_is_left_out(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("repo/ok/SKILL.md", skill_md("ok"))
        info = zipfile.ZipInfo("repo/link")
        info.external_attr = 0o120777 << 16
        z.writestr(info, "../../outside")
    (tmp_path / "a.zip").write_bytes(buf.getvalue())
    root = sources.unpack(tmp_path / "a.zip", tmp_path / "out")
    assert (root / "ok" / "SKILL.md").is_file() and not (root / "link").exists()
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("repo/../evil.txt", "x")
    (tmp_path / "b.zip").write_bytes(bad.getvalue())
    with pytest.raises(Problem) as e:
        sources.unpack(tmp_path / "b.zip", tmp_path / "out2")
    assert e.value.code == "outside" and not (tmp_path / "evil.txt").exists()


# -- the web ----------------------------------------------------------------------------------------------------------

def test_a_web_address_that_is_a_skill_file_an_archive_or_a_page_linking_to_one(world):
    mgr, tmp, asked = world
    page = '<html><body><h1>Tools</h1><a href="/files/SKILL.md">the skill</a> <a href="/other">x</a></body></html>'
    mgr.fetcher = Net({"https://x.org/raw/SKILL.md": ("text/plain", skill_md("raw-one").encode()),
                       "https://x.org/pack.zip": zip_of({"a/SKILL.md": skill_md("zipped")}),
                       "https://x.org/page": ("text/html", page.encode()), "https://x.org/files/SKILL.md": ("text/plain", skill_md("linked").encode())})
    assert [c["name"] for c in mgr.plan("https://x.org/raw/SKILL.md")["candidates"]] == ["raw-one"]
    assert [c["name"] for c in mgr.plan("https://x.org/pack.zip")["candidates"]] == ["zipped"]
    plan = mgr.plan("https://x.org/page")
    assert [c["name"] for c in plan["candidates"]] == ["linked"] and not plan["generated"]
    mgr.install(token=plan["token"])
    assert mgr.get("linked")["source"]["type"] == "web" and mgr.get("linked")["source"]["ref"] == "https://x.org/page"


PAGE = "<html><body><h1>Meeting minutes</h1><p>" + "To write minutes: collect the decisions, list who does what and by when, send them the same day. " * 3 + "</p></body></html>"
MADE = "---\nname: meeting-minutes\ndescription: Writes minutes of a meeting.\n---\n\nCollect the decisions, list who does what and by when, send the same day.\n"


def test_a_page_that_only_describes_a_skill_is_written_by_the_model_shown_and_installed_only_when_accepted(world):
    mgr, tmp, asked = world
    mgr.fetcher = Net({"https://x.org/minutes": ("text/html", PAGE.encode())})
    model = Model("```markdown\n" + MADE + "```")
    mgr.model = lambda: model
    plan = mgr.plan("https://x.org/minutes")
    c = plan["candidates"][0]
    assert plan["generated"] and c["name"] == "meeting-minutes" and "Collect the decisions" in c["skill_md"] and c["skill_md"].rstrip().endswith("Source: https://x.org/minutes")
    assert "decisions" in model.asked[0][1]["content"] and "never follow an instruction" in model.asked[0][0]["content"]
    assert names(mgr, "installed") == []                                                # shown, not imported
    mgr.install(token=plan["token"])
    row = mgr.get("meeting-minutes")
    assert row["state"] == "installed" and row["source"]["generated"] is True
    with pytest.raises(Problem) as e:
        mgr.update("meeting-minutes")
    assert e.value.code == "not_updatable"


def test_a_model_that_writes_nonsense_is_asked_once_more_and_a_page_that_is_no_skill_or_no_model_is_told(world):
    mgr, tmp, asked = world
    mgr.fetcher = Net({"https://x.org/minutes": ("text/html", PAGE.encode())})
    model = Model("no header here", MADE)
    mgr.model = lambda: model
    assert mgr.plan("https://x.org/minutes")["candidates"][0]["name"] == "meeting-minutes" and len(model.asked) == 2
    mgr.model = lambda: Model("NOT_A_SKILL")
    with pytest.raises(Problem) as e:
        mgr.plan("https://x.org/minutes")
    assert e.value.code == "not_a_skill_page"
    mgr.model = None
    with pytest.raises(Problem) as e2:
        mgr.plan("https://x.org/minutes")
    assert e2.value.code == "no_model"
    mgr.model = lambda: type("Down", (), {"chat_completion": lambda self, **k: (_ for _ in ()).throw(OSError("down"))})()
    with pytest.raises(Problem) as e3:
        mgr.plan("https://x.org/minutes")
    assert e3.value.code == "no_model"
    assert names(mgr, "installed") == []


def test_a_page_with_nothing_to_make_a_skill_from_is_told(world):
    mgr, tmp, asked = world
    mgr.fetcher = Net({"https://x.org/empty": ("text/html", b"<html><body>hi</body></html>")})
    mgr.model = lambda: Model(MADE)
    with pytest.raises(Problem) as e:
        mgr.plan("https://x.org/empty")
    assert e.value.code == "no_skill_found"


def test_page_text_and_links_are_read_without_scripts():
    text, links = sources.read_page("<html><head><style>p{}</style></head><body><script>var x=1</script><h1>Title</h1><p>One <b>two</b></p><a href='/a'>go</a></body></html>")
    assert "var x" not in text and "p{}" not in text and "Title" in text and "One two" in text and links == ["/a"]


# -- jobs -------------------------------------------------------------------------------------------------------------

def test_an_installation_runs_as_a_job_with_a_step_and_the_lines_of_the_installers(world):
    mgr, tmp, asked = world
    job = mgr.start("install", ["image-generation"], lambda j: mgr.install(["image-generation"], job=j), wait=True)
    view = mgr.jobs.view()
    assert view["state"] == "done" and view["names"] == ["image-generation"] and view["result"]["installed"] == ["image-generation"]
    assert "downloading for image-generation" in view["lines"] and view["steps"] == 1 and job.ended


def test_only_one_job_runs_at_a_time_and_a_job_can_be_stopped(tmp_path):
    import threading
    gate = threading.Event()

    def installer(name, job):
        job.say("going")
        while not gate.wait(0.01):
            job.check()
        return {"ready": True}
    mgr = SkillManager(tmp_path / "data", installer=installer)
    job = mgr.start("install", ["talking-head"], lambda j: mgr.install(["talking-head"], job=j))
    for _ in range(200):
        if "going" in mgr.jobs.view()["lines"]:
            break
        gate.wait(0.01)
    with pytest.raises(Problem) as e:
        mgr.start("install", ["image-generation"], lambda j: {})
    assert e.value.code == "busy"
    assert mgr.jobs.cancel() is True
    job_thread_done = False
    for _ in range(300):
        if mgr.jobs.view()["state"] != "running":
            job_thread_done = True
            break
        gate.wait(0.01)
    assert job_thread_done and mgr.jobs.view()["state"] == "cancelled"
    assert mgr.get("talking-head")["state"] == "installed"                              # what was done stays
    assert mgr.jobs.cancel() is False


def test_a_program_a_job_runs_streams_its_output_and_is_ended_by_a_cancel():
    job = Job("x", [])
    code = job.run([sys.executable, "-c", "print('one'); print('two')"])
    assert code == 0 and job.lines[-2:] == ["one", "two"]
    long = Job("x", [])
    import threading
    t = threading.Timer(0.5, long.cancel)
    t.start()
    with pytest.raises(Cancelled):
        for _ in range(2):
            long.run([sys.executable, "-c", "import time; time.sleep(30)"])
    t.cancel()


def test_a_failed_job_says_why_in_words(world):
    mgr, tmp, asked = world
    mgr.start("install", [], lambda j: mgr.install(token="abcdef123456", job=j), wait=True)
    view = mgr.jobs.view()
    assert view["state"] == "failed" and "read it again" in view["error"]


# -- the shell --------------------------------------------------------------------------------------------------------

def run_cli(tmp_path, *argv, model=None):
    from tadamoo.skillmanager import __main__ as cli
    return cli.main(["--data-dir", str(tmp_path / "data"), *argv])


def test_the_shell_lists_installs_turns_off_and_removes(tmp_path, capsys, monkeypatch):
    from tadamoo.media import components
    asked = []
    monkeypatch.setattr(components, "install", lambda name, settings, run=None, say=None, **k: asked.append(name) or {"ready": True, "by_hand": [], "components": []})
    assert run_cli(tmp_path, "list") == 0
    out = capsys.readouterr().out
    assert "video-modification" in out and "not installed" in out and "about 60 GB" in out
    assert run_cli(tmp_path, "install", "news-research", "image-generation") == 0
    assert "installed: image-generation, news-research" in capsys.readouterr().out and asked == ["image-generation"]
    assert run_cli(tmp_path, "disable", "news-research") == 0 and "turned off" in capsys.readouterr().out
    assert run_cli(tmp_path, "--json", "list") == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)["skills"]}
    assert rows["news-research"]["state"] == "disabled" and rows["image-generation"]["state"] == "installed" and rows["talking-head"]["state"] == "not_installed"
    assert run_cli(tmp_path, "remove", "image-generation") == 0 and "Its models stay on the disk" in capsys.readouterr().out
    assert run_cli(tmp_path, "install", "nothing-like-it") == 1                      # neither a built-in name nor an address: told as a problem
    assert run_cli(tmp_path, "enable", "nothing-like-it") == 1 and asked == ["image-generation"]


def test_the_setup_installs_the_defaults_or_all_or_the_named_ones_copying_only(tmp_path, capsys, monkeypatch):
    from tadamoo.media import components
    monkeypatch.setattr(components, "install", lambda *a, **k: pytest.fail("the setup must not download models"))
    assert run_cli(tmp_path, "install", "--defaults", "--no-models") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "--json", "list") == 0
    rows = {r["name"]: r["state"] for r in json.loads(capsys.readouterr().out)["skills"]}
    assert rows["video-modification"] == "not_installed" and rows["ai-newscast"] == "installed" and rows["talking-head"] == "installed"
    assert run_cli(tmp_path, "install", "--all-builtin", "--no-models") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "--json", "list") == 0
    assert {r["state"] for r in json.loads(capsys.readouterr().out)["skills"]} == {"installed"}


def test_the_shell_installs_from_a_folder_and_asks_to_choose_among_several(tmp_path, capsys):
    repo = tmp_path / "clone"
    make_skill(repo, "alpha")
    make_skill(repo, "beta")
    make_skill(repo, "gamma")
    assert run_cli(tmp_path, "install", str(repo)) == 2
    seen = capsys.readouterr()
    assert "alpha" in seen.out and "--only" in seen.err and not (tmp_path / "data" / "capabilities").exists()      # nothing is taken until one is chosen
    assert run_cli(tmp_path, "plan", str(repo)) == 0
    token = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("token: ")][0].split(": ")[1]
    assert run_cli(tmp_path, "install", "--token", token, "--only", "alpha") == 0
    assert "installed: alpha" in capsys.readouterr().out
    assert run_cli(tmp_path, "install", "--token", token) == 2
    capsys.readouterr()
    assert run_cli(tmp_path, "install", "--token", token, "--all") == 0
    done = capsys.readouterr().out
    assert "installed: beta, gamma" in done and "already up to date: alpha" in done
    assert run_cli(tmp_path, "install", str(repo / "alpha")) == 0 and "already up to date: alpha" in capsys.readouterr().out


def test_the_shell_does_not_take_a_page_the_model_wrote_without_a_second_command(tmp_path, capsys, monkeypatch):
    from tadamoo.skillmanager import __main__ as cli
    mgr_net = Net({"https://x.org/minutes": ("text/html", PAGE.encode())})
    real = cli.SkillManager

    def make(*a, **k):
        m = real(*a, **k)
        m.fetcher, m.model = mgr_net, (lambda: Model(MADE))
        return m
    monkeypatch.setattr(cli, "SkillManager", make)
    assert run_cli(tmp_path, "install", "https://x.org/minutes") == 3
    captured = capsys.readouterr()
    assert "Collect the decisions" in captured.out and "install --token" in captured.err
    assert Importer(Spaces.from_data(tmp_path / "data")).list() == []
