"""The page's side of import: what is imported, and import, re-import and delete for skills, plugins, MCP servers and plans.

    GET  /api/import                       everything imported, by kind, and where the copies live
    GET  /api/import/<kind>                what is imported of one kind (skill, plugin, mcp, plan)
    POST /api/import/<kind>/verify         {path, server?}          what importing that source would copy; nothing is written
    POST /api/import/<kind>/import         {path, server?, with_dependencies?}
    POST /api/import/<kind>/reimport       {name, confirm?, overwrite_local?, with_dependencies?}   without `confirm`: what would change
    POST /api/import/<kind>/update-plan    {name, instructions?, language?}   the update merged with what the person changed (skill, plugin, mcp): three versions of each file. Answers a `token`
    POST /api/import/<kind>/update-apply   {token, choice?, resolutions?}   choice: merge (default), overwrite or cancel
    POST /api/import/<kind>/delete         {name}
    POST /api/import/mcp/enable            {name, enabled}          an imported server starts turned off

The work is in tadamoo/importer/. Every route is for the PC the server runs on: a path is a path on that PC."""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from tadamoo.importer import KINDS, Importer, Problem

CONFLICT = frozenset({"exists", "blocked", "local_edits", "part", "source_missing", "renamed", "personal_exists", "plan_exists", "plan_archived", "part_exists", "stale", "conflicts",
                      "not_mergeable"})
NOT_FOUND = frozenset({"not_imported", "not_found", "no_plan"})
STATUS = {"no_model": 503}


class ImportApi:
    def __init__(self, importer: Importer, after: Optional[Callable[[str], None]] = None) -> None:
        """`after(kind)` is called after something was imported, re-imported or deleted, so the capability index can look again."""
        self.imp = importer
        self.after = after

    def overview(self) -> Dict[str, Any]:
        rows = self.imp.list()
        return {"kinds": {k: [r for r in rows if r["kind"] == k] for k in KINDS},
                "where": {"copies": str(self.imp.root), "plans": str(self.imp.spaces.plans_dir), "personal_skills": str(self.imp.spaces.personal_dir)}}

    def route(self, method: str, path: str, req: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        parts = [p for p in path[len("/api/import"):].split("/") if p]
        try:
            if method == "GET":
                if not parts:
                    return 200, self.overview()
                if len(parts) == 1 and parts[0] in KINDS:
                    return 200, {"items": self.imp.list(parts[0])}
                return 404, {"error": {"message": "not found"}}
            if parts == ["mcp", "enable"]:
                return 200, self._changed("mcp", self.imp.set_enabled(self._text(req, "name"), req.get("enabled") is True))
            if len(parts) == 2 and parts[0] in KINDS:
                return 200, self._act(parts[0], parts[1], req)
            return 404, {"error": {"message": "not found"}}
        except Problem as exc:
            code = 404 if exc.code in NOT_FOUND else 409 if exc.code in CONFLICT else STATUS.get(exc.code, 400)
            return code, {"error": {"type": "import", "code": exc.code, "message": str(exc), **exc.data}}

    @staticmethod
    def _text(req: Dict[str, Any], key: str) -> str:
        value = req.get(key)
        if not isinstance(value, str) or not value.strip():
            raise Problem("bad_request", f"Send `{key}` as text.")
        return value.strip()

    def _changed(self, kind: str, out: Dict[str, Any]) -> Dict[str, Any]:
        if self.after is not None:
            self.after(kind)
        return out

    def _act(self, kind: str, action: str, req: Dict[str, Any]) -> Dict[str, Any]:
        flag = lambda key: req.get(key) is True            # noqa: E731
        server = req.get("server") if isinstance(req.get("server"), str) else ""
        if action == "verify":
            return self.imp.verify(kind, self._text(req, "path"), server=server, with_dependencies=flag("with_dependencies"), adopt=flag("adopt"))
        if action == "import":
            return self._changed(kind, self.imp.import_(kind, self._text(req, "path"), server=server, with_dependencies=flag("with_dependencies"), adopt=flag("adopt")))
        if action == "reimport":
            out = self.imp.reimport(kind, self._text(req, "name"), confirm=flag("confirm"), overwrite_local=flag("overwrite_local"), with_dependencies=flag("with_dependencies"))
            return self._changed(kind, out) if out["status"] == "replaced" else out
        if action == "delete":
            return self._changed(kind, self.imp.delete(kind, self._text(req, "name")))
        if action == "update-plan":
            return self.imp.update_plan(kind, self._text(req, "name"), instructions=self._strings(req, "instructions"), language=self._language(req))
        if action == "update-apply":
            out = self.imp.update_apply(self._text(req, "token"), req.get("choice") if isinstance(req.get("choice"), str) else "merge", resolutions=req.get("resolutions"))
            return self._changed(kind, out) if out.get("status") in ("merged", "replaced") else out
        raise Problem("not_found", "not found")

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
