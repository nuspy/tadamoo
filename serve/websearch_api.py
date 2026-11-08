"""Settings > Web search: what the person chose, the keys, the addresses, a test, and the installers of the programs that run on this PC.

Nothing here installs by itself: an install is a job the person starts after reading its plan, one step at a time, and the output of each step is kept
for the page to show. The installers work for the system the harness runs on (Windows, Linux or macOS, found by itself), need no Docker and no
administrator rights, and put everything in one folder of the user data. A key is write-only (the page is told whether one is set, never what it is).
The commands are fixed lists, never text from a page or a model."""
from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from tadamoo import websearch as ws
from tadamoo import websearch_runtime as rt

JOB_KEEP = 8
OUTPUT_LINES = 60

Step = Tuple[str, str, Callable[[Callable[[str], None]], None]]


class WebSearchError(ValueError):
    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.code = code


class WebSearchApi:
    def __init__(self, prefs: Optional[ws.Prefs] = None, keys: Optional[ws.Keys] = None, *, base_dir: Optional[Path] = None,
                 runtime: Optional[rt.Runtime] = None, packages: Optional[rt.Packages] = None, up: Callable[[str], bool] = ws.local_up,
                 browser: Callable[[str], str] = rt.browser_channel) -> None:
        self.prefs = prefs or ws.Prefs()
        self.keys = keys or ws.Keys()
        self.base = Path(base_dir) if base_dir else ws.user_dir() / "websearch"
        self.runtime = runtime or rt.Runtime(self.base)
        self.packages = packages or rt.Packages(self.base, self.runtime)
        self._up, self._browser = up, browser
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # -- what the page shows -----------------------------------------------------------------------------------

    def overview(self) -> Dict[str, Any]:
        saved, found = self.prefs.read(), self.runtime.find()
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
                npm = local["how"] == "npm"
                url = self.prefs.url_of(pid) if mode == "local" else str(local.get("default_url") or "")
                server = ws._servers.get(f"{pid}:{local.get('package')}") if npm else None
                entry["local"] = {"how": local["how"], "needs": local["needs"], "needs_present": {n: bool(found) for n in local["needs"]},
                                  "default_url": local.get("default_url", ""), "url": url, "up": (bool(url) and self._up(url)) if not npm else False,
                                  "installed": self.packages.installed(local["package"]) if npm else "", "running": bool(server and server.running),
                                  "automatic": npm, "repo": local.get("repo", "")}
            providers.append(entry)
        return {"system": self.runtime.info, "runtime": {"found": bool(found), "version": (found or {}).get("version", ""), "source": (found or {}).get("source", "")},
                "search": saved["search"], "reader": saved["reader"], "engine": saved["engine"], "engines": list(ws.ENGINES),
                "providers": providers}

    # -- the choice --------------------------------------------------------------------------------------------

    def select(self, body: Any) -> Dict[str, Any]:
        if not isinstance(body, dict):
            raise WebSearchError("send an object")
        changes = {k: body[k] for k in ("search", "reader", "mode", "urls", "engine") if k in body}
        if not changes:
            raise WebSearchError("say what to change: search, reader, mode, urls or engine")
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
            if pid == "playwright":
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

    def _steps(self, pid: str) -> Tuple[List[Step], str]:
        """The steps an install of a program on this PC runs, and a note; ([], note) for one the harness cannot install."""
        local = (ws.PROVIDERS.get(pid) or {}).get("local")
        if not local:
            raise WebSearchError(f"{pid} has no version that runs on this PC")
        if local["how"] != "npm":
            if pid == "searxng":
                return [], ("SearXNG is a Python program with no Windows support from its project, so the harness does not install it. On Linux or macOS follow its "
                            "installation guide (no Docker is needed), then enter the address it listens on and press Test. On Windows use a public instance or run it in WSL.")
            return [], ("The project's own guide for self-hosting uses Docker, so the harness does not install it: use the cloud service, or set it up by hand and "
                        "enter its address.")
        info = self.runtime.info
        if not info["supported"]:
            raise WebSearchError(info["note"] or "this system is not supported", "unsupported")
        steps: List[Step] = list(self.runtime.steps()) + list(self.packages.steps(local["package"]))
        note = f"Detected system: {info['os']} {info['arch']}."
        if pid == "playwright":
            channel = self._browser(info["os"])
            if channel == "chromium":
                folder = self.packages.folder(local["package"])

                def chromium(say: Callable[[str], None]) -> None:
                    found = self.runtime.find()
                    cli = folder / "node_modules" / "playwright" / "cli.js"
                    if not found or not cli.is_file():
                        raise rt.RuntimeProblem("the Playwright command line is not where it was expected")
                    say("$ playwright install chromium")
                    done = rt._run([found["node"], str(cli), "install", "chromium"], 1800.0, {**os.environ, **self.runtime.env(found)})
                    for line in ((done.stdout or "") + (done.stderr or "")).strip().splitlines()[-6:]:
                        say(line)
                    if done.returncode != 0:
                        raise rt.RuntimeProblem(f"Chromium could not be downloaded (exit {done.returncode})")
                steps.append(("Download Chromium for Playwright (large)", "playwright install chromium", chromium))
                note += " No Edge or Chrome was found, so Playwright needs its own Chromium."
            else:
                note += f" It will drive the {'Microsoft Edge' if channel == 'msedge' else 'Google Chrome'} already on this PC."
        if not steps:
            note += " Everything it needs is already installed."
        return steps, note

    def plan(self, provider: Any) -> Dict[str, Any]:
        """The steps an install would run, as text for the person to read before starting; `automatic` is false for a program that is set up by hand."""
        pid = str(provider)
        steps, note = self._steps(pid)
        return {"provider": pid, "automatic": bool(steps) or ws.PROVIDERS[pid]["local"]["how"] == "npm", "system": self.runtime.info,
                "steps": [{"command": detail, "label": label} for label, detail, _ in steps], "needs": ws.PROVIDERS[pid]["local"]["needs"], "note": note}

    def install(self, provider: Any) -> Dict[str, Any]:
        pid = str(provider)
        steps, note = self._steps(pid)
        if ws.PROVIDERS[pid]["local"]["how"] != "npm":
            raise WebSearchError(f"{pid} is set up by hand: follow its guide and enter its address", "manual")
        with self._lock:
            if any(j["provider"] == pid and j["state"] == "running" for j in self._jobs.values()):
                raise WebSearchError("an install of this is already running", "busy")
            job_id = uuid.uuid4().hex[:10]
            job = {"id": job_id, "provider": pid, "state": "running", "step": 0, "steps": max(1, len(steps)), "lines": [note], "started": time.time()}
            self._jobs[job_id] = job
            for old in sorted(self._jobs.values(), key=lambda j: j["started"])[:-JOB_KEEP]:
                self._jobs.pop(old["id"], None)
        threading.Thread(target=self._install, args=(job, pid, steps), name=f"websearch-install-{pid}", daemon=True).start()
        return self.job(job_id)

    def _say(self, job: Dict[str, Any], text: str) -> None:
        job["lines"] = (job["lines"] + [text[:400]])[-OUTPUT_LINES:]

    def _install(self, job: Dict[str, Any], pid: str, steps: List[Step]) -> None:
        try:
            for label, _detail, run in steps:
                job["step"] += 1
                self._say(job, label)
                run(lambda text: self._say(job, text))
            job["step"] = job["steps"]
            self.prefs.write(mode={pid: "local"})
            job["state"] = "done"
        except Exception as exc:                                   # the message goes to the page
            self._say(job, f"stopped: {exc}")
            job["state"] = "failed"

    def job(self, job_id: Any) -> Dict[str, Any]:
        job = self._jobs.get(str(job_id))
        if job is None:
            raise WebSearchError("no such install", "not_found")
        return {k: job[k] for k in ("id", "provider", "state", "step", "steps", "lines")}

    def stop(self, provider: Any) -> Dict[str, Any]:
        """Stops the local servers now; they also stop by themselves when idle and start again when a search needs them."""
        ws.close_readers()
        return self.overview()

    def uninstall(self, provider: Any) -> Dict[str, Any]:
        pid = str(provider)
        local = (ws.PROVIDERS.get(pid) or {}).get("local")
        if not local or local["how"] != "npm":
            raise WebSearchError(f"{pid} was not installed by the harness")
        ws.close_readers()
        self.packages.remove(local["package"])
        return self.overview()
