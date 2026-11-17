"""The dark web option over the API (this PC only): its settings, Tor, and nothing else. The work is tadamoo/darkweb_runtime.py.

    GET  /api/darkweb                    {package, settings, tor: {installed, running, socks}, gpg}
    POST /api/darkweb                    any of the settings (darkweb, search, read, leak_archives, watches, navigation); write and send cannot be turned on yet
    POST /api/darkweb/tor/install        downloads the official Tor bundle, checks its hash and signature, unpacks it
    POST /api/darkweb/tor/start|stop     our own Tor process
    GET  /api/darkweb/leaks              the leak archives (the built-in ones and the ones added)
    POST /api/darkweb/leaks/add|remove   add an archive {name, address, search?, about?}; remove one that was added {name}
    GET  /api/darkweb/custom                   the person's pinned onion search engines and directories, used in every search
    POST /api/darkweb/custom/<kind>/add        add a source {name, url}; kind is "engines" or "directories"
    POST /api/darkweb/custom/<kind>/enabled    turn one on or off {name, enabled}
    POST /api/darkweb/custom/<kind>/remove     remove one {name}
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from tadamoo.darkweb_runtime import Runtime, _custom_add, _custom_remove, _custom_rows, _custom_set_enabled, _leak_add, _leak_remove, _leak_rows


class DarkwebError(ValueError):
    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.code = code


class DarkwebApi:
    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime

    def get(self) -> Dict[str, Any]:
        return self.rt.status()

    def leaks(self) -> Dict[str, Any]:
        if self.rt.status()["package"] is False:
            raise DarkwebError("the darkweb package is not installed in this Python", "no_package")
        return {"archives": _leak_rows(self.rt)}

    def custom(self) -> Dict[str, Any]:
        if self.rt.status()["package"] is False:
            raise DarkwebError("the darkweb package is not installed in this Python", "no_package")
        return _custom_rows(self.rt)

    def post(self, path: str, req: Dict[str, Any]) -> Dict[str, Any]:
        if self.rt.status()["package"] is False:
            raise DarkwebError("the darkweb package is not installed in this Python", "no_package")
        if path in ("tor/install", "tor/start", "tor/stop"):
            try:
                return {"tor/install": self.rt.install_tor, "tor/start": self.rt.start_tor, "tor/stop": self.rt.stop_tor}[path]()
            except Exception as e:                          # the reason is told as it is (no GnuPG, a hash that is not the pinned one, no Tor installed)
                raise DarkwebError(str(e)[:300] or type(e).__name__, "tor") from None
        if path in ("leaks/add", "leaks/remove"):
            try:
                return {"archives": _leak_add(self.rt, req) if path == "leaks/add" else _leak_remove(self.rt, str(req.get("name") or ""))}
            except ValueError as e:
                raise DarkwebError(str(e)) from None
        if path.startswith("custom/") and path.count("/") == 2:
            _, kind, action = path.split("/")
            if action not in ("add", "enabled", "remove"):
                raise DarkwebError("not found", "not_found")
            try:
                if action == "add":
                    return _custom_add(self.rt, kind, req)
                if action == "enabled":
                    return _custom_set_enabled(self.rt, kind, req)
                return _custom_remove(self.rt, kind, str(req.get("name") or ""))
            except ValueError as e:
                raise DarkwebError(str(e)) from None
        if path == "":
            try:
                self.rt.save(req)
            except ValueError as e:
                raise DarkwebError(str(e)) from None
            return self.rt.status()
        raise DarkwebError("not found", "not_found")
