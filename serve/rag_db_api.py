"""The Database section of a project's settings: which database holds the project's archives, 'Connect and prepare' (three steps, as a job), and the import of archives in the
old file format.

    GET  /api/rag/db?project=<key>     the project's database: its kind and settings (never a password), what it is now, the archives waiting to be imported, what drivers are there
    POST /api/rag/db/config            {project, config, password?}   keeps the settings; the password goes to the vault, the settings name its entry; {project, reset: true} goes back to the default
    POST /api/rag/db/prepare           {project, config?, password?, plan_b?, vec_file?, install_local?, create_db?}   a job: connect, vector extension, tables and a test
    POST /api/rag/db/import            {archives?: [ids], all?, replace?}   a job: copies archives in the old file format into their project's database

A job is read at /api/rag/jobs/<id>: `stage` is the step it is in, `data.steps` the result of each step so far, `result` the whole at the end."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from tadamoo.rag import RagError, RagStore
from tadamoo.rag.backends import KINDS, clean_config, legacy, prepare as prepare_mod, store_password, vault_password
from tadamoo.rag.backends.postgres_pgvector import driver_name
from tadamoo.rag.project import is_key
from serve.rag_jobs import Jobs


def _project(value: Any) -> str:
    if value in (None, "", "none"):
        return ""
    if not is_key(value):
        raise RagError("project: the key of a project")
    return value


class RagDbApi:
    def __init__(self, store: RagStore, jobs: Jobs, vault_dir: Optional[Path] = None) -> None:
        self.store, self.jobs, self.vault_dir = store, jobs, vault_dir

    def components(self) -> Dict[str, Any]:
        """The Python packages the databases need, and whether each is installed here (the Components section installs the ones that are not)."""
        return {"sqlite_vec": importlib.util.find_spec("sqlite_vec") is not None, "postgres_driver": driver_name(), "mysql_driver": importlib.util.find_spec("pymysql") is not None}

    def _has_password(self, cfg: Dict[str, Any]) -> bool:
        if not cfg.get("vault") or self.vault_dir is None:
            return False
        try:
            vault_password(self.vault_dir)(cfg["vault"])
            return True
        except RagError:
            return False

    def overview(self, project: Any) -> Dict[str, Any]:
        project = _project(project)
        router = self.store.router
        cfg = router.config(project)
        out: Dict[str, Any] = {"project": project or "none", "config": cfg, "kinds": list(KINDS), "has_password": self._has_password(cfg), "components": self.components(),
                               "waiting": [{"id": p.name, "project": legacy.read_meta(p).get("project", "")} for p in self.store.legacy() if self._readable(p)]}
        out["waiting_here"] = [w["id"] for w in out["waiting"] if w["project"] == project]
        out["default_path"] = str(router.dir / f"{router._name(project)}.sqlite")
        if router.backends(project):
            try:
                backend = router.for_project(project)
                out.update(exists=True, info=backend.info(), archives=len(backend.archive_ids()))
            except RagError as e:
                out.update(exists=True, error=str(e))
        else:
            out["exists"] = False
        return out

    @staticmethod
    def _readable(folder: Path) -> bool:
        try:
            legacy.read_meta(folder)
            return True
        except RagError:
            return False

    # -- the routes -----------------------------------------------------------------------------------------------

    def route_get(self, parts: Sequence[str], query: Optional[Dict[str, List[str]]] = None) -> Optional[Dict[str, Any]]:
        if parts == ["db"]:
            return self.overview(((query or {}).get("project") or [""])[0])
        return None

    def route_post(self, parts: Sequence[str], req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if parts == ["db", "config"]:
            return self.config(req)
        if parts == ["db", "prepare"]:
            return self.prepare(req)
        if parts == ["db", "import"]:
            return self.import_archives(req)
        return None

    def _with_password(self, project: str, cfg: Dict[str, Any], password: Any) -> Dict[str, Any]:
        """Puts a password the page sent in the vault and names its entry in the settings; no password sent keeps the entry the settings already name."""
        if password in (None, ""):
            return cfg
        if not isinstance(password, str) or self.vault_dir is None:
            raise RagError("password: text, and there must be a vault to keep it in")
        return {**cfg, "vault": store_password(self.vault_dir, project, password)}

    def config(self, req: Dict[str, Any]) -> Dict[str, Any]:
        project = _project(req.get("project"))
        if req.get("reset") is True:
            self.store.router.reset(project)
            return self.overview(project)
        cfg = clean_config(req.get("config"))
        self.store.router.set_config(project, self._with_password(project, cfg, req.get("password")))
        return self.overview(project)

    def prepare(self, req: Dict[str, Any]) -> Dict[str, Any]:
        project = _project(req.get("project"))
        router = self.store.router
        cfg = clean_config(req["config"]) if isinstance(req.get("config"), dict) else router.config(project)
        cfg = self._with_password(project, cfg, req.get("password"))
        options = {k: req[k] for k in ("plan_b", "vec_file", "install_local", "create_db") if k in req}
        pw = vault_password(self.vault_dir)(cfg["vault"]) if cfg.get("vault") and self.vault_dir is not None else ""

        def work(progress: Any, cancel: Any) -> Dict[str, Any]:
            seen: List[Dict[str, Any]] = []

            def tell(step: Dict[str, Any]) -> None:
                if step["ok"] is None:
                    progress({"event": "stage", "stage": f"{step['step']}: {step['message']}"})
                    return
                seen.append(step)
                progress({"event": "stage", "stage": step["step"]})
                progress({"event": "data", "data": {"steps": list(seen)}})
            progress({"event": "stage", "stage": "connect"})
            done = prepare_mod.prepare(cfg, pw, tell, options)
            if done["ok"]:
                router.set_config(project, done.get("config") or cfg)
            return done
        return {"job": self.jobs.start("database", work)}

    def import_archives(self, req: Dict[str, Any]) -> Dict[str, Any]:
        waiting = {p.name: p for p in self.store.legacy()}
        ids = req.get("archives")
        if ids is not None and (not isinstance(ids, list) or any(not isinstance(i, str) for i in ids)):
            raise RagError("archives: a list of archive ids")
        wanted = [i for i in (ids if ids is not None else sorted(waiting))]
        replace = req.get("replace") is True
        folders = [waiting[i] if i in waiting else Path(self.store.root) / i for i in wanted]
        if not folders:
            raise RagError("there is no archive in the old file format to import")

        def work(progress: Any, cancel: Any) -> Dict[str, Any]:
            done: List[Dict[str, Any]] = []
            failed: List[Dict[str, Any]] = []
            for n, folder in enumerate(folders):
                if cancel.is_set():
                    raise RagError("the import was cancelled", "cancelled")
                progress({"event": "stage", "stage": folder.name})
                progress({"page": n, "pages": len(folders)})
                try:
                    done.append(self.store.import_archive(folder, replace))
                except RagError as e:
                    failed.append({"archive": folder.name, "error": str(e)})
                except OSError as e:
                    failed.append({"archive": folder.name, "error": f"{type(e).__name__}: {str(e)[:160]}"})
            progress({"page": len(folders), "pages": len(folders)})
            return {"imported": done, "failed": failed, "source_untouched": True}
        return {"job": self.jobs.start("import", work)}
