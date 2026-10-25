"""Language servers for the agent: what is wrong in a file, where a name is defined and where it is used.

A language server is a program the person already has (pyright, typescript-language-server, clangd, rust-analyzer, gopls...), started on
first use for the language of a file and spoken to over its standard input and output. Which program runs for which file comes from
`lsp.json` in the user data folder or, failing that, from the first known server found on the PATH; a folder under work never names a
command to run. Results are read-only facts about the files and arrive marked as untrusted text.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse
from urllib.request import pathname2url, url2pathname

from harness.tools import EffectClass, ToolRegistry

REQUEST_TIMEOUT_S = 30.0
DIAGNOSTICS_WAIT_S = 8.0
MAX_ITEMS = 40
SEVERITY = {1: "error", 2: "warning", 3: "information", 4: "hint"}
LANGUAGES = {".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascriptreact", ".mjs": "javascript", ".cjs": "javascript",
             ".ts": "typescript", ".tsx": "typescriptreact", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cu": "cuda-cpp",
             ".rs": "rust", ".go": "go", ".cs": "csharp", ".java": "java", ".json": "json"}
# the first of these found on the PATH serves the language
KNOWN = {
    "python": [["pyright-langserver", "--stdio"], ["pylsp"]],
    "javascript": [["typescript-language-server", "--stdio"]],
    "javascriptreact": [["typescript-language-server", "--stdio"]],
    "typescript": [["typescript-language-server", "--stdio"]],
    "typescriptreact": [["typescript-language-server", "--stdio"]],
    "c": [["clangd"]], "cpp": [["clangd"]], "cuda-cpp": [["clangd"]],
    "rust": [["rust-analyzer"]], "go": [["gopls"]],
}


class LspError(Exception):
    pass


def to_uri(path: Path) -> str:
    return "file:" + pathname2url(str(Path(path).resolve())) if os.name == "nt" else Path(path).resolve().as_uri()


def from_uri(uri: str) -> str:
    parts = urlparse(uri)
    return url2pathname(unquote(parts.path)) if parts.scheme == "file" else uri


class LspClient:
    """One language server process."""

    def __init__(self, command: List[str], root: Path, env: Optional[Dict[str, str]] = None) -> None:
        self.command, self.root = list(command), Path(root).resolve()
        self.env = env
        self.proc: Optional[subprocess.Popen] = None
        self.capabilities: Dict[str, Any] = {}
        self._id = 0
        self._lock = threading.Lock()
        self._wlock = threading.Lock()
        self._waiting: Dict[int, Dict[str, Any]] = {}
        self._diagnostics: Dict[str, List[Dict[str, Any]]] = {}
        self._seen: Dict[str, threading.Event] = {}
        self._versions: Dict[str, int] = {}
        self._dead = threading.Event()

    # -- the process --------------------------------------------------------------------------------------------

    def start(self) -> None:
        try:
            self.proc = subprocess.Popen(self.command, cwd=str(self.root), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         env=self.env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            raise LspError(f"The language server {self.command[0]} could not be started: {e.strerror or e}") from None
        threading.Thread(target=self._reader, name="lsp-reader", daemon=True).start()
        result = self.request("initialize", {
            "processId": os.getpid(), "rootUri": to_uri(self.root), "rootPath": str(self.root),
            "workspaceFolders": [{"uri": to_uri(self.root), "name": self.root.name}],
            "capabilities": {"textDocument": {"synchronization": {"didSave": False}, "publishDiagnostics": {"relatedInformation": False},
                                              "diagnostic": {"dynamicRegistration": False}, "definition": {"linkSupport": False},
                                              "references": {}, "hover": {"contentFormat": ["plaintext"]}},
                             "workspace": {"workspaceFolders": True, "configuration": True}},
            "clientInfo": {"name": "Basalt"}})
        self.capabilities = (result or {}).get("capabilities") or {}
        self.notify("initialized", {})

    def stop(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            self.request("shutdown", None, timeout=3.0)
            self.notify("exit", None)
        except LspError:
            pass
        try:
            proc.wait(2.0)
        except subprocess.TimeoutExpired:
            proc.kill()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and not self._dead.is_set()

    # -- the wire -----------------------------------------------------------------------------------------------

    def _send(self, message: Dict[str, Any]) -> None:
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        try:
            with self._wlock:
                self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
                self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError):
            self._dead.set()
            raise LspError("The language server is not running") from None

    def _read_message(self) -> Optional[Dict[str, Any]]:
        length = None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            name, _, value = line.partition(b":")
            if name.lower() == b"content-length":
                try:
                    length = int(value.strip())
                except ValueError:
                    return {}
        if length is None or length < 0 or length > 64 << 20:
            return {}
        data = self.proc.stdout.read(length)
        if len(data) < length:
            return None
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            return {}
        return msg if isinstance(msg, dict) else {}

    def _reader(self) -> None:
        try:
            while True:
                msg = self._read_message()
                if msg is None:
                    break
                if msg:
                    self._handle(msg)
        except (OSError, ValueError):
            pass
        finally:
            self._dead.set()
            with self._lock:
                for slot in self._waiting.values():
                    slot["event"].set()

    def _handle(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        if method is None and "id" in msg:                                  # an answer to one of ours
            with self._lock:
                slot = self._waiting.pop(msg["id"], None)
            if slot is not None:
                slot["msg"] = msg
                slot["event"].set()
            return
        if method == "textDocument/publishDiagnostics":
            params = msg.get("params") or {}
            uri = params.get("uri", "")
            self._diagnostics[uri] = [d for d in params.get("diagnostics") or [] if isinstance(d, dict)]
            self._seen.setdefault(uri, threading.Event()).set()
            return
        if "id" in msg and method:                                         # the server asks us something: nothing to tell it
            result: Any = None
            if method == "workspace/configuration":
                result = [None for _ in (msg.get("params") or {}).get("items") or []]
            try:
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            except LspError:
                pass

    def request(self, method: str, params: Any, timeout: float = REQUEST_TIMEOUT_S) -> Any:
        with self._lock:
            self._id += 1
            rid = self._id
            slot = {"event": threading.Event(), "msg": None}
            self._waiting[rid] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            if not slot["event"].wait(timeout):
                raise LspError(f"The language server did not answer '{method}' in time")
        finally:
            with self._lock:
                self._waiting.pop(rid, None)
        msg = slot["msg"]
        if msg is None:
            raise LspError("The language server stopped")
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise LspError(f"The language server refused '{method}': {err.get('message', err)}")
        return msg.get("result")

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # -- the files ----------------------------------------------------------------------------------------------

    def sync(self, path: Path, language: str) -> str:
        """The server has the file as it is on disk now (opened, or changed when it was already open); its uri."""
        uri = to_uri(path)
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        self._seen[uri] = threading.Event()
        version = self._versions.get(uri, 0) + 1
        self._versions[uri] = version
        if version == 1:
            self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": language, "version": 1, "text": text}})
        else:
            self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]})
        return uri

    def diagnostics(self, uri: str, wait_s: float = DIAGNOSTICS_WAIT_S) -> List[Dict[str, Any]]:
        if self.capabilities.get("diagnosticProvider"):
            try:
                result = self.request("textDocument/diagnostic", {"textDocument": {"uri": uri}}, timeout=wait_s + 10)
            except LspError:
                result = None
            if isinstance(result, dict) and isinstance(result.get("items"), list):
                return [d for d in result["items"] if isinstance(d, dict)]
        self._seen.setdefault(uri, threading.Event()).wait(wait_s)
        return list(self._diagnostics.get(uri, []))

    def locations(self, method: str, uri: str, line: int, column: int, extra: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        params = {"textDocument": {"uri": uri}, "position": {"line": line, "character": column}, **(extra or {})}
        result = self.request(method, params)
        if result is None:
            return []
        found = result if isinstance(result, list) else [result]
        out = []
        for item in found:
            if isinstance(item, dict) and "targetUri" in item:             # a LocationLink
                item = {"uri": item["targetUri"], "range": item.get("targetSelectionRange") or item.get("targetRange")}
            if isinstance(item, dict) and item.get("uri") and isinstance(item.get("range"), dict):
                out.append(item)
        return out


class LspHub:
    """The servers of one workspace: one process for each command, started when a file of its language is asked about."""

    def __init__(self, workspace: Path, config_path: Optional[Path] = None, which: Optional[Callable[[str], Optional[str]]] = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.which = which or (lambda name: shutil.which(name))
        self.config = self._read(config_path)
        self._clients: Dict[Tuple[str, ...], LspClient] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _read(path: Optional[Path]) -> Dict[str, List[str]]:
        """{"python": ["pyright-langserver", "--stdio"], ...} from the person's own file; anything else in it is left out."""
        if path is None:
            try:
                from serve.userdata import user_data_dir
                path = user_data_dir() / "lsp.json"
            except Exception:
                return {}
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        servers = data.get("servers") if isinstance(data, dict) else None
        return {lang: cmd for lang, cmd in (servers or {}).items()
                if isinstance(lang, str) and isinstance(cmd, list) and cmd and all(isinstance(c, str) and c for c in cmd)}

    def command_for(self, language: str) -> Optional[List[str]]:
        if language in self.config:
            return self.config[language]
        for cmd in KNOWN.get(language, []):
            if self.which(cmd[0]):
                return cmd
        return None

    def available(self) -> List[str]:
        """The languages a server is known for, for the tools' description."""
        return sorted({lang for lang in set(KNOWN) | set(self.config) if self.command_for(lang)})

    def client_for(self, path: Path) -> Tuple[LspClient, str]:
        language = LANGUAGES.get(Path(path).suffix.lower())
        if language is None:
            raise LspError(f"No language server is known for {Path(path).suffix or 'this kind of file'}.")
        cmd = self.command_for(language)
        if cmd is None:
            raise LspError(f"No language server for {language} is installed (looked for {', '.join(c[0] for c in KNOWN.get(language, [])) or 'one in lsp.json'}).")
        key = tuple(cmd)
        with self._lock:
            client = self._clients.get(key)
            if client is None or not client.alive:
                client = LspClient(cmd, self.workspace)
                client.start()
                self._clients[key] = client
        return client, language

    def close(self) -> None:
        with self._lock:
            clients, self._clients = list(self._clients.values()), {}
        for c in clients:
            c.stop()


def _line_text(path: str, line: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, text in enumerate(f):
                if i == line:
                    return text.strip()[:160]
    except OSError:
        pass
    return ""


def _shown(workspace: Path, path: str) -> str:
    try:
        return Path(path).resolve().relative_to(workspace).as_posix()
    except ValueError:
        return path


def register_lsp_tools(registry: ToolRegistry, hub: LspHub, resolve: Callable[[str], Path]) -> bool:
    """`diagnostics`, `go_to_definition` and `find_references`, when at least one server can be found. `resolve` keeps a path inside the
    workspace (the sandbox's own check). False when there is nothing to offer."""
    languages = hub.available()
    if not languages:
        return False

    def prepare(path: str) -> Tuple[LspClient, str, Path]:
        file = resolve(path)
        if not file.is_file():
            raise LspError(f"{path} is not a file")
        client, language = hub.client_for(file)
        return client, client.sync(file, language), file

    def where(items: List[Dict[str, Any]]) -> str:
        if not items:
            return "Nothing found."
        lines = []
        for loc in items[:MAX_ITEMS]:
            target = from_uri(loc["uri"])
            start = loc["range"].get("start", {})
            ln, col = int(start.get("line", 0)), int(start.get("character", 0))
            lines.append(f"{_shown(hub.workspace, target)}:{ln + 1}:{col + 1}  {_line_text(target, ln)}")
        more = f"\n[... {len(items) - MAX_ITEMS} more]" if len(items) > MAX_ITEMS else ""
        return "\n".join(lines) + more

    def position(line: Any, column: Any) -> Tuple[int, int]:
        if isinstance(line, bool) or not isinstance(line, int) or line < 1 or isinstance(column, bool) or not isinstance(column, int) or column < 1:
            raise LspError("line and column count from 1")
        return line - 1, column - 1

    def diagnostics(path: str) -> str:
        try:
            client, uri, file = prepare(path)
            found = client.diagnostics(uri)
        except (LspError, PermissionError) as e:
            return str(e)
        if not found:
            return f"No problems reported in {path}."
        rows = []
        for d in sorted(found, key=lambda d: ((d.get("range") or {}).get("start") or {}).get("line", 0))[:MAX_ITEMS]:
            start = (d.get("range") or {}).get("start") or {}
            rows.append(f"{path}:{int(start.get('line', 0)) + 1}:{int(start.get('character', 0)) + 1} {SEVERITY.get(d.get('severity'), 'problem')}: "
                        f"{' '.join(str(d.get('message', '')).split())[:300]}" + (f" [{d['code']}]" if d.get("code") not in (None, "") else ""))
        more = f"\n[... {len(found) - MAX_ITEMS} more]" if len(found) > MAX_ITEMS else ""
        return "\n".join(rows) + more

    def go_to_definition(path: str, line: int, column: int) -> str:
        try:
            ln, col = position(line, column)
            client, uri, _ = prepare(path)
            return where(client.locations("textDocument/definition", uri, ln, col))
        except (LspError, PermissionError) as e:
            return str(e)

    def find_references(path: str, line: int, column: int) -> str:
        try:
            ln, col = position(line, column)
            client, uri, _ = prepare(path)
            return where(client.locations("textDocument/references", uri, ln, col, {"context": {"includeDeclaration": True}}))
        except (LspError, PermissionError) as e:
            return str(e)

    obj = lambda props, req: {"type": "object", "properties": props, "required": req}
    at = {"path": {"type": "string", "description": "file in the workspace"}, "line": {"type": "integer", "description": "from 1"},
          "column": {"type": "integer", "description": "from 1"}}
    registry.register("diagnostics", f"The problems a language server finds in a file (errors, warnings), with line and column. Languages: {', '.join(languages)}.",
                      obj({"path": {"type": "string", "description": "file in the workspace"}}, ["path"]), EffectClass.PURE, diagnostics, untrusted=True)
    registry.register("go_to_definition", "Where the name at a line and column of a file is defined.", obj(at, ["path", "line", "column"]),
                      EffectClass.PURE, go_to_definition, untrusted=True)
    registry.register("find_references", "Every place the name at a line and column of a file is used.", obj(at, ["path", "line", "column"]),
                      EffectClass.PURE, find_references, untrusted=True)
    return True
