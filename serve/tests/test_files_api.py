"""The folders a phone may browse: the ones added in the Agent tab and the Chat's, and nothing outside them."""
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from serve import handler, userdata
from serve.files_api import FilesApi, FilesError
from serve.projects import ProjectStore
from serve.tests import test_api_plans as base


class UnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.site = self.root / "site"
        (self.site / "src").mkdir(parents=True)
        (self.site / ".basalt").mkdir()
        (self.site / ".git").mkdir()
        (self.site / "src" / "app.py").write_bytes(b"print(1)" + bytes([10]))
        (self.site / "readme.md").write_bytes(b"hi")
        (self.site / ".basalt" / "secret").write_bytes(b"s")
        (self.root / "outside.txt").write_bytes(b"o")
        self.api = FilesApi(lambda: [("site", self.site), ("gone", self.root / "missing")])

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_folders_that_exist_are_offered(self):
        self.assertEqual([r["name"] for r in self.api.roots()], ["site"])

    def test_a_folder_lists_folders_first_and_never_the_agents_own_or_git(self):
        got = self.api.list(str(self.site))
        self.assertEqual([(e["name"], e["dir"]) for e in got["entries"]], [("src", True), ("readme.md", False)])
        self.assertEqual(self.api.list(str(self.site / "src"))["entries"][0]["size"], 9)

    def test_nothing_outside_the_roots_or_in_hidden_folders_is_listed_or_read(self):
        for bad in (str(self.root), str(self.root / "outside.txt"), str(self.site / ".." / "outside.txt"), str(self.site / ".basalt"), str(self.site / ".git"),
                    str(self.site / ".basalt" / "secret"), "", "x\0y", "C:\\Windows" if os.name == "nt" else "/etc"):
            with self.assertRaises(FilesError, msg=bad):
                self.api.list(bad)
            with self.assertRaises(FilesError, msg=bad):
                self.api.file(bad)
        with self.assertRaises(FilesError):
            self.api.list(str(self.site / "readme.md"))                       # a file is not a folder
        with self.assertRaises(FilesError):
            self.api.file(str(self.site / "src"))                             # a folder is not a file
        self.assertEqual(self.api.file(str(self.site / "src" / "app.py"))[1], "app.py")


class RouteTest(base.PlansApiBase):
    def test_the_routes_serve_the_shared_folders_and_a_file_in_one(self):
        folder = Path(self.tmp.name) / "shared"
        (folder / "docs").mkdir(parents=True)
        (folder / "docs" / "a è.txt").write_bytes(b"hello")
        projects = ProjectStore(Path(self.tmp.name) / "pj")
        projects.add(str(folder))
        with mock.patch.object(handler, "_PROJECTS", projects):
            code, roots = self.call("GET", "/api/files/roots")
            names = [r["name"] for r in roots["roots"]]
            self.assertEqual((code, "shared" in names), (200, True))                       # the Chat's folder too, once it exists
            path = next(r["path"] for r in roots["roots"] if r["name"] == "shared")
            code, listing = self.call("GET", "/api/files/list?path=" + urllib.parse.quote(path))
            self.assertEqual((code, [e["name"] for e in listing["entries"]]), (200, ["docs"]))
            inner = listing["entries"][0]["path"]
            code, listing = self.call("GET", "/api/files/list?path=" + urllib.parse.quote(inner))
            file_path = listing["entries"][0]["path"]
            with urllib.request.urlopen(self.base + "/api/files/read?path=" + urllib.parse.quote(file_path), timeout=10) as r:
                self.assertEqual((r.status, r.read()), (200, b"hello"))
                self.assertIn("attachment", r.headers["Content-Disposition"])
            self.assertEqual(self.call("GET", "/api/files/list?path=" + urllib.parse.quote(str(Path(self.tmp.name))))[0], 404)
            self.assertEqual(self.call("GET", "/api/files/read?path=" + urllib.parse.quote(str(folder / ".." / "x")))[0], 404)
            self.assertEqual(self.call("GET", "/api/files/list")[0], 400)


if __name__ == "__main__":
    unittest.main()
