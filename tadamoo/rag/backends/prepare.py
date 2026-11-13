"""Making a project's database ready, in three steps that each report what they found:

    connect     reach the database (a SQLite file is created; a PostgreSQL one may be created when the user has the right), say its version and what the user may do
    extension   the vector extension: sqlite-vec for SQLite, pgvector for PostgreSQL. Present, or enabled with CREATE EXTENSION, or (PostgreSQL on this PC, with administrator rights, when
                the person says so) installed; otherwise the exact steps for the system, and a choice of Plan B: the vectors in a binary column searched by the application, or in a
                local sqlite-vec file
    configure   the tables with their indexes, a test insert and search, the project's settings saved

Each step is a dict `{step, state, ok, message, ...}`; `prepare` returns `{ok, stopped_at, steps, vectors, config}` and tells a `progress(step)` callback as each starts and ends."""
from __future__ import annotations

import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from tadamoo.rag.backends import clean_config, open_backend
from tadamoo.rag.backends.base import Backend, BackendError
from tadamoo.rag.backends.postgres_pgvector import MIN_PGVECTOR, PostgresBackend, driver_name, parse_version
from tadamoo.rag.backends.sqlite_vec import SqliteVecBackend
from tadamoo.rag.errors import RagError

STEPS = ("connect", "extension", "configure")
LOCAL_HOSTS = ("", "localhost", "127.0.0.1", "::1")
Progress = Callable[[Dict[str, Any]], None]


def _step(name: str, state: str, ok: Optional[bool], message: str, **more: Any) -> Dict[str, Any]:
    return {"step": name, "state": state, "ok": ok, "message": message, **more}


def is_admin() -> bool:
    """Whether this process runs with administrator (Windows) or root rights."""
    try:
        if sys.platform == "win32":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


def install_steps(major: int, system: Optional[str] = None) -> List[str]:
    """The commands that put pgvector on a PostgreSQL server of version `major` on this kind of system, as a person would run them."""
    system = system or platform.system()
    if system == "Linux":
        return [f"Debian or Ubuntu: sudo apt install postgresql-{major}-pgvector", f"Fedora or RHEL: sudo dnf install pgvector_{major}",
                "then in the database: CREATE EXTENSION vector;"]
    if system == "Darwin":
        return ["brew install pgvector", "then in the database: CREATE EXTENSION vector;"]
    return ["Windows: pgvector has no installer of its own. Build it with the Visual Studio C++ build tools, in a command prompt started with vcvars64.bat: "
            "set PGROOT=<the PostgreSQL folder>  then  git clone --branch v0.8.7 https://github.com/pgvector/pgvector.git  then  cd pgvector  then  "
            "nmake /F Makefile.win  then  nmake /F Makefile.win install  (about a minute, nothing else to download). The PostgreSQL service need not be restarted.",
            "then, as a PostgreSQL superuser, in the database: CREATE EXTENSION vector;"]


def install_command(major: int, system: Optional[str] = None) -> Optional[List[str]]:
    """The command TaDaMoo may run itself (Linux and macOS, with the rights it already has); None where there is no single command."""
    system = system or platform.system()
    if system == "Linux" and shutil.which("apt-get"):
        return ["apt-get", "install", "-y", f"postgresql-{major}-pgvector"]
    if system == "Linux" and shutil.which("dnf"):
        return ["dnf", "install", "-y", f"pgvector_{major}"]
    if system == "Darwin" and shutil.which("brew"):
        return ["brew", "install", "pgvector"]
    return None


def selftest(backend: Backend) -> Dict[str, Any]:
    """Writes a small archive, searches it by words and by vector, and removes it: what the person is told works."""
    aid = "selftest-" + secrets.token_hex(3)
    started = time.monotonic()
    meta = {"id": aid, "name": "selftest", "description": "", "project": "", "created": 0.0, "updated": 0.0, "chunks": 2, "embed_model": "selftest", "embed_dim": 4, "embed_version": "t",
            "documents": [{"id": "dselftest", "name": "t.txt", "sha256": "t", "chunks": 2}]}
    chunks = [{"doc": "dselftest", "n": i, "text": t, "path": "t", "page_start": 1, "page_end": 1, "kind": "text", "section": "dselftest.s0"} for i, t in enumerate(("a small boat with a keel", "rice cooked in stock"))]
    vectors = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=np.float32)
    try:
        backend.commit(aid, meta, chunks, {}, vectors, "selftest", "t")
        nearest = backend.vector_search(aid, np.array([0, 1, 0, 0], dtype=np.float32), 2)
        if not nearest or nearest[0][0] != 1:
            raise RagError("the vector search did not find the nearest passage")
        words = backend.word_search(aid, "keel", 2)
        if words is not None and (not words or words[0][0] != 0):
            raise RagError("the word search did not find the passage")
    finally:
        try:
            backend.delete_archive(aid)
        except Exception:
            pass
    return {"vectors": "native" if backend.native_vectors else "app", "words": "native" if backend.native_words else "app", "ms": round((time.monotonic() - started) * 1000)}


# -- SQLite -----------------------------------------------------------------------------------------------

def _sqlite(cfg: Dict[str, Any], tell: Progress) -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = []

    def done(s: Dict[str, Any]) -> Dict[str, Any]:
        steps.append(s)
        tell(s)
        return s
    try:
        backend = SqliteVecBackend(cfg["path"], cfg.get("prefix") or "rag_")
    except Exception as e:
        done(_step("connect", "failed", False, f"the file could not be opened: {type(e).__name__}: {str(e)[:160]}"))
        return {"ok": False, "stopped_at": "connect", "steps": steps}
    done(_step("connect", "ok", True, f"SQLite file {cfg['path']}", version=backend.info()["sqlite"]))
    try:
        import sqlite_vec  # noqa: F401
        have = True
    except ImportError:
        have = False
    if backend.vec:
        done(_step("extension", "present", True, "sqlite-vec is loaded: vectors are searched by the database"))
    elif have:
        done(_step("extension", "not_loadable", True, "sqlite-vec is installed but this SQLite cannot load extensions: vectors are searched by the application"))
    else:
        done(_step("extension", "not_installed", True, "sqlite-vec is not installed (the Components section installs it): vectors are searched by the application", component="sqlite-vec"))
    try:
        backend.prepare()
        test = selftest(backend)
    except Exception as e:
        done(_step("configure", "failed", False, f"{type(e).__name__}: {str(e)[:200]}"))
        return {"ok": False, "stopped_at": "configure", "steps": steps}
    done(_step("configure", "ok", True, "tables and indexes are in place; a test insert and search worked", test=test))
    backend.close()
    return {"ok": True, "stopped_at": None, "steps": steps, "vectors": "sqlite-vec" if backend.vec else "app"}


# -- PostgreSQL --------------------------------------------------------------------------------------------

def _server_facts(backend: PostgresBackend) -> Dict[str, Any]:
    row = backend.one("SELECT current_user, current_setting('server_version'), current_database(), "
                      "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user), has_database_privilege(current_database(), 'CREATE')")
    return {"user": row[0], "version": row[1], "database": row[2], "superuser": bool(row[3]), "can_create": bool(row[4])}


def _postgres(cfg: Dict[str, Any], password: str, tell: Progress, options: Dict[str, Any], connect: Optional[Callable[[], Any]]) -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = []

    def done(s: Dict[str, Any]) -> Dict[str, Any]:
        steps.append(s)
        tell(s)
        return s

    def stop(at: str) -> Dict[str, Any]:
        return {"ok": False, "stopped_at": at, "steps": steps}
    params = {k: cfg[k] for k in ("host", "port", "dbname", "user", "sslmode", "dsn") if k in cfg}
    prefix = cfg.get("prefix") or "rag_"
    if connect is None and not driver_name():
        done(_step("connect", "no_driver", False, "no PostgreSQL driver is installed: install psycopg from the Components section", component="psycopg"))
        return stop("connect")
    backend = PostgresBackend(params, password, prefix, "auto", connect)
    try:
        backend._open()
    except BackendError as e:
        if options.get("create_db") and "does not exist" in str(e) and params.get("host", "") in LOCAL_HOSTS and not params.get("dsn"):
            try:
                _create_database(params, password, connect)
                backend = PostgresBackend(params, password, prefix, "auto", connect)
                backend._open()
            except Exception as e2:
                done(_step("connect", "failed", False, f"the database could not be created: {type(e2).__name__}: {str(e2)[:200]}"))
                return stop("connect")
        else:
            done(_step("connect", "failed", False, str(e), can_create_database=("does not exist" in str(e) and params.get("host", "") in LOCAL_HOSTS)))
            return stop("connect")
    try:
        facts = _server_facts(backend)
    except Exception as e:
        done(_step("connect", "failed", False, f"connected, but the server did not answer a simple question: {type(e).__name__}: {str(e)[:160]}"))
        return stop("connect")
    done(_step("connect", "ok", True, f"PostgreSQL {facts['version']}, database {facts['database']}, user {facts['user']}", **facts))
    if not facts["can_create"] and not facts["superuser"]:
        steps[-1].update(ok=False, state="no_rights", message=steps[-1]["message"] + ": this user may not create tables in the database")
        return stop("connect")
    major = parse_version(facts["version"])[0]
    local = params.get("host", "") in LOCAL_HOSTS

    # the vector extension
    available = backend.one("SELECT default_version, installed_version FROM pg_available_extensions WHERE name = 'vector'")
    plan_b = options.get("plan_b")
    if available and available[1] and parse_version(available[1]) >= MIN_PGVECTOR:
        done(_step("extension", "present", True, f"pgvector {available[1]} is enabled in this database"))
    elif available and available[1]:
        try:
            backend.run("ALTER EXTENSION vector UPDATE").close()
            backend.pgvector = backend._detect_vector(backend._open())
        except Exception:
            pass
        if backend.pgvector:
            done(_step("extension", "updated", True, f"pgvector was updated to {backend.pgvector}"))
        else:
            done(_step("extension", "too_old", False, f"pgvector {available[1]} is older than {'.'.join(map(str, MIN_PGVECTOR))} (no halfvec): update it on the server", steps=install_steps(major)))
    elif available:
        try:
            backend.run("CREATE EXTENSION IF NOT EXISTS vector").close()
            backend.pgvector = backend._detect_vector(backend._open())
            done(_step("extension", "enabled", bool(backend.pgvector), f"pgvector {available[0]} is available on the server and was enabled in this database"
                       if backend.pgvector else "pgvector was enabled but is older than halfvec needs: update it"))
        except Exception as e:
            done(_step("extension", "needs_admin", False, f"pgvector is on the server but this user cannot enable it ({type(e).__name__}: {str(e)[:120]}): "
                                                         "ask a PostgreSQL administrator to run  CREATE EXTENSION vector;  in this database, then connect again",
                       sql="CREATE EXTENSION vector;"))
    else:
        info: Dict[str, Any] = {"steps": install_steps(major), "local": local, "admin": is_admin(), "plan_b": ["blob", "file"]}
        command = install_command(major)
        if local and is_admin() and command and options.get("install_local"):
            try:
                tell(_step("extension", "installing", None, " ".join(command)))
                out = subprocess.run(command, capture_output=True, text=True, timeout=900)
                if out.returncode == 0:
                    backend.run("CREATE EXTENSION IF NOT EXISTS vector").close()
                    backend.pgvector = backend._detect_vector(backend._open())
            except Exception as e:
                info["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        if backend.pgvector:
            done(_step("extension", "installed", True, "pgvector was installed on this PC and enabled"))
        else:
            done(_step("extension", "not_available", False, "pgvector is not available on this server. Install it with the steps shown and connect again, or use Plan B: "
                                                          "the vectors in a binary column searched by the application, or in a local sqlite-vec file",
                       can_install=bool(local and is_admin() and command), command=command, **info))
    if not backend.pgvector and plan_b not in ("blob", "file"):
        return stop("extension")
    vectors = "auto" if backend.pgvector else plan_b
    if plan_b == "file" and not backend.pgvector and not options.get("vec_file"):
        steps[-1].update(message="Plan B with a local file needs the file's path")
        return stop("extension")
    final = {**cfg, "vectors": vectors, **({"vec_file": options["vec_file"]} if vectors == "file" else {})}
    backend.close()

    # the tables, the test, the settings
    try:
        ready = open_backend(clean_config(final), password) if connect is None else PostgresBackend(params, password, prefix, "blob" if vectors != "auto" else "auto", connect)
        ready.prepare()
        test = selftest(ready)
        info = ready.info()
        ready.close()
    except Exception as e:
        done(_step("configure", "failed", False, f"{type(e).__name__}: {str(e)[:200]}"))
        return stop("configure")
    done(_step("configure", "ok", True, "tables, the full-text index and the vector index are in place; a test insert and search worked", test=test, info=info))
    return {"ok": True, "stopped_at": None, "steps": steps, "vectors": vectors, "config": final}


def _create_database(params: Dict[str, Any], password: str, connect: Optional[Callable[[], Any]]) -> None:
    """Creates the database a local connection names, from the server's maintenance database `postgres`."""
    name = str(params["dbname"])
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name):
        raise BackendError("the database name is letters, digits and underscores")
    admin = PostgresBackend({**params, "dbname": "postgres"}, password, "rag_", "blob", connect)
    try:
        admin.run(f'CREATE DATABASE "{name}"').close()
    finally:
        admin.close()


# -- the entry ---------------------------------------------------------------------------------------------

def prepare(cfg: Dict[str, Any], password: str = "", progress: Optional[Progress] = None, options: Optional[Dict[str, Any]] = None,
            connect: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
    """Runs the steps for the configuration `cfg` (see backends.clean_config). `options`: `plan_b` (`blob` or `file`), `vec_file`, `install_local`, `create_db`."""
    cfg = clean_config(cfg)
    tell = progress or (lambda event: None)
    options = options or {}
    if cfg["kind"] == "sqlite":
        return _sqlite(cfg, tell)
    if cfg["kind"] == "postgres":
        return _postgres(cfg, password, tell, options, connect)
    steps: List[Dict[str, Any]] = []
    try:
        backend = open_backend(cfg, password)
        backend.prepare()
        steps.append(_step("connect", "ok", True, f"{cfg['kind']} database reached"))
        steps.append(_step("extension", "none", True, "this kind of database has no vector extension here: vectors are a binary column searched by the application"))
        test = selftest(backend)
        steps.append(_step("configure", "ok", True, "tables are in place; a test insert and search worked", test=test))
        backend.close()
    except Exception as e:
        steps.append(_step("connect" if not steps else "configure", "failed", False, f"{type(e).__name__}: {str(e)[:200]}"))
        return {"ok": False, "stopped_at": steps[-1]["step"], "steps": steps}
    for s in steps:
        tell(s)
    return {"ok": True, "stopped_at": None, "steps": steps, "vectors": "app", "config": cfg}
