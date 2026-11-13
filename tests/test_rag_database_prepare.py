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


# -- an administrator for one run -----------------------------------------------------------------------------

ADMIN_WORD = "an-admin-word-for-tests"


class Down(Exception):
    pass


def admin_conn(target=None, **answers):
    """A stand-in administrator connection; its CREATE EXTENSION / GRANT makes the project user's connection able to do what it could not."""
    def opened(c):
        if target is not None:
            target.created, target.fail = True, {}
        return []
    return FakePg({"CREATE EXTENSION": opened, **answers})


def test_an_administrator_enables_the_extension_a_user_cannot_and_the_word_is_kept_nowhere():
    user = conn_with(extension="0.8.0", available=("0.8.0", None), late=True)
    user.fail = {"CREATE EXTENSION": RuntimeError("permission denied to create extension")}
    admin = admin_conn(user)
    asked = []
    out, seen = run(user, superuser="postgres", superpassword=ADMIN_WORD, admin_connect=lambda db: (asked.append(db), admin)[1])
    assert out["ok"] and out["steps"][1]["state"] == "enabled" and out["steps"][1]["used_superuser"] is True and asked == ["kb"]
    assert any(s == "CREATE EXTENSION IF NOT EXISTS vector" for s, _ in admin.sql)
    assert ADMIN_WORD not in str(out) and ADMIN_WORD not in str(seen) and "superpassword" not in str(out["config"])


def test_nothing_is_asked_of_an_administrator_when_the_extension_and_the_user_are_there():
    conn = conn_with(extension="0.8.0", available=("0.8.0", "0.8.0"))

    def never(db):
        raise AssertionError("the administrator was used although nothing needed it")
    out, _ = run(conn, superuser="postgres", superpassword=ADMIN_WORD, admin_connect=never)
    assert out["ok"] and out["steps"][1]["state"] == "present"


def test_without_an_administrator_the_step_says_one_would_do_it():
    conn = conn_with(extension=None, available=("0.8.0", None))
    conn.fail = {"CREATE EXTENSION": RuntimeError("permission denied to create extension")}
    out, _ = run(conn)
    assert out["steps"][1]["state"] == "needs_admin" and out["steps"][1]["needs_superuser"] is True


def test_an_administrator_who_cannot_either_leaves_the_step_asking_for_one_who_can():
    user = conn_with(extension=None, available=("0.8.0", None))
    user.fail = {"CREATE EXTENSION": RuntimeError("permission denied to create extension")}
    admin = FakePg({}, fail={"CREATE EXTENSION": RuntimeError("must be superuser")})
    out, _ = run(user, superuser="someone", superpassword=ADMIN_WORD, admin_connect=lambda db: admin)
    assert not out["ok"] and out["stopped_at"] == "extension" and out["steps"][1]["state"] == "needs_admin" and ADMIN_WORD not in str(out)


def test_an_administrator_makes_the_missing_user_and_database_then_the_flow_goes_on():
    state = {"made": False}
    ready = conn_with(extension="0.8.0", available=("0.8.0", "0.8.0"))

    def connect():
        if not state["made"]:
            raise Down('FATAL: password authentication failed for user "kb_user"')
        return ready
    admin = FakePg({"FROM pg_roles": [], "FROM pg_database": []})
    original = admin.cursor

    def cursor():
        c = original()
        run_ = c.execute

        def execute(sql, params=()):
            if sql.startswith("CREATE DATABASE"):
                state["made"] = True
            return run_(sql, params)
        c.execute = execute
        return c
    admin.cursor = cursor
    cfg = {**PG, "user": "kb_user"}
    out = prep.prepare(cfg, "project-word", None, {"superuser": "postgres", "superpassword": ADMIN_WORD, "admin_connect": lambda db: admin}, connect=connect)
    sql = [s for s, _ in admin.sql]
    assert out["ok"] and out["steps"][0]["created"] == ["role", "database"]
    assert 'CREATE ROLE "kb_user" LOGIN PASSWORD \'project-word\'' in sql and 'CREATE DATABASE "kb" OWNER "kb_user"' in sql
    assert ADMIN_WORD not in str(out) and ADMIN_WORD not in str(sql)


def test_a_user_and_database_that_exist_are_not_made_again():
    state = {"n": 0}
    ready = conn_with(extension="0.8.0", available=("0.8.0", "0.8.0"))

    def connect():
        state["n"] += 1
        if state["n"] == 1:
            raise Down("server closed the connection")
        return ready
    admin = FakePg({"FROM pg_roles": [(1,)], "FROM pg_database": [(1,)]})
    out = prep.prepare(PG, "pw", None, {"superuser": "postgres", "superpassword": ADMIN_WORD, "admin_connect": lambda db: admin}, connect=connect)
    assert out["ok"] and not any(s.startswith(("CREATE ROLE", "CREATE DATABASE")) for s, _ in admin.sql) and "created" not in out["steps"][0]


def test_names_that_are_not_plain_identifiers_are_not_made():
    def connect():
        raise Down("down")
    admin = FakePg({})
    out = prep.prepare({**PG, "dbname": 'kb"; DROP'}, "pw", None, {"superuser": "postgres", "superpassword": ADMIN_WORD, "admin_connect": lambda db: admin}, connect=connect)
    assert not out["ok"] and not admin.sql


def test_an_administrator_gives_a_user_without_rights_the_right_to_create_tables():
    rights = {"granted": False}
    conn = conn_with(extension="0.8.0", available=("0.8.0", "0.8.0"),
                     **{"current_setting('server_version')": lambda c: [("me", "17.2", "kb", False, rights["granted"])]})
    admin = admin_conn()
    original = admin.cursor

    def cursor():
        c = original()
        run_ = c.execute
        c.execute = lambda sql, params=(): (rights.update(granted=True) if sql.startswith("GRANT") else None, run_(sql, params))[1]
        return c
    admin.cursor = cursor
    out, _ = run(conn, superuser="postgres", superpassword=ADMIN_WORD, admin_connect=lambda db: admin)
    assert out["ok"] and out["steps"][0]["created"] == ["rights"] and any(s.startswith("GRANT CREATE ON SCHEMA public TO") for s, _ in admin.sql)
