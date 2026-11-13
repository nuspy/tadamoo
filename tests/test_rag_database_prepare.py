"""Making a project's database ready: the three steps for SQLite and, with stand-in connections, for PostgreSQL in each state its vector extension can be in."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.rag.backends import prepare as prep
from test_rag_backends import FakePg

PG = {"kind": "postgres", "host": "localhost", "dbname": "kb", "user": "me"}
FACTS = [("me", "17.2", "kb", False, True)]
OK_SELFTEST = lambda b: {"vectors": "native", "words": "native", "ms": 1}      # noqa: E731


@pytest.fixture(autouse=True)
def no_real_test(monkeypatch):
    """The stand-in connection keeps no data, so the small test archive is not run against it (the tests of the backends do that on a real SQLite file)."""
    monkeypatch.setattr(prep, "selftest", OK_SELFTEST)


def run(conn, cfg=PG, **options):
    seen = []
    return prep.prepare(cfg, "pw", seen.append, options, connect=lambda: conn), seen


def conn_with(extension=None, available=None, late=False, **more):
    """`extension` is the version pgvector reports; with `late` it reports it only once CREATE EXTENSION has run."""
    answers = {"current_setting('server_version')": FACTS, **more}
    if available is not None:
        answers["FROM pg_available_extensions"] = [available]
    if extension is not None:
        answers["extversion"] = lambda c: [(extension,)] if (c.created or not late) else []
    return FakePg(answers)


def test_sqlite_goes_through_the_three_steps_and_says_what_the_extension_is(tmp_path):
    out = prep.prepare({"kind": "sqlite", "path": str(tmp_path / "x.sqlite")})
    assert out["ok"] and [s["step"] for s in out["steps"]] == ["connect", "extension", "configure"] and all(s["ok"] for s in out["steps"])
    assert out["steps"][1]["state"] in ("present", "not_installed", "not_loadable") and (tmp_path / "x.sqlite").is_file()


def test_a_sqlite_file_that_cannot_be_opened_stops_at_the_first_step(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    out = prep.prepare({"kind": "sqlite", "path": str(blocker / "inside.sqlite")})
    assert not out["ok"] and out["stopped_at"] == "connect" and out["steps"][0]["state"] == "failed"


def test_postgres_with_the_extension_enabled_goes_all_the_way_and_saves_halfvec_mode():
    conn = conn_with(extension="0.8.0", available=("0.8.0", "0.8.0"))
    out, seen = run(conn)
    assert out["ok"] and [s["step"] for s in out["steps"]] == ["connect", "extension", "configure"] and out["steps"][1]["state"] == "present"
    assert out["vectors"] == "auto" and out["config"]["vectors"] == "auto" and "password" not in str(out["config"])
    assert [s["step"] for s in seen] == ["connect", "extension", "configure"] and out["steps"][0]["user"] == "me" and out["steps"][0]["version"] == "17.2"
    assert any("USING GIN (tsv)" in s for s, _ in conn.sql)


def test_an_extension_that_is_on_the_server_but_not_in_the_database_is_enabled_by_the_flow():
    conn = conn_with(extension="0.8.0", available=("0.8.0", None), late=True)
    out, _ = run(conn)
    assert out["ok"] and out["steps"][1]["state"] == "enabled" and any(s == "CREATE EXTENSION IF NOT EXISTS vector" for s, _ in conn.sql)


def test_a_user_who_may_not_enable_it_is_told_exactly_what_to_ask_for():
    conn = conn_with(extension=None, available=("0.8.0", None))
    conn.fail = {"CREATE EXTENSION": RuntimeError("permission denied to create extension")}
    out, _ = run(conn)
    step = out["steps"][1]
    assert not out["ok"] and out["stopped_at"] == "extension" and step["state"] == "needs_admin" and "CREATE EXTENSION vector;" in step["message"] and step["sql"] == "CREATE EXTENSION vector;"


def test_an_old_extension_is_updated_or_refused_with_the_reason():
    old = conn_with(extension="0.5.1", available=("0.8.0", "0.5.1"))
    out, _ = run(old)
    assert not out["ok"] and out["steps"][1]["state"] == "too_old" and any(s == "ALTER EXTENSION vector UPDATE" for s, _ in old.sql) and "steps" in out["steps"][1]


def test_a_server_without_the_extension_shows_the_steps_and_waits_for_a_choice_of_plan_b(monkeypatch):
    monkeypatch.setattr(prep.platform, "system", lambda: "Linux")
    conn = conn_with(extension=None, available=None)
    out, _ = run(conn)
    step = out["steps"][1]
    assert not out["ok"] and out["stopped_at"] == "extension" and step["state"] == "not_available" and step["plan_b"] == ["blob", "file"] and step["local"] is True
    assert any("postgresql-17-pgvector" in x for x in step["steps"]) and not any("CREATE EXTENSION IF NOT EXISTS" in s for s, _ in conn.sql)


def test_plan_b_blob_goes_on_with_the_vectors_in_a_binary_column_searched_by_the_application():
    out, _ = run(conn_with(extension=None, available=None), plan_b="blob")
    assert out["ok"] and out["vectors"] == "blob" and out["config"]["vectors"] == "blob" and out["steps"][-1]["step"] == "configure"


def test_plan_b_file_needs_the_path_of_the_local_vector_file_and_keeps_it_in_the_settings(tmp_path):
    out, _ = run(conn_with(extension=None, available=None), plan_b="file")
    assert not out["ok"] and "path" in out["steps"][1]["message"]
    out, _ = run(conn_with(extension=None, available=None), plan_b="file", vec_file=str(tmp_path / "vectors.sqlite"))
    assert out["config"]["vectors"] == "file" and out["config"]["vec_file"] == str(tmp_path / "vectors.sqlite")


def test_installing_it_from_here_is_offered_only_on_this_pc_with_the_rights_and_a_command(monkeypatch):
    monkeypatch.setattr(prep.platform, "system", lambda: "Linux")
    monkeypatch.setattr(prep, "is_admin", lambda: True)
    monkeypatch.setattr(prep.shutil, "which", lambda name: "/usr/bin/" + name if name == "apt-get" else None)
    ran = []
    monkeypatch.setattr(prep.subprocess, "run", lambda cmd, **k: ran.append(cmd) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    conn = conn_with(extension="0.8.0", available=None, late=True)
    out, _ = run(conn, install_local=True)
    assert ran == [["apt-get", "install", "-y", "postgresql-17-pgvector"]] and out["ok"] and out["steps"][1]["state"] == "installed"
    ran.clear()
    monkeypatch.setattr(prep, "is_admin", lambda: False)
    out, _ = run(conn_with(extension=None, available=None), install_local=True)
    assert ran == [] and out["steps"][1]["can_install"] is False
    remote = dict(PG, host="db.example.org")
    monkeypatch.setattr(prep, "is_admin", lambda: True)
    out, _ = run(conn_with(extension=None, available=None), cfg=remote, install_local=True)
    assert ran == [] and out["steps"][1]["local"] is False


def test_windows_gets_the_steps_for_windows_and_no_command(monkeypatch):
    assert prep.install_command(17, "Windows") is None and "Visual Studio" in prep.install_steps(17, "Windows")[0] and "brew install pgvector" in prep.install_steps(17, "Darwin")[0]


def test_a_connection_that_fails_stops_at_the_first_step_and_a_missing_driver_names_the_component(monkeypatch):
    def refuse():
        raise OSError("connection refused")
    out = prep.prepare(PG, "pw", None, {}, connect=refuse)
    assert not out["ok"] and out["stopped_at"] == "connect" and "could not be reached" in out["steps"][0]["message"]
    monkeypatch.setattr(prep, "driver_name", lambda: "")
    out = prep.prepare(PG, "pw", None, {})
    assert out["steps"][0]["state"] == "no_driver" and out["steps"][0]["component"] == "psycopg"


def test_a_user_with_no_right_to_create_tables_is_stopped_with_that_said():
    conn = conn_with(extension="0.8.0", available=("0.8.0", "0.8.0"), **{"current_setting('server_version')": [("me", "17.2", "kb", False, False)]})
    out, _ = run(conn)
    assert not out["ok"] and out["steps"][0]["state"] == "no_rights" and "may not create tables" in out["steps"][0]["message"]


def test_a_local_database_that_does_not_exist_is_created_only_when_asked(monkeypatch):
    class Missing(Exception):
        pass
    state = {"made": False}
    calls = []

    def connect():
        calls.append(1)
        if not state["made"]:
            raise Missing('database "kb" does not exist')
        return conn_with(extension="0.8.0", available=("0.8.0", "0.8.0"))
    out = prep.prepare(PG, "pw", None, {}, connect=connect)
    assert not out["ok"] and out["steps"][0]["can_create_database"] is True

    monkeypatch.setattr(prep, "_create_database", lambda params, pw, conn: state.__setitem__("made", True))
    out = prep.prepare(PG, "pw", None, {"create_db": True}, connect=connect)
    assert out["ok"] and state["made"]


def test_other_databases_have_no_extension_and_the_flow_says_so(tmp_path):
    out = prep.prepare({"kind": "other", "driver": "sqlite3", "dsn": str(tmp_path / "o.db"), "dialect": "sqlite"})
    assert out["ok"] and out["steps"][1]["state"] == "none" and out["vectors"] == "app"
