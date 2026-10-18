"""Downloading the catalog's models from Hugging Face (serve/catalog.py), from the Download panel of the web page.

A download is planned first (the files at the catalog's pinned revision, their sizes and SHA-256 as Hugging Face
reports them, the disk and the access), and starts only from that plan: the caller sends back the plan's token, with
`confirm` and the license acknowledgement. Files are written as `<name>.part` and renamed after their SHA-256 matches;
an interrupted download continues from the `.part` with an HTTP range request. Only the standard library is used.

The Hugging Face token is read from the environment or the file `huggingface-cli login` writes, or held in memory for
the session when it is pasted in the page. It is sent only to Hugging Face, never follows a redirect to another host,
and is never written, logged or returned."""
from __future__ import annotations

import errno
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

from serve import catalog

ENDPOINT = "https://huggingface.co"          # tests point it at a local server
TRUSTED_SUFFIXES = ("huggingface.co", "hf.co")
CHUNK = 1 << 20
TIMEOUT = 30                                 # a read of the file's data
API_TIMEOUT = 10                             # a call of the API (small answers)
API_TRIES = 3
MAX_STALLS = 30                              # consecutive failures without a byte of progress
DISK_MARGIN = 2 << 30
LOG_LINES = 80
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+()-]{0,200}$")
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.I)
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_REV_RE = re.compile(r"^[0-9a-f]{40}$")
_TOKEN_RE = re.compile(r"^hf_[A-Za-z0-9]{8,200}$")

_session_token: str | None = None
_whoami_cache: dict = {}


def _say(code: str, it: str, en: str) -> dict:
    return {"code": code, "text_it": it, "text": en}


class _Io(Exception):
    """A failure writing the file (not the network): retrying would not help."""

    def __init__(self, err: OSError):
        super().__init__(str(err))
        self.err = err


class DownloadError(Exception):
    """A failure with a code, and its text in both languages for the page."""

    def __init__(self, code: str, it: str, en: str):
        super().__init__(en)
        self.code, self.it, self.en = code, it, en

    def body(self) -> dict:
        return {"code": self.code, "it": self.it, "en": self.en}


# ---- the token -----------------------------------------------------------------------------------------------
def _token_files() -> list[Path]:
    out = []
    if os.environ.get("HF_TOKEN_PATH"):
        out.append(Path(os.environ["HF_TOKEN_PATH"]))
    if os.environ.get("HF_HOME"):
        out.append(Path(os.environ["HF_HOME"]) / "token")
    out += [Path.home() / ".cache" / "huggingface" / "token", Path.home() / ".huggingface" / "token"]
    return out


def hf_token() -> tuple[str | None, str | None]:
    """(token, where it came from: session | env | file) or (None, None)."""
    if _session_token:
        return _session_token, "session"
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        v = os.environ.get(name, "").strip()
        if _TOKEN_RE.match(v):
            return v, "env"
    for p in _token_files():
        try:
            v = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if _TOKEN_RE.match(v):
            return v, "file"
    return None, None


def set_session_token(value: str | None) -> dict:
    """Keep a pasted token in memory (None or "" forgets it) after Hugging Face accepts it; the answer names the
    account, never the token."""
    global _session_token
    value = (value or "").strip()
    if not value:
        _session_token = None
        _whoami_cache.clear()
        return hf_status()
    if not _TOKEN_RE.match(value):
        raise DownloadError("bad_token", "Il token non ha la forma di un token Hugging Face (inizia con hf_).",
                            "That is not the shape of a Hugging Face token (it starts with hf_).")
    who = whoami(value)
    if who is None:
        raise DownloadError("bad_token", "Hugging Face non accetta questo token.", "Hugging Face does not accept "
                            "this token.")
    _session_token = value
    _whoami_cache.clear()
    return hf_status()


def whoami(token: str) -> str | None:
    """The account name Hugging Face gives this token, or None when it refuses it; DownloadError when offline."""
    req = urllib.request.Request(ENDPOINT + "/api/whoami-v2", headers=_headers(token))
    try:
        with _api_open(req) as r:
            return str(json.loads(r.read().decode("utf-8", "replace")).get("name") or "?")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None
        raise _network(e) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise _network(e) from None


def hf_status() -> dict:
    """Where the token comes from and whose it is; nothing secret."""
    token, source = hf_token()
    out = {"source": source, "user": None, "error": None}
    if token:
        hit = _whoami_cache.get(source)
        if hit and time.time() - hit[0] < 60:
            out["user"], out["error"] = hit[1], hit[2]
        else:
            user, err = None, None
            try:
                user = whoami(token)
                if user is None:
                    err = "refused"
            except DownloadError as e:
                err = e.code
            _whoami_cache[source] = (time.time(), user, err)
            out["user"], out["error"] = user, err
    return out


# ---- talking to Hugging Face ----------------------------------------------------------------------------------
def _host_ok(url: str) -> bool:
    u, base = urllib.parse.urlsplit(url), urllib.parse.urlsplit(ENDPOINT)
    host = (u.hostname or "").lower()
    if host == (base.hostname or "").lower() and u.scheme == base.scheme and (u.port or 0) == (base.port or 0):
        return True
    return u.scheme == base.scheme and any(host == s or host.endswith("." + s) for s in TRUSTED_SUFFIXES)


class _Redirect(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to a trusted host, and never takes the token along to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _host_ok(newurl):
            raise DownloadError("redirect", "Hugging Face ha rimandato a un indirizzo non fidato: scarico interrotto.",
                                "Hugging Face redirected to an address that is not trusted: download stopped.")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            for store in (new.headers, new.unredirected_hdrs):
                for k in [k for k in store if k.lower() == "authorization"]:
                    del store[k]
        return new


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _opener(follow: bool):
    return urllib.request.build_opener(_Redirect() if follow else _NoRedirect())


def _headers(token: str | None, extra: dict | None = None) -> dict:
    h = {"User-Agent": "basalt-win-gui"}
    if token:
        h["Authorization"] = "Bearer " + token
    h.update(extra or {})
    return h


def _api_open(req: urllib.request.Request, follow: bool = True):
    """Open an API request, trying again a few times when the connection itself fails (an HTTP answer is final)."""
    for attempt in range(API_TRIES):
        try:
            return _opener(follow).open(req, timeout=API_TIMEOUT)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError):
            if attempt + 1 == API_TRIES:
                raise
            time.sleep(0.4 * (attempt + 1))


def _network(e: Exception) -> DownloadError:
    return DownloadError("network", "Nessuna risposta da Hugging Face: controlla la connessione.",
                         "No answer from Hugging Face: check the connection.")


def _resolve_url(repo: str, revision: str, path: str) -> str:
    return f"{ENDPOINT}/{repo}/resolve/{revision}/" + "/".join(urllib.parse.quote(p) for p in path.split("/"))


def repo_tree(repo: str, revision: str) -> dict[str, dict]:
    """{path: {"size", "sha256" (LFS files) or None}} of the repository at that revision."""
    if not _REPO_RE.match(repo) or not (_REV_RE.match(revision) or revision == "main"):
        raise DownloadError("catalog", "Voce di catalogo non valida.", "Invalid catalog entry.")
    token, _ = hf_token()
    url = f"{ENDPOINT}/api/models/{repo}/tree/{revision}?recursive=true"
    out: dict[str, dict] = {}
    for _ in range(40):
        try:
            with _api_open(urllib.request.Request(url, headers=_headers(token))) as r:
                items = json.loads(r.read().decode("utf-8", "replace"))
                link = r.headers.get("Link", "")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise DownloadError("gone", "Il repository o la revisione non esiste più su Hugging Face.",
                                    "The repository or revision no longer exists on Hugging Face.") from None
            raise _network(e) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise _network(e) from None
        for it in items if isinstance(items, list) else []:
            if it.get("type") == "file":
                lfs = it.get("lfs") or {}
                out[it["path"]] = {"size": int(lfs.get("size") or it.get("size") or 0), "sha256": lfs.get("oid")}
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if not m:
            return out
        url = m.group(1)
        if not _host_ok(url):
            return out
    return out


def access_check(repo: str, revision: str, path: str) -> str:
    """ok | token (none or refused) | terms (account fine, terms of a gated repository not accepted) | missing | offline."""
    token, _ = hf_token()
    req = urllib.request.Request(_resolve_url(repo, revision, path), method="HEAD", headers=_headers(token))
    try:
        with _api_open(req, follow=False):
            return "ok"
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400 or e.code == 200:
            return "ok"
        return {401: "token", 403: "terms", 404: "missing"}.get(e.code, "offline")
    except (urllib.error.URLError, OSError):
        return "offline"


# ---- the plan --------------------------------------------------------------------------------------------------
def safe_name(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if not _NAME_RE.match(name) or _RESERVED.match(name) or name.endswith("."):
        raise DownloadError("name", "Nome di file non valido nel catalogo.", "Invalid file name in the catalog.")
    return name


def _inside(path: str, folders: list[str]) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    for f in folders:
        q = os.path.normcase(os.path.abspath(f))
        try:
            if os.path.commonpath([p, q]) == q:
                return True
        except ValueError:
            continue
    return False


def plan(entry_id: str, dest: str | None = None, folders: list[str] | None = None) -> dict:
    """What a download of this entry would do and need, and the token a confirmation must carry back."""
    from serve import models
    entry = catalog.find(entry_id)
    if entry is None:
        raise models.Refused(404, {"error": {"type": "not_found", "message": "no such model in the catalog"}})
    if entry["convert"] == "no" or not entry["files"]:
        raise models.Refused(400, {"error": {"type": "invalid_request_error", "code": "not_downloadable",
                                             "message": (entry.get("reason") or {}).get("en", "not offered")}})
    problems: list[dict] = []
    known = folders if folders is not None else models.dirs()
    root = (dest or "").strip().strip('"') or models.default_output_dir() or ""
    if not root or not os.path.isabs(root):
        problems.append(_say("dest", "Scegli la cartella in cui scaricare (percorso completo).",
                             "Choose the folder to download to (a full path)."))
    elif not os.path.isdir(root):
        problems.append(_say("dest", "La cartella di destinazione non esiste.", "The destination folder does not exist."))
    elif not os.access(root, os.W_OK):
        problems.append(_say("dest", "Nella cartella di destinazione non si può scrivere.",
                             "The destination folder is not writable."))
    folder = os.path.join(root, entry["folder"]) if root else ""
    files, tree = [], {}
    try:
        tree = repo_tree(entry["repo"], entry["revision"])
    except DownloadError as e:
        problems.append(_say(e.code, e.it, e.en))
    for path in entry["files"]:
        info = tree.get(path)
        if tree and info is None:
            problems.append(_say("missing_file", f"Il file {path} non c'è più nel repository.",
                                 f"The file {path} is no longer in the repository."))
        size = (info or {}).get("size") or 0
        name = safe_name(path)
        final = os.path.join(folder, name) if folder else ""
        have = bool(final) and os.path.isfile(final) and size > 0 and os.path.getsize(final) == size
        part = 0
        if folder and not have and os.path.isfile(final + ".part"):
            part = min(os.path.getsize(final + ".part"), size)
        files.append({"path": path, "name": name, "size": size, "sha256": (info or {}).get("sha256"), "have": have,
                      "part": part})
    total = sum(f["size"] for f in files)
    remaining = sum(f["size"] - (f["size"] if f["have"] else f["part"]) for f in files)
    free = None
    if root and os.path.isdir(root):
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            free = None
    need = remaining + DISK_MARGIN
    if free is not None and free < need:
        problems.append(_say("disk", f"Spazio insufficiente su {root}: servono circa {need / 2**30:.0f} GB (margine "
                             f"compreso), liberi {free / 2**30:.0f} GB.",
                             f"Not enough space on {root}: about {need / 2**30:.0f} GB needed (margin included), "
                             f"{free / 2**30:.0f} GB free."))
    token, source = hf_token()
    access = "ok"
    todo = next((f for f in files if not f["have"] and f["size"]), None)
    if todo is not None and tree:
        access = access_check(entry["repo"], entry["revision"], todo["path"])
        if access == "token":
            problems.append(_say("token", "Questo repository richiede un token Hugging Face valido." if token
                                 else "Questo repository richiede un account e un token Hugging Face.",
                                 "This repository needs a valid Hugging Face token." if token
                                 else "This repository needs a Hugging Face account and token."))
        elif access == "terms":
            problems.append(_say("terms", "Il tuo account non ha ancora accettato i termini di questo repository: "
                                 "aprine la pagina su Hugging Face e accettali.",
                                 "Your account has not accepted this repository's terms yet: open its page on "
                                 "Hugging Face and accept them."))
        elif access == "missing":
            problems.append(_say("missing_file", "Il primo file non è raggiungibile su Hugging Face.",
                                 "The first file cannot be reached on Hugging Face."))
        elif access == "offline":
            problems.append(_say("network", "Nessuna risposta da Hugging Face: controlla la connessione.",
                                 "No answer from Hugging Face: check the connection."))
    lic = dict(catalog.LICENSES[entry["license"]])
    out = {"id": entry["id"], "name": entry["name"], "repo": entry["repo"], "revision": entry["revision"],
           "kind": entry["kind"], "convert": entry["convert"], "gated": entry["gated"],
           "uncensored": entry["uncensored"], "dest": root, "folder": folder, "files": files, "total_bytes": total,
           "remaining_bytes": remaining, "free_bytes": free, "need_bytes": need, "problems": problems,
           "access": access, "token_source": source, "license": lic, "in_models_dirs": bool(root) and _inside(root, known),
           "ok": not problems and bool(files) and all(f["size"] for f in files)}
    out["token"] = hashlib.sha256(json.dumps([entry["id"], folder, [(f["path"], f["size"], f["sha256"]) for f in files]],
                                             sort_keys=True).encode()).hexdigest()
    return out


# ---- the job ----------------------------------------------------------------------------------------------------
class Downloader:
    """One download at a time: its progress, speed, and the tail of its log."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cancel_flag = threading.Event()
        self.thread: threading.Thread | None = None
        self.state: dict = {"phase": "idle"}
        self.log: list[str] = []
        self.window: deque = deque()

    def status(self) -> dict:
        with self.lock:
            s = dict(self.state)
            s["log"] = self.log[-LOG_LINES:]
        return s

    def running(self) -> bool:
        with self.lock:
            return self.state.get("phase") in ("running", "verifying")

    def _set(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)

    def _line(self, text: str) -> None:
        with self.lock:
            self.log.append(text)
            del self.log[:-LOG_LINES * 2]

    def start(self, entry_id: str, dest: str | None, token, confirm, ack, folders=None) -> dict:
        from serve import models
        p = plan(entry_id, dest, folders)
        if confirm is not True or ack is not True or not token:
            raise models.Refused(409, {"error": {"type": "confirmation_required", "code": "confirm",
                                                 "message": "a download starts only from its plan: send it back with "
                                                            "confirm: true, ack: true and its token"}, "plan": p})
        if token != p["token"]:
            raise models.Refused(409, {"error": {"type": "plan_changed", "code": "plan_changed",
                                                 "message": "the plan changed since it was shown: look at it again"},
                                       "plan": p})
        if not p["ok"]:
            raise models.Refused(422, {"error": {"type": "not_possible", "code": "problems",
                                                 "message": " ".join(x["text"] for x in p["problems"])}, "plan": p})
        with self.lock:
            if self.state.get("phase") in ("running", "verifying"):
                raise models.Refused(409, {"error": {"type": "busy", "code": "busy",
                                                     "message": "a download is already running"}})
            self.cancel_flag.clear()
            self.log = []
            self.window.clear()
            todo = [f for f in p["files"] if not f["have"]]
            self.state = {"phase": "running", "id": p["id"], "name": p["name"], "folder": p["folder"],
                          "files_total": len(p["files"]), "files_done": len(p["files"]) - len(todo),
                          "file": None, "bytes_total": p["total_bytes"],
                          "bytes_done": p["total_bytes"] - p["remaining_bytes"], "speed_bps": 0.0, "eta_s": None,
                          "started": time.time(), "finished": None, "error": None, "convert": p["convert"],
                          "kind": p["kind"]}
        self.thread = threading.Thread(target=self._run, args=(p,), name="model-download", daemon=True)
        self.thread.start()
        return self.status()

    def cancel(self) -> bool:
        if not self.running():
            return False
        self.cancel_flag.set()
        return True

    # ---- the thread
    def _run(self, p: dict) -> None:
        try:
            os.makedirs(p["folder"], exist_ok=True)
            for f in p["files"]:
                if f["have"]:
                    continue
                if self.cancel_flag.is_set():
                    break
                self._set(file=f["name"], phase="running")
                self._file(p, f)
                with self.lock:
                    self.state["files_done"] = self.state.get("files_done", 0) + 1
            if self.cancel_flag.is_set():
                self._set(phase="cancelled", finished=time.time(), speed_bps=0.0, eta_s=None)
                self._line("cancelled; the partial files stay and the download continues from them")
            else:
                self._set(phase="done", finished=time.time(), speed_bps=0.0, eta_s=0, file=None)
                self._line("done: " + p["folder"])
        except DownloadError as e:
            self._set(phase="failed", finished=time.time(), error=e.body(), speed_bps=0.0, eta_s=None)
            self._line("failed: " + e.en)
        except (_Io, OSError) as e:
            e = e.err if isinstance(e, _Io) else e
            full = e.errno == errno.ENOSPC
            err = DownloadError("disk" if full else "io", "Disco pieno." if full else f"Errore di scrittura: {e}",
                                "The disk is full." if full else f"Write error: {e}")
            self._set(phase="failed", finished=time.time(), error=err.body(), speed_bps=0.0, eta_s=None)
            self._line("failed: " + err.en)

    def _progress(self, delta: int) -> None:
        now = time.time()
        with self.lock:
            self.state["bytes_done"] += delta
            self.window.append((now, self.state["bytes_done"]))
            while len(self.window) > 2 and now - self.window[0][0] > 8:
                self.window.popleft()
            t0, b0 = self.window[0]
            speed = (self.state["bytes_done"] - b0) / (now - t0) if now - t0 > 0.5 else self.state["speed_bps"]
            self.state["speed_bps"] = speed
            left = self.state["bytes_total"] - self.state["bytes_done"]
            self.state["eta_s"] = int(left / speed) if speed > 1 else None

    def _file(self, p: dict, f: dict) -> None:
        final = os.path.join(p["folder"], f["name"])
        part = final + ".part"
        size = f["size"]
        url = _resolve_url(p["repo"], p["revision"], f["path"])
        stalls = 0
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        if have > size:
            os.remove(part)
            self._progress(-have)
            have = 0
        while have < size and not self.cancel_flag.is_set():
            token, _ = hf_token()
            extra = {"Range": f"bytes={have}-"} if have else {}
            req = urllib.request.Request(url, headers=_headers(token, extra))
            before = have
            try:
                with _opener(True).open(req, timeout=TIMEOUT) as r:
                    code = r.status
                    if code == 200 and have:
                        self._progress(-have)                      # the server ignored the range: start again
                        have = 0
                    elif code == 206:
                        m = re.match(r"bytes (\d+)-", r.headers.get("Content-Range", ""))
                        if not m or int(m.group(1)) != have:
                            raise DownloadError("range", "Risposta di ripresa non valida da Hugging Face.",
                                                "Hugging Face sent an invalid resume answer.")
                    self._line(f"{f['name']}: from {have} of {size}")
                    try:
                        out = open(part, "ab" if have else "wb")
                    except OSError as e:
                        raise _Io(e) from None
                    with out:
                        while not self.cancel_flag.is_set():
                            chunk = r.read1(CHUNK)         # what has arrived, so progress and cancel stay quick
                            if not chunk:
                                break
                            try:
                                out.write(chunk)
                            except OSError as e:
                                raise _Io(e) from None
                            have += len(chunk)
                            self._progress(len(chunk))
                            if have > size:
                                raise DownloadError("size", "Il file è più grande del previsto.",
                                                    "The file is larger than expected.")
            except urllib.error.HTTPError as e:
                if e.code == 416:                              # the range is past the file: start again
                    if os.path.isfile(part):
                        os.remove(part)
                    self._progress(-have)
                    have = before = 0
                    stalls = self._stall(stalls, 0, 1, "HTTP 416")
                    continue
                if e.code in (401, 403):
                    raise DownloadError("terms" if e.code == 403 else "token",
                                        "Hugging Face rifiuta l'accesso: controlla token e accettazione dei termini.",
                                        "Hugging Face refuses access: check the token and the terms.") from None
                if e.code == 404:
                    raise DownloadError("missing_file", "Il file non è più su Hugging Face.",
                                        "The file is no longer on Hugging Face.") from None
                stalls = self._stall(stalls, have, before, f"HTTP {e.code}")
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException,
                    OSError) as e:
                stalls = self._stall(stalls, have, before, type(e).__name__)
            have = os.path.getsize(part) if os.path.isfile(part) else 0
        if self.cancel_flag.is_set():
            return
        if os.path.getsize(part) != size:
            raise DownloadError("size", f"{f['name']}: dimensione diversa dall'attesa.",
                                f"{f['name']}: the size is not what was expected.")
        if f["sha256"]:
            self._set(phase="verifying")
            self._line(f"{f['name']}: checking SHA-256")
            if self._sha256(part) != f["sha256"]:
                os.remove(part)
                raise DownloadError("hash", f"{f['name']}: lo SHA-256 non corrisponde, il file è stato scartato.",
                                    f"{f['name']}: the SHA-256 does not match, the file was discarded.")
            self._set(phase="running")
        os.replace(part, final)

    def _stall(self, stalls: int, have: int, before: int, why: str) -> int:
        stalls = 0 if have > before else stalls + 1
        if stalls >= MAX_STALLS:
            raise DownloadError("network", "Il download si è interrotto e non riparte: controlla la connessione.",
                                "The download stopped and will not restart: check the connection.")
        self._line(f"interrupted ({why}); retrying")
        self.cancel_flag.wait(min(30.0, 2.0 ** min(stalls, 5)))
        return stalls

    def _sha256(self, path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while not self.cancel_flag.is_set():
                b = fh.read(CHUNK * 4)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()


DOWNLOADS = Downloader()
