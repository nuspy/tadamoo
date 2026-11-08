"""Where the agent searches the web and reads pages, as the person chose it in Settings > Web search.

The person picks one search provider and, when a plain fetch fails, one page reader. Each provider is a cloud service (an account and a key, kept in
the credential store, never in the file that holds the choice) or a program on this PC (a local address, started by the harness when a search needs it).
The choice is a small JSON file in the user data folder, read at every search, so a change applies to the next one. Everything a provider or a reader
returns is text from outside: the callers mark it untrusted."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import urlencode, urlparse

FILE = "websearch.json"
KEY_PREFIX = "websearch/"
DEFAULT_SEARCH = "duckduckgo"
READERS = ("none", "firecrawl", "playwright")
MODES = ("local", "cloud")
API_SECONDS = 25.0
MAX_READER_CHARS = 250_000

# What each provider is. `cloud`: the service, with where to make the account and the key; `local`: the program on this PC and what it needs.
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "duckduckgo": {"kind": "search", "builtin": True, "cloud": {"needs_key": False, "signup_url": "https://duckduckgo.com/", "keys_url": ""}, "local": None},
    "searxng": {
        "kind": "search",
        "cloud": {"needs_key": False, "needs_url": True, "signup_url": "https://searx.space/", "keys_url": ""},
        "local": {"how": "docker", "needs": ["docker"], "default_url": "http://127.0.0.1:8888", "container": "tadamoo-searxng", "image": "searxng/searxng"},
    },
    "brave": {
        "kind": "search", "base": "https://api.search.brave.com/res/v1/web/search", "key_env": "BRAVE_API_KEY",
        "cloud": {"needs_key": True, "signup_url": "https://brave.com/search/api/", "keys_url": "https://api-dashboard.search.brave.com/app/keys"},
        "local": None,
    },
    "tavily": {
        "kind": "search", "key_env": "TAVILY_API_KEY",
        "cloud": {"needs_key": True, "signup_url": "https://app.tavily.com/", "keys_url": "https://app.tavily.com/home"},
        "local": None,
    },
    "exa": {
        "kind": "search", "key_env": "EXA_API_KEY",
        "cloud": {"needs_key": True, "signup_url": "https://exa.ai/", "keys_url": "https://dashboard.exa.ai/api-keys"},
        "local": None,
    },
    "firecrawl": {
        "kind": "both", "key_env": "FIRECRAWL_API_KEY", "base": "https://api.firecrawl.dev",
        "cloud": {"needs_key": True, "signup_url": "https://www.firecrawl.dev/", "keys_url": "https://www.firecrawl.dev/app/api-keys"},
        "local": {"how": "compose", "needs": ["docker", "git"], "default_url": "http://127.0.0.1:3002", "repo": "https://github.com/firecrawl/firecrawl"},
    },
    "playwright": {
        "kind": "reader",
        "cloud": None,
        "local": {"how": "npx", "needs": ["node"], "package": "@playwright/mcp@latest"},
    },
}


def user_dir() -> Path:
    from serve.userdata import user_data_dir
    return Path(user_data_dir())


def provider_ids(kind: str = "") -> List[str]:
    return [i for i, p in PROVIDERS.items() if not kind or p["kind"] in (kind, "both")]


def tools_present() -> Dict[str, bool]:
    """Which programs the installers need are on this PC."""
    return {name: shutil.which(name) is not None for name in ("docker", "git", "node", "npx")}


# -- the choice ----------------------------------------------------------------------------------------------------


def _clean_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or len(text) > 300:
        raise ValueError("an address that starts with http:// or https://, with no user name or password")
    return text


class Prefs:
    """The choice, in one JSON file: the search provider, the page reader, local or cloud for each, the addresses of the local programs."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else user_dir() / FILE

    def read(self) -> Dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
        search = raw.get("search") if raw.get("search") in provider_ids("search") else DEFAULT_SEARCH
        reader = raw.get("reader") if raw.get("reader") in READERS else "none"
        modes = {k: v for k, v in (raw.get("mode") or {}).items() if k in PROVIDERS and v in MODES} if isinstance(raw.get("mode"), dict) else {}
        urls = {k: v for k, v in (raw.get("urls") or {}).items() if k in PROVIDERS and isinstance(v, str)} if isinstance(raw.get("urls"), dict) else {}
        return {"search": search, "reader": reader, "mode": modes, "urls": urls, "autostart": raw.get("autostart") is not False}

    def write(self, **changes: Any) -> Dict[str, Any]:
        """Applies the changes (`search`, `reader`, `mode` and `urls` as {provider: value}, `autostart`) and returns what is kept; ValueError for a bad one."""
        now = self.read()
        if "search" in changes:
            if changes["search"] not in provider_ids("search"):
                raise ValueError("search: one of " + ", ".join(provider_ids("search")))
            now["search"] = changes["search"]
        if "reader" in changes:
            if changes["reader"] not in READERS:
                raise ValueError("reader: one of " + ", ".join(READERS))
            now["reader"] = changes["reader"]
        for field in ("mode", "urls"):
            for pid, value in (changes.get(field) or {}).items():
                if pid not in PROVIDERS:
                    raise ValueError(f"{field}: unknown provider {pid}")
                if field == "mode":
                    if value not in MODES:
                        raise ValueError("mode: local or cloud")
                    if PROVIDERS[pid].get(value) is None:
                        raise ValueError(f"{pid} has no {value} version")
                    now["mode"][pid] = value
                else:
                    url = _clean_url(value)
                    if url:
                        now["urls"][pid] = url
                    else:
                        now["urls"].pop(pid, None)
        if "autostart" in changes:
            if not isinstance(changes["autostart"], bool):
                raise ValueError("autostart: true or false")
            now["autostart"] = changes["autostart"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(now, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        return now

    def mode_of(self, pid: str) -> str:
        """local or cloud: the person's choice, else the local version when the provider has one, else the cloud one."""
        chosen = self.read()["mode"].get(pid)
        if chosen in MODES and PROVIDERS[pid].get(chosen) is not None:
            return chosen
        return "local" if PROVIDERS[pid].get("local") else "cloud"

    def url_of(self, pid: str) -> str:
        """The address of the provider's program or instance: the person's, else the local default; empty for a cloud service."""
        saved = self.read()["urls"].get(pid, "")
        if saved:
            return saved
        local = PROVIDERS[pid].get("local") or {}
        if pid == "firecrawl" and self.mode_of(pid) == "cloud":
            return PROVIDERS[pid]["base"]
        return str(local.get("default_url") or "") if self.mode_of(pid) == "local" else ""


# -- the keys ------------------------------------------------------------------------------------------------------


class Keys:
    """The keys of the cloud services, in the credential store under `websearch/<provider>`; a key is never returned to a page."""

    def __init__(self, store: Any = None) -> None:
        if store is None:
            from tadamoo.llm.credentials import default_store
            store = default_store(user_dir())
        self.store = store

    def get(self, pid: str, environ: Optional[Mapping[str, str]] = None) -> str:
        env = os.environ if environ is None else environ
        name = PROVIDERS.get(pid, {}).get("key_env") or ""
        if name and env.get(name):
            return str(env[name])
        try:
            return self.store.read(KEY_PREFIX + pid) or ""
        except Exception:                                   # a store that cannot be read has no key
            return ""

    def has(self, pid: str) -> bool:
        return bool(self.get(pid))

    def set(self, pid: str, key: str) -> None:
        if pid not in PROVIDERS or not (PROVIDERS[pid].get("cloud") or {}).get("needs_key"):
            raise ValueError(f"{pid} takes no key")
        key = str(key or "").strip()
        if key and (len(key) > 400 or not re.fullmatch(r"[\x21-\x7e]+", key)):
            raise ValueError("a key is one line of letters, digits and signs, up to 400 characters")
        if key:
            self.store.write(KEY_PREFIX + pid, key)
        else:
            self.store.delete(KEY_PREFIX + pid)


# -- the calls to the services -------------------------------------------------------------------------------------


class SearchError(RuntimeError):
    """A search provider or a reader failed; the message says why, for the model and the person to read."""


def _call(url: str, *, data: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, timeout: float = API_SECONDS) -> Any:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, headers={"Accept": "application/json", "User-Agent": "TaDaMoo/1", **({"Content-Type": "application/json"} if body else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read(8_000_000).decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        raise SearchError({401: "the key was refused", 403: "the key was refused or the plan does not allow this", 429: "too many requests: the plan's limit is reached, try later",
                           404: "the address answers but not to this call"}.get(exc.code, f"the service answered HTTP {exc.code}")) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SearchError(f"could not be reached: {getattr(exc, 'reason', exc)}") from None


def _results(rows: Any, title: str, url: str, snippet: str, limit: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get(url) or "").startswith(("http://", "https://")):
            out.append({"title": str(row.get(title) or "").strip(), "url": str(row[url]), "snippet": str(row.get(snippet) or "").strip()})
        if len(out) >= limit:
            break
    return out


def searxng_search(base: str, query: str, limit: int) -> List[Dict[str, Any]]:
    try:
        data = _call(base.rstrip("/") + "/search?" + urlencode({"q": query, "format": "json"}))
    except SearchError as exc:
        text = str(exc)
        if "403" in text or "refused" in text:
            text += " (this SearXNG does not give JSON: its settings.yml needs search.formats with json)"
        raise SearchError(f"SearXNG at {base}: {text}") from None
    return _results(data.get("results"), "title", "url", "content", limit)


def brave_search(key: str, query: str, limit: int) -> List[Dict[str, Any]]:
    data = _call(PROVIDERS["brave"]["base"] + "?" + urlencode({"q": query, "count": max(1, min(limit, 20))}), headers={"X-Subscription-Token": key, "Accept-Encoding": "identity"})
    return _results((data.get("web") or {}).get("results"), "title", "url", "description", limit)


def firecrawl_search(base: str, key: str, query: str, limit: int) -> List[Dict[str, Any]]:
    data = _call(base.rstrip("/") + "/v1/search", data={"query": query, "limit": max(1, min(limit, 20))}, headers={"Authorization": f"Bearer {key}"} if key else {})
    return _results(data.get("data"), "title", "url", "description", limit)


def firecrawl_read(base: str, key: str, url: str) -> str:
    data = _call(base.rstrip("/") + "/v1/scrape", data={"url": url, "formats": ["markdown"]}, headers={"Authorization": f"Bearer {key}"} if key else {}, timeout=60.0)
    text = ((data.get("data") or {}).get("markdown") or "") if isinstance(data, dict) else ""
    if not text:
        raise SearchError("Firecrawl returned no text for this page")
    return text[:MAX_READER_CHARS]


_playwright: Dict[str, Any] = {"client": None}


def playwright_read(url: str) -> str:
    """The page through a real browser (Edge, headless) driven by the Playwright MCP server, started on the first use and kept."""
    from tadamoo.mcp_client import MCPClient
    client = _playwright["client"]
    try:
        if client is None:
            client = MCPClient(["npx", "-y", PROVIDERS["playwright"]["local"]["package"], "--browser", "msedge", "--headless"])
            client.start()
            client.initialize()
            _playwright["client"] = client
        client.call_tool("browser_navigate", {"url": url}, timeout=60.0)
        text = client.call_tool("browser_snapshot", {}, timeout=60.0)
    except Exception as exc:
        close_readers()
        raise SearchError(f"the browser could not read the page: {exc}") from None
    return str(text or "")[:MAX_READER_CHARS]


def close_readers() -> None:
    client, _playwright["client"] = _playwright["client"], None
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


# -- a program on this PC, started when a search needs it ----------------------------------------------------------


def _run(argv: List[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def local_up(url: str, timeout: float = 3.0) -> bool:
    """Whether something answers at the address of a local program."""
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "TaDaMoo/1"}), timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True                                         # it answered, even if with an error
    except (urllib.error.URLError, OSError, ValueError):
        return False


def ensure_searxng(url: str, run: Callable[..., Any] = _run, up: Callable[[str], bool] = local_up, wait_s: float = 40.0) -> None:
    """Starts the container the harness made for SearXNG when nothing answers at its address; SearchError says what is missing."""
    if up(url):
        return
    name = PROVIDERS["searxng"]["local"]["container"]
    try:
        started = run(["docker", "start", name], 60.0)
    except (OSError, subprocess.SubprocessError):
        raise SearchError("SearXNG is not running and Docker cannot be started from here: start Docker Desktop, or install it from Settings > Web search") from None
    if started.returncode != 0:
        raise SearchError("SearXNG is not running and there is no container to start: install it from Settings > Web search, and keep Docker Desktop running")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if up(url):
            return
        time.sleep(1.0)
    raise SearchError("SearXNG was started but does not answer yet: try again in a moment")


# -- what the harness uses -----------------------------------------------------------------------------------------


def search_backend(provider: str, prefs: Prefs, keys: Keys) -> Optional[Callable[[str, int], List[Dict[str, Any]]]]:
    """The function that runs one search at `provider` as the person set it up, or None when it cannot work yet (no key, no address)."""
    mode = prefs.mode_of(provider)
    if provider == "searxng":
        url = prefs.url_of(provider)
        if not url:
            return None

        def run_searx(query: str, limit: int) -> List[Dict[str, Any]]:
            if mode == "local" and prefs.read()["autostart"]:
                ensure_searxng(url)
            return searxng_search(url, query, limit)
        return run_searx
    if provider == "brave":
        key = keys.get("brave")
        return (lambda q, n: brave_search(key, q, n)) if key else None
    if provider == "firecrawl":
        key = keys.get("firecrawl") if mode == "cloud" else ""
        base = prefs.url_of("firecrawl")
        return (lambda q, n: firecrawl_search(base, key, q, n)) if base and (key or mode == "local") else None
    return None


def read_after_failure(url: str, why: str, prefs: Optional[Prefs] = None, keys: Optional[Keys] = None) -> Optional[str]:
    """The page through the reader the person chose, after a plain fetch failed for `why`; None when no reader is set or it failed too."""
    prefs = prefs or Prefs()
    reader = prefs.read()["reader"]
    if reader == "none":
        return None
    try:
        if reader == "firecrawl":
            keys = keys or Keys()
            mode = prefs.mode_of("firecrawl")
            return f"(read through Firecrawl because the page could not be fetched plainly: {why})\n\n" + firecrawl_read(prefs.url_of("firecrawl"), keys.get("firecrawl") if mode == "cloud" else "", url)
        return f"(read through a browser because the page could not be fetched plainly: {why})\n\n" + playwright_read(url)
    except SearchError as exc:
        return f"(the page reader chosen in Settings > Web search failed too: {exc})"

def reader_may_help(message: str) -> bool:
    """Whether a page reader could succeed where the plain fetch failed: the site refused or limited the request, or took too long, not an address that does not exist."""
    return bool(re.search(r"answered (401|403|406|429|5\d\d)|took too long|could not connect|could not read ", message))
