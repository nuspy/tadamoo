"""llama-server (llama.cpp) as a second engine the Loader can run beside Basalt's own, mostly to hold a small model for the technical session.

  LlamaCpp.find()          the llama-server programs on this PC: the saved path, TADAMOO_LLAMA_SERVER, Basalt's own folders, PATH, a few usual folders
  LlamaCpp.set_path(p)     remember a program the person points to ("" forgets it)
  LlamaCpp.status()        found / version / state (stopped, starting, ready, error, external) / health / saved settings / update plan
  LlamaCpp.start(cfg)      run it on 127.0.0.1 with a GGUF; the settings are remembered; LlamaCpp.stop() ends what this server started
  LlamaCpp.update_check()  what an update would download (release, files, sizes, destination); nothing is fetched but the release list
  LlamaCpp.update_apply()  download and unpack exactly the plan the person confirmed, through a downloader that tests replace

It listens on loopback only and takes no free-form flags: the model, the port, the context, the layers on the GPU and the number of
slots. Everything that touches the machine goes through `Host`, which tests replace."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import platform as _platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from serve import ROOT

EXE = "llama-server" + (".exe" if os.name == "nt" else "")
ENV_PATH = "TADAMOO_LLAMA_SERVER"
RELEASES_URL = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
RELEASE_LIST_URL = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=20"
DOWNLOAD_HOSTS = ("github.com", "githubusercontent.com")        # a release file may come only from these hosts
SETTINGS_KEY = "llamacpp"
DEFAULTS = {"port": 8080, "ctx": 8192, "ngl": 99, "parallel": 1, "alias": "", "autostart": False}
LIMITS = {"port": (1024, 65535), "ctx": (256, 1 << 20), "ngl": (0, 999), "parallel": (1, 16)}
HEALTH_S = 2.0
HEALTH_CODES = (200, 503)                # what llama-server's /health answers: ready, or still loading
VERSION_S = 15.0
STOP_S = 10.0
USUAL = ("C:/llama.cpp", "C:/llama.cpp/build/bin/Release", "C:/llama.cpp/build/bin", "~/llama.cpp", "~/llama.cpp/build/bin",
         "/usr/local/bin", "/opt/llama.cpp/bin")


class LlamaError(Exception):
    """What was asked cannot be done: an HTTP status, a code the page translates, and a sentence for the person."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    @property
    def body(self) -> Dict[str, Any]:
        return {"error": {"type": "invalid_request_error", "code": self.code, "message": self.message}}


class Host:
    """What the module asks of the machine; tests replace it."""
    platform = sys.platform
    machine = (_platform.machine() or "").lower()

    def environ(self) -> Dict[str, str]:
        return dict(os.environ)

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name)

    def is_file(self, path: str) -> bool:
        return os.path.isfile(path)

    def data_dir(self) -> Path:
        from serve import userdata
        return userdata.user_data_dir()

    def load_settings(self) -> Dict[str, Any]:
        from serve import loader
        got = loader.machine_settings().get(SETTINGS_KEY)
        return dict(got) if isinstance(got, dict) else {}

    def save_settings(self, value: Dict[str, Any]) -> None:
        from serve import loader
        loader.save_machine_settings({SETTINGS_KEY: value})

    def run(self, argv: List[str], timeout: float) -> Tuple[int, str]:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if self.platform == "win32" else 0
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                                  creationflags=flags, text=True, encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)
        return done.returncode, done.stdout or ""

    def popen(self, argv: List[str], log: Path, cwd: str) -> Any:
        """Start the program with its output in `log`, tied to this server's lifetime."""
        from serve.util.winjob import contain
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "wb") as out:
            proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        contain(proc)
        return proc

    def kill(self, proc: Any) -> None:
        if proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            proc.terminate()
        try:
            proc.wait(timeout=STOP_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def port_free(self, port: int) -> bool:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                return False
        return True

    def http_get(self, port: int, path: str, timeout: float = HEALTH_S) -> Tuple[int, bytes]:
        """(status, body) of a GET on 127.0.0.1:port; (0, b"") when nothing answers."""
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
            c.request("GET", path)
            r = c.getresponse()
            body = r.read()
            c.close()
            return r.status, body
        except (OSError, http.client.HTTPException):
            return 0, b""

    def fetch_json(self, url: str) -> Dict[str, Any]:
        """The release list: the one request this module makes to the internet by itself, and only when the person asks to check."""
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "basalt-llamacpp-check"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def download(self, url: str, dest: Path, progress: Callable[[int], None]) -> None:
        """Stream `url` into `dest`; `progress(bytes so far)`. Only reached after the person confirmed the plan."""
        req = urllib.request.Request(url, headers={"User-Agent": "basalt-llamacpp-update"})
        dest.parent.mkdir(parents=True, exist_ok=True)
        got = 0
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as out:
            while True:
                block = r.read(1 << 20)
                if not block:
                    break
                out.write(block)
                got += len(block)
                progress(got)


def _ok_url(url: Any) -> bool:
    u = urlparse(url) if isinstance(url, str) else None
    host = (u.hostname or "") if u else ""
    return bool(u) and u.scheme == "https" and any(host == d or host.endswith("." + d) for d in DOWNLOAD_HOSTS)


class LlamaCpp:
    def __init__(self, host: Optional[Host] = None) -> None:
        self.host = host or Host()
        self.lock = threading.RLock()
        self.proc: Any = None
        self.running_cfg: Dict[str, Any] = {}
        self.started = 0.0
        self.error: Optional[Dict[str, str]] = None
        self.plan: Optional[Dict[str, Any]] = None
        self.job: Optional[Dict[str, Any]] = None
        self._versions: Dict[str, str] = {}

    # -- the program -------------------------------------------------------------------------------------

    def _names(self) -> List[str]:
        return [EXE]

    def candidates(self) -> List[Tuple[str, str]]:
        """[(source, path)] of every llama-server found, the first being the one used."""
        h, out, seen = self.host, [], set()

        def add(source: str, path: Optional[str]) -> None:
            if path and path not in seen and h.is_file(path):
                seen.add(path)
                out.append((source, path))
        add("saved", str(self.host.load_settings().get("path") or ""))
        add("env", h.environ().get(ENV_PATH))
        add("update", self._current_update())
        add("bin", str(ROOT / "bin" / EXE))
        add("bin", str(ROOT / "bin" / "llama.cpp" / EXE))
        add("path", h.which("llama-server"))
        for folder in USUAL:
            add("usual", str(Path(os.path.expanduser(folder)) / EXE))
        return out

    def _current_update(self) -> str:
        try:
            info = json.loads((self.host.data_dir() / "llama.cpp" / "current.json").read_text(encoding="utf-8"))
            return str(info.get("path") or "")
        except (OSError, ValueError, AttributeError):
            return ""

    def version_of(self, path: str) -> str:
        """The line `llama-server --version` prints; asked once per program (the page polls the status)."""
        if path in self._versions:
            return self._versions[path]
        code, text = self.host.run([path, "--version"], VERSION_S)
        line = next((l.strip()[:120] for l in text.splitlines() if "version" in l.lower()), "")
        if line:
            self._versions[path] = line
        return line

    def find(self) -> Dict[str, Any]:
        found = self.candidates()
        first = found[0] if found else None
        return {"path": first[1] if first else None, "source": first[0] if first else None,
                "version": self.version_of(first[1]) if first else "", "candidates": [{"source": s, "path": p} for s, p in found]}

    def set_path(self, path: Any) -> Dict[str, Any]:
        if path in (None, ""):
            value = {k: v for k, v in self.host.load_settings().items() if k != "path"}
        else:
            if not isinstance(path, str) or Path(path).name.lower() not in ("llama-server", "llama-server.exe") or not self.host.is_file(path):
                raise LlamaError(400, "bad_path", "That is not a llama-server program on this PC (the file must be named llama-server).")
            value = {**self.host.load_settings(), "path": path}
        self.host.save_settings(value)
        self._versions.clear()
        return self.status()

    # -- running it ----------------------------------------------------------------------------------------

    def _log(self) -> Path:
        return self.host.data_dir() / "llamacpp-run" / "llama-server.log"

    def _tail(self, n: int = 6) -> str:
        try:
            lines = self._log().read_text(encoding="utf-8", errors="replace").strip().splitlines()
        except OSError:
            return ""
        return " | ".join(l.strip() for l in lines[-n:] if l.strip())[-600:]

    def settings(self) -> Dict[str, Any]:
        saved = self.host.load_settings()
        return {**DEFAULTS, **{k: saved[k] for k in ("model", *DEFAULTS) if k in saved}}

    @staticmethod
    def check_config(cfg: Dict[str, Any], is_file: Callable[[str], bool]) -> Dict[str, Any]:
        """The settings of a start, each checked; LlamaError names the first one that is not right."""
        model = cfg.get("model")
        if not isinstance(model, str) or Path(model).suffix.lower() != ".gguf" or not is_file(model):
            raise LlamaError(400, "model_missing", "model: the path of a .gguf file on this PC.")
        out: Dict[str, Any] = {"model": model}
        for key, (lo, hi) in LIMITS.items():
            value = cfg.get(key, DEFAULTS[key])
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise LlamaError(400, "bad_" + key, f"{key}: a whole number from {lo} to {hi}.")
            out[key] = value
        alias = cfg.get("alias", DEFAULTS["alias"])
        if not isinstance(alias, str) or len(alias) > 80 or any(c in alias for c in "\r\n\0"):
            raise LlamaError(400, "bad_alias", "alias: a short name for the model, or empty.")
        out["alias"] = alias.strip() or Path(model).stem
        out["autostart"] = cfg.get("autostart", DEFAULTS["autostart"]) is True
        return out

    def argv(self, exe: str, cfg: Dict[str, Any]) -> List[str]:
        return [exe, "-m", cfg["model"], "--host", "127.0.0.1", "--port", str(cfg["port"]), "-c", str(cfg["ctx"] * cfg["parallel"]),
                "-ngl", str(cfg["ngl"]), "-np", str(cfg["parallel"]), "--alias", cfg["alias"], "--jinja"]

    def start(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if self._alive():
                raise LlamaError(409, "running", "llama-server is running already: stop it first.")
            exe = self.find()["path"]
            if not exe:
                raise LlamaError(409, "not_found", "No llama-server program was found: point to one or use the update.")
            clean = self.check_config(cfg, self.host.is_file)
            if not self.host.port_free(clean["port"]):
                if self._answers(clean["port"]):
                    raise LlamaError(409, "port_busy", f"A llama-server is answering on port {clean['port']} already (not started here).")
                raise LlamaError(409, "port_busy", f"Port {clean['port']} is in use by another program.")
            self.host.save_settings({**self.host.load_settings(), **clean})
            self.error = None
            try:
                self.proc = self.host.popen(self.argv(exe, clean), self._log(), str(Path(exe).parent))
            except OSError as exc:
                self.proc = None
                raise LlamaError(500, "spawn_failed", f"llama-server did not start: {exc}") from None
            self.running_cfg, self.started = clean, time.time()
        return self.status()

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> Dict[str, Any]:
        with self.lock:
            if not self._alive():
                if self.proc is None and self._answers(self.settings()["port"]):
                    raise LlamaError(409, "not_ours", "That llama-server was not started here: close it where it was started.")
            else:
                self.host.kill(self.proc)
            self.proc, self.error, self.running_cfg = None, None, {}
        return self.status()

    def _answers(self, port: int) -> bool:
        """Whether a llama-server answers on the port (its /health says ready or loading; any other answer is some other program)."""
        return self.host.http_get(port, "/health")[0] in HEALTH_CODES

    def health(self, port: int) -> Dict[str, Any]:
        code, body = self.host.http_get(port, "/health")
        try:
            detail = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            detail = {}
        status = str(detail.get("status") or "") if isinstance(detail, dict) else ""
        return {"code": code, "status": status, "ready": code == 200 and status in ("", "ok")}

    def state(self) -> Tuple[str, Dict[str, Any]]:
        """(state, health): stopped | starting | ready | error | external (answering, but not started here)."""
        with self.lock:
            cfg = self.running_cfg or self.settings()
            if self.proc is not None and self.proc.poll() is not None:
                code = self.proc.poll()
                self.error = {"code": "exited", "message": f"llama-server stopped (exit {code}): {self._tail() or 'no output'}"}
                self.proc, self.running_cfg = None, {}
            if self._alive():
                health = self.health(cfg["port"])
                return ("ready" if health["ready"] else "starting"), health
            if self.error:
                return "error", {"code": 0, "status": "", "ready": False}
        health = self.health(cfg["port"])
        return ("external" if health["code"] in HEALTH_CODES else "stopped"), health

    def status(self) -> Dict[str, Any]:
        found = self.find()
        state, health = self.state()
        cfg = self.running_cfg or self.settings()
        return {"found": bool(found["path"]), "path": found["path"], "source": found["source"], "version": found["version"],
                "candidates": found["candidates"], "state": state, "health": health, "error": self.error, "settings": self.settings(),
                "base_url": f"http://127.0.0.1:{cfg['port']}/v1", "model_name": cfg.get("alias") or "", "context_per_slot": cfg.get("ctx"),
                "pid": getattr(self.proc, "pid", None) if self._alive() else None, "plan": self.plan, "job": self.job, "log_tail": self._tail() if state == "error" else ""}

    def start_if_saved(self) -> bool:
        """At server start: run the program again when the person asked for that. False when it did not start."""
        saved = self.host.load_settings()
        if saved.get("autostart") is not True or not saved.get("model"):
            return False
        try:
            self.start({k: saved[k] for k in ("model", *DEFAULTS) if k in saved})
            return True
        except LlamaError:
            return False

    # -- the update ------------------------------------------------------------------------------------------

    def _assets_for(self, assets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The release files this PC needs: the build for its system and, on Windows, the CUDA runtime that goes with it."""
        names = {a.get("name", ""): a for a in assets if isinstance(a, dict)}
        win = self.host.platform == "win32"
        arm = "arm" in (self.host.machine or "")
        picks: List[str] = []
        if win:
            main = sorted(n for n in names if "bin-win-cuda-12" in n and "x64" in n and not n.startswith("cudart"))
            if main:
                tag = main[-1].split("bin-win-cuda-")[1]
                runtime = [n for n in names if n.startswith("cudart-") and tag in n]
                picks = [main[-1], *runtime[:1]]
            else:
                picks = sorted(n for n in names if "bin-win-vulkan-x64" in n)[-1:]
        elif self.host.platform == "darwin":
            picks = sorted(n for n in names if "bin-macos-" + ("arm64" if arm else "x64") in n)[-1:]
        else:
            picks = sorted(n for n in names if "bin-ubuntu-vulkan-x64" in n or ("bin-ubuntu-x64" in n))[-1:]
        out = []
        for n in picks:
            a = names[n]
            digest = str(a.get("digest") or "")
            out.append({"name": n, "size": int(a.get("size") or 0), "url": a.get("browser_download_url") or "",
                        "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else ""})
        return out

    def update_check(self) -> Dict[str, Any]:
        """What an update would download. Fetches the release list only; the files are fetched by update_apply after a confirmation."""
        try:
            release = self.host.fetch_json(RELEASES_URL)
        except Exception as exc:                                  # any failure of the network is an outcome to show
            raise LlamaError(502, "release_unreachable", f"The release list could not be read: {exc}") from None
        if not isinstance(release, dict) or not release.get("tag_name"):
            raise LlamaError(502, "release_unreadable", "The release list is not in the form expected.")
        tag = str(release["tag_name"])
        files = self._assets_for(release.get("assets") or [])
        if not files:                                             # the latest tag may carry no binaries: the newest release that has a build for this system is used
            try:
                listed = self.host.fetch_json(RELEASE_LIST_URL)
            except Exception:
                listed = []
            for candidate in listed if isinstance(listed, list) else []:
                if isinstance(candidate, dict) and candidate.get("tag_name") and not candidate.get("draft") and not candidate.get("prerelease"):
                    found = self._assets_for(candidate.get("assets") or [])
                    if found:
                        release, tag, files = candidate, str(candidate["tag_name"]), found
                        break
        if not files:
            raise LlamaError(404, "no_asset", f"Release {tag} has no build for this system.")
        for f in files:
            if not _ok_url(f["url"]):
                raise LlamaError(502, "bad_url", f"{f['name']}: the download address is not on GitHub.")
        dest = self.host.data_dir() / "llama.cpp" / tag
        installed = self.find()["version"]
        plan = {"release": tag, "name": str(release.get("name") or tag), "published": str(release.get("published_at") or ""),
                "files": files, "total_bytes": sum(f["size"] for f in files), "destination": str(dest), "installed": installed,
                "up_to_date": _build_number(installed) is not None and _build_number(installed) == _build_number(tag),
                "token": hashlib.sha256(json.dumps([tag, [(f["name"], f["size"], f["url"]) for f in files], str(dest)]).encode()).hexdigest()[:24],
                "needs_confirmation": True}
        self.plan = plan
        return plan

    def update_apply(self, token: Any, confirmed: Any, downloader: Optional[Callable[[str, Path, Callable[[int], None]], None]] = None,
                     wait: bool = False) -> Dict[str, Any]:
        """Download and unpack the plan with this `token`, once the person confirmed it. `downloader(url, dest, progress)` defaults to the host's."""
        with self.lock:
            plan = self.plan
            if confirmed is not True:
                raise LlamaError(400, "not_confirmed", "Nothing is downloaded until you confirm what the update shows.")
            if plan is None or token != plan["token"]:
                raise LlamaError(409, "plan_changed", "Check for the update again: the plan you confirmed is not the current one.")
            if self.job and self.job["status"] == "running":
                raise LlamaError(409, "update_running", "An update is being downloaded already.")
            if self._alive():
                raise LlamaError(409, "in_use", "llama-server is running: stop it before the update replaces its files.")
            self.job = {"status": "running", "release": plan["release"], "done": 0, "total": plan["total_bytes"], "file": "", "error": None}
        run = downloader or self.host.download
        thread = threading.Thread(target=self._update_job, args=(plan, run), daemon=True, name="llamacpp-update")
        thread.start()
        if wait:
            thread.join()
        return dict(self.job)

    def _update_job(self, plan: Dict[str, Any], run: Callable[[str, Path, Callable[[int], None]], None]) -> None:
        dest = Path(plan["destination"])
        part_dir = dest.with_name(dest.name + ".download")
        done = 0
        try:
            shutil.rmtree(part_dir, ignore_errors=True)
            part_dir.mkdir(parents=True, exist_ok=True)
            archives = []
            for f in plan["files"]:
                self.job.update(file=f["name"])
                target = part_dir / f["name"]
                before = done
                run(f["url"], target, lambda n, b=before: self.job.update(done=b + n))
                size = target.stat().st_size
                if f["size"] and size != f["size"]:
                    raise LlamaError(502, "size_mismatch", f"{f['name']}: {size} bytes arrived, {f['size']} were announced.")
                if f["sha256"] and _sha256(target) != f["sha256"]:
                    raise LlamaError(502, "hash_mismatch", f"{f['name']}: the file does not match the checksum of the release.")
                done += size
                archives.append(target)
            stage = dest.with_name(dest.name + ".unpack")
            shutil.rmtree(stage, ignore_errors=True)
            for a in archives:
                _unpack(a, stage)
            exe = next((p for p in sorted(stage.rglob(EXE)) if p.is_file()), None)
            if exe is None:
                raise LlamaError(502, "no_program", f"{EXE} is not in the files that arrived.")
            shutil.rmtree(dest, ignore_errors=True)
            os.replace(stage, dest)
            new = dest / exe.relative_to(stage)
            (self.host.data_dir() / "llama.cpp").mkdir(parents=True, exist_ok=True)
            (self.host.data_dir() / "llama.cpp" / "current.json").write_text(json.dumps({"release": plan["release"], "path": str(new)}), encoding="utf-8")
            saved = self.host.load_settings()
            if saved.get("path") and str(saved["path"]).startswith(str(self.host.data_dir() / "llama.cpp")):
                self.host.save_settings({**saved, "path": str(new)})         # a program an earlier update put there moves to the new one; the person's own stays
            self._versions.clear()
            self.job.update(status="done", file="")
        except LlamaError as exc:
            self.job.update(status="error", error={"code": exc.code, "message": exc.message})
        except Exception as exc:                                   # the thread always ends the job
            self.job.update(status="error", error={"code": "update_failed", "message": str(exc)})
        finally:
            shutil.rmtree(part_dir, ignore_errors=True)
            shutil.rmtree(dest.with_name(dest.name + ".unpack"), ignore_errors=True)


def _build_number(text: str) -> Optional[int]:
    """The build number in `version: 6000 (abc)` or in the release tag `b6000`."""
    m = re.search(r"(?:version:\s*|\bb)(\d+)", text or "")
    return int(m.group(1)) if m else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _unpack(archive: Path, into: Path) -> None:
    root = into.resolve()
    try:
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                target = (into / info.filename).resolve()
                if root != target and root not in target.parents:
                    raise LlamaError(502, "bad_archive", f"{info.filename}: outside the folder it is unpacked into.")
            z.extractall(into)
    except zipfile.BadZipFile:
        raise LlamaError(502, "bad_archive", f"{archive.name} is not a readable .zip.") from None


_MANAGER: Optional[LlamaCpp] = None


def get() -> LlamaCpp:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = LlamaCpp()
    return _MANAGER
