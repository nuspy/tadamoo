"""Keeping or undoing the agent's edits one block at a time."""
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from serve import handler, review as rv
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.tasks import ChangeJournal, TaskJournal
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]

OLD = "".join(f"line {i}\n" for i in range(1, 41))


def changed(text, edits):
    lines = text.splitlines(keepends=True)
    for index, new in edits.items():
        lines[index - 1] = new
    return "".join(lines)


NEW = changed(OLD, {3: "line three\n", 35: "line thirty-five\n"})          # two blocks, far apart


class DiffTest(unittest.TestCase):
    def test_blocks_of_the_diff_are_the_blocks_acted_on(self):
        text = rv.unified(OLD, NEW, "f.txt")
        self.assertEqual(text.count("\n@@ "), 2)
        self.assertEqual(sum(1 for l in text.splitlines() if l.startswith("@@")), len(rv.groups(OLD, NEW)))
        self.assertEqual(len(rv.groups(OLD, NEW)), 2)
        self.assertEqual(rv.counts(text), (2, 2))

    def test_undoing_one_block_puts_back_only_its_lines(self):
        first = rv.revert(OLD, NEW, 0)
        self.assertEqual(first, changed(OLD, {35: "line thirty-five\n"}))
        second = rv.revert(OLD, NEW, 1)
        self.assertEqual(second, changed(OLD, {3: "line three\n"}))
        with self.assertRaises(rv.ReviewError):
            rv.revert(OLD, NEW, 2)

    def test_added_and_removed_lines_and_crlf(self):
        old, new = "a\r\nb\r\nc\r\n", "a\r\nb\r\nx\r\ny\r\nc\r\n"
        self.assertEqual(rv.revert(old, new, 0), old)
        self.assertEqual(rv.revert("a\nb\nc\n", "a\nc\n", 0), "a\nb\nc\n")
        self.assertEqual(rv.revert("", "one\ntwo", 0), "")

    def test_the_same_diff_whatever_the_size_of_the_file(self):
        big_old = "".join("same\n" if i % 2 else f"v{i}\n" for i in range(600))
        big_new = big_old.replace("v10\n", "changed\n", 1) + "tail\n"
        text = rv.unified(big_old, big_new, "big.txt")
        self.assertEqual(sum(1 for l in text.splitlines() if l.startswith("@@")), len(rv.groups(big_old, big_new)))

    def test_what_cannot_be_reviewed_is_refused_up_front(self):
        good = {"root": "x", "old_text": "a\n", "new_text": "b\n"}
        self.assertIsNotNone(rv.usable(good, "modified"))
        self.assertIsNone(rv.usable(good, "deleted"))
        self.assertIsNone(rv.usable({**good, "old_text": "a�"}, "modified"))
        self.assertIsNone(rv.usable({**good, "new_text": "b" * 300_000}, "modified"))
        self.assertIsNone(rv.usable({**good, "root": ""}, "modified"))
        self.assertIsNone(rv.usable(None, "modified"))


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()
        self.journal = ChangeJournal(Path(self.tmp.name) / "j")

    def record(self, file="f.txt", old=OLD, new=NEW, kind="modified", write=True):
        if write:
            (self.root / file).write_text(new, encoding="utf-8", newline="")
        edit = {"file": file, "kind": kind, "added": 0, "removed": 0, "diff": "",
                "review": {"root": str(self.root), "old_text": old, "new_text": new}}
        return self.journal.add([edit])["edits"][-1]

    def on_disk(self, file="f.txt"):
        return (self.root / file).read_bytes().decode("utf-8")

    def test_an_edit_is_open_for_review_with_its_blocks(self):
        edit = self.record()
        self.assertEqual((edit["review"], edit["blocks"], edit["added"], edit["removed"]), ("pending", [False, False], 2, 2))
        self.assertNotIn("old_text", json.dumps(self.journal.view()))     # the stored texts never reach the panel

    def test_undoing_a_block_changes_the_file_and_the_diff(self):
        edit = self.record()
        out = self.journal.review_action(edit["id"], "reject", 0)
        self.assertEqual(self.on_disk(), changed(OLD, {35: "line thirty-five\n"}))
        self.assertEqual((out["review"], out["blocks"], out["added"]), ("pending", [False], 1))
        out = self.journal.review_action(edit["id"], "reject", 0)
        self.assertEqual((self.on_disk(), out["review"], out["diff"]), (OLD, "reverted", ""))

    def test_keeping_blocks_finishes_the_review(self):
        edit = self.record()
        out = self.journal.review_action(edit["id"], "keep", 1)
        self.assertEqual((out["review"], out["blocks"]), ("partial", [False, True]))
        self.assertEqual(self.on_disk(), NEW)                              # keeping touches nothing
        out = self.journal.review_action(edit["id"], "reject", 0)          # the kept block stays kept, now alone
        self.assertEqual((out["review"], out["added"]), ("kept", 1))
        self.assertEqual(self.on_disk(), changed(OLD, {35: "line thirty-five\n"}))
        with self.assertRaises(rv.ReviewError):
            self.journal.review_action(edit["id"], "reject", 0)            # nothing left to decide

    def test_keep_all_and_reject_all(self):
        a = self.record("a.txt")
        self.assertEqual(self.journal.review_action(a["id"], "keep_all")["review"], "kept")
        b = self.record("b.txt")
        self.assertEqual(self.journal.review_action(b["id"], "reject_all")["review"], "reverted")
        self.assertEqual((self.on_disk("a.txt"), self.on_disk("b.txt")), (NEW, OLD))

    def test_undoing_a_new_file_removes_it(self):
        edit = self.record("new.txt", old="", new="hello\nworld\n", kind="added")
        self.assertEqual(edit["blocks"], [False])
        self.assertEqual(self.journal.review_action(edit["id"], "reject", 0)["review"], "reverted")
        self.assertFalse((self.root / "new.txt").exists())

    def test_nothing_the_person_did_since_is_overwritten(self):
        edit = self.record()
        (self.root / "f.txt").write_text(NEW + "my own line\n", encoding="utf-8", newline="")
        with self.assertRaisesRegex(rv.ReviewError, "changed after"):
            self.journal.review_action(edit["id"], "reject", 0)
        self.assertEqual(self.on_disk(), NEW + "my own line\n")
        (self.root / "f.txt").unlink()
        with self.assertRaisesRegex(rv.ReviewError, "changed after"):
            self.journal.review_action(edit["id"], "reject", 0)

    def test_an_earlier_edit_of_a_file_waits_for_the_later_one(self):
        mid = changed(OLD, {3: "line three\n"})
        first = self.record(old=OLD, new=mid)
        second = self.record(old=mid, new=NEW)
        states = {e["id"]: e["review"] for e in self.journal.view()["edits"]}
        self.assertEqual(states, {first["id"]: "superseded", second["id"]: "pending"})
        with self.assertRaisesRegex(rv.ReviewError, "later edit"):
            self.journal.review_action(first["id"], "reject", 0)
        self.journal.review_action(second["id"], "reject_all")             # undo the later one, then the earlier
        self.journal.review_action(first["id"], "reject_all")
        self.assertEqual(self.on_disk(), OLD)

    def test_a_file_outside_the_folder_is_never_touched(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text(NEW, encoding="utf-8")
        edit = self.record(file="../outside.txt", write=False)
        with self.assertRaisesRegex(rv.ReviewError, "outside"):
            self.journal.review_action(edit["id"], "reject_all")
        self.assertEqual(outside.read_text(encoding="utf-8"), NEW)

    def test_bad_requests(self):
        edit = self.record()
        for bad in (("explode", None), ("reject", None), ("reject", -1), ("keep", True), ("reject", "0")):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.journal.review_action(edit["id"], *bad)
        with self.assertRaisesRegex(rv.ReviewError, "not in the list"):
            self.journal.review_action("nope", "reject_all")
        with self.assertRaisesRegex(rv.ReviewError, "no block 3"):
            self.journal.review_action(edit["id"], "reject", 2)

    def test_an_edit_without_texts_is_shown_but_not_reviewable(self):
        out = self.journal.add([{"file": "x.txt", "kind": "modified", "added": 1, "removed": 0, "diff": "+a\n"}])["edits"][-1]
        self.assertEqual(out["review"], "unavailable")
        with self.assertRaisesRegex(rv.ReviewError, "cannot be reviewed"):
            self.journal.review_action(out["id"], "reject_all")

    def test_only_the_latest_edits_keep_their_text(self):
        from serve import tasks
        with mock.patch.object(tasks, "MAX_REVIEWABLE", 2):
            ids = [self.record(f"f{i}.txt")["id"] for i in range(4)]
        states = {e["id"]: e["review"] for e in self.journal.view()["edits"]}
        self.assertEqual([states[i] for i in ids], ["unavailable", "unavailable", "pending", "pending"])

    def test_clear_forgets_the_texts_too(self):
        self.record()
        self.journal.clear()
        self.assertEqual((self.journal.view()["edits"], json.loads(self.journal.review_path.read_text(encoding="utf-8"))), ([], {}))


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "Hello.", max_context=4096), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.svc.telemetry = type("T", (), {"snapshot": lambda self: {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}})()
        cls.svc.api_key = "k"
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.root = base / "ws"
        cls.root.mkdir()
        cls.patches = [mock.patch.object(handler, "_TASKS", TaskJournal(base / "j")),
                       mock.patch.object(handler, "_CHANGES", ChangeJournal(base / "j"))]
        for p in cls.patches:
            p.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def call(self, method, path, payload=None, key="k", headers=None):
        hdrs = {"Authorization": f"Bearer {key}"} if key else {}
        if method == "POST":
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=hdrs, method=method), timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_the_panel_flow_over_http(self):
        (self.root / "f.txt").write_text(NEW, encoding="utf-8", newline="")
        status, body = self.call("POST", "/api/changes", {"edits": [
            {"file": "f.txt", "kind": "modified", "review": {"root": str(self.root), "old_text": OLD, "new_text": NEW}}]})
        edit = body["edits"][-1]
        self.assertEqual((status, edit["review"], edit["blocks"]), (200, "pending", [False, False]))
        status, body = self.call("GET", "/api/changes")
        self.assertEqual(body["edits"][-1]["id"], edit["id"])
        status, body = self.call("POST", "/api/changes/review", {"id": edit["id"], "action": "reject", "block": 1})
        self.assertEqual((status, body["edit"]["blocks"]), (200, [False]))
        self.assertEqual((self.root / "f.txt").read_bytes().decode("utf-8"), changed(OLD, {3: "line three\n"}))
        status, body = self.call("POST", "/api/changes/review", {"id": edit["id"], "action": "reject", "block": 5})
        self.assertEqual((status, body["error"]["type"]), (409, "review"))
        self.assertEqual(self.call("POST", "/api/changes/review", {"id": edit["id"], "action": "explode"})[0], 400)
        self.assertEqual(self.call("POST", "/api/changes/review", {"action": "keep_all"})[0], 400)

    def test_review_needs_the_key_and_this_pages_origin(self):
        self.assertEqual(self.call("POST", "/api/changes/review", {"id": "x", "action": "keep_all"}, key="")[0], 401)
        self.assertEqual(self.call("POST", "/api/changes/review", {"id": "x", "action": "keep_all"}, key="wrong")[0], 401)
        foreign = self.call("POST", "/api/changes/review", {"id": "x", "action": "keep_all"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(foreign[0], 403)


if __name__ == "__main__":
    unittest.main()
