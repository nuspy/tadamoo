"""The Database section's server side: the project's database and its settings, 'Connect and prepare' as a job, the import of the old file format as a job, and the routes."""
import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from serve import handler
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.plans import PlanService
from serve.rag_api import RagApi
from serve.service import Service
from serve.tasks import ChangeJournal, TaskJournal
from serve.util.text import ByteTokenizer
from tadamoo.rag import RagError, RagStore, project_key
from tadamoo.rag.backends import open_backend, prepare as prep
from tadamoo.rag.backends.sqlite_vec import SqliteVecBackend
from test_rag_backends import PROJ, FakePg, make_legacy, tree_hash


def api_for(tmp_path):
    """An API over a store whose router never opens a network database: a PostgreSQL configuration opens a SQLite file in its place."""
    store = RagStore(tmp_path / "rag")
    store.router.open_fn = lambda cfg, pw: SqliteVecBackend(tmp_path / "stand-in.sqlite") if cfg["kind"] != "sqlite" else open_backend(cfg, pw)
    return RagApi(store, vault_dir=tmp_path / "vault"), store


def finish(api, job, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        got = api.route_get(["jobs", job["id"]])["job"]
        if got["state"] != "running":
            return got
        time.sleep(0.02)
    raise AssertionError("the job did not end")


def started(api, parts, body):
    return api.route_post(parts, {}, json.dumps(body).encode())["job"]


def test_a_project_with_no_database_yet_shows_the_default_and_what_waits_to_be_imported(tmp_path):
    api, store = api_for(tmp_path)
    make_legacy(tmp_path / "rag")
    view = api.route_get(["db"], {"project": [PROJ]})
    assert view["exists"] is False and view["config"]["kind"] == "sqlite" and view["config"]["path"].endswith(f"{PROJ}.sqlite") and view["default_path"] == view["config"]["path"]
    assert view["waiting_here"] == ["old-abc123"] and view["waiting"][0]["project"] == PROJ and view["has_password"] is False
    assert set(view["components"]) == {"sqlite_vec", "postgres_driver", "mysql_driver"} and view["kinds"] == ["sqlite", "postgres", "mysql", "other"]
    assert not (tmp_path / "rag" / "db").exists()                                          # looking creates nothing
    with pytest.raises(RagError, match="project"):
        api.route_get(["db"], {"project": ["not-a-key"]})
    store.router.close()


def test_the_password_goes_to_the_vault_and_the_settings_name_the_entry_only(tmp_path):
    api, store = api_for(tmp_path)
    out = api.route_post(["db", "config"], {}, json.dumps({"project": PROJ, "password": "s3cret", "config": {"kind": "postgres", "host": "db.invalid", "dbname": "kb", "user": "me"}}).encode())
    assert out["config"]["vault"] == f"rag-db-{PROJ}" and out["has_password"] is True and out["config"]["kind"] == "postgres"
    everything = "".join(p.read_text(errors="ignore") for p in (tmp_path / "rag").rglob("*.json"))
    assert "s3cret" not in everything and "password" not in everything
    for bad in ({"project": PROJ, "config": {"kind": "postgres"}}, {"project": PROJ, "config": {"kind": "sqlite"}}, {"project": PROJ, "config": {"kind": "postgres", "dbname": "d", "password": "x"}}, {"project": PROJ}):
        with pytest.raises(RagError):
            api.route_post(["db", "config"], {}, json.dumps(bad).encode())
    back = api.route_post(["db", "config"], {}, json.dumps({"project": PROJ, "reset": True}).encode())
    assert back["config"]["kind"] == "sqlite" and not (tmp_path / "rag" / "db" / f"{PROJ}.json").exists()
    store.router.close()


def test_connect_and_prepare_is_a_job_that_reports_each_step_and_saves_the_settings(tmp_path):
    api, store = api_for(tmp_path)
    job = started(api, ["db", "prepare"], {"project": PROJ})
    done = finish(api, job)
    assert done["state"] == "done" and done["result"]["ok"] and [s["step"] for s in done["data"]["steps"]] == ["connect", "extension", "configure"]
    assert (tmp_path / "rag" / "db" / f"{PROJ}.json").is_file() and api.route_get(["db"], {"project": [PROJ]})["info"]["schema"] == 1
    store.router.close()


def test_a_postgres_that_lacks_the_extension_stops_at_that_step_and_goes_on_with_plan_b(tmp_path, monkeypatch):
    api, store = api_for(tmp_path)
    real = prep.prepare
    conn = FakePg({"current_setting('server_version')": [("me", "17.2", "kb", False, True)]})
    monkeypatch.setattr(prep, "selftest", lambda b: {"vectors": "app", "words": "native", "ms": 1})
    monkeypatch.setattr(prep, "prepare", lambda cfg, pw, tell, options, connect=None: real(cfg, pw, tell, options, connect=lambda: conn))
    cfg = {"kind": "postgres", "host": "localhost", "dbname": "kb", "user": "me"}
    first = finish(api, started(api, ["db", "prepare"], {"project": PROJ, "config": cfg, "password": "pw"}))
    assert first["state"] == "done" and not first["result"]["ok"] and first["result"]["stopped_at"] == "extension" and first["data"]["steps"][1]["state"] == "not_available"
    assert not (tmp_path / "rag" / "db" / f"{PROJ}.json").exists()                          # nothing is saved for a database that is not ready
    second = finish(api, started(api, ["db", "prepare"], {"project": PROJ, "config": cfg, "password": "pw", "plan_b": "blob"}))
    assert second["result"]["ok"] and api.route_get(["db"], {"project": [PROJ]})["config"]["vectors"] == "blob"
    saved = (tmp_path / "rag" / "db" / f"{PROJ}.json").read_text()
    assert "pw" not in saved.replace("password", "") or '"pw"' not in saved
    store.router.close()


def test_the_administrator_of_a_run_is_used_for_it_and_kept_in_no_file_and_not_in_the_vault(tmp_path, monkeypatch):
    api, store = api_for(tmp_path)
    word = "an-admin-word-for-tests"
    seen = {}
    real = prep.prepare

    def fake(cfg, pw, tell, options, connect=None):
        seen.update(options, project_word=pw)
        return real(cfg, pw, tell, {k: v for k, v in options.items() if k not in ("superuser", "superpassword")}, connect=lambda: FakePg({"current_setting('server_version')": [("me", "17.2", "kb", False, True)]}))
    monkeypatch.setattr(prep, "prepare", fake)
    monkeypatch.setattr(prep, "selftest", lambda b: {"vectors": "app", "words": "native", "ms": 1})
    cfg = {"kind": "postgres", "host": "localhost", "dbname": "kb", "user": "me"}
    done = finish(api, started(api, ["db", "prepare"], {"project": PROJ, "config": cfg, "password": "project-word", "plan_b": "blob", "superuser": "postgres", "superpassword": word}))
    assert done["state"] == "done" and seen["superuser"] == "postgres" and seen["superpassword"] == word and seen["project_word"] == "project-word"
    assert word not in json.dumps(done)
    for f in (tmp_path).rglob("*"):
        if f.is_file() and f.suffix != ".sqlite":
            assert word not in f.read_text(errors="ignore"), f
    assert api.route_get(["db"], {"project": [PROJ]})["config"]["user"] == "me"
    with pytest.raises(RagError):
        api.route_post(["db", "prepare"], {}, json.dumps({"project": PROJ, "config": cfg, "superuser": 5}).encode())
    store.router.close()


def test_the_import_is_a_job_that_copies_what_waits_reports_each_archive_and_leaves_the_folders_alone(tmp_path):
    api, store = api_for(tmp_path)
    folder = make_legacy(tmp_path / "rag")
    broken = tmp_path / "rag" / "broken-000000"
    broken.mkdir()
    (broken / "meta.json").write_text("{nope")
    before = tree_hash(folder)
    done = finish(api, started(api, ["db", "import"], {"archives": ["old-abc123", "broken-000000"]}))
    assert done["state"] == "done" and done["result"]["source_untouched"] is True and done["result"]["imported"][0]["chunks"] == 6
    assert done["result"]["failed"][0]["archive"] == "broken-000000" and "meta.json" in done["result"]["failed"][0]["error"]
    assert tree_hash(folder) == before and api.route_get(["db"], {"project": [PROJ]})["archives"] == 1 and api.route_get(["db"], {"project": [PROJ]})["waiting_here"] == []
    broken.rename(tmp_path / "broken-moved")
    with pytest.raises(RagError, match="no archive in the old file format"):
        api.route_post(["db", "import"], {}, b"{}")
    store.router.close()


class RoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.rag = RagStore(base / "rag")
        cls.patches = [mock.patch.object(handler, "_TASKS", TaskJournal(base / "j")), mock.patch.object(handler, "_CHANGES", ChangeJournal(base / "j")),
                       mock.patch.object(handler, "_PLANS", PlanService(base / "planner")), mock.patch.object(handler, "_RAG", cls.rag)]
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
        cls.rag.router.close()
        cls.tmp.cleanup()

    def call(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"} if method == "POST" else {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_the_database_routes_work_over_http(self):
        status, view = self.call("GET", f"/api/rag/db?project={PROJ}")
        self.assertEqual((status, view["config"]["kind"], view["exists"]), (200, "sqlite", False))
        status, started_job = self.call("POST", "/api/rag/db/prepare", {"project": PROJ})
        self.assertEqual(status, 200)
        jid = started_job["job"]["id"]
        for _ in range(200):
            job = self.call("GET", f"/api/rag/jobs/{jid}")[1]["job"]
            if job["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual((job["state"], job["result"]["ok"]), ("done", True))
        self.assertEqual(self.call("GET", f"/api/rag/db?project={PROJ}")[1]["exists"], True)
        self.assertEqual(self.call("POST", "/api/rag/db/config", {"project": PROJ, "config": {"kind": "oracle"}})[0], 400)


# -- the packages the databases need ------------------------------------------------------------------------------------

class FakePip:
    def __init__(self, lines, code=0):
        self.lines, self.code, self.stopped = lines, code, False
        self.stdout = iter(l + "\n" for l in lines)

    def wait(self):
        return self.code

    def terminate(self):
        self.stopped = True


def test_the_components_list_says_what_pip_would_install_how_big_and_whether_it_is_there():
    from tadamoo.rag.backends import components
    rows = {r["id"]: r for r in components.listing()}
    assert set(rows) == {"sqlite-vec", "psycopg", "pymysql"} and rows["sqlite-vec"]["requirement"] == "sqlite-vec==0.1.6" and rows["psycopg"]["requirement"] == "psycopg[binary]==3.3.4"
    assert all(r["size_mb"] > 0 and r["what"]["en"] and r["what"]["it"] and r["python"] for r in rows.values())
    with pytest.raises(RagError):
        components.find("numpy")


def test_installing_a_component_runs_one_pinned_pip_command_and_reports_a_failure_with_the_end_of_its_output():
    from tadamoo.rag.backends import components
    told, cmds = [], []
    done = components.install("psycopg", told.append, popen=lambda cmd, **k: cmds.append(cmd) or FakePip(["Collecting psycopg", "Installed"]))
    assert done["ok"] is True and cmds[0][1:4] == ["-m", "pip", "install"] and cmds[0][-1] == "psycopg[binary]==3.3.4" and "--no-input" in cmds[0] and "Installed" in told
    with pytest.raises(RagError, match="could not install sqlite-vec==0.1.6.*no matching distribution"):
        components.install("sqlite-vec", popen=lambda cmd, **k: FakePip(["ERROR: no matching distribution"], 1))
    stop = type("E", (), {"is_set": lambda self: True})()
    with pytest.raises(RagError, match="cancelled"):
        components.install("pymysql", cancel=stop, popen=lambda cmd, **k: FakePip(["one", "two"]))


def test_a_component_is_installed_only_by_a_job_the_person_starts_and_the_databases_are_opened_again_after(tmp_path):
    api, store = api_for(tmp_path)
    store.router.for_project(PROJ)
    calls = []

    def fake(component_id, tell, cancel):
        calls.append(component_id)
        tell("Collecting sqlite-vec")
        return {"ok": True, "version": "0.1.6"}
    api.database.installer = fake
    assert api.route_get(["db", "components"], {})["components"][0]["id"] == "sqlite-vec" and calls == []              # looking installs nothing
    done = finish(api, started(api, ["db", "components", "install"], {"id": "sqlite-vec"}))
    assert calls == ["sqlite-vec"] and done["state"] == "done" and done["result"]["version"] == "0.1.6" and done["data"]["requirement"] == "sqlite-vec==0.1.6"
    assert store.router._open == {}
    with pytest.raises(RagError):
        api.route_post(["db", "components", "install"], {}, json.dumps({"id": "something-else"}).encode())
    store.router.close()
