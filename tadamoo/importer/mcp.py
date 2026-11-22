"""MCP servers: one entry of a config file (.mcp.json, claude_desktop_config.json and the like) becomes a record in the imported servers. The
record is imported turned OFF: a server is a program the harness starts, so the person turns it on.

The copy keeps the command, the arguments, the address and the variables the server reads; the value of a variable that looks like a key is
not kept, it is read from the vault entry the record names when the server starts."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List

from tadamoo.capabilities import LONG_TOKEN, SECRET_FLAG, redact_args
from tadamoo.discovery.installed import env_spec

from .common import Problem, Snapshot, Unit, canonical, check_name, now_iso, sha_text

KIND = "mcp"
SERVER_NAME = re.compile(r"^[A-Za-z0-9_\-.]{1,64}$")
MAX_CONFIG = 2_000_000


def servers_in(config: Any) -> Dict[str, Dict[str, Any]]:
    """The servers a config file lists, by name."""
    if not isinstance(config, dict):
        return {}
    servers = config.get("mcpServers")
    if servers is None and config and all(isinstance(v, dict) and ("command" in v or "url" in v) for v in config.values()):
        servers = config
    return {k: v for k, v in (servers or {}).items() if isinstance(k, str) and isinstance(v, dict) and SERVER_NAME.match(k)} if isinstance(servers, dict) else {}


def _record(name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    command, args, url = spec.get("command"), spec.get("args"), str(spec.get("url") or "")
    if isinstance(command, str) and command.strip():
        if args is not None and (not isinstance(args, list) or not all(isinstance(a, (str, int, float)) for a in args)):
            raise Problem("bad_server", f"The arguments of '{name}' are not a list of text.")
        line = [command.strip(), *[str(a) for a in (args or [])]]
        url = ""
    elif url:
        line = []
    else:
        raise Problem("bad_server", f"'{name}' has neither a command nor an address.")
    env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
    variables = []
    for key, value in env.items():
        secret = bool(SECRET_FLAG.search(str(key))) or bool(LONG_TOKEN.match(str(value)))
        variables.append({"name": str(key), "required": secret, "secret": secret, "value": "" if secret else str(value)})
    return {"name": name, "command": line, "url": url, "env": env_spec(variables)}


def _shown(record: Dict[str, Any]) -> str:
    c = record["command"]
    return " ".join([c[0], *redact_args(c[1:])]) if c else record["url"].split("?", 1)[0].split("#", 1)[0]


def _keys(record: Dict[str, Any]) -> List[Dict[str, str]]:
    return [{"name": e["name"], "entry": e["entry"]} for e in record["env"] if e.get("secret")]


class McpKind:
    kind = KIND

    def __init__(self, imp: Any) -> None:
        self.imp = imp

    def file(self, name: str) -> Path:
        return self.imp.root / "mcp" / f"{check_name(name)}.json"

    def off_id(self, name: str) -> str:
        return f"mcp:{name}"

    # -- reading a source ----------------------------------------------------------------------------------

    def snapshot(self, path: Any, server: Any = "", **_: Any) -> Snapshot:
        p = Path(str(path or "").strip().strip('"'))
        if not p.is_file():
            raise Problem("no_source", f"There is no config file at {p}.")
        try:
            if p.stat().st_size > MAX_CONFIG:
                raise Problem("too_big", "That file is too large to be an MCP config.")
            config = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeDecodeError):
            raise Problem("bad_source", "That file is not valid JSON.") from None
        return self.snapshot_from(config, str(p), str(server or ""))

    def snapshot_from(self, config: Any, source: str, server: str = "", owner: str = "") -> Snapshot:
        found = servers_in(config)
        if not found and isinstance(config, dict) and ("command" in config or "url" in config):         # a file that is one server's own definition
            if not SERVER_NAME.match(server):
                raise Problem("choose_server", "Give the server a name (letters, digits, dot, dash, underscore).", servers=[])
            found = {server: config}
        if not server:
            if len(found) != 1:
                raise Problem("choose_server", "Choose which server to import." if found else "That file lists no MCP server.", servers=sorted(found))
            server = next(iter(found))
        if server not in found:
            raise Problem("no_server", f"That file has no server named '{server}'.", servers=sorted(found))
        record = _record(server, found[server])
        warnings = []
        if isinstance(found[server].get("headers"), dict) and found[server]["headers"]:
            warnings.append("The headers of the server are not copied; a remote server that needs one signs in from the Capabilities page.")
        snap = Snapshot(KIND, server, source, sha_text(canonical(record)), description=f"MCP server: {_shown(record)}", payload={"record": record}, warnings=warnings,
                        extra={"runs": _shown(record), "remote": not record["command"], "needs_keys": _keys(record), "servers": sorted(found)})
        return snap

    # -- writing -------------------------------------------------------------------------------------------

    def blockers(self, snap: Snapshot, items: Dict[str, Any], **_: Any) -> List[Dict[str, Any]]:
        if self.file(snap.name).exists() and f"mcp:{snap.name}" not in items:
            return [{"code": "file_exists", "message": f"The imported servers already hold '{snap.name}' and the manifest does not know it."}]
        return []

    def _set_off(self, txn: Any, name: str, off: bool) -> None:
        path = self.imp.spaces.cap_dir / "settings.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        current = [x for x in data.get("off", []) if isinstance(x, str)]
        wanted = sorted(({*current, self.off_id(name)}) if off else (set(current) - {self.off_id(name)}))
        if wanted != current:
            txn.write_file(path, {**data, "off": wanted})

    def add(self, txn: Any, snap: Snapshot, items: Dict[str, Any], parent: str = "", **_: Any) -> Dict[str, Any]:
        record = snap.payload["record"]
        txn.write_file(self.file(snap.name), record)
        self._set_off(txn, snap.name, True)
        self.imp.keep_original(txn, snap)
        entry = {"kind": KIND, "name": snap.name, "version": "", "description": snap.description, "source": snap.source, "hash": snap.hash, "copy_hash": sha_text(canonical(record)),
                 "imported_at": now_iso(), "updated_at": now_iso(), "parent": parent, "runs": snap.extra["runs"], "needs_keys": snap.extra["needs_keys"]}
        items[f"mcp:{snap.name}"] = entry
        return entry

    def replace(self, txn: Any, snap: Snapshot, items: Dict[str, Any], entry: Dict[str, Any], **_: Any) -> None:
        """A server that now starts something else is turned off again until the person turns it on."""
        old = self.read(entry["name"])
        record = snap.payload["record"]
        txn.write_file(self.file(snap.name), record)
        if old is None or (old["command"], old["url"]) != (record["command"], record["url"]):
            self._set_off(txn, snap.name, True)
        self.imp.keep_original(txn, snap)
        entry.update(description=snap.description, source=snap.source, hash=snap.hash, copy_hash=sha_text(canonical(record)), updated_at=now_iso(), runs=snap.extra["runs"],
                     needs_keys=snap.extra["needs_keys"])

    def drop(self, txn: Any, items: Dict[str, Any], entry: Dict[str, Any]) -> None:
        txn.remove_file(self.file(entry["name"]))
        self._set_off(txn, entry["name"], False)
        self.imp.drop_original(txn, KIND, entry["name"])
        items.pop(f"mcp:{entry['name']}", None)

    # -- an update that keeps what the person changed ------------------------------------------------------

    @staticmethod
    def _text(record: Any) -> bytes:
        return (json.dumps(record, indent=1, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")

    def keep_original(self, snap: Snapshot, dest: Path) -> None:
        (dest / "record.json").write_bytes(self._text(snap.payload["record"]))

    def merge_units(self, snap: Snapshot, entry: Dict[str, Any], work: Path) -> List[Unit]:
        """The record of the server as it is now, the new source's record, and the record that was imported."""
        ours, theirs = work / "ours", work / "theirs"
        ours.mkdir(parents=True)
        theirs.mkdir(parents=True)
        record = self.read(entry["name"])
        if record is not None:
            (ours / "record.json").write_bytes(self._text(record))
        self.keep_original(snap, theirs)
        base, base_from = work / "base", ""
        original = self.imp.originals(KIND, entry["name"])
        if (original / "record.json").is_file():
            base.mkdir()
            shutil.copyfile(original / "record.json", base / "record.json")
            base_from = "original"
        elif record is not None and sha_text(canonical(record)) == entry.get("copy_hash"):
            shutil.copytree(ours, base)                                  # the record was never edited: it is the original
            base_from = "copy"
        return [Unit(f"mcp:{entry['name']}", entry["name"], ours, theirs, base if base_from else None, base_from)]

    def merged_snapshot(self, snap: Snapshot, finals: Dict[str, Path]) -> Snapshot:
        try:
            record = json.loads((finals[f"mcp:{snap.name}"] / "record.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise Problem("bad_merge", f"The merged record of the server '{snap.name}' is not valid JSON.") from None
        if not (isinstance(record, dict) and record.get("name") == snap.name and isinstance(record.get("command"), list) and isinstance(record.get("url"), str)
                and isinstance(record.get("env"), list) and all(isinstance(e, dict) and isinstance(e.get("name"), str) for e in record["env"])):
            raise Problem("bad_merge", f"The merged record of the server '{snap.name}' is not a server record (name, command, url, env).")
        shown = _shown(record)
        return Snapshot(KIND, snap.name, snap.source, snap.hash, description=f"MCP server: {shown}", payload={"record": record}, warnings=snap.warnings,
                        extra={"runs": shown, "remote": not record["command"], "needs_keys": _keys(record), "servers": snap.extra.get("servers", [])}, original=snap)

    def set_enabled(self, txn: Any, name: str, enabled: bool) -> None:
        self._set_off(txn, name, not enabled)

    # -- what the copy is now ------------------------------------------------------------------------------

    def read(self, name: str) -> Any:
        try:
            return json.loads(self.file(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def enabled(self, name: str) -> bool:
        try:
            data = json.loads((self.imp.spaces.cap_dir / "settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        return self.off_id(name) not in (data.get("off", []) if isinstance(data, dict) else [])

    def local_changes(self, entry: Dict[str, Any]) -> List[str]:
        record = self.read(entry["name"])
        if record is None:
            return ["missing"]
        return [] if sha_text(canonical(record)) == entry.get("copy_hash") else ["record"]

    def preview(self, snap: Snapshot, entry: Dict[str, Any]) -> Dict[str, Any]:
        old = self.read(entry["name"]) or {}
        new = snap.payload["record"]
        rows = [{"file": k} for k in ("command", "url", "env") if old.get(k) != new.get(k)]
        starts_other = (old.get("command"), old.get("url")) != (new["command"], new["url"])
        return {"source_changed": snap.hash != entry["hash"], "diff": {"added": [], "removed": [], "changed": rows, "counts": {"added": 0, "removed": 0, "changed": len(rows)}},
                "now_runs": snap.extra["runs"], "turns_off": starts_other}
