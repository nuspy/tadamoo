"""A separate PostgreSQL of our own, for the knowledge archives' database (Knowledge > Database): detect what is already here, install a copy TaDaMoo starts and
stops by itself, and keep it running only while a project's database actually uses it.

    GET  /api/rag/postgres                 what is on this PC (detect + the choices) and our own copy's state
    POST /api/rag/postgres/install         install a separate PostgreSQL of our own (bundled with this package if there is one, else downloaded and checked), start it
    POST /api/rag/postgres/start|stop      our own copy

A project still points its own database at ours the same way as any other PostgreSQL (Knowledge > Database, "use an existing one"), with the connection this GET
gives and the superuser password from `api/rag/postgres/password` (server-side only: a project's "Connect and prepare" asks for it there, it is never shown by
this page). Nothing is downloaded or started except by a POST the person made, until a project's database is actually opened through it."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo import postgres_setup
from tadamoo.rag import RagError, RagStore

OWN = "own.json"
SUPERUSER_ENTRY = "rag-postgres-super"
IDLE_S = 600.0             # our own PostgreSQL stops after this long with no project actually using it
SWEEP_S = 30.0

_SHARED: Dict[str, postgres_setup.PostgresService] = {}      # our own PostgreSQL of each programs folder: the API is made again at each request, the process is not
_LAST_USED: Dict[str, float] = {}
_sweeper_started = False
_sweeper_lock = threading.Lock()


def stop_all() -> None:
    """Stops the PostgreSQL processes this server started (called when the server ends)."""
    for service in list(_SHARED.values()):
        try:
            service.stop()
        except Exception:
            pass
    _SHARED.clear()


def _touch(key: str, clock: Callable[[], float] = time.time) -> None:
    _LAST_USED[key] = clock()


def sweep(clock: Callable[[], float] = time.time) -> None:
    """Stops a running copy of ours that went unused for `IDLE_S`: called by the background sweeper, and directly by tests."""
    now = clock()
    for key, service in list(_SHARED.items()):
        try:
            if service.running() and now - _LAST_USED.get(key, now) > IDLE_S:
                service.stop()
        except Exception:
            pass


def _ensure_sweeper() -> None:
    global _sweeper_started
    with _sweeper_lock:
        if _sweeper_started:
            return
        _sweeper_started = True

        def loop() -> None:
            while True:
                time.sleep(SWEEP_S)
                sweep()
        threading.Thread(target=loop, daemon=True, name="postgres-idle").start()


def _put_secret(vault_dir: Any, entry: str, secret: str) -> None:
    from tadamoo.vault import Vault
    Vault(vault_dir).put(entry, kind="secret", label="Superuser password of TaDaMoo's own PostgreSQL", fields={"value": secret})


class RagPostgresApi:
    def __init__(self, store: RagStore, vault_dir: Any = None, tools_dir: Any = None, detect: Callable[[], Dict[str, Any]] = postgres_setup.detect,
                 fetch: Optional[Callable[[str, Path], None]] = None, runner: Optional[Callable[..., Any]] = None,
                 put_secret: Optional[Callable[[str, str], None]] = None, get_secret: Optional[Callable[[str], str]] = None) -> None:
        self.store, self.vault_dir, self.tools = store, vault_dir, Path(tools_dir) if tools_dir else None
        self._detect, self._fetch, self._runner = detect, fetch, runner
        self._put = put_secret or (lambda entry, secret: _put_secret(vault_dir, entry, secret))
        self._get = get_secret or self._vault_secret

    @property
    def _service(self) -> Optional[postgres_setup.PostgresService]:
        return _SHARED.get(str(self.tools))

    @_service.setter
    def _service(self, value: Optional[postgres_setup.PostgresService]) -> None:
        if value is None:
            _SHARED.pop(str(self.tools), None)
        else:
            _SHARED[str(self.tools)] = value

    def _vault_secret(self, entry: str) -> str:
        from tadamoo.rag.backends import vault_password
        return vault_password(self.vault_dir)(entry)

    def _own_file(self) -> Optional[Path]:
        return (self.tools / OWN) if self.tools else None

    def _own(self) -> Dict[str, Any]:
        f = self._own_file()
        try:
            return json.loads(f.read_text(encoding="utf-8")) if f and f.is_file() else {}
        except (OSError, ValueError):
            return {}

    def _bin_dir(self) -> Optional[Path]:
        bundled = postgres_setup.bundled_program()
        if bundled is not None:
            return bundled.parent
        exe = Path(self.tools) / "bin" / postgres_setup.EXE if self.tools else None
        return exe.parent if exe and exe.is_file() else None

    # -- reading -----------------------------------------------------------------------------------

    def overview(self) -> Dict[str, Any]:
        own = self._own()
        found = self._detect()
        bin_dir = self._bin_dir()
        return {"found": found, **postgres_setup.plan(found), "release": {"version": postgres_setup.RELEASE["version"], "bytes": postgres_setup.RELEASE["bytes"]},
                "own": {"installed": bin_dir is not None, "running": bool(self._service and self._service.running()),
                        "host": "127.0.0.1" if own else "", "port": own.get("port", ""), "user": postgres_setup.LOCAL_USER if own else "",
                        "database": postgres_setup.LOCAL_DATABASE if own else ""}}

    def route_get(self, parts: List[str], query: Optional[Dict[str, List[str]]] = None) -> Optional[Dict[str, Any]]:
        if parts == ["postgres"]:
            return self.overview()
        return None

    def route_post(self, parts: List[str], req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if parts[:1] != ["postgres"]:
            return None
        if parts == ["postgres", "install"]:
            return self.install_separate()
        if parts == ["postgres", "start"]:
            return self.start_own()
        if parts == ["postgres", "stop"]:
            if self._service:
                self._service.stop()
            return self.overview()
        raise RagError("not found", "not_found")

    # -- the person's choices -----------------------------------------------------------------------

    def install_separate(self) -> Dict[str, Any]:
        if self.tools is None:
            raise RagError("there is no folder for the programs of this app")
        bundled = postgres_setup.bundled_program()
        if bundled is None:
            postgres_setup.install_release(self.tools, fetch=self._fetch)
        own = self._own()
        port = int(own.get("port") or postgres_setup.free_port())
        password = self._get(SUPERUSER_ENTRY) if own else postgres_setup.new_password()
        bin_dir = self._bin_dir()
        if bin_dir is None:
            raise RagError("PostgreSQL was not installed")
        self._service = postgres_setup.PostgresService(bin_dir, self.tools / "data", port, password, runner=self._runner) if self._runner else \
            postgres_setup.PostgresService(bin_dir, self.tools / "data", port, password)
        self._service.start()
        _touch(str(self.tools))
        self._put(SUPERUSER_ENTRY, password)
        self._own_file().write_text(json.dumps({"port": port}), encoding="utf-8")
        return self.overview()

    def start_own(self) -> Dict[str, Any]:
        own = self._own()
        bin_dir = self._bin_dir()
        if not own or bin_dir is None:
            raise RagError("there is no PostgreSQL of our own installed: install it first")
        if self._service and self._service.running():
            return self.overview()
        password = self._get(SUPERUSER_ENTRY)
        self._service = postgres_setup.PostgresService(bin_dir, self.tools / "data", int(own["port"]), password, runner=self._runner) if self._runner else \
            postgres_setup.PostgresService(bin_dir, self.tools / "data", int(own["port"]), password)
        self._service.start()
        _touch(str(self.tools))
        return self.overview()

    def autostart(self) -> bool:
        """Starts our own PostgreSQL when it is installed and not running: called whenever a project's database actually uses it (`wire()`), so a copy the idle
        sweep stopped comes back on its own. A failure is left for the page to show; it never stops the server."""
        if not self._own() or self._bin_dir() is None or (self._service and self._service.running()):
            return False
        try:
            self.start_own()
            return True
        except Exception:
            return False

    def password(self) -> str:
        """The superuser password of our own copy, for a project's 'Connect and prepare' to use as its superuser(password) for this one run; never kept by the page."""
        if not self._own():
            raise RagError("there is no PostgreSQL of our own installed")
        return self._get(SUPERUSER_ENTRY)

    def wire(self) -> None:
        """Ties this API to its store: every time a project's database is actually opened (Router.on_open) and that project points at our own copy's host and
        port, it is touched as just used and started first if it had gone idle. Safe to call again on every request."""
        key, own = str(self.tools), self._own()
        own_port = int(own["port"]) if own else None

        def used(cfg: Dict[str, Any]) -> None:
            if own_port is not None and cfg.get("kind") == "postgres" and cfg.get("host") == "127.0.0.1" and int(cfg.get("port") or 0) == own_port:
                _touch(key)
                _ensure_sweeper()
                self.autostart()
        self.store.router.on_open = used
