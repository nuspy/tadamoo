"""Our own PostgreSQL, installed and managed from Knowledge > Database: detected, installed, started on real use by a project and stopped after being idle."""
import hashlib
import time
import zipfile
from pathlib import Path

import pytest

from serve.rag_api import RagApi
from serve.rag_postgres_api import RagPostgresApi
from tadamoo import postgres_setup
from tadamoo.rag import RagError, RagStore


def package(tmp_path):
    pkg = tmp_path / "pg.zip"
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("pgsql/bin/" + postgres_setup.EXE, b"MZ-postgres")
        z.writestr("pgsql/bin/initdb" + (".exe" if postgres_setup.EXE.endswith(".exe") else ""), b"MZ-initdb")
        z.writestr("pgsql/bin/pg_ctl" + (".exe" if postgres_setup.EXE.endswith(".exe") else ""), b"MZ-pg_ctl")
    return pkg


class FakeRun:
    def __init__(self, returncode=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = returncode, out, err


def fake_runner_factory(data_dir):
    """A runner that behaves like initdb/pg_ctl well enough for the API: makes PG_VERSION on initdb, answers "running" on status once started."""
    state = {"running": False}

    def runner(argv, **kw):
        tool = Path(argv[0]).name
        if "initdb" in tool:
            d = Path(argv[argv.index("-D") + 1])
            d.mkdir(parents=True, exist_ok=True)
            (d / "PG_VERSION").write_text("18")
            return FakeRun(0)
        if "pg_ctl" in tool:
            if "start" in argv:
                state["running"] = True
                return FakeRun(0)
            if "stop" in argv:
                state["running"] = False
                return FakeRun(0)
            if "status" in argv:
                return FakeRun(0 if state["running"] else 1)
        return FakeRun(0)
    return runner


def make(tmp_path, monkeypatch, found=None):
    store = RagStore(tmp_path / "rag")
    vault = {}
    pkg = package(tmp_path)
    monkeypatch.setattr(postgres_setup, "RELEASE", {**postgres_setup.RELEASE, "sha256": hashlib.sha256(pkg.read_bytes()).hexdigest()})
    runner = fake_runner_factory(tmp_path / "tools" / "data")
    api = RagPostgresApi(store, tools_dir=tmp_path / "tools", detect=lambda: found or {"programs": [], "broken": [], "listening": []},
                         fetch=lambda url, dest: dest.write_bytes(pkg.read_bytes()), runner=runner, put_secret=vault.__setitem__, get_secret=lambda entry: vault[entry])
    return store, api, vault, runner


def test_the_overview_says_what_is_here_and_what_our_own_copy_is(tmp_path, monkeypatch):
    store, api, _, _ = make(tmp_path, monkeypatch)
    got = api.route_get(["postgres"])
    assert got["exists"] is False and got["own"] == {"installed": False, "running": False, "host": "", "port": "", "user": "", "database": ""}
    assert [o["id"] for o in got["options"]] == ["install_separate", "skip"]
    there = make(tmp_path, monkeypatch, {"programs": [{"path": "x", "size": 1}], "broken": [], "listening": []})[1].overview()
    assert there["exists"] is True and [o["id"] for o in there["options"]] == ["use_existing", "install_separate", "skip"]


def test_installing_our_own_downloads_checks_starts_it_and_the_password_goes_to_the_vault(tmp_path, monkeypatch):
    from serve import rag_postgres_api
    store, api, vault, runner = make(tmp_path, monkeypatch)
    try:
        got = api.route_post(["postgres", "install"], {})
        own = got["own"]
        assert own["installed"] and own["running"] and own["host"] == "127.0.0.1" and own["port"] >= 5500 and own["user"] == postgres_setup.LOCAL_USER
        assert rag_postgres_api.SUPERUSER_ENTRY in vault and len(vault[rag_postgres_api.SUPERUSER_ENTRY]) >= 24
        assert (tmp_path / "tools" / "bin" / postgres_setup.EXE).read_bytes() == b"MZ-postgres"
        assert api.password() == vault[rag_postgres_api.SUPERUSER_ENTRY]
        assert api.route_post(["postgres", "stop"], {})["own"]["running"] is False
        again = api.route_post(["postgres", "start"], {})
        assert again["own"]["running"] is True
        with pytest.raises(RagError):
            api.route_post(["postgres", "nothing"], {})
    finally:
        rag_postgres_api.stop_all()


def test_nothing_is_started_without_an_installation_of_our_own(tmp_path, monkeypatch):
    store, api, _, _ = make(tmp_path, monkeypatch)
    with pytest.raises(RagError, match="install it first"):
        api.start_own()
    assert api.autostart() is False


def test_idle_postgres_is_stopped_by_the_sweep_and_a_project_pointed_at_it_touches_it_and_restarts_it(tmp_path, monkeypatch):
    from serve import rag_postgres_api
    store, api, vault, runner = make(tmp_path, monkeypatch)
    try:
        own = api.route_post(["postgres", "install"], {})["own"]
        key = str(tmp_path / "tools")
        assert key in rag_postgres_api._LAST_USED
        rag_postgres_api.sweep(clock=lambda: time.time() + rag_postgres_api.IDLE_S + 1)
        assert api.overview()["own"]["running"] is False

        api.wire()
        store.router.for_project("")                                                          # the project's database is "sqlite" by default: not ours, no touch
        assert api.overview()["own"]["running"] is False
        store.router.set_config("", {"kind": "postgres", "host": "127.0.0.1", "port": own["port"], "dbname": "d", "user": "tadamoo"})
        store.router.close("")
        store.router.open_fn = lambda cfg, pw: type("B", (), {"prepare": lambda self: None, "close": lambda self: None})()
        store.router.for_project("")                                                          # pointed at our own copy: touched and restarted
        assert api.overview()["own"]["running"] is True
    finally:
        rag_postgres_api.stop_all()


def test_use_own_postgres_starts_it_if_needed_and_asks_prepare_to_make_the_projects_own_database(tmp_path, monkeypatch):
    from serve import rag_postgres_api
    store = RagStore(tmp_path / "rag")
    vault = {}
    pkg = package(tmp_path)
    monkeypatch.setattr(postgres_setup, "RELEASE", {**postgres_setup.RELEASE, "sha256": hashlib.sha256(pkg.read_bytes()).hexdigest()})
    runner = fake_runner_factory(tmp_path / "tools" / "postgres" / "data")
    api = RagApi(store, vault_dir=tmp_path / "vault", postgres_tools_dir=tmp_path / "tools" / "postgres")
    api.postgres._fetch = lambda url, dest: dest.write_bytes(pkg.read_bytes())
    api.postgres._runner = runner
    api.postgres._put = vault.__setitem__
    api.postgres._get = lambda entry: vault[entry]
    PROJECT = "a" * 12
    try:
        with pytest.raises(RagError, match="install"):
            api.use_own_postgres(PROJECT)

        api.postgres.install_separate()                                                  # installed but not started in this request's api object
        api.postgres._service = None
        seen = {}
        api.database.prepare = lambda req: seen.update(req) or {"ok": True, "steps": []}
        out = api.use_own_postgres(PROJECT)
        assert out == {"ok": True, "steps": []}
        assert seen["project"] == PROJECT and seen["create_db"] is True and seen["password"] == vault[rag_postgres_api.SUPERUSER_ENTRY]
        assert seen["config"] == {"kind": "postgres", "host": "127.0.0.1", "port": api.postgres._own()["port"], "dbname": postgres_setup.LOCAL_DATABASE,
                                   "user": postgres_setup.LOCAL_USER, "prefix": f"rag_{PROJECT}_"}
        assert api.postgres.overview()["own"]["running"] is True                          # autostart() really started it, not just pretended to
    finally:
        rag_postgres_api.stop_all()


def test_wiring_only_touches_projects_that_actually_point_at_our_own_copy(tmp_path, monkeypatch):
    store, api, vault, runner = make(tmp_path, monkeypatch)
    api.route_post(["postgres", "install"], {})
    try:
        api.wire()
        store.router.set_config("", {"kind": "postgres", "host": "elsewhere.example", "port": 5432, "dbname": "d", "user": "x"})
        store.router.open_fn = lambda cfg, pw: type("B", (), {"prepare": lambda self: None, "close": lambda self: None})()
        api.route_post(["postgres", "stop"], {})
        store.router.for_project("")                                                          # a different PostgreSQL: never touched or restarted
        assert api.overview()["own"]["running"] is False
    finally:
        from serve import rag_postgres_api
        rag_postgres_api.stop_all()
