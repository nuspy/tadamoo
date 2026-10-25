"""Local stand-ins for the public services the online search asks: the MCP registry, npm, PyPI and GitHub.

One server answers under a prefix for each: /registry, /npm, /npmdl, /pypi, /gh and /raw. `World` holds what they know; a test
changes it and reads `requests` (path and headers of every request) to see what was asked and what was sent.
"""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

SHA = "a" * 40


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class World:
    def __init__(self):
        self.requests = []
        self.registry = []                 # [{"server": {...}, "_meta": {...}}]
        self.npm_search = []               # objects as npm's search returns them
        self.npm_releases = {}             # (name, version) -> release document
        self.npm_packuments = {}           # name -> full record
        self.npm_weekly = {}
        self.pypi = {}                     # name -> latest document; (name, version) -> release document
        self.repos = {}                    # "owner/repo" -> repository document
        self.repo_search = {}              # query substring -> [repository documents]
        self.trees = {}                    # "owner/repo" -> tree list
        self.files = {}                    # (owner/repo, path) -> bytes
        self.github_limited = False
        self.redirect_registry_to = ""
        self.big_registry = False

    def add_repo(self, full, stars=100, forks=10, issues=2, pushed="2026-10-01T00:00:00Z", created="2024-01-01T00:00:00Z", archived=False, license_id="MIT", topics=()):
        self.repos[full] = {"full_name": full, "stargazers_count": stars, "forks_count": forks, "open_issues_count": issues, "pushed_at": pushed,
                            "created_at": created, "archived": archived, "disabled": False, "license": {"spdx_id": license_id} if license_id else None,
                            "default_branch": "main", "description": "d", "topics": list(topics)}
        return self.repos[full]

    def add_npm(self, name, version="1.0.0", weekly=5000, bins=None, integrity="sha512-GOOD", scripts=None, repo=None, license_id="MIT", date="2026-10-10T00:00:00Z",
                description="", history=None):
        doc = {"name": name, "version": version, "bin": bins if bins is not None else {name.rsplit("/", 1)[-1]: "build/index.js"}, "license": license_id,
               "dist": {"integrity": integrity, "tarball": "x"}, "scripts": scripts or {}}
        if repo:
            doc["repository"] = {"url": f"git+https://github.com/{repo}.git"}
        self.npm_releases[(name, version)] = doc
        self.npm_weekly[name] = weekly
        dates = history or ["2025-01-01T00:00:00Z", "2026-05-01T00:00:00Z", date]
        self.npm_packuments[name] = {"time": {"created": dates[0], "modified": dates[-1], **{f"0.{i}.0": d for i, d in enumerate(dates)}}}
        self.npm_search.append({"downloads": {"weekly": weekly, "monthly": weekly * 4}, "package": {
            "name": name, "version": version, "description": description or f"{name} mcp server", "date": date, "license": license_id,
            "links": {"repository": f"git+https://github.com/{repo}.git"} if repo else {}, "publisher": {"username": "someone"}}})

    def add_registry(self, name, description, packages=None, remotes=None, repo_url=None, status="active", version="1.0.0"):
        server = {"name": name, "description": description, "version": version}
        if repo_url:
            server["repository"] = {"url": repo_url, "source": "github"}
        if packages:
            server["packages"] = packages
        if remotes:
            server["remotes"] = remotes
        self.registry.append({"server": server, "_meta": {"io.modelcontextprotocol.registry/official": {
            "status": status, "publishedAt": "2025-06-01T00:00:00Z", "updatedAt": "2026-10-05T00:00:00Z", "isLatest": True}}})


def make_handler(world):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlsplit(self.path)
            path, qs = unquote(u.path), parse_qs(u.query)
            world.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            if path.startswith("/registry/v0.1/servers"):
                if world.redirect_registry_to:
                    return self._send(302, b"", extra={"Location": world.redirect_registry_to})
                if world.big_registry:
                    return self._send(200, b"x" * 3_000_000)
                word = (qs.get("search") or [""])[0].lower()
                hits = [i for i in world.registry if word in i["server"]["name"].lower()]
                return self._send(200, {"servers": hits, "metadata": {"count": len(hits)}})
            if path.startswith("/npm/-/v1/search"):
                return self._send(200, {"objects": world.npm_search, "total": len(world.npm_search)})
            if path.startswith("/npmdl/downloads/point/last-week/"):
                name = path[len("/npmdl/downloads/point/last-week/"):]
                return self._send(200, {"downloads": world.npm_weekly.get(name, 0), "package": name})
            if path.startswith("/npm/"):
                rest = path[len("/npm/"):]
                for (name, version), doc in world.npm_releases.items():
                    if rest == f"{name}/{version}":
                        return self._send(200, doc)
                    if rest == name:
                        return self._send(200, world.npm_packuments[name])
                return self._send(404, {"error": "not found"})
            if path.startswith("/pypiroot/pypi/"):
                parts = path[len("/pypiroot/pypi/"):].split("/")
                key = parts[0] if len(parts) == 2 else (parts[0], parts[1])
                if key in world.pypi:
                    return self._send(200, world.pypi[key])
                return self._send(404, {})
            if path.startswith("/gh/"):
                if world.github_limited:
                    return self._send(403, {"message": "API rate limit exceeded"}, extra={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1893456000"})
                rest = path[len("/gh/"):]
                if rest == "search/repositories":
                    query = (qs.get("q") or [""])[0]
                    hits = [r for k, v in world.repo_search.items() if k in query for r in v]
                    return self._send(200, {"items": hits})
                parts = rest.split("/")
                if parts[0] == "repos" and len(parts) == 3:
                    repo = world.repos.get(f"{parts[1]}/{parts[2]}")
                    return self._send(200, repo) if repo else self._send(404, {})
                if parts[0] == "repos" and len(parts) == 5 and parts[3] == "commits":
                    return self._send(200, SHA.encode(), "text/plain")
                if parts[0] == "repos" and len(parts) >= 6 and parts[3] == "git" and parts[4] == "trees":
                    tree = world.trees.get(f"{parts[1]}/{parts[2]}")
                    return self._send(200, {"sha": parts[5], "tree": tree, "truncated": False}) if tree is not None else self._send(404, {})
            if path.startswith("/raw/"):
                owner, repo, ref, *file = path[len("/raw/"):].split("/")
                data = world.files.get((f"{owner}/{repo}", "/".join(file)))
                return self._send(200, data, "text/plain") if data is not None else self._send(404, b"nope", "text/plain")
            self._send(404, {})
    return Handler


class FakeServices:
    def __init__(self):
        self.world = World()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.world))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def bases(self):
        return {"registry": self.url + "/registry", "npm": self.url + "/npm", "npm_downloads": self.url + "/npmdl", "pypi": self.url + "/pypiroot",
                "github": self.url + "/gh", "github_raw": self.url + "/raw"}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
