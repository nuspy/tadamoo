"""Settings > Web search: what the person chose, the keys, the addresses, a test, and the installers of the programs that run on this PC.

Nothing here installs by itself: an install is a job the person starts after reading its plan, one step at a time, and the output of each step is kept
for the page to show. A key is write-only (the page is told whether one is set, never what it is). The jobs and the programs they start run as the
person; the commands are fixed lists, never text from a page or a model."""
from __future__ import annotations

import secrets
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from tadamoo import websearch as ws

JOB_KEEP = 8
OUTPUT_LINES = 60
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class WebSearchError(ValueError):
    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.code = code


def _port_of(url: str, default: int) -> int:
    try:
        return urlparse(url).port or default
    except ValueError:
        return default


class WebSearchApi:
    def __init__(self, prefs: Optional[ws.Prefs] = None, keys: Optional[ws.Keys] = None, *, base_dir: Optional[Path] = None,
                 run: Callable[..., Any] = ws._run, tools: Callable[[], Dict[str, bool]] = ws.tools_present,
                 up: Callable[[str], bool] = ws.local_up) -> None:
        self.prefs = prefs or ws.Prefs()
        self.keys = keys or ws.Keys()
        self.base = Path(base_dir) if base_dir else ws.user_dir() / "websearch"
        self._run, self._tools, self._up = run, tools, up
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # -- what the page shows -----------------------------------------------------------------------------------

    def _container_state(self, name: str) -> Tuple[bool, bool]:
        """(the container exists, it is running); (False, False) when Docker cannot be asked."""
        if not self._tools().get("docker"):
            return False, False
        try:
            out = self._run(["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}} {{.State}}"], 6.0)
        except (OSError, subprocess.SubprocessError):
            return False, False
        line = (getattr(out, "stdout", "") or "").strip().splitlines()
        return (bool(line), bool(line) and line[0].split(" ")[-1] == "running") if getattr(out, "returncode", 1) == 0 else (False, False)

    def overview(self) -> Dict[str, Any]:
        saved, present = self.prefs.read(), self._tools()
        providers = []
        for pid, p in ws.PROVIDERS.items():
            mode = self.prefs.mode_of(pid)
            local, cloud = p.get("local"), p.get("cloud")
            entry: Dict[str, Any] = {"id": pid, "kind": p["kind"], "builtin": bool(p.get("builtin")), "mode": mode,
                                     "modes": [m for m in ws.MODES if p.get(m)], "url": self.prefs.url_of(pid),
                                     "search": saved["search"] == pid, "reader": saved["reader"] == pid, "cloud": None, "local": None}
            if cloud:
                entry["cloud"] = {**cloud, "key_set": self.keys.has(pid) if cloud.get("needs_key") else False}
            if local:
                url = self.prefs.url_of(pid) if mode == "local" else str(local.get("default_url") or "")
                installed, running = (self._container_state(local["container"]) if local.get("container") else (False, False))
                entry["local"] = {"how": local["how"], "needs": local["needs"], "needs_present": {n: present.get(n, False) for n in local["needs"]},
                                  "default_url": local.get("default_url", ""), "url": url, "up": bool(url) and self._up(url) if local["how"] != "npx" else False,
                                  "installed": installed, "running": running, "automatic": pid in ("searxng", "playwright"),
                                  "repo": local.get("repo", "")}
            providers.append(entry)
        return {"search": saved["search"], "reader": saved["reader"], "autostart": saved["autostart"], "tools": present, "providers": providers}

    # -- the choice --------------------------------------------------------------------------------------------

    def select(self, body: Any) -> Dict[str, Any]:
        if not isinstance(body, dict):
            raise WebSearchError("send an object")
        changes = {k: body[k] for k in ("search", "reader", "mode", "urls", "autostart") if k in body}
        if not changes:
            raise WebSearchError("say what to change: search, reader, mode, urls or autostart")
        try:
            self.prefs.write(**changes)
        except ValueError as exc:
            raise WebSearchError(str(exc)) from None
        if "reader" in changes:
            ws.close_readers()
        return self.overview()

    def set_key(self, provider: Any, key: Any) -> Dict[str, Any]:
        try:
            self.keys.set(str(provider), str(key or ""))
        except ValueError as exc:
            raise WebSearchError(str(exc)) from None
        return self.overview()

    # -- a test of one provider ------------------------------------------------------------------------------------

    def test(self, provider: Any) -> Dict[str, Any]:
        """One real search (or page read) at the provider as it is set up now: how many results and how long it took, or why not."""
        pid = str(provider)
        if pid not in ws.PROVIDERS:
            raise WebSearchError(f"unknown provider {pid}")
        started = time.monotonic()
        try:
            if ws.PROVIDERS[pid]["kind"] == "reader":
                text = ws.playwright_read("https://example.com/")
                found, what = len(text), "characters"
            elif pid == "duckduckgo":
                from tadamoo.tools.search import duckduckgo_search
                found, what = len(duckduckgo_search("test", 3)), "results"
            elif pid in ("tavily", "exa"):
                key = self.keys.get(pid)
                if not key:
                    raise ws.SearchError("no key is set")
                from tadamoo.tools.search import get_provider, _LazyMcpSearch
                found, what = len(_LazyMcpSearch(get_provider(pid), key)("test", 3)), "results"
            else:
                backend = ws.search_backend(pid, self.prefs, self.keys)
                if backend is None:
                    raise ws.SearchError("it is not set up: a key or an address is missing")
                found, what = len(backend("test", 3)), "results"
        except ws.SearchError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:                                  # the provider's own error text, for the person to read
            return {"ok": False, "message": str(exc)[:300]}
        return {"ok": found > 0, "count": found, "what": what, "ms": int((time.monotonic() - started) * 1000),
                "message": "" if found else "it answered with nothing"}

    # -- installers ----------------------------------------------------------------------------------------------

    def _searxng_dir(self) -> Path:
        return self.base / "searxng"

    def _searxng_settings(self) -> str:
        return ("use_default_settings: true\nserver:\n  secret_key: \"" + secrets.token_hex(32) + "\"\n  limiter: false\n  image_proxy: false\n"
                "search:\n  formats:\n    - html\n    - json\n")

    def plan(self, provider: Any) -> Dict[str, Any]:
        """The steps an install would run, as text for the person to read before starting; `automatic` is false for a program that is set up by hand."""
        pid = str(provider)
        local = (ws.PROVIDERS.get(pid) or {}).get("local")
        if not local:
            raise WebSearchError(f"{pid} has no version that runs on this PC")
        if pid == "searxng":
            url = self.prefs.url_of(pid) or local["default_url"]
            host, port = (urlparse(url).hostname or "127.0.0.1"), _port_of(url, 8888)
            if host not in LOOPBACK:
                raise WebSearchError("a SearXNG that the harness installs listens only on this PC: use 127.0.0.1 or localhost as its address")
            steps = [("docker version", "Check that Docker answers"),
                     (f"write {self._searxng_dir() / 'settings.yml'}", "Write SearXNG's settings with the JSON format on"),
                     (f"docker pull {local['image']}", "Download the SearXNG image"),
                     (f"docker rm -f {local['container']}", "Remove an earlier container of the same name, if there is one"),
                     (f"docker run -d --name {local['container']} --restart unless-stopped -p 127.0.0.1:{port}:8080 -v {self._searxng_dir()}:/etc/searxng {local['image']}",
                      "Start the container, reachable only from this PC"),
                     (f"GET {url}/search?q=test&format=json", "Check that it answers with results")]
            return {"provider": pid, "automatic": True, "steps": [{"command": c, "label": l} for c, l in steps], "needs": local["needs"]}
        if pid == "playwright":
            steps = [("node --version", "Check that Node.js is installed"), (f"npx -y {local['package']} --help", "Download the Playwright MCP server (npm) and check that it starts")]
            return {"provider": pid, "automatic": True, "steps": [{"command": c, "label": l} for c, l in steps], "needs": local["needs"],
                    "note": "It drives the Microsoft Edge already on this PC, in a window you do not see; no other browser is downloaded."}
        steps = [(f"git clone --depth 1 {local['repo']}", "Download the Firecrawl source"), ("docker compose up -d --build", "Build and start its containers (long, large)")]
        return {"provider": pid, "automatic": False, "steps": [{"command": c, "label": l} for c, l in steps], "needs": local["needs"],
                "note": "Follow the self-hosting guide of the project, then enter the address it listens on here and press Test."}

    def install(self, provider: Any) -> Dict[str, Any]:
        pid = str(provider)
        plan = self.plan(pid)
        if not plan["automatic"]:
            raise WebSearchError(f"{pid} is set up by hand: follow its guide and enter its address", "manual")
        missing = [n for n in plan["needs"] if not self._tools().get(n)]
        if missing:
            raise WebSearchError("this needs " + ", ".join(missing) + " on this PC", "missing_tool")
        with self._lock:
            if any(j["provider"] == pid and j["state"] == "running" for j in self._jobs.values()):
                raise WebSearchError("an install of this is already running", "busy")
            job_id = uuid.uuid4().hex[:10]
            job = {"id": job_id, "provider": pid, "state": "running", "step": 0, "steps": len(plan["steps"]), "lines": [], "started": time.time()}
            self._jobs[job_id] = job
            for old in sorted(self._jobs.values(), key=lambda j: j["started"])[:-JOB_KEEP]:
                self._jobs.pop(old["id"], None)
        threading.Thread(target=self._install, args=(job, pid), name=f"websearch-install-{pid}", daemon=True).start()
        return self.job(job_id)

    def _say(self, job: Dict[str, Any], text: str) -> None:
        job["lines"] = (job["lines"] + [text[:400]])[-OUTPUT_LINES:]

    def _step(self, job: Dict[str, Any], argv: List[str], timeout: float, tolerate: bool = False) -> None:
        job["step"] += 1
        self._say(job, "$ " + " ".join(argv))
        done = self._run(argv, timeout)
        for line in ((getattr(done, "stdout", "") or "") + (getattr(done, "stderr", "") or "")).strip().splitlines()[-6:]:
            self._say(job, line)
        if done.returncode != 0 and not tolerate:
            raise ws.SearchError(f"{argv[0]} failed (exit {done.returncode})")

    def _install(self, job: Dict[str, Any], pid: str) -> None:
        try:
            if pid == "searxng":
                local = ws.PROVIDERS["searxng"]["local"]
                url = self.prefs.url_of(pid) or local["default_url"]
                port = _port_of(url, 8888)
                self._step(job, ["docker", "version"], 30.0)
                job["step"] += 1
                folder = self._searxng_dir()
                folder.mkdir(parents=True, exist_ok=True)
                if not (folder / "settings.yml").exists():
                    (folder / "settings.yml").write_text(self._searxng_settings(), encoding="utf-8")
                self._say(job, f"wrote {folder / 'settings.yml'}")
                self._step(job, ["docker", "pull", local["image"]], 1800.0)
                self._step(job, ["docker", "rm", "-f", local["container"]], 60.0, tolerate=True)
                self._step(job, ["docker", "run", "-d", "--name", local["container"], "--restart", "unless-stopped", "-p", f"127.0.0.1:{port}:8080",
                                 "-v", f"{folder}:/etc/searxng", local["image"]], 120.0)
                job["step"] += 1
                deadline = time.monotonic() + 60.0
                while not self._up(url) and time.monotonic() < deadline:
                    time.sleep(1.0)
                found = ws.searxng_search(url, "test", 3)
                self._say(job, f"it answers: {len(found)} results")
                self.prefs.write(mode={"searxng": "local"}, urls={"searxng": url})
            elif pid == "playwright":
                self._step(job, ["node", "--version"], 30.0)
                self._step(job, ["npx", "-y", ws.PROVIDERS["playwright"]["local"]["package"], "--help"], 600.0)
            job["state"] = "done"
        except Exception as exc:                                   # the message goes to the page
            self._say(job, f"stopped: {exc}")
            job["state"] = "failed"

    def job(self, job_id: Any) -> Dict[str, Any]:
        job = self._jobs.get(str(job_id))
        if job is None:
            raise WebSearchError("no such install", "not_found")
        return {k: job[k] for k in ("id", "provider", "state", "step", "steps", "lines")}

    def uninstall(self, provider: Any) -> Dict[str, Any]:
        pid = str(provider)
        if pid == "searxng":
            try:
                self._run(["docker", "rm", "-f", ws.PROVIDERS["searxng"]["local"]["container"]], 60.0)
            except (OSError, subprocess.SubprocessError) as exc:
                raise WebSearchError(f"Docker could not be asked: {exc}") from None
        elif pid == "playwright":
            ws.close_readers()
        else:
            raise WebSearchError(f"{pid} was not installed by the harness")
        return self.overview()
