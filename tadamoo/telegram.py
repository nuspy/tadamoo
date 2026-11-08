"""The Telegram Bot API as the standard library reaches it, and the plain rules around it.

`BotApi` calls the HTTPS API with `urllib` (long polling, no webhook): every error is a `TelegramError` whose text never holds the token.
`Settings` is the person's choice (on or off, the allow-list of user ids, the home channel, the permission level) with the chats that
are bound to a conversation and the senders that were turned away; `Token` keeps the bot token in the credential store, never in a file
of its own. The helpers cut a long answer at the 4096-character limit, shorten a stranger's first line to something safe to keep, and
read the `.env` file that Hermes keeps its Telegram values in.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

API_BASE = "https://api.telegram.org"
TOKEN_NAME = "telegram/bot"
TOKEN_RE = re.compile(r"\d{3,15}:[A-Za-z0-9_-]{20,64}")
MAX_LEN = 4096                           # characters in one message, counted the way Telegram counts them (UTF-16 units)
MAX_REPLY_BYTES = 8_000_000
MODES = ("plan", "read_only", "default", "auto")      # the permission levels of the Chat (serve/agent_runs.py)
ALLOWED_MAX = 50
PAIRING_MAX = 50
LINE_CHARS = 60
USERNAME_RE = re.compile(r"[A-Za-z0-9_]{3,32}")
FILE_PATH_RE = re.compile(r"[A-Za-z0-9_./-]{1,300}")
ID_LIMIT = 1 << 52


class TelegramError(Exception):
    """A call that failed. `kind` is conflict, auth, rate, forbidden, bad_request, server or network; `code` is the HTTP status or 0."""

    def __init__(self, kind: str, message: str, code: int = 0, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.kind, self.code, self.retry_after = kind, code, retry_after


def _kind(code: int) -> str:
    return {409: "conflict", 401: "auth", 404: "auth", 429: "rate", 403: "forbidden", 400: "bad_request"}.get(code, "server" if code >= 500 else "bad_request")


class BotApi:
    def __init__(self, token: str, base: str = API_BASE) -> None:
        if not TOKEN_RE.fullmatch(token or ""):
            raise ValueError("that is not the shape of a bot token")
        self._token, self.base = token, base.rstrip("/")

    def _scrub(self, text: Any) -> str:
        return " ".join(str(text).replace(self._token, "***").split())[:200]

    def _open(self, url: str, body: Optional[bytes], timeout: float, limit: int) -> Tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, headers={"User-Agent": "TaDaMoo/1", **({"Content-Type": "application/json"} if body is not None else {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.status, resp.read(limit + 1)[:limit]
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read(limit)
            except OSError:
                return exc.code, b""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TelegramError("network", "could not be reached: " + self._scrub(getattr(exc, "reason", exc))) from None

    def call(self, method: str, params: Optional[Mapping[str, Any]] = None, timeout: float = 30.0) -> Any:
        status, raw = self._open(f"{self.base}/bot{self._token}/{method}", json.dumps(params or {}).encode("utf-8"), timeout, MAX_REPLY_BYTES)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("ok") is True and status == 200:
            return data.get("result")
        info = data if isinstance(data, dict) else {}
        code = int(info.get("error_code") or status or 0)
        wait = (info.get("parameters") or {}).get("retry_after") if isinstance(info.get("parameters"), dict) else 0
        raise TelegramError(_kind(code), self._scrub(info.get("description") or f"HTTP {status}"), code, float(wait or 0))

    def download(self, file_id: str, max_bytes: int, timeout: float = 60.0) -> Tuple[bytes, str]:
        """A file a person sent: its bytes and its name on Telegram's side; refused when it is larger than `max_bytes`."""
        found = self.call("getFile", {"file_id": file_id}, timeout=timeout)
        path = str((found or {}).get("file_path") or "")
        if not FILE_PATH_RE.fullmatch(path) or ".." in path:
            raise TelegramError("bad_request", "the file has no usable path")
        if int((found or {}).get("file_size") or 0) > max_bytes:
            raise TelegramError("bad_request", "the file is larger than the limit", 0)
        status, raw = self._open(f"{self.base}/file/bot{self._token}/{path}", None, timeout, max_bytes)
        if status != 200:
            raise TelegramError(_kind(status), f"the file could not be fetched (HTTP {status})", status)
        return raw, path.rsplit("/", 1)[-1]


# -- text -------------------------------------------------------------------------------------------------------


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = MAX_LEN) -> List[str]:
    """The pieces an answer is sent in: none longer than `limit`, cut at a line or a space near the end where there is one. A piece depends only
    on the text before its cut, so while an answer grows the earlier pieces do not change."""
    text = text.strip()
    if not text:
        return []
    out: List[str] = []
    while utf16_len(text) > limit:
        cut, units = 0, 0
        for ch in text:
            step = 2 if ord(ch) > 0xFFFF else 1
            if units + step > limit:
                break
            units, cut = units + step, cut + 1
        window = text[:cut]
        floor = int(cut * 0.7)
        at = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if at >= floor:
            cut = at
        piece = text[:cut].rstrip()
        out.append(piece or text[:cut])
        text = text[cut:].lstrip("\n ") if piece else text[cut:]
    if text.strip():
        out.append(text.rstrip())
    return out


def clean_line(text: Any, limit: int = LINE_CHARS) -> str:
    """The first line of a stranger's message made safe to keep and to show: printable characters only, one space between words, short."""
    first = next((ln for ln in str(text or "").splitlines() if ln.strip()), "")
    first = "".join(ch if ch.isprintable() else " " for ch in first)
    first = re.sub(r"<[^>]*>", " ", first)
    first = " ".join(first.split())
    return first[:limit]


def clean_username(value: Any) -> str:
    return value if isinstance(value, str) and USERNAME_RE.fullmatch(value) else ""


def check_id(value: Any, *, user: bool = False) -> int:
    """A chat or user id: a whole number; a user's is positive. Text of digits is taken (the page and a .env file send text)."""
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,16}", value.strip()):
        value = int(value.strip())
    if not isinstance(value, int) or isinstance(value, bool) or value == 0 or abs(value) > ID_LIMIT or (user and value < 0):
        raise ValueError("an id is a whole number" + (" above zero" if user else ""))
    return value


def parse_ids(value: Any) -> Tuple[List[int], int]:
    """The user ids in a comma-separated list, and how many items were not ids (names are not taken: the id is what Telegram checks)."""
    ids: List[int] = []
    skipped = 0
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            one = check_id(item, user=True)
        except ValueError:
            skipped += 1
            continue
        if one not in ids:
            ids.append(one)
    return ids, skipped


# -- Hermes' .env ---------------------------------------------------------------------------------------------------


def parse_dotenv(text: str) -> Dict[str, str]:
    """`KEY=value` lines of a dotenv file: `export` allowed, single or double quotes, a ` #` ends an unquoted value, comment and empty lines skipped."""
    out: Dict[str, str] = {}
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, eq, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not eq or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if value[:1] in ("'", '"') and value.find(value[0], 1) > 0:
            value = value[1:value.find(value[0], 1)]
        else:
            value = re.split(r"\s#", value, maxsplit=1)[0].strip()
        out[key] = value
    return out


def hermes_env_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get("TADAMOO_HERMES_ENV"):
        return Path(env["TADAMOO_HERMES_ENV"])
    if env.get("LOCALAPPDATA"):
        return Path(env["LOCALAPPDATA"]) / "hermes" / ".env"
    return Path.home() / ".hermes" / ".env"


def read_hermes(path: Path) -> Dict[str, Any]:
    """What Hermes keeps for its Telegram gateway: `present` (the file is there), the `token` ("" when it holds none), the user ids that may talk
    to it, how many entries were not ids, and the home channel (0 when none). The file is only read."""
    try:
        values = parse_dotenv(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {"present": False, "token": "", "allowed": [], "skipped": 0, "home": 0}
    ids, skipped = parse_ids(values.get("TELEGRAM_ALLOWED_USERS"))
    try:
        home = check_id(values.get("TELEGRAM_HOME_CHANNEL"))
    except ValueError:
        home = 0
    return {"present": True, "token": values.get("TELEGRAM_BOT_TOKEN", ""), "allowed": ids, "skipped": skipped, "home": home}


# -- the token ------------------------------------------------------------------------------------------------------


class Token:
    """The bot token, in the credential store under `telegram/bot`. The page is told whether one is set, never what it is."""

    def __init__(self, store: Any = None, folder: Optional[Path] = None) -> None:
        if store is None:
            from tadamoo.llm.credentials import default_store
            store = default_store(folder)
        self.store = store

    def get(self) -> str:
        try:
            return self.store.read(TOKEN_NAME) or ""
        except Exception:                                  # a store that cannot be read has no token
            return ""

    def has(self) -> bool:
        return bool(self.get())

    def set(self, token: Any) -> None:
        token = str(token or "").strip()
        if not token:
            self.store.delete(TOKEN_NAME)
            return
        if not TOKEN_RE.fullmatch(token):
            raise ValueError("a bot token is digits, a colon and 20 to 64 letters, digits, dashes or underscores")
        self.store.write(TOKEN_NAME, token)


# -- the settings ---------------------------------------------------------------------------------------------------


class Settings:
    """`telegram.json`: what the person chose, the chat of each conversation and the senders turned away. Replaced whole on every change."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
        allowed = [i for i in raw.get("allowed", []) if isinstance(i, int) and not isinstance(i, bool) and i > 0][:ALLOWED_MAX] if isinstance(raw.get("allowed"), list) else []
        home = raw.get("home") if isinstance(raw.get("home"), int) and not isinstance(raw.get("home"), bool) else 0
        chats = {k: v for k, v in raw.get("chats", {}).items() if isinstance(k, str) and isinstance(v, str)} if isinstance(raw.get("chats"), dict) else {}
        names = {k: v for k, v in raw.get("names", {}).items() if isinstance(k, str) and clean_username(v)} if isinstance(raw.get("names"), dict) else {}
        pairing = [p for p in raw.get("pairing", []) if isinstance(p, dict) and isinstance(p.get("id"), int)][:PAIRING_MAX] if isinstance(raw.get("pairing"), list) else []
        return {"enabled": raw.get("enabled") is True, "allowed": allowed, "home": home, "mode": raw.get("mode") if raw.get("mode") in MODES else "default",
                "chats": chats, "names": names, "pairing": pairing}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".telegram.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self.path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    # -- the choice

    def read(self) -> Dict[str, Any]:
        with self._lock:
            return {"enabled": self._data["enabled"], "allowed": list(self._data["allowed"]), "home": self._data["home"], "mode": self._data["mode"]}

    def write(self, **changes: Any) -> Dict[str, Any]:
        with self._lock:
            new = dict(self._data)
            if "enabled" in changes:
                if not isinstance(changes["enabled"], bool):
                    raise ValueError("on or off")
                new["enabled"] = changes["enabled"]
            if "mode" in changes:
                if changes["mode"] not in MODES:
                    raise ValueError("the permission level is one of: " + ", ".join(MODES))
                new["mode"] = changes["mode"]
            if "home" in changes:
                new["home"] = 0 if changes["home"] in (None, "", 0) else check_id(changes["home"])
            if "allowed" in changes:
                if not isinstance(changes["allowed"], list):
                    raise ValueError("the allow-list is a list of ids")
                ids = [check_id(i, user=True) for i in changes["allowed"]]
                new["allowed"] = list(dict.fromkeys(ids))
                if len(new["allowed"]) > ALLOWED_MAX:
                    raise ValueError(f"at most {ALLOWED_MAX} people")
            self._data = new
            self._save()
            return self.read()

    def is_allowed(self, user: Any) -> bool:
        with self._lock:
            return isinstance(user, int) and not isinstance(user, bool) and user in self._data["allowed"]

    def allow(self, user: Any, name: str = "") -> None:
        user = check_id(user, user=True)
        with self._lock:
            if user not in self._data["allowed"]:
                if len(self._data["allowed"]) >= ALLOWED_MAX:
                    raise ValueError(f"at most {ALLOWED_MAX} people")
                self._data["allowed"].append(user)
            if clean_username(name):
                self._data["names"][str(user)] = name
            self._data["pairing"] = [p for p in self._data["pairing"] if p["id"] != user]
            self._save()

    def disallow(self, user: Any) -> None:
        user = check_id(user, user=True)
        with self._lock:
            self._data["allowed"] = [i for i in self._data["allowed"] if i != user]
            self._data["chats"] = {c: s for c, s in self._data["chats"].items() if c != str(user)}
            self._save()

    def note_name(self, user: int, name: str) -> None:
        """Remembers the user name of an allowed person, to label their chat."""
        with self._lock:
            if clean_username(name) and self._data["names"].get(str(user)) != name:
                self._data["names"][str(user)] = name
                self._save()

    def name_of(self, user: int) -> str:
        with self._lock:
            return self._data["names"].get(str(user), "")

    # -- the chats of the conversations

    def chats(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._data["chats"])

    def bind(self, chat: int, sid: str) -> None:
        with self._lock:
            if self._data["chats"].get(str(chat)) != sid:
                self._data["chats"][str(chat)] = sid
                self._save()

    # -- the senders turned away

    def see_stranger(self, user: int, username: str, line: str, now: float) -> bool:
        """Notes a sender who is not allowed: their id, user name and a short first line, never more. True the first time and then once in a while
        (so a flood does not fill the log or the disk)."""
        with self._lock:
            row = next((p for p in self._data["pairing"] if p["id"] == user), None)
            if row is None:
                row = {"id": user, "username": clean_username(username), "line": clean_line(line), "first": now, "last": 0.0, "count": 0}
                self._data["pairing"].append(row)
                del self._data["pairing"][:-PAIRING_MAX]
            row["count"] = int(row.get("count", 0)) + 1
            fresh = now - float(row.get("last") or 0.0) >= 60.0
            if fresh:
                row.update(last=now, line=clean_line(line) or row.get("line", ""), username=clean_username(username) or row.get("username", ""))
                self._save()
            return fresh

    def pairing(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(p) for p in self._data["pairing"]]

    def authorise(self, user: Any) -> None:
        user = check_id(user, user=True)
        with self._lock:
            row = next((p for p in self._data["pairing"] if p["id"] == user), None)
            if row is None:
                raise KeyError(user)
            self.allow(user, row.get("username", ""))

    def dismiss(self, user: Any) -> None:
        user = check_id(user, user=True)
        with self._lock:
            self._data["pairing"] = [p for p in self._data["pairing"] if p["id"] != user]
            self._save()


class Offset:
    """The id of the next update to ask for, kept in a file so a restart takes up where it stopped."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def get(self) -> Optional[int]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8")).get("offset")
        except (OSError, ValueError, AttributeError):
            return None
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def set(self, value: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"offset": int(value), "at": time.time()}), encoding="utf-8")
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
