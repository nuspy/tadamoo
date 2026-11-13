"""The storage of the knowledge archives: one database for each project, behind one interface (`Backend`).

    sqlite_vec           the default: a SQLite file, FTS5 for words, the sqlite-vec extension for vectors
    postgres_pgvector    PostgreSQL with pgvector (halfvec, HNSW) and tsvector
    sql_exact_app        any SQL database: the vectors are a binary column and the application does the exact search (MariaDB, MySQL, a PostgreSQL without pgvector...)

`open_backend(config, password)` makes the one a project's configuration names; `Router` keeps the backend of each project. Nothing else opens an archive's database."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo.rag.backends.base import MAIN, RT, Backend, BackendError
from tadamoo.rag.backends.postgres_pgvector import PostgresBackend
from tadamoo.rag.backends.split import SplitBackend
from tadamoo.rag.backends.sql import DIALECTS, MYSQL, POSTGRES, SQLITE, SqlBackend
from tadamoo.rag.backends.sqlite_vec import SqliteVecBackend
from tadamoo.rag.errors import RagError

KINDS = ("sqlite", "postgres", "mysql", "other")
PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,30}$")
FIELDS = ("kind", "path", "host", "port", "dbname", "user", "sslmode", "vault", "prefix", "vectors", "vec_file", "driver", "dsn", "dialect", "database")


def clean_config(raw: Any) -> Dict[str, Any]:
    """The configuration of a project's database as it is kept (a file in the rag folder): no password, only the name of the vault entry that holds it."""
    if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
        raise RagError("kind: one of " + ", ".join(KINDS))
    if raw.get("password"):
        raise RagError("the password is not part of the configuration: it goes in the vault")
    out = {k: (raw[k] if isinstance(raw[k], int) and not isinstance(raw[k], bool) else str(raw[k]).strip()[:500]) for k in FIELDS if raw.get(k) not in (None, "")}
    if out.get("prefix") and not PREFIX.match(out["prefix"]):
        raise RagError("prefix: letters, digits and underscores, starting with a letter")
    if out["kind"] == "sqlite" and not out.get("path"):
        raise RagError("path: the SQLite file")
    if out["kind"] in ("postgres", "mysql") and not (out.get("dsn") or out.get("dbname")):
        raise RagError("dbname: the name of the database")
    if out["kind"] == "other" and not (out.get("driver") and out.get("dsn")):
        raise RagError("driver and dsn: the Python module of the driver and its connection string")
    if out.get("vectors") not in (None, "auto", "blob", "file"):
        raise RagError("vectors: auto, blob or file")
    return out


def _mysql_connect(cfg: Dict[str, Any], password: str) -> Callable[[], Any]:
    def connect() -> Any:
        for name in ("pymysql", "mariadb", "mysql.connector", "MySQLdb"):
            try:
                module = __import__(name, fromlist=["connect"])
            except ImportError:
                continue
            db = cfg.get("dbname") or cfg.get("database") or ""
            conn = module.connect(host=cfg.get("host", "localhost"), port=int(cfg.get("port") or 3306), user=cfg.get("user", ""), password=password,
                                  **({"db": db} if name == "MySQLdb" else {"database": db}))
            try:
                conn.autocommit(True) if callable(getattr(conn, "autocommit", None)) else setattr(conn, "autocommit", True)
            except Exception:
                pass
            return conn
        raise BackendError("no MariaDB or MySQL driver is installed (pymysql)")
    return connect


def open_backend(cfg: Dict[str, Any], password: str = "") -> Backend:
    """The storage a configuration names."""
    kind, prefix = cfg["kind"], cfg.get("prefix") or "rag_"
    if kind == "sqlite":
        return SqliteVecBackend(cfg["path"], prefix)
    if kind == "postgres":
        main = PostgresBackend({k: cfg[k] for k in ("host", "port", "dbname", "user", "sslmode", "dsn") if k in cfg}, password, prefix, "blob" if cfg.get("vectors") in ("blob", "file") else "auto")
        if cfg.get("vectors") == "file":
            if not cfg.get("vec_file"):
                raise RagError("vec_file: the SQLite file that holds the vectors")
            return SplitBackend(main, SqliteVecBackend(cfg["vec_file"], prefix))
        return main
    if kind == "mysql":
        return SqlBackend(_mysql_connect(cfg, password), MYSQL, prefix, label=f"{cfg.get('host', 'localhost')}/{cfg.get('dbname', '')}")
    module = __import__(cfg["driver"], fromlist=["connect"])
    dialect = DIALECTS.get(cfg.get("dialect") or "postgres", POSTGRES)

    def connect() -> Any:
        conn = module.connect(cfg["dsn"].replace("{password}", password))
        try:
            conn.isolation_level = None                       # autocommit: a change is wrapped by the backend's own BEGIN and COMMIT
        except Exception:
            pass
        try:
            conn.autocommit = True
        except Exception:
            pass
        return conn
    return SqlBackend(connect, dialect, prefix, label=cfg["driver"])


def vault_entry(project: str) -> str:
    """The name of the vault entry that holds the password of a project's database."""
    return f"rag-db-{project or 'none'}"


def vault_password(directory: str | Path) -> Callable[[str], str]:
    """A function that reads a database password from the vault in `directory` (the entry's `value` field)."""
    def get(entry: str) -> str:
        try:
            from tadamoo.vault import Vault
            return str(Vault(directory).reveal(entry, "value"))
        except Exception:
            raise RagError(f"the database password is not in the vault: put it in the entry {entry} (field value)") from None
    return get


def store_password(directory: str | Path, project: str, password: str) -> str:
    """Puts the password in the vault (never in a file of the archives) and returns the name of its entry."""
    from tadamoo.vault import Vault
    entry = vault_entry(project)
    Vault(directory).put(entry, kind="secret", label="Database password of the knowledge archives", fields={"value": password})
    return entry


class Router:
    """The backend of each project of a rag folder. A project with no configuration has a SQLite file of its own, `<root>/db/<project>.sqlite`; one with a configuration
    (`<root>/db/<project>.json`, no password in it) has the database it names, the password read from the vault when it is opened."""

    def __init__(self, root: str | Path, password: Callable[[str], str] = lambda entry: "", open_fn: Callable[..., Backend] = open_backend) -> None:
        self.dir = Path(root) / "db"
        self.password, self.open_fn = password, open_fn
        self._open: Dict[str, Backend] = {}
        self._owner: Dict[str, str] = {}

    @staticmethod
    def _name(project: str) -> str:
        return project if re.match(r"^[0-9a-f]{12}$", project or "") else "none"

    def config(self, project: str) -> Dict[str, Any]:
        name = self._name(project)
        try:
            return clean_config(json.loads((self.dir / f"{name}.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, RagError):
            return {"kind": "sqlite", "path": str(self.dir / f"{name}.sqlite")}

    def set_config(self, project: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Keeps the configuration of a project's database (the password is not part of it) and closes what was open for the project."""
        cfg = clean_config(cfg)
        self.close(project)
        self.dir.mkdir(parents=True, exist_ok=True)
        target = self.dir / f"{self._name(project)}.json"
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
        tmp.replace(target)
        return cfg

    def reset(self, project: str) -> None:
        """Takes a project back to its default database (the SQLite file of its own); what the old one holds is not touched."""
        self.close(project)
        (self.dir / f"{self._name(project)}.json").unlink(missing_ok=True)

    def for_project(self, project: str) -> Backend:
        name = self._name(project)
        if name not in self._open:
            cfg = self.config(project)
            backend = self.open_fn(cfg, self.password(cfg["vault"]) if cfg.get("vault") else "")
            backend.prepare()
            self._open[name] = backend
        return self._open[name]

    def projects(self) -> List[str]:
        """The projects that have a database here (a file or a configuration), as the files name them."""
        names = set()
        if self.dir.is_dir():
            for p in self.dir.iterdir():
                if p.suffix in (".sqlite", ".json"):
                    names.add(p.stem)
        return sorted(names | set(self._open))

    def backends(self, project: Optional[str] = None) -> List[Backend]:
        if project is not None:
            name = self._name(project)
            return [self.for_project(project)] if name in self._open or (self.dir / f"{name}.sqlite").is_file() or (self.dir / f"{name}.json").is_file() else []
        out = []
        for name in self.projects():
            try:
                out.append(self.for_project("" if name == "none" else name))
            except RagError:
                continue                                           # a database that cannot be reached leaves its archives out of the list; the Database section says why
        return out

    def find(self, aid: str) -> Optional[Backend]:
        """The backend that holds archive `aid`, or None."""
        name = self._owner.get(aid)
        if name in self._open and self._open[name].revision(aid) is not None:
            return self._open[name]
        for b in self.backends():
            if b.revision(aid) is not None:
                self._owner[aid] = next(n for n, o in self._open.items() if o is b)
                return b
        self._owner.pop(aid, None)
        return None

    def forget(self, aid: str) -> None:
        self._owner.pop(aid, None)

    def close(self, project: Optional[str] = None) -> None:
        for name in ([self._name(project)] if project is not None else list(self._open)):
            b = self._open.pop(name, None)
            if b is not None:
                b.close()
        self._owner = {a: n for a, n in self._owner.items() if n in self._open}
