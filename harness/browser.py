"""A browser the agent drives to fill in forms, buy and book: Edge or Chrome on this PC, over the DevTools protocol.

The page's content is untrusted data: the tools return it fenced, and what the person keeps in the vault reaches a page only as a
placeholder the agent writes in `browser_type`, filled in here, at the last step, and only if the page's site is one the vault entry names.
A value is never echoed back: what the model reads of a page is masked of every value in the vault. Each conversation has its own browser. Its
profile is thrown away with it, unless the person chose to keep sign-ins: then one profile is kept and one browser at a time uses it.

Reaches: the page, the frames of its own site, the frames of other sites it holds (a card field in a payment provider's frame is a session of its
own, and a value typed there goes only to a site the vault entry names), and the windows the page opens (a payment sheet).
"""
from __future__ import annotations

import base64
import re
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

PAGE_TEXT_CHARS = 5000
MAX_ELEMENTS = 120
NAVIGATION_SECONDS = 20.0
CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/microsoft-edge", "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


class BrowserError(RuntimeError):
    """The browser cannot do what was asked; the message says why, for the model to read."""


def find_browser(explicit: str = "") -> Optional[str]:
    for path in (explicit, os.environ.get("BASALT_BROWSER", "")):
        if path and Path(path).is_file():
            return path
    for path in CANDIDATES:
        if Path(path).is_file():
            return path
    for name in ("msedge", "chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


# -- a small WebSocket client (RFC 6455), enough to speak to the browser on this PC -----------------------

class WebSocket:
    def __init__(self, host: str, port: int, path: str, timeout: float = 30.0) -> None:
        self.sock = socket.create_connection((host, port), timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BrowserError("the browser closed the connection while it was being made")
            head += chunk
        header, _, self._buf = head.partition(b"\r\n\r\n")
        if b" 101 " not in header.split(b"\r\n")[0]:
            raise BrowserError("the browser refused the connection: " + header.split(b"\r\n")[0].decode("latin-1", "replace"))
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        if accept.encode("ascii") not in header:
            raise BrowserError("the browser did not answer the handshake correctly")

    def _read(self, n: int, deadline: Optional[float]) -> bytes:
        while len(self._buf) < n:
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("no answer from the browser")
                self.sock.settimeout(left)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise BrowserError("the browser closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_text(self, text: str) -> None:
        data = text.encode("utf-8")
        mask = os.urandom(4)
        head = bytearray([0x81])
        if len(data) < 126:
            head.append(0x80 | len(data))
        elif len(data) < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", len(data))
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", len(data))
        self.sock.settimeout(30.0)
        self.sock.sendall(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv_text(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        message = b""
        while True:
            b1, b2 = self._read(2, deadline)
            fin, opcode, length = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2, deadline))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8, deadline))[0]
            payload = self._read(length, deadline)             # a server does not mask
            if opcode == 0x8:
                raise BrowserError("the browser closed the connection")
            if opcode == 0x9:                                  # ping: answer with the same bytes
                self.sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + b"\0\0\0\0" + payload if len(payload) < 126 else b"")
                continue
            if opcode == 0xA:
                continue
            message += payload
            if fin:
                return message.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Cdp:
    """Calls and events of the DevTools protocol on one page, and on the frames of other sites it holds (each is a session of its own)."""

    def __init__(self, ws: WebSocket, target_id: str = "") -> None:
        self.ws, self._id, self.events, self._lock = ws, 0, [], threading.RLock()
        self.target_id = target_id
        self.closed = False                                    # the page went away (a window that closed itself)
        self.frames: Dict[str, Dict[str, Any]] = {}          # session id -> the frame of another site: {"target", "url"}
        self.request_guard: Optional[Callable[[str], bool]] = None   # says whether a request may go out; none means all may

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 30.0, session: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            self._id += 1
            mine = self._id
            message: Dict[str, Any] = {"id": mine, "method": method, "params": params or {}}
            if session:
                message["sessionId"] = session
            try:
                self.ws.send_text(json.dumps(message))
            except OSError:
                self.closed = True
                raise BrowserError("the page was closed") from None
            deadline = time.monotonic() + timeout
            while True:
                try:
                    reply = json.loads(self.ws.recv_text(max(0.05, deadline - time.monotonic())))
                except TimeoutError:
                    raise BrowserError(f"the browser did not answer {method}") from None
                except (BrowserError, OSError):
                    self.closed = True
                    raise BrowserError("the page was closed") from None
                if reply.get("id") == mine:
                    if "error" in reply:
                        raise BrowserError(f"{method}: {reply['error'].get('message', 'failed')}")
                    return reply.get("result", {})
                self._event(reply)

    def _event(self, message: Dict[str, Any]) -> None:
        self.events.append(message)
        del self.events[:-200]
        method, params = message.get("method"), message.get("params") or {}
        if method == "Page.javascriptDialogOpening":            # an alert or a question would stop the page: answer it
            self.ws.send_text(json.dumps({"id": self._next(), "method": "Page.handleJavaScriptDialog", "params": {"accept": params.get("type") == "alert"}}))
        elif method == "Fetch.requestPaused" and self.request_guard is not None:
            ok = self.request_guard(str((params.get("request") or {}).get("url", "")))
            self._raw("Fetch.continueRequest" if ok else "Fetch.failRequest",
                      {"requestId": params.get("requestId")} if ok else {"requestId": params.get("requestId"), "errorReason": "AccessDenied"},
                      message.get("sessionId"))
        elif method == "Target.attachedToTarget":
            info, sid = params.get("targetInfo") or {}, params.get("sessionId")
            if info.get("type") == "iframe":
                self.frames[sid] = {"target": info.get("targetId", ""), "url": info.get("url", "")}
            if self.request_guard is not None:                  # a frame or worker is held at its start until its requests are watched too
                self._raw("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, sid)
            if params.get("waitingForDebugger"):
                self._raw("Runtime.runIfWaitingForDebugger", {}, sid)
        elif method == "Target.detachedFromTarget":
            self.frames.pop(params.get("sessionId", ""), None)

    def _raw(self, method: str, params: Dict[str, Any], session: Optional[str] = None) -> None:
        """A call whose answer nobody waits for."""
        message: Dict[str, Any] = {"id": self._next(), "method": method, "params": params}
        if session:
            message["sessionId"] = session
        try:
            self.ws.send_text(json.dumps(message))
        except OSError:
            self.closed = True

    def _next(self) -> int:
        self._id += 1
        return self._id

    def pump(self, seconds: float) -> None:
        """Reads what the browser says for a while, so that events are seen."""
        with self._lock:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                try:
                    self._event(json.loads(self.ws.recv_text(max(0.05, end - time.monotonic()))))
                except TimeoutError:
                    return
                except (BrowserError, OSError):
                    self.closed = True
                    return

    def saw(self, method: str, since: int = 0) -> bool:
        return any(e.get("method") == method for e in self.events[since:])


# -- the page's elements ----------------------------------------------------------------------------------

SNAPSHOT_JS = r"""
(() => {
  const visible = e => { const r = e.getBoundingClientRect(); const s = e.ownerDocument.defaultView.getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const label = e => {
    const aria = e.getAttribute('aria-label'); if (aria) return aria.trim();
    if (e.labels && e.labels.length) return [...e.labels].map(l => l.innerText).join(' ').trim();
    const by = e.getAttribute('aria-labelledby'); if (by) { const t = by.split(/\s+/).map(i => (e.ownerDocument.getElementById(i) || {}).innerText || '').join(' ').trim(); if (t) return t; }
    return (e.placeholder || e.title || e.alt || ((e.tagName === 'INPUT' && /^(submit|button|reset)$/.test(e.type)) ? e.value : '') || e.innerText || e.getAttribute('name') || '').trim();
  };
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[onclick],[contenteditable=true]';
  const roots = [document];
  document.querySelectorAll('iframe').forEach(f => { try { if (f.contentDocument) roots.push(f.contentDocument); } catch (x) { /* another site: its own session */ } });
  const found = roots.flatMap(d => [...d.querySelectorAll(sel)]).filter(e => e.type !== 'hidden' && visible(e));
  window.__bref = found.slice(0, MAX);
  const items = window.__bref.map((e, i) => {
    const tag = e.tagName.toLowerCase(), type = (e.type || '').toLowerCase();
    const o = { ref: i + 1, tag, type, name: label(e).replace(/\s+/g, ' ').slice(0, 90) };
    if (tag === 'a') { try { o.href = new URL(e.href).host + new URL(e.href).pathname.slice(0, 40); } catch (x) {} }
    if (tag === 'input' || tag === 'textarea') { o.value = (type === 'password' || e.__bsecret) ? (e.value ? '(set)' : '') : String(e.value || '').slice(0, 60); }
    if (type === 'checkbox' || type === 'radio') o.checked = !!e.checked;
    if (tag === 'select') { o.value = e.value; o.options = [...e.options].slice(0, 30).map(x => x.value + '=' + x.text.trim().slice(0, 30)); }
    if (e.disabled) o.disabled = true;
    if (e.required) o.required = true;
    return o;
  });
  const text = roots.map(d => (d.body ? d.body.innerText : '')).join('\n').replace(/\n{3,}/g, '\n\n').slice(0, TEXT);
  return JSON.stringify({ url: location.href, host: location.hostname, title: document.title, items, more: Math.max(0, found.length - MAX), text });
})()
""".replace("MAX", str(MAX_ELEMENTS)).replace("TEXT", str(PAGE_TEXT_CHARS))

# where to click an element: the middle of it in the page's own viewport, counting the frames of the same site it sits in
CENTER_JS = r"""
(() => {
  const e = window.__bref[%I%]; e.scrollIntoView({block: 'center', inline: 'center'});
  const r = e.getBoundingClientRect(); let x = r.left + r.width / 2, y = r.top + r.height / 2, w = e.ownerDocument.defaultView;
  while (w && w.frameElement) { const f = w.frameElement.getBoundingClientRect(); x += f.left; y += f.top; w = w.parent; }
  return [x, y];
})()
"""

# links and forms open in the same tab; a window a page opens itself (a payment sheet) is a page of its own, which the browser follows
INIT_JS = r"""
(() => {
  document.addEventListener('click', e => { const a = e.target.closest && e.target.closest('a[target]'); if (a) a.removeAttribute('target'); }, true);
  document.addEventListener('submit', e => { if (e.target && e.target.removeAttribute) e.target.removeAttribute('target'); }, true);
})();
"""

KEYS = {"Enter": (13, "Enter", "\r"), "Tab": (9, "Tab", ""), "Escape": (27, "Escape", ""), "Backspace": (8, "Backspace", ""), "ArrowDown": (40, "ArrowDown", ""),
        "ArrowUp": (38, "ArrowUp", ""), "ArrowLeft": (37, "ArrowLeft", ""), "ArrowRight": (39, "ArrowRight", ""), "Space": (32, " ", " ")}


_RESOLVED: Dict[str, Tuple[float, Optional[bool]]] = {}           # host -> (until, whether it is not on the internet)


def blocked_host(host: str, port: int = 443) -> Optional[bool]:
    """Whether a host is an address inside this PC or its network (private, loopback, link-local, shared-carrier, multicast, reserved):
    anything that is not a public address. None when the name cannot be resolved. Answers are kept for a minute."""
    host = host.strip("[]").lower()
    now = time.monotonic()
    hit = _RESOLVED.get(host)
    if hit and hit[0] > now:
        return hit[1]
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        answer: Optional[bool] = False
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            ip = getattr(ip, "ipv4_mapped", None) or ip
            if not ip.is_global:
                answer = True
    except (OSError, ValueError):
        answer = None
    if len(_RESOLVED) > 2000:
        _RESOLVED.clear()
    _RESOLVED[host] = (now + 60.0, answer)
    return answer


def check_url(url: str, allow_private: bool = False) -> str:
    """Only web pages, and not this PC's or its network's own unless the person allowed them (a page there can be an admin panel, not a shop)."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BrowserError("only web addresses (http or https) can be opened")
    if allow_private:
        return parsed.geturl()
    verdict = blocked_host(parsed.hostname, parsed.port or 443)
    if verdict is None:
        raise BrowserError(f"the site {parsed.hostname} could not be found")
    if verdict:
        raise BrowserError(f"{parsed.hostname} is an address inside this PC or its network, not a site on the internet: the person has not allowed such pages")
    return parsed.geturl()


class BrowserPrefs:
    """What the person chose about the agent's browser: whether it has one, whether it shows a window, whether it may open pages on this PC and
    its network, and whether it keeps its sign-ins between conversations."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> Dict[str, bool]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return {"enabled": data.get("enabled") is not False, "headless": data.get("headless") is True, "private": data.get("private") is True,
                "persistent": data.get("persistent") is True}

    def write(self, enabled: bool, headless: bool, private: bool = False, persistent: bool = False) -> Dict[str, bool]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"enabled": bool(enabled), "headless": bool(headless), "private": bool(private), "persistent": bool(persistent)}), encoding="utf-8")
        return self.read()


class ProfileLock:
    """One browser at a time on a profile that keeps sign-ins: a browser cannot share its profile folder, and two would overwrite each other's sign-ins."""

    def __init__(self, directory: str | Path) -> None:
        self.path = Path(directory) / "basalt.lock"
        self.handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self.handle.close()
            self.handle = None


def open_signin_window(profile_dir: str | Path, url: str, executable: str = "") -> None:
    """The browser that keeps sign-ins, in a window of its own, for the person to sign in to Google, a bank or a shop. The profile is theirs while the
    window is open; when it is closed the agent's browsers can use it."""
    exe = find_browser(executable)
    if not exe:
        raise BrowserError("no Edge or Chrome was found on this PC")
    if urlparse(url).scheme not in ("http", "https"):
        raise BrowserError("only web addresses (http or https) can be opened")
    lock = ProfileLock(profile_dir)
    if not lock.acquire():
        raise BrowserError("the browser that keeps sign-ins is in use: finish the conversation that uses it, or close its window, first")
    try:
        proc = subprocess.Popen([exe, f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check", url],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    except OSError as e:
        lock.release()
        raise BrowserError(f"the browser could not be started: {e}") from None
    threading.Thread(target=lambda: (proc.wait(), lock.release()), daemon=True, name="signin-window").start()


def forget_signins(profile_dir: str | Path) -> None:
    """Throws the kept profile away: every sign-in in it is gone."""
    lock = ProfileLock(profile_dir)
    if not lock.acquire():
        raise BrowserError("the browser that keeps sign-ins is in use: finish the conversation that uses it, or close its window, first")
    try:
        for child in Path(profile_dir).iterdir():
            if child.name != "basalt.lock":                       # the lock file stays while it is held
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
    finally:
        lock.release()
    shutil.rmtree(profile_dir, ignore_errors=True)


class Browser:
    def __init__(self, profile_dir: str | Path = "", headless: bool = False, executable: str = "", allow_private: bool = False,
                 mask: Optional[Callable[[str], str]] = None, persistent: bool = False) -> None:
        self.persistent = bool(persistent and profile_dir)             # the profile is kept, and its sign-ins with it
        self.profile_dir = Path(profile_dir) if profile_dir else Path(tempfile.mkdtemp(prefix="basalt-browser-"))
        self.keep_profile = self.persistent
        self.headless, self.executable, self.allow_private = headless, executable, allow_private
        self.mask = mask or (lambda t: t)
        self.proc: Optional[subprocess.Popen] = None
        self.job: Any = None
        self.lock: Optional[ProfileLock] = None
        self.pages: List[Cdp] = []                                      # the page the agent works on, and above it the windows that page opened
        self.seen: set = set()
        self.refs: List[Tuple[Optional[str], int, str]] = []            # element number - 1 -> (frame session or None, index in that frame, its site)
        self.host = ""
        self.notice = ""                                                # said to the model once, with the next page it reads
        self.hidden: set = set()                      # what was typed from the vault: a field that holds it is listed as set, not as its value
        self.page_url = ""                                              # the page the agent works on, as last read
        self.blocked: List[str] = []                                    # the hosts a request was refused for, until the model is told

    @property
    def cdp(self) -> Optional[Cdp]:
        return self.pages[-1] if self.pages else None

    # -- starting and stopping -------------------------------------------------------------------

    def start(self) -> None:
        if self.pages:
            return
        exe = find_browser(self.executable)
        if not exe:
            raise BrowserError("no Edge or Chrome was found on this PC (set BASALT_BROWSER to its program)")
        if self.persistent:
            self.lock = ProfileLock(self.profile_dir)
            if not self.lock.acquire():                               # another conversation, or the window the person signs in with, has it
                self.lock, self.keep_profile = None, False
                self.profile_dir = Path(tempfile.mkdtemp(prefix="basalt-browser-"))
                self.notice = ("The browser that keeps the person's sign-ins is in use by another conversation or window, so this one started without "
                               "any sign-in. Sign in here again if you must, or tell the person.")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = [exe, f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile_dir}", "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--window-size=1280,900", "--disable-background-networking"]
        if self.headless:
            args.append("--headless=new")
        args.append("about:blank")
        from harness.sandbox.jobs import CREATE_SUSPENDED, CommandJob
        self.job = CommandJob()                                   # the browser ends with the conversation, however that ends
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                     creationflags=CREATE_SUSPENDED if self.job.active else 0)
        if self.job.active:
            self.job.attach(self.proc)
        target = None
        for _ in range(120):
            if self.proc.poll() is not None:
                raise BrowserError("the browser closed as soon as it started")
            try:
                pages = self._targets()
                if pages:
                    target = pages[0]
                    break
            except Exception:
                pass
            time.sleep(0.25)
        if target is None:
            self.close()
            raise BrowserError("the browser did not open a page")
        self.seen = {target["id"]}
        self._attach(target)
        try:
            self.cdp.call("Browser.setDownloadBehavior", {"behavior": "deny"})
        except BrowserError:
            pass

    def _targets(self) -> List[Dict[str, Any]]:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2) as r:
            return [t for t in json.loads(r.read()) if t.get("type") == "page" and str(t.get("url", "")).startswith(("http://", "https://", "about:blank"))]   # not the browser's own dialogs

    def _attach(self, target: Dict[str, Any]) -> None:
        """Speaks to a page: its own protocol connection, with the frames of other sites it holds followed as sessions."""
        cdp = Cdp(WebSocket("127.0.0.1", self.port, urlparse(target["webSocketDebuggerUrl"]).path), target["id"])
        if not self.allow_private:
            cdp.request_guard = self._request_ok                        # every request, of the page and of its frames, is looked at before it goes out
        for method, params in (("Page.enable", None), ("Runtime.enable", None), ("DOM.enable", None),
                               ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": cdp.request_guard is not None, "flatten": True}),
                               ("Page.addScriptToEvaluateOnNewDocument", {"source": INIT_JS})):
            cdp.call(method, params)
        if cdp.request_guard is not None:
            cdp.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
        self.pages.append(cdp)

    def _request_ok(self, url: str) -> bool:
        """A page on the internet reaches the internet only: not this PC or its network, and no local file. Redirects, links, scripts and
        images are all requests, so all go through here."""
        p = urlparse(url)
        if p.scheme in ("data", "blob", "about"):
            return True
        if p.scheme not in ("http", "https", "ws", "wss") or not p.hostname:
            return False
        if blocked_host(p.hostname, p.port or (443 if p.scheme in ("https", "wss") else 80)) is True:
            if p.hostname not in self.blocked:
                self.blocked.append(p.hostname)
            return False
        return True

    def insecure_public(self, host: str) -> bool:
        """Whether a page is plain http on a site of the internet: what is typed there crosses the network in the clear."""
        return self.page_url.startswith("http://") and blocked_host(host, 80) is False

    def _sync_pages(self) -> None:
        """A window the page opened becomes the page the agent works on; when it closes, the agent is back on the page that opened it."""
        if not self.pages:
            return
        try:
            targets = self._targets()
        except Exception:
            return
        ids = {t["id"] for t in targets}
        for t in targets:
            if t["id"] not in self.seen:
                self.seen.add(t["id"])
                try:
                    self._attach(t)
                except BrowserError:
                    pass
        while len(self.pages) > 1 and (self.pages[-1].closed or self.pages[-1].target_id not in ids):
            self.pages.pop().ws.close()

    def close(self) -> None:
        try:
            if self.pages:
                try:
                    self.pages[0].call("Browser.close", timeout=3)
                except Exception:
                    pass
                for p in self.pages:
                    p.ws.close()
        finally:
            self.pages = []
            if self.proc is not None:
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
                self.proc = None
            if self.job is not None:
                self.job.close()
                self.job = None
            if self.lock is not None:
                self.lock.release()
                self.lock = None
            if not self.keep_profile:
                shutil.rmtree(self.profile_dir, ignore_errors=True)

    # -- the page --------------------------------------------------------------------------------

    def _js(self, expression: str, session: Optional[str] = None) -> Any:
        self.start()
        out = self.cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}, session=session)
        if "exceptionDetails" in out:
            raise BrowserError("the page refused: " + str((out["exceptionDetails"].get("exception") or {}).get("description", "script error"))[:200])
        return (out.get("result") or {}).get("value")

    def settle(self, since: int) -> None:
        """After an action: if the page started to navigate, wait for it to load; a change inside the page needs no wait."""
        self.cdp.pump(0.4)
        if self.cdp.closed:
            self._sync_pages()
            return
        if self.cdp.saw("Page.navigatedWithinDocument", since) and not self.cdp.saw("Page.frameStartedLoading", since):
            self._sync_pages()
            return
        if self.cdp.saw("Page.frameStartedLoading", since) or self.cdp.saw("Page.frameNavigated", since):
            end = time.monotonic() + 12.0
            while time.monotonic() < end and not self.cdp.saw("Page.loadEventFired", since):
                self.cdp.pump(0.25)
            self.cdp.pump(0.3)
        self._sync_pages()

    def open(self, url: str) -> Dict[str, Any]:
        url = check_url(url, self.allow_private)
        self.start()
        since = len(self.cdp.events)
        result = self.cdp.call("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise BrowserError(f"the page could not be opened: {result['errorText']}")
        deadline = time.monotonic() + NAVIGATION_SECONDS
        while time.monotonic() < deadline and not self.cdp.saw("Page.loadEventFired", since):
            self.cdp.pump(0.25)
        self.cdp.pump(0.3)
        return self.snapshot()

    def snapshot(self) -> Dict[str, Any]:
        """The page and, after its own elements, those of the frames of other sites it holds (a card field in a payment provider's frame)."""
        self.start()
        self._sync_pages()
        self.cdp.pump(0.05)
        page = json.loads(self._js(SNAPSHOT_JS))
        self.page_url = page["url"]
        if not self.allow_private:
            if self.page_url.startswith("chrome-error:") and self.blocked:
                host = self.blocked.pop()
                self.blocked.clear()
                raise BrowserError(f"{host} is an address inside this PC or its network, not a site on the internet: the person has not allowed such pages")
            if self.page_url.startswith(("http://", "https://")):
                try:
                    check_url(self.page_url, False)
                except BrowserError:
                    self.cdp.call("Page.navigate", {"url": "about:blank"})
                    raise
        items = list(page["items"])
        refs: List[Tuple[Optional[str], int, str]] = [(None, i, page["host"]) for i in range(len(items))]
        texts = [page["text"]]
        for session, frame in list(self.cdp.frames.items()):
            try:
                sub = json.loads(self._js(SNAPSHOT_JS, session=session))
            except (BrowserError, ValueError):
                continue
            if not sub["items"] and not sub["text"].strip():
                continue
            for k, it in enumerate(sub["items"]):
                items.append({**it, "frame": sub["host"] or "frame"})
                refs.append((session, k, sub["host"]))
            if sub["text"].strip():
                texts.append(f"[frame {sub['host'] or 'frame'}] " + sub["text"].strip())
        for n, it in enumerate(items, 1):
            it["ref"] = n
        self.refs, self.host = refs, page["host"]
        return {**page, "items": items, "text": "\n".join(texts)[:2 * PAGE_TEXT_CHARS]}

    def _where(self, ref: int) -> Tuple[Optional[str], int, str]:
        """Which frame an element is in and its place there, refused when the page has changed since it was read."""
        if isinstance(ref, bool) or not isinstance(ref, int) or not 1 <= ref <= len(self.refs):
            raise BrowserError("there is no element with that number on this page: read the page again (the page may have changed)")
        session, local, host = self.refs[ref - 1]
        count = self._js("(window.__bref || []).length", session=session)
        if local >= (count or 0):
            raise BrowserError("the page has changed since it was read: read the page again")
        return session, local, host

    def host_for(self, ref: int) -> str:
        """The site that would receive what is typed into an element: the frame's own, not the page's."""
        return self._where(ref)[2]

    def _frame_origin(self, session: str) -> Tuple[float, float]:
        """Where a frame of another site starts in the page, so that a click in it can be made in the page's own coordinates."""
        frame = self.cdp.frames.get(session)
        if frame is None:
            raise BrowserError("the frame went away: read the page again")
        owner = self.cdp.call("DOM.getFrameOwner", {"frameId": frame["target"]})["backendNodeId"]
        quad = self.cdp.call("DOM.getBoxModel", {"backendNodeId": owner})["model"]["content"]
        return quad[0], quad[1]

    def click(self, ref: int) -> Dict[str, Any]:
        session, local, _ = self._where(ref)
        x, y = self._js(CENTER_JS.replace("%I%", str(local)), session=session)
        if session is not None:
            dx, dy = self._frame_origin(session)
            x, y = x + dx, y + dy
        since = len(self.cdp.events)
        page = self.cdp
        try:
            for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
                page.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "buttons": 1 if kind == "mousePressed" else 0, "clickCount": 1})
        except BrowserError:
            if not page.closed:
                raise                                                   # the click closed the window it was made in: that is a result, not a failure
        self.settle(since)
        return self.snapshot()

    def type(self, ref: int, text: str, submit: bool = False, clear: bool = True, secret: bool = False) -> Dict[str, Any]:
        session, local, _ = self._where(ref)
        kind = self._js(f"(() => {{ const e = window.__bref[{local}]; e.scrollIntoView({{block: 'center'}}); e.focus(); return e.type || e.tagName.toLowerCase(); }})()", session=session)
        if kind in ("checkbox", "radio", "submit", "button", "file", "image", "reset"):
            raise BrowserError(f"that element is a {kind}: click it instead of typing")
        if clear:
            self._js(f"(() => {{ const e = window.__bref[{local}]; if (e.select) e.select(); else if (e.isContentEditable) e.ownerDocument.execCommand('selectAll'); }})()", session=session)
            self.cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
            self.cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
        if text:
            self.cdp.call("Input.insertText", {"text": text})
        if secret:                                                      # the element is marked, so that what it holds is never listed
            self._js(f"(() => {{ window.__bref[{local}].__bsecret = true; }})()", session=session)
        since = len(self.cdp.events)
        if submit:
            self._key("Enter")
            self.settle(since)
        return self.snapshot()

    def select(self, ref: int, value: str) -> Dict[str, Any]:
        session, local, _ = self._where(ref)
        ok = self._js(f"(() => {{ const e = window.__bref[{local}]; if (e.tagName !== 'SELECT') return 'notselect'; const want = {json.dumps(value)}.toLowerCase(); "
                      "const o = [...e.options].find(x => x.value.toLowerCase() === want || x.text.trim().toLowerCase() === want); if (!o) return 'nooption'; "
                      "e.value = o.value; e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true})); return 'ok'; })()", session=session)
        if ok == "notselect":
            raise BrowserError("that element is not a list to choose from")
        if ok == "nooption":
            raise BrowserError("that list has no such choice: read the page for its choices")
        return self.snapshot()

    def _key(self, name: str) -> None:
        if name not in KEYS:
            raise BrowserError(f"keys: {', '.join(KEYS)}")
        code, key, text = KEYS[name]
        base = {"key": key, "code": name, "windowsVirtualKeyCode": code}
        self.cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", **base, **({"text": text} if text else {})})
        self.cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    def press(self, name: str) -> Dict[str, Any]:
        self.start()
        since = len(self.cdp.events)
        self._key(name)
        self.settle(since)
        return self.snapshot()

    def back(self) -> Dict[str, Any]:
        self.start()
        since = len(self.cdp.events)
        history = self.cdp.call("Page.getNavigationHistory")
        index = history.get("currentIndex", 0)
        if index <= 0:
            raise BrowserError("there is no earlier page")
        self.cdp.call("Page.navigateToHistoryEntry", {"entryId": history["entries"][index - 1]["id"]})
        self.settle(since)
        return self.snapshot()


def _squeeze(value: str) -> str:
    """A value without the spaces, dashes and dots a page may add or take away, and in lower case, to tell it from another."""
    return re.sub(r"[\s\-.]", "", value).lower()


def describe(page: Dict[str, Any], hidden: Optional[set] = None) -> str:
    """The page as the model reads it: numbered elements, then the text. A password field, or one holding what came from the vault, says only
    that it is set (a short value, like a card's security code, could not be masked in text without damaging it)."""
    lines = [f"URL: {page['url']}", f"Title: {page['title']}", "Elements:"]
    for it in page["items"]:
        kind = "link" if it["tag"] == "a" else (it["tag"] if it["tag"] not in ("input", "button") else (f"{it['tag']}[{it['type']}]" if it["type"] else it["tag"]))
        bits = [f"[{it['ref']}] {kind}", f"\"{it['name']}\""] + ([f"(in {it['frame']})"] if it.get("frame") else [])
        for key in ("value", "href"):
            if it.get(key):
                bits.append(f"{key}={'(set)' if key == 'value' and hidden and _squeeze(it[key]) in hidden else it[key]}")
        if "checked" in it:
            bits.append("checked" if it["checked"] else "unchecked")
        if it.get("options"):
            bits.append("choices: " + "; ".join(it["options"]))
        for flag in ("disabled", "required"):
            if it.get(flag):
                bits.append(flag)
        lines.append(" ".join(bits))
    if page["more"]:
        lines.append(f"... and {page['more']} more elements not listed")
    lines += ["Text:", page["text"]]
    return "\n".join(lines)


def register_browser_tools(registry: Any, secrets: Any, profile_dir: str | Path = "", headless: bool = False, executable: str = "",
                           allow_private: bool = False, browser: Optional[Browser] = None, persistent: bool = False) -> Browser:
    """Adds the browser tools. The browser starts at the first use and is closed with the conversation."""
    from harness.tools.registry import EffectClass

    mask = secrets.mask_secrets if secrets is not None else (lambda t: t)
    b = browser or Browser(profile_dir, headless, executable, allow_private, mask, persistent)
    b.mask = mask

    def shown(page: Dict[str, Any]) -> str:
        text = mask(describe(page, b.hidden))
        if b.notice:
            text, b.notice = b.notice + "\n" + text, ""
        return text

    def guard(fn: Callable[..., Any]) -> Callable[..., Any]:
        def run(**kw: Any) -> Any:
            try:
                return fn(**kw)
            except BrowserError as e:
                return {"error": str(e)}
        return run

    ref_prop = {"ref": {"type": "integer", "description": "the number of the element, from the last reading of the page"}}
    registry.register("browser_open", "Opens a web page in the browser and reads it: numbered elements (links, buttons, fields) and the text.",
                      {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, EffectClass.PURE,
                      guard(lambda url: shown(b.open(url))), untrusted=True)
    registry.register("browser_read", "Reads the page that is open again, with its numbered elements.", {"type": "object", "properties": {}},
                      EffectClass.PURE, guard(lambda: shown(b.snapshot())), untrusted=True)
    registry.register("browser_click", "Clicks an element of the page (a link, a button, a box to tick) and reads the page that follows.",
                      {"type": "object", "properties": ref_prop, "required": ["ref"]}, EffectClass.IRREVERSIBLE, guard(lambda ref: shown(b.click(ref))), untrusted=True)

    def type_into(ref: int, text: str, submit: bool = False, clear: bool = True) -> Any:
        from_vault = secrets is not None and secrets.has_references(text)
        if from_vault:
            host = b.host_for(ref)                                          # the site that would receive it: the frame's own, when the field is in one
            if b.insecure_public(host):
                return {"error": f"this page is not encrypted (http) and {host} is on the internet: a value from the vault is not typed where it would cross the network in the clear"}
            try:
                text = secrets.resolve_tokens(text, destinations=[host])
                b.hidden.add(_squeeze(text[:60]))                            # the page lists a field's value cut to 60 characters
            except Exception as e:                                          # the site is not one the entry names, the card is guarded, a code is missing
                return {"error": str(e)}
        page = b.type(ref, text, submit, clear, secret=from_vault)
        return shown(page)

    registry.register("browser_type", "Types text into a field of the page. Write {{vault:ENTRY.FIELD}} (or {{secret:NAME}}) for a value from the person's vault; "
                      "it is filled in only on a site its entry names, and you never see it.",
                      {"type": "object", "properties": {**ref_prop, "text": {"type": "string"}, "submit": {"type": "boolean", "description": "press Enter after"},
                                                        "clear": {"type": "boolean", "description": "replace what is in the field (default true)"}},
                       "required": ["ref", "text"]}, EffectClass.IRREVERSIBLE, guard(type_into), untrusted=True)
    def sign_in(entry: str, submit: bool = False) -> Any:
        """Fills the sign-in fields of the page from a login in the vault: the user name in the field before the password, the password in
        the password field. Only on a site the entry names. A page that asks for the user name first shows just that field: it is filled and
        the password comes on the next page."""
        if secrets is None or getattr(secrets, "vault", None) is None:
            return {"error": "There is no vault to sign in from."}
        try:
            fields = secrets.vault.describe(str(entry))
        except Exception:
            return {"error": f"There is no vault entry '{entry}'."}
        if fields["kind"] != "login":
            return {"error": f"'{entry}' is not a login."}

        def places() -> tuple:
            items = b.snapshot()["items"]
            inputs = [i for i in items if i["tag"] == "input" and not i.get("disabled")]
            pw = next((i for i in inputs if i["type"] == "password"), None)
            texty = [i for i in inputs if i["type"] in ("text", "email", "tel", "")]
            hint = re.compile(r"user|mail|login|account|utente|accesso|identifiant|usuario|phone|telefono", re.I)
            pool = [i for i in texty if pw is None or i["ref"] < pw["ref"]]
            user = next((i for i in reversed(pool) if hint.search(i["name"])), None) or (pool[-1] if pool else None)
            return user, pw

        user, pw = places()
        if user is None and pw is None:
            return {"error": "No sign-in field was found on this page. Read the page and open the sign-in first."}
        out: Any = None
        if user is not None and "username" in fields["fields"]:
            out = type_into(user["ref"], "{{vault:%s.username}}" % entry, False, True)
            if isinstance(out, dict):
                return out
            _, pw = places()
        if pw is not None and "password" in fields["fields"]:
            out = type_into(pw["ref"], "{{vault:%s.password}}" % entry, submit, True)
            if isinstance(out, dict):
                return out
        elif submit and out is not None:
            out = shown(b.press("Enter"))
        return out if out is not None else {"error": f"'{entry}' has no user name or password to fill in."}

    registry.register("browser_sign_in", "Signs in on the open page with a login from the person's vault: fills the user name and the password (never shown to you). "
                      "Only on a site the entry names. If the page asks for a code, write {{vault:ENTRY.totp}} in the field with browser_type.",
                      {"type": "object", "properties": {"entry": {"type": "string", "description": "the name of the login in the vault"},
                                                        "submit": {"type": "boolean", "description": "press Enter after the password"}}, "required": ["entry"]},
                      EffectClass.IRREVERSIBLE, guard(sign_in), untrusted=True)
    registry.register("browser_select", "Chooses an option in a list of the page, by its value or its visible text.",
                      {"type": "object", "properties": {**ref_prop, "value": {"type": "string"}}, "required": ["ref", "value"]}, EffectClass.IRREVERSIBLE,
                      guard(lambda ref, value: shown(b.select(ref, value))), untrusted=True)
    registry.register("browser_press", "Presses a key: Enter, Tab, Escape, Backspace, the arrows or Space.",
                      {"type": "object", "properties": {"key": {"type": "string", "enum": list(KEYS)}}, "required": ["key"]}, EffectClass.IRREVERSIBLE,
                      guard(lambda key: shown(b.press(key))), untrusted=True)
    registry.register("browser_back", "Goes back to the page before.", {"type": "object", "properties": {}}, EffectClass.IRREVERSIBLE,
                      guard(lambda: shown(b.back())), untrusted=True)
    return b
