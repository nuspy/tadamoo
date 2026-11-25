"""The page's side of the skill manager (Settings > Skills, and the `/install-skill` command of the Chat).

    GET  /api/skills                  every skill (shipped and installed) with its state, and the job that runs or ran last
    GET  /api/skills/job              the job: step, skill, last lines of what the installers print
    POST /api/skills/plan             {address}                   what an address holds, with sizes; nothing is installed. Answers a `token`
    POST /api/skills/install          {names} or {token, only?}   starts the job: the copy and the models. Answers the job
    POST /api/skills/models           {name}                      starts the job that downloads the models an installed skill lacks
    POST /api/skills/models/check     {name}                      reads the disk for the models of an installed skill; downloads nothing
    POST /api/skills/remove           {name}
    POST /api/skills/enable           {name}
    POST /api/skills/disable          {name}
    POST /api/skills/update           {name, overwrite_local?, check?}   the old way: the copy is replaced (refused when it was edited here, unless overwrite_local)
    POST /api/skills/update/plan      {name, instructions?, language?}   the update merged with what the person changed: three versions of each file, told in words. Answers a `token`
    POST /api/skills/update/apply     {token, choice?, resolutions?}   choice: merge (default), overwrite or cancel
    POST /api/skills/discard          {token}                     forgets what an address held
    POST /api/skills/job/cancel

The work is in tadamoo/skillmanager/. Every route is for the PC the server runs on: an address may be a folder of that PC, and an installation runs programs on it."""
from __future__ import annotations

from typing import Any, Dict, Tuple

from tadamoo.skillmanager import Problem, SkillManager

CONFLICT = frozenset({"busy", "blocked", "local_edits", "exists", "part", "source_missing", "renamed", "personal_exists", "part_exists", "not_updatable", "stale", "conflicts",
                      "not_mergeable"})
NOT_FOUND = frozenset({"not_imported", "not_found", "unknown_skill", "not_installed", "no_plan"})
STATUS = {"fetch_failed": 502, "no_model": 503}
MAX_NAMES = 60


class SkillsApi:
    def __init__(self, manager: SkillManager) -> None:
        self.mgr = manager

    def overview(self) -> Dict[str, Any]:
        return {"skills": self.mgr.list(), "job": self.mgr.jobs.view(),
                "where": {"state": str(self.mgr.choices.path), "copies": str(self.mgr.imp.root), "program": str(self.mgr.program)}}

    def route(self, method: str, path: str, req: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        parts = [p for p in path[len("/api/skills"):].split("/") if p]
        try:
            if method == "GET":
                if not parts:
                    return 200, self.overview()
                if parts == ["job"]:
                    return 200, {"job": self.mgr.jobs.view()}
                return 404, {"error": {"message": "not found"}}
            return 200, self._post(parts, req)
        except Problem as exc:
            code = 404 if exc.code in NOT_FOUND else 409 if exc.code in CONFLICT else STATUS.get(exc.code, 400)
            return code, {"error": {"type": "skills", "code": exc.code, "message": str(exc), **{k: v for k, v in exc.data.items() if k != "status"}}}

    @staticmethod
    def _text(req: Dict[str, Any], key: str) -> str:
        value = req.get(key)
        if not isinstance(value, str) or not value.strip():
            raise Problem("bad_request", f"Send `{key}` as text.")
        return value.strip()

    @staticmethod
    def _list(req: Dict[str, Any], key: str) -> Any:
        value = req.get(key)
        if value is None:
            return None
        if not isinstance(value, list) or len(value) > MAX_NAMES or not all(isinstance(x, str) for x in value):
            raise Problem("bad_request", f"Send `{key}` as a list of names.")
        return value

    @staticmethod
    def _strings(req: Dict[str, Any], key: str) -> list:
        value = req.get(key)
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 20 or not all(isinstance(x, str) for x in value):
            raise Problem("bad_request", f"Send `{key}` as a list of texts.")
        return value

    @staticmethod
    def _language(req: Dict[str, Any]) -> str:
        value = req.get("language")
        return value if isinstance(value, str) and value in ("it", "en", "hu") else "en"

    def _post(self, parts: list, req: Dict[str, Any]) -> Dict[str, Any]:
        m = self.mgr
        flag = lambda key: req.get(key) is True            # noqa: E731
        if parts == ["plan"]:
            return m.plan(self._text(req, "address"))
        if parts == ["discard"]:
            m.discard(self._text(req, "token"))
            return {"discarded": True}
        if parts == ["install"]:
            names, token, only = self._list(req, "names"), req.get("token") if isinstance(req.get("token"), str) else "", self._list(req, "only")
            if not names and not token:
                raise Problem("bad_request", "Send `names` (built-in skills) or the `token` of an address.")
            m.check_request(names, token)
            models = req.get("models") is not False
            job = m.start("install", names or [], lambda j: m.install(names, token, only, models, j, flag("overwrite_local")))
            return {"job": job.view()}
        if parts == ["models"]:
            name = self._text(req, "name")
            if m.get(name)["state"] == "not_installed":
                raise Problem("not_installed", f"'{name}' is not installed: models are downloaded only for a skill that is.")
            return {"job": m.start("models", [name], lambda j: m.download_models(name, j)).view()}
        if parts == ["models", "check"]:
            name = self._text(req, "name")
            if m.get(name)["state"] == "not_installed":
                raise Problem("not_installed", f"'{name}' is not installed.")
            return m.check_models(name)
        if parts == ["job", "cancel"]:
            return {"cancelled": m.jobs.cancel(), "job": m.jobs.view()}
        if parts == ["update", "plan"]:
            return m.update_plan(self._text(req, "name"), self._strings(req, "instructions"), self._language(req))
        if parts == ["update", "apply"]:
            return m.update_apply(self._text(req, "token"), req.get("choice") if isinstance(req.get("choice"), str) else "merge", req.get("resolutions"))
        if parts == ["load"]:
            return m.set_load(self._text(req, "name"), self._text(req, "load"))
        if parts in (["remove"], ["enable"], ["disable"], ["update"]):
            name = self._text(req, "name")
            if parts[0] == "remove":
                return m.remove(name)
            if parts[0] == "update":
                return m.update(name, overwrite_local=flag("overwrite_local"), check_only=flag("check"))
            return m.set_enabled(name, parts[0] == "enable")
        raise Problem("not_found", "not found")
