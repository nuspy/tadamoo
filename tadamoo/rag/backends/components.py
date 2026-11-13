"""The Python packages the databases need, as components the person installs with a button: each is one pinned requirement, installed with pip into the Python the app
runs in, only when the person asks. The size shown beforehand is an estimate of what pip downloads."""
from __future__ import annotations

import importlib.metadata
import importlib.util
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, List, Optional

from tadamoo.rag.errors import RagError

COMPONENTS: List[Dict[str, Any]] = [
    {"id": "sqlite-vec", "package": "sqlite-vec", "module": "sqlite_vec", "requirement": "sqlite-vec==0.1.6", "size_mb": 1,
     "what": {"en": "Vector search inside SQLite (the default database).", "it": "Ricerca per vettori dentro SQLite (il database predefinito)."}},
    {"id": "psycopg", "package": "psycopg", "module": "psycopg", "requirement": "psycopg[binary]==3.3.4", "size_mb": 6,
     "what": {"en": "The PostgreSQL driver.", "it": "Il driver di PostgreSQL."}},
    {"id": "pymysql", "package": "pymysql", "module": "pymysql", "requirement": "pymysql==1.1.1", "size_mb": 1,
     "what": {"en": "The MariaDB and MySQL driver.", "it": "Il driver di MariaDB e MySQL."}},
]
Progress = Callable[[str], None]


def find(component_id: Any) -> Dict[str, Any]:
    for c in COMPONENTS:
        if c["id"] == component_id:
            return c
    raise RagError("there is no such component", "not_found")


def installed_version(component: Dict[str, Any]) -> str:
    """The version of the package in this Python, or "" when it is not installed."""
    if importlib.util.find_spec(component["module"]) is None:
        return ""
    try:
        return importlib.metadata.version(component["package"])
    except importlib.metadata.PackageNotFoundError:
        return "?"


def listing() -> List[Dict[str, Any]]:
    """Every component with what it is, what pip would install and about how big it is, and whether it is installed here."""
    out = []
    for c in COMPONENTS:
        have = installed_version(c)
        out.append({"id": c["id"], "what": c["what"], "requirement": c["requirement"], "size_mb": c["size_mb"], "installed": bool(have), "version": have if have != "?" else "",
                    "python": sys.executable})
    return out


def install(component_id: str, progress: Optional[Progress] = None, cancel: Optional[threading.Event] = None,
            popen: Callable[..., Any] = subprocess.Popen) -> Dict[str, Any]:
    """Runs `pip install <requirement>` in this Python, telling each line of its output; a cancel stops it. Returns `{ok, version}` or raises RagError with the end of what pip said."""
    c = find(component_id)
    tell = progress or (lambda line: None)
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", c["requirement"]]
    tell(" ".join(cmd))
    proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0)
    tail: List[str] = []
    for line in proc.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-12:]
        tell(line)
        if cancel is not None and cancel.is_set():
            proc.terminate()
            raise RagError("the installation was cancelled", "cancelled")
    if proc.wait() != 0:
        raise RagError("pip could not install " + c["requirement"] + ": " + " | ".join(x for x in tail if x)[-400:])
    importlib.invalidate_caches()
    have = installed_version(c)
    if not have:
        raise RagError(c["requirement"] + " was installed but cannot be imported by this Python")
    return {"ok": True, "version": have if have != "?" else ""}
