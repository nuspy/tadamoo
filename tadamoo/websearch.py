"""Where the agent searches the web and reads pages, as the person chose it in Settings > Web search.

The person picks one search provider and, when a plain fetch fails, one page reader. Each provider is a cloud service (an account and a key, kept in
the credential store, never in the file that holds the choice) or a program on this PC (a local address, started by the harness when a search needs it).
The choice is a small JSON file in the user data folder, read at every search, so a change applies to the next one. Everything a provider or a reader
returns is text from outside: the callers mark it untrusted."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import urlencode, urlparse

FILE = "websearch.json"
KEY_PREFIX = "websearch/"
DEFAULT_SEARCH = "duckduckgo"
READERS = ("none", "firecrawl", "playwright", "openwebsearch")
ENGINES = ("duckduckgo", "bing", "brave", "startpage")      # the engines the open-websearch server can be told to ask
MODES = ("local", "cloud")
API_SECONDS = 25.0
MAX_READER_CHARS = 250_000

# What each provider is. `cloud`: the service, with where to make the account and the key; `local`: the program on this PC and what it needs.
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "duckduckgo": {"kind": "search", "builtin": True, "cloud": {"needs_key": False, "signup_url": "https://duckduckgo.com/", "keys_url": ""}, "local": None},
    "searxng": {
        "kind": "search",
        "cloud": {"needs_key": False, "needs_url": True, "signup_url": "https://searx.space/", "keys_url": ""},
        "local": {"how": "external", "needs": [], "default_url": "http://127.0.0.1:8888"},
    },
    "openwebsearch": {
        "kind": "both",
        "cloud": None,
        "local": {"how": "npm", "needs": ["node"], "package": "open-websearch"},
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
        "local": {"how": "external", "needs": [], "default_url": "http://127.0.0.1:3002", "repo": "https://github.com/firecrawl/firecrawl"},
    },
    "playwright": {
        "kind": "reader",
        "cloud": None,
        "local": {"how": "npm", "needs": ["node"], "package": "@playwright/mcp"},
    },
}


def user_dir() -> Path:
    from serve.userdata import user_data_dir
    return Path(user_data_dir())


def provider_ids(kind: str = "") -> List[str]:
    return [i for i, p in PROVIDERS.items() if not kind or p["kind"] in (kind, "both")]


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
        return {"search": search, "reader": reader, "mode": modes, "urls": urls,
                "engine": raw.get("engine") if raw.get("engine") in ENGINES else ENGINES[0]}

    def write(self, **changes: Any) -> Dict[str, Any]:
        """Applies the changes (`search`, `reader`, `mode` and `urls` as {provider: value}, `engine`) and returns what is kept; ValueError for a bad one."""
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
        if "engine" in changes:
            if changes["engine"] not in ENGINES:
                raise ValueError("engine: one of " + ", ".join(ENGINES))
            now["engine"] = changes["engine"]
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


# -- the MCP servers that run on this PC, started when a search or a page read needs them ------------------------------


def _stack() -> Any:
    from tadamoo import websearch_runtime as rt
    base = user_dir() / "websearch"
    runtime = rt.Runtime(base)
    return rt, runtime, rt.Packages(base, runtime)


_servers: Dict[str, Any] = {}


def local_server(provider: str, prefs: Optional[Prefs] = None) -> Any:
    """The managed process of an npm provider (open-websearch, playwright); made on first use, started by its first call, stopped when idle."""
    rt, runtime, packages = _stack()
    package = PROVIDERS[provider]["local"]["package"]
    prefs = prefs or Prefs()

    def launch() -> Any:
        if provider == "openwebsearch":
            argv, env = packages.command(package)
            return argv, {**env, "MODE": "stdio", "DEFAULT_SEARCH_ENGINE": prefs.read()["engine"]}
        channel = rt.browser_channel(runtime.info["os"])
        return packages.command(package, "--browser", channel, "--headless")
    key = f"{provider}:{package}"
    if key not in _servers:
        _servers[key] = rt.ManagedServer(provider, launch)
    return _servers[key]


def _results_of_text(text: str, limit: int) -> List[Dict[str, Any]]:
    """The results in what the open-websearch search tool returned: its JSON when it is JSON, else the text itself as one result."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    rows = data.get("results") if isinstance(data, dict) else data if isinstance(data, list) else None
    found = _results(rows, "title", "url", "description", limit) if rows is not None else []
    return found or [{"title": "open-websearch", "url": "", "snippet": text[:8000]}]


def openwebsearch_search(query: str, limit: int, prefs: Optional[Prefs] = None) -> List[Dict[str, Any]]:
    from tadamoo.websearch_runtime import RuntimeProblem
    prefs = prefs or Prefs()
    try:
        text = local_server("openwebsearch", prefs).call("search", {"query": query, "limit": max(1, min(limit, 20)), "engines": [prefs.read()["engine"]]})
    except RuntimeProblem as exc:
        raise SearchError(str(exc)) from None
    except Exception as exc:
        raise SearchError(f"open-websearch failed: {exc}") from None
    return _results_of_text(text, limit)


def openwebsearch_read(url: str) -> str:
    from tadamoo.websearch_runtime import RuntimeProblem
    try:
        text = local_server("openwebsearch").call("fetchWebContent", {"url": url, "maxChars": 200000, "renderMode": "auto"}, timeout=90.0)
    except RuntimeProblem as exc:
        raise SearchError(str(exc)) from None
    except Exception as exc:
        raise SearchError(f"open-websearch failed: {exc}") from None
    return str(text or "")[:MAX_READER_CHARS]


def playwright_read(url: str) -> str:
    """The page through a real browser driven by the Playwright MCP server (an Edge or Chrome already on the PC, else its own Chromium), no visible window."""
    from tadamoo.websearch_runtime import RuntimeProblem
    server = local_server("playwright")
    try:
        server.call("browser_navigate", {"url": url}, timeout=60.0)
        text = server.call("browser_snapshot", {}, timeout=60.0)
    except RuntimeProblem as exc:
        raise SearchError(str(exc)) from None
    except Exception as exc:
        raise SearchError(f"the browser could not read the page: {exc}") from None
    return str(text or "")[:MAX_READER_CHARS]


def close_readers() -> None:
    """Stops every local server now (they also stop by themselves when idle and when the harness ends)."""
    from tadamoo.websearch_runtime import stop_all
    stop_all()


def local_up(url: str, timeout: float = 3.0) -> bool:
    """Whether something answers at an address on this PC."""
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "TaDaMoo/1"}), timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True                                         # it answered, even if with an error
    except (urllib.error.URLError, OSError, ValueError):
        return False


# -- what the harness uses -----------------------------------------------------------------------------------------


def search_backend(provider: str, prefs: Prefs, keys: Keys) -> Optional[Callable[[str, int], List[Dict[str, Any]]]]:
    """The function that runs one search at `provider` as the person set it up, or None when it cannot work yet (no key, no address)."""
    mode = prefs.mode_of(provider)
    if provider == "searxng":
        url = prefs.url_of(provider)
        return (lambda q, n: searxng_search(url, q, n)) if url else None
    if provider == "openwebsearch":
        return lambda q, n: openwebsearch_search(q, n, prefs)
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
