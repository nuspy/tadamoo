"""Read-only adapters that bring events from the world to the planner: a feed (RSS or Atom), a mailbox (IMAP) and a Telegram bot.

They only read. The feed is fetched, the mailbox is opened read-only and only the sender and the subject of new messages are
asked for (never the body), the bot's updates are fetched and nothing is ever sent back. Credentials come from the vault through
a callable the owner of the adapter supplies; they are never stored in the adapter's settings, its state or its messages.

An adapter's `poll()` returns the events that are new since the last poll. The first poll of a feed or a mailbox only learns what
is already there (so a long-standing backlog does not flood the plan); the bot's pending messages are taken as they come.
"""
from __future__ import annotations

import email
import email.header
import email.policy
import email.utils
import html
import imaplib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

from harness.integrations._http import _OPENER

EVENT_CHARS = 500
MAX_FEED_BYTES = 2 << 20
MAX_NEW_PER_POLL = 50
SEEN_KEPT = 500
SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,31}$")
KINDS = ("rss", "imap", "telegram")
MIN_INTERVAL, MAX_INTERVAL = 1, 86400
MAX_SOURCES = 10


class SourceError(Exception):
    """A poll failed; the message says why and never holds a credential."""


@dataclass
class SourceEvent:
    text: str
    source: str
    at: float


def redact(message: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***").replace(urllib.parse.quote(secret, safe=""), "***")
    return message


def _clip(text: str, n: int = EVENT_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


class Source:
    """Common part: an id, a kind, how often to poll and the state that survives between polls (plain data, no credentials)."""
    kind = ""

    def __init__(self, source_id: str, interval: float, state: Optional[Dict[str, Any]] = None) -> None:
        self.id, self.interval = source_id, interval
        self.state: Dict[str, Any] = state if state is not None else {}

    def secrets(self) -> List[str]:
        return []

    def poll(self) -> List[SourceEvent]:  # pragma: no cover - overridden
        raise NotImplementedError


# -- feeds ---------------------------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node: ET.Element, name: str) -> str:
    for c in node:
        if _local(c.tag) == name:
            return "".join(c.itertext()).strip()
    return ""


def parse_feed(data: bytes) -> List[Dict[str, str]]:
    """The items of an RSS 2.0 or Atom document: {"id", "title", "summary"}. Documents with a DTD or entities are refused."""
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)", data, re.I):
        raise SourceError("the feed declares a DTD or entities, which feeds do not need: refused")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise SourceError(f"the feed is not valid XML ({exc})") from None
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title, summary = _child_text(node, "title"), _child_text(node, "description") or _child_text(node, "summary")
        ident = _child_text(node, "guid") or _child_text(node, "id") or _child_text(node, "link") or title
        if not ident and not title:
            continue
        link = _child_text(node, "link")
        if not link:
            for c in node:
                if _local(c.tag) == "link" and c.get("href"):
                    link = c.get("href", "")
                    break
        items.append({"id": ident or link, "title": " ".join(html.unescape(_TAG.sub(" ", title)).split()), "summary": " ".join(html.unescape(_TAG.sub(" ", summary)).split())})
    return items


def fetch_url(url: str, timeout: float = 10.0, max_bytes: int = MAX_FEED_BYTES) -> bytes:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SourceError("a feed address starts with http:// or https://")
    req = urllib.request.Request(url, headers={"User-Agent": "basalt-planner", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise SourceError(f"the feed answered HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise SourceError(f"the feed could not be reached ({type(exc).__name__})") from None
    if len(data) > max_bytes:
        raise SourceError("the feed is larger than the limit")
    return data


class RssSource(Source):
    kind = "rss"

    def __init__(self, source_id: str, url: str, interval: float = 900.0, state: Optional[Dict[str, Any]] = None,
                 fetch: Callable[[str], bytes] = fetch_url) -> None:
        super().__init__(source_id, interval, state)
        self.url, self._fetch = url, fetch
        self.label = f"rss:{urllib.parse.urlsplit(url).hostname}"

    def poll(self) -> List[SourceEvent]:
        items = parse_feed(self._fetch(self.url))
        seen: List[str] = self.state.setdefault("seen", [])
        known = set(seen)
        fresh = [i for i in items if i["id"] not in known]
        first = not self.state.get("baseline")
        self.state["baseline"] = True
        self.state["seen"] = (seen + [i["id"] for i in fresh])[-SEEN_KEPT:]
        if first:
            return []
        now = time.time()
        return [SourceEvent(_clip(f"{i['title']}. {i['summary']}" if i["summary"].strip() else i["title"]), self.label, now) for i in fresh[:MAX_NEW_PER_POLL]]


# -- a mailbox -----------------------------------------------------------------------------------------

def _decoded(value: Any) -> str:
    """A header as text: encoded words are decoded, and a header sent as raw UTF-8 (which the format does not allow but some servers do) is read as such."""
    text = str(value or "").encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    try:
        return str(email.header.make_header(email.header.decode_header(text)))
    except Exception:
        return text


class ImapSource(Source):
    """New messages of one mailbox, as "Email from <sender>: <subject>". The mailbox is opened read-only (EXAMINE) and the headers
    are fetched with BODY.PEEK, so nothing is marked read or changed, and the body is never asked for."""
    kind = "imap"

    def __init__(self, source_id: str, host: str, credentials: Callable[[], Tuple[str, str]], port: int = 993, ssl: bool = True,
                 mailbox: str = "INBOX", interval: float = 300.0, state: Optional[Dict[str, Any]] = None, timeout: float = 15.0) -> None:
        super().__init__(source_id, interval, state)
        self.host, self.port, self.ssl, self.mailbox, self.timeout = host, port, ssl, mailbox, timeout
        self._credentials = credentials
        self._known: List[str] = []
        self.label = f"imap:{host}"

    def secrets(self) -> List[str]:
        return list(self._known)

    def poll(self) -> List[SourceEvent]:
        user, password = self._credentials()
        self._known = [user, password]
        conn = None
        try:
            conn = (imaplib.IMAP4_SSL if self.ssl else imaplib.IMAP4)(self.host, self.port, timeout=self.timeout)
            conn.login(user, password)
            typ, _ = conn.select(self.mailbox, readonly=True)
            if typ != "OK":
                raise SourceError("the mailbox could not be opened")
            validity = (conn.response("UIDVALIDITY")[1] or [b""])[0]
            validity = validity.decode() if isinstance(validity, bytes) else str(validity)
            typ, data = conn.uid("SEARCH", None, "ALL")
            uids = sorted(int(x) for x in (data[0] or b"").split()) if typ == "OK" else []
            last = self.state.get("last_uid")
            if last is None or self.state.get("validity") != validity:
                self.state.update(last_uid=max(uids, default=0), validity=validity)
                return []
            fresh = [u for u in uids if u > last][:MAX_NEW_PER_POLL]
            out = []
            for uid in fresh:
                typ, parts = conn.uid("FETCH", str(uid), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
                raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), b"")
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                name, addr = email.utils.parseaddr(_decoded(msg.get("From")))
                sender = name or addr or "unknown sender"
                out.append(SourceEvent(_clip(f"Email from {sender}: {_decoded(msg.get('Subject')) or '(no subject)'}"), self.label, time.time()))
            if fresh:
                self.state["last_uid"] = fresh[-1]
            return out
        except SourceError:
            raise
        except (imaplib.IMAP4.error, OSError, ValueError) as exc:
            raise SourceError(redact(f"the mailbox could not be read ({type(exc).__name__}: {exc})", self._known)) from None
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass


# -- a Telegram bot ------------------------------------------------------------------------------------

class TelegramSource(Source):
    """Messages sent to a bot, by long polling its updates. Only the chats in `allowed_chat_ids` are listened to, so that nobody else
    who finds the bot can put events into a plan. The bot never sends anything."""
    kind = "telegram"
    DEFAULT_BASE = "https://api.telegram.org"

    def __init__(self, source_id: str, token: Callable[[], str], allowed_chat_ids: Iterable[int], api_base: str = DEFAULT_BASE,
                 interval: float = 1.0, state: Optional[Dict[str, Any]] = None, long_poll: float = 20.0) -> None:
        super().__init__(source_id, interval, state)
        self._token, self.allowed, self.api_base, self.long_poll = token, set(int(c) for c in allowed_chat_ids), api_base.rstrip("/"), long_poll
        self._known: List[str] = []
        self.label = "telegram"

    def secrets(self) -> List[str]:
        return list(self._known)

    def poll(self) -> List[SourceEvent]:
        token = self._token()
        self._known = [token]
        query = urllib.parse.urlencode({"offset": int(self.state.get("offset", 0)), "timeout": int(self.long_poll),
                                        "allowed_updates": json.dumps(["message"])})
        url = f"{self.api_base}/bot{token}/getUpdates?{query}"
        try:
            with _OPENER.open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=self.long_poll + 10) as resp:
                data = json.loads(resp.read(4 << 20).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise SourceError(f"Telegram answered HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SourceError(redact(f"Telegram could not be read ({type(exc).__name__})", self._known)) from None
        if not isinstance(data, dict) or not data.get("ok") or not isinstance(data.get("result"), list):
            raise SourceError("Telegram's answer was not the expected one")
        out = []
        for upd in data["result"]:
            if not isinstance(upd, dict) or not isinstance(upd.get("update_id"), int):
                continue
            self.state["offset"] = max(int(self.state.get("offset", 0)), upd["update_id"] + 1)
            msg = upd.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("text"), str) or not msg["text"].strip():
                continue
            if (msg.get("chat") or {}).get("id") not in self.allowed:
                continue
            who = (msg.get("from") or {}).get("first_name") or "someone"
            out.append(SourceEvent(_clip(f"Telegram from {who}: {msg['text']}"), self.label, float(msg.get("date") or time.time())))
        return out


# -- configuration ---------------------------------------------------------------------------------------

def _interval(cfg: Dict[str, Any], default: float) -> float:
    v = cfg.get("interval_seconds", default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not MIN_INTERVAL <= v <= MAX_INTERVAL:
        raise ValueError(f"interval_seconds is a number from {MIN_INTERVAL} to {MAX_INTERVAL}")
    return float(v)


def _entry(cfg: Dict[str, Any], key: str) -> str:
    v = cfg.get(key)
    if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,39}", v):
        raise ValueError(f"{key} is the name of an entry of the vault")
    return v


def _address(url: Any, what: str, local_http: bool = True) -> str:
    parts = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname or len(url) > 500:
        raise ValueError(f"{what} is an address that starts with http:// or https://")
    return url


def clean_source(cfg: Any, index: int = 0) -> Dict[str, Any]:
    """Checks one source's settings and returns them complete. Nothing secret is in them: a mailbox or a bot names the vault entry
    that holds its credentials."""
    if not isinstance(cfg, dict):
        raise ValueError("a source is an object")
    kind = cfg.get("kind")
    if kind not in KINDS:
        raise ValueError(f"a source's kind is one of: {', '.join(KINDS)}")
    sid = cfg.get("id") or f"{kind}-{index + 1}"
    if not isinstance(sid, str) or not SOURCE_ID.match(sid):
        raise ValueError("a source's id is 1 to 32 lowercase letters, digits, - or _")
    out: Dict[str, Any] = {"id": sid, "kind": kind, "enabled": cfg.get("enabled", True) is not False}
    plan = cfg.get("plan_id")
    if plan not in (None, ""):
        if not isinstance(plan, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", plan):
            raise ValueError("plan_id is the id of a plan")
        out["plan_id"] = plan
    if kind == "rss":
        out.update(url=_address(cfg.get("url"), "url"), interval_seconds=_interval(cfg, 900.0))
    elif kind == "imap":
        host, port = cfg.get("host"), cfg.get("port", 993)
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host):
            raise ValueError("host is the mail server's name")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port is a number from 1 to 65535")
        mailbox = cfg.get("mailbox", "INBOX")
        if not isinstance(mailbox, str) or not mailbox or len(mailbox) > 100 or any(c in mailbox for c in '\r\n"\\'):
            raise ValueError("mailbox is a folder name")
        out.update(host=host, port=port, ssl=cfg.get("ssl", True) is not False, mailbox=mailbox, login_entry=_entry(cfg, "login_entry"),
                   interval_seconds=_interval(cfg, 300.0))
    else:
        chats = cfg.get("allowed_chat_ids")
        if not isinstance(chats, list) or not chats or any(isinstance(c, bool) or not isinstance(c, int) for c in chats):
            raise ValueError("allowed_chat_ids lists the chats the bot listens to (whole numbers); without it anyone could feed the plan")
        out.update(secret_entry=_entry(cfg, "secret_entry"), allowed_chat_ids=chats, interval_seconds=_interval(cfg, 1.0),
                   api_base=_address(cfg.get("api_base", TelegramSource.DEFAULT_BASE), "api_base"))
    return out


def clean_sources(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_SOURCES:
        raise ValueError(f"sources is a list of at most {MAX_SOURCES}")
    cleaned = [clean_source(c, i) for i, c in enumerate(value)]
    ids = [c["id"] for c in cleaned]
    if len(set(ids)) != len(ids):
        raise ValueError("two sources have the same id")
    return cleaned


def build_source(cfg: Dict[str, Any], reveal: Callable[[str, str], str], state: Optional[Dict[str, Any]] = None) -> Source:
    """The adapter for a cleaned configuration. `reveal(entry, field)` reads a vault value at the moment it is needed."""
    interval = cfg.get("interval_seconds", 60.0)
    if cfg["kind"] == "rss":
        return RssSource(cfg["id"], cfg["url"], interval, state)
    if cfg["kind"] == "imap":
        entry = cfg["login_entry"]
        return ImapSource(cfg["id"], cfg["host"], lambda: (reveal(entry, "username"), reveal(entry, "password")), cfg["port"], cfg["ssl"],
                          cfg["mailbox"], interval, state)
    entry = cfg["secret_entry"]
    return TelegramSource(cfg["id"], lambda: reveal(entry, "value"), cfg["allowed_chat_ids"], cfg["api_base"], interval, state)


# -- running them ----------------------------------------------------------------------------------------

class SourceRunner:
    """Polls each adapter on its own thread at its own interval and hands what is new to `deliver`. `stop()` ends them all."""

    def __init__(self, sources: List[Source], deliver: Callable[[Source, List[SourceEvent]], None],
                 save_state: Optional[Callable[[Source], None]] = None) -> None:
        self.sources, self.deliver, self.save_state = sources, deliver, save_state
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._status: Dict[str, Dict[str, Any]] = {s.id: {"id": s.id, "kind": s.kind, "polls": 0, "events": 0, "last_ok": None, "last_error": None} for s in sources}
        self._lock = threading.Lock()

    def poll_once(self, source: Source) -> int:
        try:
            events = source.poll()
            if self.save_state:
                self.save_state(source)
            if events:
                self.deliver(source, events)
            with self._lock:
                st = self._status[source.id]
                st.update(polls=st["polls"] + 1, events=st["events"] + len(events), last_ok=time.time(), last_error=None)
            return len(events)
        except Exception as exc:
            message = redact(f"{type(exc).__name__}: {exc}" if not isinstance(exc, SourceError) else str(exc), source.secrets())
            with self._lock:
                st = self._status[source.id]
                st.update(polls=st["polls"] + 1, last_error=message[:300])
            return 0

    def _run(self, source: Source) -> None:
        while not self._stop.is_set():
            self.poll_once(source)
            if self._stop.wait(source.interval):
                return

    def start(self) -> None:
        self._stop.clear()
        for s in self.sources:
            t = threading.Thread(target=self._run, args=(s,), daemon=True, name=f"source-{s.id}")
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout)
        self._threads = []

    def status(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._status.values()]
