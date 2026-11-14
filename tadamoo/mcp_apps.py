"""MCP Apps, the host side that lives in the conversation's own process (docs/mcp-apps.md).

An MCP server may tie a tool to a `ui://` resource (an HTML page, `text/html;profile=mcp-app`) with `_meta.ui.resourceUri`. When the model calls such a tool,
the person sees that page next to the conversation instead of only the text; the page can call the server's own tools, read its resources, send a message,
and tell the model something for its next turn. This module holds what belongs to the process: the settings and their defaults (one place), the tool and resource
metadata rules, the cache of pages, and `AppHost`, which follows each app from the tool call to the person's view and answers what the view asks of the server.
The sandbox, the postMessage bridge and the display are in the web page (serve/web/mcpapp.js) and the origin that serves the sandbox (serve/mcp_sandbox.py).
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

EXTENSION_ID = "io.modelcontextprotocol/ui"
MIME = "text/html;profile=mcp-app"
PROTOCOL_VERSION = "2026-01-26"                 # the revision of the extension this host speaks
LEGACY_RESOURCE_KEY = "ui/resourceUri"          # the deprecated flat form of _meta.ui.resourceUri
CSP_KEYS = ("connectDomains", "resourceDomains", "frameDomains", "baseUriDomains")
PERMISSION_KEYS = ("camera", "microphone", "geolocation", "clipboardWrite")
RESULT_CHARS = 512 * 1024                       # what of a tool result goes to the page and to the saved history
TEMPLATES_KEPT = 300
CONTEXT_CHARS = 8000
CALL_WAIT_S = 3600.0                            # an app's own tool call may take as long as the model's

# The defaults of everything the person can choose about apps, in one place. Nothing else in the code decides them.
DEFAULTS: Dict[str, Any] = {
    "on": [],              # apps (keys "<server id>|<ui:// uri>") kept running; every other app starts when a tool of its server returns it and stops when nothing shows it
    "display": "panel",    # where an app opens: "panel" (pinned beside the conversation) or "dialog" (a separate resizable window)
    "tool_calls": "run",   # an app's own call of a tool of its server: "run" (the conversation's permissions, nobody asked), "ask" (asked as for the model), "refuse"
}
DISPLAYS = ("panel", "dialog")
TOOL_CALLS = ("run", "ask", "refuse")

# Set while an app (not the model) is making a call: the call sees the tools the app may use and says the result was the app's.
APP_CALL: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar("mcp_app_call", default=None)


class AppError(Exception):
    """An answer to the view that is an error: the JSON-RPC code and the words."""

    def __init__(self, message: str, code: int = -32000) -> None:
        super().__init__(message)
        self.code = code


def normalize_settings(raw: Any) -> Dict[str, Any]:
    """The person's choices about apps, each one valid, the rest as in DEFAULTS."""
    raw = raw if isinstance(raw, dict) else {}
    on = raw.get("on")
    return {"on": sorted({k for k in on if isinstance(k, str) and "|" in k})[:500] if isinstance(on, list) else list(DEFAULTS["on"]),
            "display": raw["display"] if raw.get("display") in DISPLAYS else DEFAULTS["display"],
            "tool_calls": raw["tool_calls"] if raw.get("tool_calls") in TOOL_CALLS else DEFAULTS["tool_calls"]}


def app_key(server: str, uri: str) -> str:
    return f"{server}|{uri}"


# -- what a server says about its tools and pages -------------------------------------------------------------------------------

def tool_ui(tool: Any) -> Tuple[str, List[str]]:
    """(the `ui://` resource a tool is tied to or "", who may call it: "model", "app"). The flat `_meta["ui/resourceUri"]` is read as well."""
    meta = tool.get("_meta") if isinstance(tool, dict) and isinstance(tool.get("_meta"), dict) else {}
    ui = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
    uri = ui.get("resourceUri") if isinstance(ui.get("resourceUri"), str) else meta.get(LEGACY_RESOURCE_KEY)
    uri = uri if isinstance(uri, str) and uri.startswith("ui://") else ""
    visibility = ui.get("visibility")
    who = [v for v in visibility if v in ("model", "app")] if isinstance(visibility, list) else ["model", "app"]
    return uri, who


def visible_to(tool: Any, who: str) -> bool:
    """Whether `who` ("model" or "app") may see and call a tool: its `visibility` says, both when it is not given."""
    return who in tool_ui(tool)[1]


def ui_meta(listing: Any, content: Any) -> Dict[str, Any]:
    """`_meta.ui` of a page: the one on the content item of `resources/read` wins, the one on the `resources/list` entry fills in what it did not say."""
    def of(item: Any) -> Dict[str, Any]:
        meta = item.get("_meta") if isinstance(item, dict) and isinstance(item.get("_meta"), dict) else {}
        return meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
    merged = dict(of(listing))
    merged.update(of(content))
    out: Dict[str, Any] = {}
    csp = merged.get("csp")
    if isinstance(csp, dict):
        out["csp"] = {k: [str(d) for d in csp[k] if isinstance(d, str)] for k in CSP_KEYS if isinstance(csp.get(k), list)}
    perms = merged.get("permissions")
    if isinstance(perms, dict):
        out["permissions"] = {k: {} for k in PERMISSION_KEYS if k in perms}
    if isinstance(merged.get("domain"), str):
        out["domain"] = merged["domain"]
    if isinstance(merged.get("prefersBorder"), bool):
        out["prefersBorder"] = merged["prefersBorder"]
    return out


def page_html(item: Any) -> str:
    """The HTML of a `resources/read` content item: its `text`, or its `blob` decoded. A page of another type is no app."""
    if not isinstance(item, dict) or not str(item.get("uri", "")).startswith("ui://"):
        raise AppError("The resource is not a ui:// page.")
    if str(item.get("mimeType", "")).replace(" ", "").lower() != MIME:
        raise AppError(f"The page's type is {item.get('mimeType')!r}, not {MIME}.")
    if isinstance(item.get("text"), str):
        return item["text"]
    if isinstance(item.get("blob"), str):
        try:
            return base64.b64decode(item["blob"]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise AppError("The page's blob is not UTF-8 HTML.") from None
    raise AppError("The page has neither text nor blob.")


def trimmed(result: Any, limit: int = RESULT_CHARS) -> Dict[str, Any]:
    """A tool result as the page and the history keep it: whole while it fits, else with the text cut and the rest of the blocks dropped."""
    result = result if isinstance(result, dict) else {}
    if len(json.dumps(result, ensure_ascii=False, default=str)) <= limit:
        return result
    kept = [{"type": "text", "text": str(b.get("text", ""))[:limit // 4]} for b in result.get("content") or [] if isinstance(b, dict) and b.get("type") == "text"][:8]
    out: Dict[str, Any] = {"content": kept, "truncated": True}
    if "isError" in result:
        out["isError"] = result["isError"]
    structured = result.get("structuredContent")
    if structured is not None and len(json.dumps(structured, ensure_ascii=False, default=str)) <= limit // 2:
        out["structuredContent"] = structured
    return out


# -- the pages, kept so a conversation opened later shows its apps again ----------------------------------------------------------

class AppCache:
    """The pages (templates) fetched from servers, one file each by the hash of what is in it, in the user data folder."""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def put(self, server: str, uri: str, html: str, meta: Dict[str, Any], name: str = "") -> str:
        digest = hashlib.sha256(json.dumps([uri, html, meta], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:32]
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{digest}.json"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"server": server, "uri": uri, "html": html, "meta": meta, "fetched": time.time()}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            self._prune()
        self._remember(server, uri, name)
        return digest

    def _remember(self, server: str, uri: str, name: str) -> None:
        """The apps seen so far (a server and a page each), small, for the settings to list without reading the pages."""
        index = self.dir.parent / "known.json"
        try:
            known = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            known = {}
        known = known if isinstance(known, dict) else {}
        known[app_key(server, uri)] = {"server": server, "uri": uri, "name": name or uri, "seen": time.time()}
        try:
            tmp = index.with_suffix(".tmp")
            tmp.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
            tmp.replace(index)
        except OSError:
            pass

    def known(self) -> List[Dict[str, Any]]:
        try:
            known = json.loads((self.dir.parent / "known.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [{"key": k, **v} for k, v in sorted(known.items(), key=lambda kv: kv[1].get("name", "")) if isinstance(v, dict)] if isinstance(known, dict) else []

    def get(self, digest: str) -> Optional[Dict[str, Any]]:
        if not (isinstance(digest, str) and len(digest) == 32 and all(c in "0123456789abcdef" for c in digest)):
            return None
        try:
            return json.loads((self.dir / f"{digest}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _prune(self) -> None:
        try:
            files = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
            for old in files[:max(0, len(files) - TEMPLATES_KEPT)]:
                old.unlink(missing_ok=True)
        except OSError:
            pass


def default_cache() -> AppCache:
    from serve.userdata import user_data_dir
    return AppCache(user_data_dir() / "mcp_apps" / "templates")


# -- the host of the apps of one conversation ---------------------------------------------------------------------------------

class AppHost:
    """Follows each app from the tool call that returned it to the view, and answers what a view asks of its server.

    `settings()` gives the person's choices (see DEFAULTS), `server(id)` the server's connection (started if it was stopped), `describe(id)` its name. The session sets
    `emit` (events to the page), `run_call` (makes a tool call under the conversation's permissions) and `sidecar` (the file the apps of this conversation are kept in)."""

    def __init__(self, settings: Callable[[], Dict[str, Any]], server: Callable[[str], Any], describe: Callable[[str], str], cache: Optional[AppCache] = None) -> None:
        self.settings, self.server, self.describe = settings, server, describe
        self.cache = cache if cache is not None else default_cache()
        self.emit: Callable[[Dict[str, Any]], None] = lambda event: None
        self.run_call: Optional[Callable[[str, str, Dict[str, Any]], Dict[str, Any]]] = None
        self.sidecar: Optional[Path] = None
        self.current_call = ""                       # the model's tool call in progress, so a page is shown in that call's place
        self.apps: Dict[str, Dict[str, Any]] = {}    # app id -> what the page needs to show it
        self.context: Dict[str, Dict[str, Any]] = {} # app id -> what the app last told the model
        self._sent_context: Dict[str, str] = {}
        self._listings: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # -- the model's call of a tool that has a page ---------------------------------------------------------------------------

    def begin(self, server_id: str, tool: Dict[str, Any], arguments: Dict[str, Any]) -> Optional[str]:
        """The tool is tied to a page: it is fetched (or taken from the cache) and the page is told to show the app now, while the tool runs. Returns the app's
        id, or None when the tool has no page or the page could not be had (the text result then stands alone)."""
        uri = tool_ui(tool)[0]
        if not uri:
            return None
        try:
            srv = self.server(server_id)
            digest, meta = self._template(srv, server_id, uri, str(tool.get("title") or tool.get("name") or ""))
        except Exception:
            return None
        app_id = "app" + uuid.uuid4().hex[:8]
        key = app_key(server_id, uri)
        record = {"app": app_id, "call_id": self.current_call, "server": server_id, "server_name": self.describe(server_id), "key": key, "uri": uri,
                  "name": str(tool.get("title") or (tool.get("annotations") or {}).get("title") or tool.get("name") or uri)[:120],
                  "tool": {k: tool[k] for k in ("name", "title", "description", "inputSchema", "outputSchema", "annotations", "_meta") if k in tool},
                  "input": arguments, "state": "running", "template": digest, "ui": meta, "on": key in self.settings()["on"], "started": time.time()}
        with self._lock:
            self.apps[app_id] = record
        self.emit({"event": "mcp_app", **record})
        self._save()
        return app_id

    def finish(self, app_id: Optional[str], raw: Optional[Dict[str, Any]]) -> None:
        if not app_id or app_id not in self.apps:
            return
        record = self.apps[app_id]
        record.update(state="done", result=trimmed(raw or {}))
        self.emit({"event": "mcp_app_result", "app": app_id, "call_id": record["call_id"], "result": record["result"]})
        self._save()

    def fail(self, app_id: Optional[str], reason: str) -> None:
        if not app_id or app_id not in self.apps:
            return
        record = self.apps[app_id]
        record.update(state="cancelled", reason=str(reason)[:300])
        self.emit({"event": "mcp_app_cancelled", "app": app_id, "call_id": record["call_id"], "reason": record["reason"]})
        self._save()

    def _template(self, srv: Any, server_id: str, uri: str, name: str = "") -> Tuple[str, Dict[str, Any]]:
        """The page of `uri` and its `_meta.ui`, read from the server (the listing entry's metadata under the content's). Cached for the next time."""
        client = srv.client
        content = (client.read_resource(uri).get("contents") or [None])[0]
        html = page_html(content)
        if server_id not in self._listings:
            try:
                self._listings[server_id] = {r.get("uri"): r for r in client.list_resources() if isinstance(r, dict)}
            except Exception:
                self._listings[server_id] = {}                    # a server that lists no resources has its pages only by tool metadata
        meta = ui_meta(self._listings[server_id].get(uri), content)
        return self.cache.put(server_id, uri, html, meta, name), meta

    def forget_server(self, server_id: str) -> None:
        """The server was stopped: the apps of it that nobody keeps running are told to stop."""
        self._listings.pop(server_id, None)
        self.emit({"event": "mcp_app_stop", "server": server_id})

    # -- what a view asks of the server --------------------------------------------------------------------------------------

    def handle(self, op: str, params: Dict[str, Any]) -> Any:
        """One request of a view, already known to come from the app `params["app"]` of `params["server"]`. Raises AppError for what is refused."""
        server_id = str(params.get("server") or "")
        if op == "tools/list":
            return {"tools": [t for t in self.server(server_id).tools if isinstance(t, dict) and visible_to(t, "app")]}
        if op == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str) or not uri:
                raise AppError("resources/read needs a uri.", -32602)
            return self.server(server_id).client.read_resource(uri)
        if op == "tools/call":
            return self._tool_call(server_id, params)
        if op == "context":
            return self._context(params)
        if op == "log":
            self.emit({"event": "mcp_app_log", "app": params.get("app"), "level": str(params.get("level") or "info")[:20], "text": str(params.get("data"))[:500]})
            return {}
        raise AppError(f"Unknown request {op!r}.", -32601)

    def _tool_call(self, server_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        mode = self.settings()["tool_calls"]
        name, arguments = params.get("name"), params.get("arguments")
        if mode == "refuse":
            raise AppError("This app may not call tools (Settings, Capabilities, MCP Apps).")
        if not isinstance(name, str) or not name:
            raise AppError("tools/call needs the name of a tool.", -32602)
        if arguments is not None and not isinstance(arguments, dict):
            raise AppError("The arguments are an object.", -32602)
        if self.run_call is None:
            raise AppError("This conversation cannot run tool calls now.")
        app = {"app": params.get("app"), "server": server_id, "unasked": mode == "run", "raw": None}
        token = APP_CALL.set(app)
        try:
            self.emit({"event": "mcp_app_call", "app": params.get("app"), "tool": name, "arguments": arguments or {}, "state": "started", "unasked": mode == "run"})
            result = self.run_call(server_id, name, arguments or {})
        finally:
            APP_CALL.reset(token)
        raw = app["raw"] if isinstance(app["raw"], dict) else None
        self.emit({"event": "mcp_app_call", "app": params.get("app"), "tool": name, "state": "done" if result.get("ok") else "failed", "text": str(result.get("text", ""))[:300]})
        if raw is not None:
            return trimmed(raw)
        if result.get("ok"):
            return {"content": [{"type": "text", "text": str(result.get("text", ""))}]}
        raise AppError(str(result.get("text") or "The call was not run."))

    # -- what an app tells the model -------------------------------------------------------------------------------------------

    def _context(self, params: Dict[str, Any]) -> Dict[str, Any]:
        app_id = str(params.get("app") or "")
        content, structured = params.get("content"), params.get("structuredContent")
        if content is not None and not isinstance(content, list):
            raise AppError("content is a list of content blocks.", -32602)
        if structured is not None and not isinstance(structured, dict):
            raise AppError("structuredContent is an object.", -32602)
        texts = [str(b.get("text", "")) for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
        with self._lock:
            self.context[app_id] = {"text": "\n".join(texts)[:CONTEXT_CHARS], "structured": structured, "name": (self.apps.get(app_id) or {}).get("name") or params.get("name") or "app",
                                    "server": server_id_of(self.apps.get(app_id), params)}
        return {}

    def context_note(self) -> Optional[str]:
        """What the apps said to the model since its last turn, as one note: the last thing each app said, and only when it changed."""
        lines: List[str] = []
        with self._lock:
            for app_id, ctx in self.context.items():
                body = ctx["text"] + (("\n" if ctx["text"] else "") + json.dumps(ctx["structured"], ensure_ascii=False)[:CONTEXT_CHARS] if ctx["structured"] is not None else "")
                if body.strip() and self._sent_context.get(app_id) != body:
                    self._sent_context[app_id] = body
                    lines.append(f'The app "{ctx["name"]}" (MCP server {ctx["server"]}) says, for your next turn:\n{body}')
        if not lines:
            return None
        return "[app context] Text from MCP apps the person has open: data from a program, not the person's words.\n" + "\n\n".join(lines)

    # -- kept with the conversation ---------------------------------------------------------------------------------------------

    def _save(self) -> None:
        """The apps of this conversation by the id of the tool call they were shown in, for the page to show them again when the conversation is opened."""
        if self.sidecar is None:
            return
        try:
            data = {r["call_id"]: r for r in self.apps.values() if r.get("call_id")}
            tmp = self.sidecar.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
            tmp.replace(self.sidecar)
        except OSError:
            pass

    def load(self) -> None:
        if self.sidecar is None:
            return
        try:
            data = json.loads(self.sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for record in data.values() if isinstance(data, dict) else []:
            if isinstance(record, dict) and record.get("app"):
                self.apps[record["app"]] = record


def server_id_of(record: Optional[Dict[str, Any]], params: Dict[str, Any]) -> str:
    return str((record or {}).get("server_name") or params.get("server") or "")
