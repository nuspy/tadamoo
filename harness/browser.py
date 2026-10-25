"""A browser the agent drives to fill in forms, buy and book: Edge or Chrome on this PC, over the DevTools protocol.

The page's content is untrusted data: the tools return it fenced, and what the person keeps in the vault reaches a page only as a
placeholder the agent writes in `browser_type`, filled in here, at the last step, and only if the page's site is one the vault entry names.
A value is never echoed back: what the model reads of a page is masked of every value in the vault. Each conversation has its own browser
and its own throw-away profile, so a sign-in made for one is not there for another.

Reaches: the page and the frames that share its site. A card field inside a payment provider's own frame is another site's page and is not
reachable here.
"""
from __future__ import annotations

import base64
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
from typing import Any, Callable, Dict, List, Optional
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
    """Calls and events of the DevTools protocol on one page."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws, self._id, self.events, self._lock = ws, 0, [], threading.RLock()

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
        with self._lock:
            self._id += 1
            mine = self._id
            self.ws.send_text(json.dumps({"id": mine, "method": method, "params": params or {}}))
            deadline = time.monotonic() + timeout
            while True:
                try:
                    message = json.loads(self.ws.recv_text(max(0.05, deadline - time.monotonic())))
                except TimeoutError:
                    raise BrowserError(f"the browser did not answer {method}") from None
                if message.get("id") == mine:
                    if "error" in message:
                        raise BrowserError(f"{method}: {message['error'].get('message', 'failed')}")
                    return message.get("result", {})
                self._event(message)

    def _event(self, message: Dict[str, Any]) -> None:
        self.events.append(message)
        del self.events[:-200]
        if message.get("method") == "Page.javascriptDialogOpening":     # an alert or a question would stop the page: answer it
            kind = (message.get("params") or {}).get("type")
            self.ws.send_text(json.dumps({"id": self._next(), "method": "Page.handleJavaScriptDialog", "params": {"accept": kind == "alert"}}))

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

    def saw(self, method: str, since: int = 0) -> bool:
        return any(e.get("method") == method for e in self.events[since:])


# -- the page's elements ----------------------------------------------------------------------------------

SNAPSHOT_JS = r"""
(() => {
  const visible = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const label = e => {
    const aria = e.getAttribute('aria-label'); if (aria) return aria.trim();
    if (e.labels && e.labels.length) return [...e.labels].map(l => l.innerText).join(' ').trim();
    const by = e.getAttribute('aria-labelledby'); if (by) { const t = by.split(/\s+/).map(i => (document.getElementById(i) || {}).innerText || '').join(' ').trim(); if (t) return t; }
    return (e.placeholder || e.title || e.alt || ((e.tagName === 'INPUT' && /^(submit|button|reset)$/.test(e.type)) ? e.value : '') || e.innerText || e.getAttribute('name') || '').trim();
  };
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[onclick],[contenteditable=true]';
  const found = [...document.querySelectorAll(sel)].filter(e => e.type !== 'hidden' && visible(e));
  window.__bref = found.slice(0, MAX);
  const items = window.__bref.map((e, i) => {
    const tag = e.tagName.toLowerCase(), type = (e.type || '').toLowerCase();
    const o = { ref: i + 1, tag, type, name: label(e).replace(/\s+/g, ' ').slice(0, 90) };
    if (tag === 'a') { try { o.href = new URL(e.href).host + new URL(e.href).pathname.slice(0, 40); } catch (x) {} }
    if (tag === 'input' || tag === 'textarea') { o.value = type === 'password' ? (e.value ? '(set)' : '') : String(e.value || '').slice(0, 60); }
    if (type === 'checkbox' || type === 'radio') o.checked = !!e.checked;
    if (tag === 'select') { o.value = e.value; o.options = [...e.options].slice(0, 30).map(x => x.value + '=' + x.text.trim().slice(0, 30)); }
    if (e.disabled) o.disabled = true;
    if (e.required) o.required = true;
    return o;
  });
  return JSON.stringify({ url: location.href, host: location.hostname, title: document.title, items, more: Math.max(0, found.length - MAX), text: (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, TEXT) });
})()
""".replace("MAX", str(MAX_ELEMENTS)).replace("TEXT", str(PAGE_TEXT_CHARS))

# links open in the same tab, so the agent never loses the page it works on
INIT_JS = r"""
(() => {
  window.open = u => { if (u) location.href = u; return null; };
  document.addEventListener('click', e => { const a = e.target.closest && e.target.closest('a[target]'); if (a) a.removeAttribute('target'); }, true);
  document.addEventListener('submit', e => { if (e.target && e.target.removeAttribute) e.target.removeAttribute('target'); }, true);
})();
"""

KEYS = {"Enter": (13, "Enter", "\r"), "Tab": (9, "Tab", ""), "Escape": (27, "Escape", ""), "Backspace": (8, "Backspace", ""), "ArrowDown": (40, "ArrowDown", ""),
        "ArrowUp": (38, "ArrowUp", ""), "ArrowLeft": (37, "ArrowLeft", ""), "ArrowRight": (39, "ArrowRight", ""), "Space": (32, " ", " ")}


def check_url(url: str, allow_private: bool = False) -> str:
    """Only web pages, and not this PC's or its network's own (a page there can be an admin panel, not a shop)."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BrowserError("only web addresses (http or https) can be opened")
    if allow_private:
        return parsed.geturl()
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        raise BrowserError(f"the site {parsed.hostname} could not be found") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise BrowserError(f"{parsed.hostname} is an address inside this PC or its network, not a site on the internet: it is not opened")
    return parsed.geturl()


class BrowserPrefs:
    """What the person chose about the agent's browser: whether it has one, and whether it shows a window."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> Dict[str, bool]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return {"enabled": data.get("enabled") is not False, "headless": data.get("headless") is True}

    def write(self, enabled: bool, headless: bool) -> Dict[str, bool]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"enabled": bool(enabled), "headless": bool(headless)}), encoding="utf-8")
        return self.read()


class Browser:
    def __init__(self, profile_dir: str | Path = "", headless: bool = False, executable: str = "", allow_private: bool = False,
                 mask: Optional[Callable[[str], str]] = None) -> None:
        self.profile_dir = Path(profile_dir) if profile_dir else Path(tempfile.mkdtemp(prefix="basalt-browser-"))
        self.headless, self.executable, self.allow_private = headless, executable, allow_private
        self.mask = mask or (lambda t: t)
        self.proc: Optional[subprocess.Popen] = None
        self.job: Any = None
        self.cdp: Optional[Cdp] = None
        self.host = ""
        self.hidden: set = set()                      # what was typed from the vault: a field that holds it is listed as set, not as its value

    # -- starting and stopping -------------------------------------------------------------------

    def start(self) -> None:
        if self.cdp is not None:
            return
        exe = find_browser(self.executable)
        if not exe:
            raise BrowserError("no Edge or Chrome was found on this PC (set BASALT_BROWSER to its program)")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={self.profile_dir}", "--no-first-run", "--no-default-browser-check",
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
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                    pages = [t for t in json.loads(r.read()) if t.get("type") == "page"]
                if pages:
                    target = pages[0]
                    break
            except Exception:
                pass
            time.sleep(0.25)
        if target is None:
            self.close()
            raise BrowserError("the browser did not open a page")
        path = urlparse(target["webSocketDebuggerUrl"]).path
        self.cdp = Cdp(WebSocket("127.0.0.1", port, path))
        for method in ("Page.enable", "Runtime.enable"):
            self.cdp.call(method)
        self.cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": INIT_JS})
        try:
            self.cdp.call("Browser.setDownloadBehavior", {"behavior": "deny"})
        except BrowserError:
            pass

    def close(self) -> None:
        try:
            if self.cdp is not None:
                try:
                    self.cdp.call("Browser.close", timeout=3)
                except Exception:
                    pass
                self.cdp.ws.close()
        finally:
            self.cdp = None
            if self.proc is not None:
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
                self.proc = None
            if self.job is not None:
                self.job.close()
                self.job = None
            shutil.rmtree(self.profile_dir, ignore_errors=True)

    # -- the page --------------------------------------------------------------------------------

    def _js(self, expression: str) -> Any:
        self.start()
        out = self.cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in out:
            raise BrowserError("the page refused: " + str((out["exceptionDetails"].get("exception") or {}).get("description", "script error"))[:200])
        return (out.get("result") or {}).get("value")

    def settle(self, since: int) -> None:
        """After an action: if the page started to navigate, wait for it to load; a change inside the page needs no wait."""
        self.cdp.pump(0.4)
        if self.cdp.saw("Page.navigatedWithinDocument", since) and not self.cdp.saw("Page.frameStartedLoading", since):
            return
        if self.cdp.saw("Page.frameStartedLoading", since) or self.cdp.saw("Page.frameNavigated", since):
            end = time.monotonic() + 12.0
            while time.monotonic() < end and not self.cdp.saw("Page.loadEventFired", since):
                self.cdp.pump(0.25)
            self.cdp.pump(0.3)

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
        page = self.snapshot()
        if not self.allow_private:
            try:
                check_url(page["url"], False)
            except BrowserError:
                self.cdp.call("Page.navigate", {"url": "about:blank"})
                raise
        return page

    def snapshot(self) -> Dict[str, Any]:
        raw = self._js(SNAPSHOT_JS)
        page = json.loads(raw)
        self.host = page["host"]
        return page

    def _element(self, ref: int) -> None:
        count = self._js("(window.__bref || []).length")
        if not isinstance(ref, int) or not 1 <= ref <= (count or 0):
            raise BrowserError("there is no element with that number on this page: read the page again (the page may have changed)")

    def click(self, ref: int) -> Dict[str, Any]:
        self._element(ref)
        box = self._js(f"(() => {{ const e = window.__bref[{ref - 1}]; e.scrollIntoView({{block: 'center', inline: 'center'}}); const r = e.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }})()")
        since = len(self.cdp.events)
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.cdp.call("Input.dispatchMouseEvent", {"type": kind, "x": box[0], "y": box[1], "button": "left", "buttons": 1 if kind == "mousePressed" else 0, "clickCount": 1})
        self.settle(since)
        return self.snapshot()

    def type(self, ref: int, text: str, submit: bool = False, clear: bool = True) -> Dict[str, Any]:
        self._element(ref)
        kind = self._js(f"(() => {{ const e = window.__bref[{ref - 1}]; e.scrollIntoView({{block: 'center'}}); e.focus(); return e.type || e.tagName.toLowerCase(); }})()")
        if kind in ("checkbox", "radio", "submit", "button", "file", "image", "reset"):
            raise BrowserError(f"that element is a {kind}: click it instead of typing")
        if clear:
            self._js(f"(() => {{ const e = window.__bref[{ref - 1}]; if (e.select) e.select(); else if (e.isContentEditable) document.execCommand('selectAll'); }})()")
            self.cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
            self.cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
        if text:
            self.cdp.call("Input.insertText", {"text": text})
        since = len(self.cdp.events)
        if submit:
            self._key("Enter")
            self.settle(since)
        return self.snapshot()

    def select(self, ref: int, value: str) -> Dict[str, Any]:
        self._element(ref)
        ok = self._js(f"(() => {{ const e = window.__bref[{ref - 1}]; if (e.tagName !== 'SELECT') return 'notselect'; const want = {json.dumps(value)}.toLowerCase(); "
                      "const o = [...e.options].find(x => x.value.toLowerCase() === want || x.text.trim().toLowerCase() === want); if (!o) return 'nooption'; "
                      "e.value = o.value; e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true})); return 'ok'; })()")
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


def describe(page: Dict[str, Any], hidden: Optional[set] = None) -> str:
    """The page as the model reads it: numbered elements, then the text. A password field, or one holding what came from the vault, says only
    that it is set (a short value, like a card's security code, could not be masked in text without damaging it)."""
    lines = [f"URL: {page['url']}", f"Title: {page['title']}", "Elements:"]
    for it in page["items"]:
        kind = "link" if it["tag"] == "a" else (it["tag"] if it["tag"] not in ("input", "button") else (f"{it['tag']}[{it['type']}]" if it["type"] else it["tag"]))
        bits = [f"[{it['ref']}] {kind}", f"\"{it['name']}\""]
        for key in ("value", "href"):
            if it.get(key):
                bits.append(f"{key}={'(set)' if key == 'value' and hidden and it[key] in hidden else it[key]}")
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
                           allow_private: bool = False, browser: Optional[Browser] = None) -> Browser:
    """Adds the browser tools. The browser starts at the first use and is closed with the conversation."""
    from harness.tools.registry import EffectClass

    mask = secrets.mask_secrets if secrets is not None else (lambda t: t)
    b = browser or Browser(profile_dir, headless, executable, allow_private, mask)
    b.mask = mask

    def shown(page: Dict[str, Any]) -> str:
        return mask(describe(page, b.hidden))

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
        if secrets is not None and secrets.has_references(text):
            host = b.snapshot()["host"]                                     # the site the value would be typed into, as the page is now
            try:
                text = secrets.resolve_tokens(text, destinations=[host])
                b.hidden.add(text[:60])                                      # the page lists a field's value cut to 60 characters
            except Exception as e:                                          # the site is not one the entry names, the card is guarded, a code is missing
                return {"error": str(e)}
        page = b.type(ref, text, submit, clear)
        return shown(page)

    registry.register("browser_type", "Types text into a field of the page. Write {{vault:ENTRY.FIELD}} (or {{secret:NAME}}) for a value from the person's vault; "
                      "it is filled in only on a site its entry names, and you never see it.",
                      {"type": "object", "properties": {**ref_prop, "text": {"type": "string"}, "submit": {"type": "boolean", "description": "press Enter after"},
                                                        "clear": {"type": "boolean", "description": "replace what is in the field (default true)"}},
                       "required": ["ref", "text"]}, EffectClass.IRREVERSIBLE, guard(type_into), untrusted=True)
    registry.register("browser_select", "Chooses an option in a list of the page, by its value or its visible text.",
                      {"type": "object", "properties": {**ref_prop, "value": {"type": "string"}}, "required": ["ref", "value"]}, EffectClass.IRREVERSIBLE,
                      guard(lambda ref, value: shown(b.select(ref, value))), untrusted=True)
    registry.register("browser_press", "Presses a key: Enter, Tab, Escape, Backspace, the arrows or Space.",
                      {"type": "object", "properties": {"key": {"type": "string", "enum": list(KEYS)}}, "required": ["key"]}, EffectClass.IRREVERSIBLE,
                      guard(lambda key: shown(b.press(key))), untrusted=True)
    registry.register("browser_back", "Goes back to the page before.", {"type": "object", "properties": {}}, EffectClass.IRREVERSIBLE,
                      guard(lambda: shown(b.back())), untrusted=True)
    return b
