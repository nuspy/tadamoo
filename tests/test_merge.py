"""The three-way merge of an update: by lines inside a file, then file by file over a folder, and what the model is asked for."""
import json
from pathlib import Path

import pytest

from tadamoo.merge import assist, text, tree


def lines(*rows: str) -> str:
    return "".join(r + "\n" for r in rows)


BASE = lines("a", "b", "c", "d", "e", "f", "g")


def test_a_change_only_one_side_made_is_taken_and_a_change_both_made_the_same_way_once():
    ours = lines("a", "B", "c", "d", "e", "f", "g")
    theirs = lines("a", "b", "c", "d", "e", "F", "g")
    got = text.merge_text(BASE, ours, theirs)
    assert got.clean and got.render() == lines("a", "B", "c", "d", "e", "F", "g")
    same = text.merge_text(BASE, ours, ours)
    assert same.clean and same.render() == ours


def test_both_sides_changing_the_same_lines_differently_is_a_conflict_shown_with_both_sides():
    ours = lines("a", "OURS", "c", "d", "e", "f", "g")
    theirs = lines("a", "THEIRS", "c", "d", "e", "f", "g")
    got = text.merge_text(BASE, ours, theirs)
    assert got.conflicts == 1
    assert got.render("ours") == ours and got.render("theirs") == theirs
    marked = got.render("markers")
    assert "<<<<<<< installed\nOURS\n=======\nTHEIRS\n>>>>>>> new\n" in marked and marked.startswith("a\n") and marked.endswith("g\n")


def test_an_added_and_a_removed_line_in_different_places_are_both_kept():
    ours = lines("a", "b", "c", "d", "e", "f", "g", "mine")
    theirs = lines("b", "c", "d", "e", "f", "g")
    assert text.merge_text(BASE, ours, theirs).render() == lines("b", "c", "d", "e", "f", "g", "mine")


def test_a_last_line_without_a_newline_does_not_join_a_marker():
    got = text.merge_text("x\nend", "x\nours", "x\ntheirs")
    assert got.conflicts == 1 and got.render("markers").splitlines()[-1] == ">>>>>>> new"


def put(folder: Path, files: dict) -> Path:
    for name, data in files.items():
        p = folder / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
    return folder


@pytest.fixture
def three(tmp_path):
    base = put(tmp_path / "base", {"SKILL.md": BASE, "run.py": "x = 1\n", "keep.txt": "same\n", "gone.txt": "old\n", "img.bin": b"\0\1\2"})
    ours = put(tmp_path / "ours", {"SKILL.md": lines("a", "B", "c", "d", "e", "f", "g"), "run.py": "x = 1\n", "keep.txt": "same\n", "gone.txt": "old\n", "img.bin": b"\0\1\2",
                                   "mine.txt": "mine\n"})
    theirs = put(tmp_path / "theirs", {"SKILL.md": lines("a", "b", "c", "d", "e", "F", "g"), "run.py": "x = 2\n", "keep.txt": "same\n", "img.bin": b"\0\1\2", "new.txt": "new\n"})
    return base, ours, theirs


def test_every_file_gets_one_outcome(three):
    base, ours, theirs = three
    got = tree.plan_trees(base, ours, theirs)
    assert {p: f.outcome for p, f in got.items()} == {"SKILL.md": tree.MERGED, "run.py": tree.UPSTREAM, "keep.txt": tree.SAME, "gone.txt": tree.UPSTREAM, "img.bin": tree.SAME,
                                                      "mine.txt": tree.KEPT, "new.txt": tree.UPSTREAM}
    assert got["SKILL.md"].merged.decode() == lines("a", "B", "c", "d", "e", "F", "g")
    assert got["gone.txt"].merged is None
    assert tree.counts(got)[tree.MERGED] == 1 and not tree.unresolved(got, {})


def test_a_conflict_must_be_chosen_and_the_choice_is_written(three, tmp_path):
    base, ours, theirs = three
    (ours / "run.py").write_bytes(b"x = 3\n")
    got = tree.plan_trees(base, ours, theirs)
    f = got["run.py"]
    assert f.outcome == tree.CONFLICT and f.needs_choice and f.unresolved
    assert tree.unresolved(got, {}) == ["run.py"]
    assert tree.unresolved(got, {"run.py": "merged"}) == ["run.py"]                  # the proposal still holds markers: it is not a choice
    assert tree.unresolved(got, {"run.py": "bogus"}) == ["run.py"]
    assert tree.unresolved(got, {"run.py": "theirs"}) == []
    out = tmp_path / "final"
    tree.write_final(got, {"run.py": "ours"}, out)
    assert (out / "run.py").read_text() == "x = 3\n" and not (out / "gone.txt").exists() and (out / "mine.txt").exists() and (out / "new.txt").exists()


def test_a_file_changed_on_both_sides_that_is_not_text_is_a_conflict(three):
    base, ours, theirs = three
    (ours / "img.bin").write_bytes(b"\0\1\3")
    (theirs / "img.bin").write_bytes(b"\0\1\4")
    f = tree.plan_trees(base, ours, theirs)["img.bin"]
    assert f.outcome == tree.CONFLICT and not f.is_text and f.unresolved


def test_a_file_removed_by_one_side_and_changed_by_the_other_is_a_conflict(three):
    base, ours, theirs = three
    (ours / "gone.txt").write_bytes(b"changed by the person\n")
    f = tree.plan_trees(base, ours, theirs)["gone.txt"]
    assert f.outcome == tree.CONFLICT and f.theirs is None and f.ours == b"changed by the person\n"


def test_without_the_original_nothing_is_guessed(three):
    _, ours, theirs = three
    got = tree.plan_trees(None, ours, theirs)
    assert got["SKILL.md"].outcome == tree.CONFLICT and got["SKILL.md"].unresolved and got["SKILL.md"].merged == got["SKILL.md"].ours
    assert got["mine.txt"].outcome == tree.KEPT and got["new.txt"].outcome == tree.UPSTREAM and got["keep.txt"].outcome == tree.SAME


def test_tool_folders_and_versions_are_not_part_of_a_merge(tmp_path):
    put(tmp_path / "t", {"a.txt": "a\n", "__pycache__/x.pyc": b"x", ".versions/1/skill.py": "old\n", ".git/config": "x"})
    assert sorted(tree.read_tree(tmp_path / "t")) == ["a.txt"]


class Model:
    """A model that answers from a script and keeps what it was asked."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def chat_completion(self, messages, **kw):
        self.asked.append(messages)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {"choices": [{"message": {"content": answer}}]}


class ByFile:
    """A model that answers for the file the prompt names, in order, and keeps what it was asked for each."""

    def __init__(self, answers):
        self.answers, self.asked = {k: list(v) for k, v in answers.items()}, {}

    def chat_completion(self, messages, **kw):
        name = messages[1]["content"].split("\n", 1)[0].removeprefix("File: ")
        self.asked.setdefault(name, []).append(messages)
        return {"choices": [{"message": {"content": self.answers[name].pop(0)}}]}


def test_the_description_is_asked_with_the_diffs_and_survives_a_missing_or_failing_model(three):
    base, ours, theirs = three
    got = tree.plan_trees(base, ours, theirs)
    model = Model("The new version fixes the ending and your change stays.")
    out = assist.describe(model, "demo", got, "it")
    assert out["text"].startswith("The new version") and "Answer in Italian" in model.asked[0][1]["content"]
    prompt = model.asked[0][1]["content"]
    assert "SKILL.md: merged" in prompt and "installed -> new" in prompt and "keep.txt" not in prompt
    assert assist.describe(None, "demo", got)["text"] == "" and assist.describe(Model(RuntimeError("down")), "demo", got)["note"]
    assert assist.describe(model, "demo", tree.plan_trees(base, base, base))["note"] == "nothing differs"


def test_instructions_change_the_proposal_and_a_bad_answer_is_asked_again(three):
    base, ours, theirs = three
    (ours / "run.py").write_bytes(b"x = 3\n")
    got = tree.plan_trees(base, ours, theirs)
    model = ByFile({"run.py": ["x = 2  # my default\n"], "SKILL.md": [got["SKILL.md"].merged.decode()], "new.txt": ["new\n"]})
    done = assist.apply_notes(model, got, ["keep my default"])
    assert done["run.py"] == "revised" and got["run.py"].merged == b"x = 2  # my default\n" and not got["run.py"].unresolved
    assert tree.unresolved(got, {}) == []
    assert done["SKILL.md"] == "unchanged" and done["new.txt"] == "unchanged"      # asked for every file the update changes, left alone when the model sends the proposal back
    assert set(done) == {"SKILL.md", "run.py", "new.txt"}                      # a file the new version removed is not sent: there is nothing new in it to change
    prompt = model.asked["run.py"][0][1]["content"]
    assert "keep my default" in prompt and "===== proposed merge =====" in prompt and "x = 3" in prompt and "x = 2" in prompt


def test_an_answer_with_markers_or_broken_json_is_refused_and_after_two_tries_the_proposal_stays(tmp_path):
    base = put(tmp_path / "b", {"c.json": json.dumps({"a": 1})})
    ours = put(tmp_path / "o", {"c.json": json.dumps({"a": 2})})
    theirs = put(tmp_path / "t", {"c.json": json.dumps({"a": 3})})
    got = tree.plan_trees(base, ours, theirs)
    model = Model("<<<<<<< installed\n{}\n", "{not json")
    assert assist.apply_notes(model, got, ["take the new one"])["c.json"].startswith("left as proposed")
    assert got["c.json"].unresolved and len(model.asked) == 2 and "conflict markers" in model.asked[1][1]["content"]


def test_what_looks_like_a_key_never_reaches_the_model_and_is_put_back_in_what_it_writes(tmp_path):
    record = {"command": ["npx", "-y", "feed", "--api-key", "sk-live-1234", "--token=abcdefghij", "--region", "eu"], "url": "https://x.example/mcp?k=1",
              "env": [{"name": "FEED_TOKEN", "value": "A" * 40}]}
    old = json.dumps(record, indent=1)
    new = json.dumps({**record, "command": record["command"][:3] + ["--fast"] + record["command"][3:]}, indent=1)
    base = put(tmp_path / "b", {"record.json": old})
    ours = put(tmp_path / "o", {"record.json": old.replace('"eu"', '"it"')})
    theirs = put(tmp_path / "t", {"record.json": new})
    got = tree.plan_trees(base, ours, theirs)
    seen = []

    class Spy:
        def chat_completion(self, messages, **kw):
            seen.append(messages[1]["content"])
            if messages[1]["content"].startswith("File: "):
                return {"choices": [{"message": {"content": messages[1]["content"].split("===== proposed merge =====\n", 1)[1].rsplit("\n=====", 1)[0].replace('"it"', '"fr"')}}]}
            return {"choices": [{"message": {"content": "The new version adds --fast."}}]}
    assert assist.describe(Spy(), "feed", got)["text"] == "The new version adds --fast."
    assert assist.apply_notes(Spy(), got, ["use France"])["record.json"] == "revised"
    for sent in seen:
        assert "sk-live-1234" not in sent and "abcdefghij" not in sent and "A" * 40 not in sent and "⟦secret-1⟧" in sent
    merged = json.loads(got["record.json"].merged)
    assert merged["command"][4:7] == ["--api-key", "sk-live-1234", "--token=abcdefghij"] and merged["command"][3] == "--fast" and merged["env"][0]["value"] == "A" * 40 and "fr" in merged["command"]


def test_a_secret_mark_the_model_invents_is_refused():
    scrub = assist.Scrubber()
    assert scrub.scrub('{"a": "x", "key": "short"}') == '{"a": "x", "key": "short"}'
    assert scrub.restore(scrub.scrub('"--password", "hunter2"')) == '"--password", "hunter2"' and "hunter2" not in scrub.scrub('"--password", "hunter2"')
    assert assist._check("f.txt", "token: ⟦secret-9⟧\n")


def test_instructions_without_a_model_are_told_and_nothing_is_done(three):
    base, ours, theirs = three
    with pytest.raises(Exception) as caught:
        assist.apply_notes(None, tree.plan_trees(base, ours, theirs), ["keep my default"])
    assert getattr(caught.value, "code", "") == "no_model" and type(caught.value).__name__ == "MergeProblem"
    assert assist.apply_notes(None, {}, []) == {}
