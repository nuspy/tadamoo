"""The Telegram bot of TaDaMoo: the thread that listens, the settings the page edits and the move of a conversation to a chat.

It listens to one bot by long polling (`getUpdates`), from the start of the server when the person turned it on and a token is kept, and
hands each update to `tadamoo/telegram_bot.py`. Only one program may read a bot's updates at a time: when Telegram says another one does
(Hermes' own gateway with the same token), the status says so plainly and the poll waits and tries again, never ends. The token is kept in
the credential store; no route, log line or error text holds it. The permission level is the server's setting for Telegram (`levels`): a level
found in telegram.json is handed to it once, when Telegram has none there yet.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from serve.voice_stt import VoiceSpeech
from tadamoo import telegram as tg
from tadamoo.permission_level import LEVELS, PermissionLevels
from tadamoo.telegram import BotApi, TelegramError
from tadamoo.telegram_bot import TelegramBot

POLL_S = 25.0
RELAY_S = 0.5
WAITS = (2.0, 5.0, 15.0, 30.0, 60.0)
CONFLICT_WAIT_S = 30.0
AUTH_WAIT_S = 300.0
RATE_WAIT_MAX_S = 300.0
STATES = ("off", "no_token", "starting", "running", "conflict", "bad_token", "error")
CONFLICT_TEXT = "Another program reads this bot's messages (for example the Hermes Telegram gateway). Switch it off there, or give TaDaMoo a bot of its own."


class TelegramServiceError(ValueError):
    def __init__(self, message: str, code: str = "bad_request", status: int = 400) -> None:
        super().__init__(message)
        self.code, self.status = code, status


class TelegramService:
    def __init__(self, settings: tg.Settings, token: tg.Token, sessions: Callable[[], Any], *, levels: PermissionLevels, attachments_dir: Path, state_dir: Path,
                 api_base: str = tg.API_BASE, poll_s: float = POLL_S, relay_s: float = RELAY_S, hermes_env: Optional[Callable[[], Path]] = None, speech: Any = None) -> None:
        self.settings, self.token, self._sessions, self.levels = settings, token, sessions, levels
        self.speech = speech if speech is not None else VoiceSpeech()
        levels.adopt("telegram", settings.take_mode())
        self.attachments_dir, self.state_dir, self.api_base = Path(attachments_dir), Path(state_dir), api_base
        self.poll_s, self.relay_s = poll_s, relay_s
        self._hermes_env = hermes_env or tg.hermes_env_path
        self.offset = tg.Offset(self.state_dir / "telegram_offset.json")
        self._lock = threading.RLock()
        self._stop: Optional[threading.Event] = None
        self._bot: Optional[TelegramBot] = None
        self._threads: List[threading.Thread] = []
        self._state, self._message, self._username = "off", "", ""

    # -- logging and status

    def log(self, line: str) -> None:
        secret = self.token.get()
        text = line.replace(secret, "***") if secret else line
        print(f"[telegram] {text}", file=sys.stderr, flush=True)

    def _set(self, state: str, message: str = "") -> None:
        with self._lock:
            self._state, self._message = state, message

    def targets(self) -> List[Dict[str, Any]]:
        """The chats a conversation may be moved to: the allowed people's own chats, the chats they wrote from, and the home channel."""
        seen: Dict[int, Dict[str, Any]] = {}
        for user in self.settings.read()["allowed"]:
            name = self.settings.name_of(user)
            seen[user] = {"id": user, "label": f"@{name}" if name else str(user), "kind": "private"}
        for chat in self.settings.chats():
            seen.setdefault(int(chat), {"id": int(chat), "label": chat, "kind": "group"})
        home = self.settings.read()["home"]
        if home:
            seen.setdefault(home, {"id": home, "label": str(home), "kind": "home"})
        return list(seen.values())

    def overview(self) -> Dict[str, Any]:
        s = self.settings.read()
        with self._lock:
            state, message, username = self._state, self._message, self._username
        targets = self.targets()
        return {"configured": self.token.has(), "enabled": s["enabled"], "state": state, "message": message, "bot": username, "home": s["home"] or "",
                "mode": self.levels.get("telegram"), "mode_set": self.levels.is_set("telegram"), "modes": list(LEVELS), "allowed": [{"id": i, "name": self.settings.name_of(i)} for i in s["allowed"]],
                "pairing": [{"id": p["id"], "username": p.get("username", ""), "line": p.get("line", ""), "count": p.get("count", 0)} for p in self.settings.pairing()],
                "targets": targets, "can_move": state == "running" and bool(targets)}

    # -- lifecycle

    def start_if_enabled(self) -> None:
        try:
            self.start()
        except Exception as exc:                                  # the bot never keeps the server from starting
            self._set("error", type(exc).__name__)

    def start(self) -> None:
        with self._lock:
            self.stop()
            enabled, token = self.settings.read()["enabled"], self.token.get()
            if not enabled:
                return self._set("off")
            if not token:
                return self._set("no_token")
            api = BotApi(token, self.api_base)
            self._bot = bot = TelegramBot(api, self.settings, self._sessions(), self._attachments(), levels=self.levels, tmp_dir=self.state_dir / "telegram-incoming", speech=self.speech, log=self.log)
            self._stop = stop = threading.Event()
            self._set("starting")
            self._threads = [threading.Thread(target=self._poll, args=(stop, api, bot), name="telegram-poll", daemon=True),
                             threading.Thread(target=self._relay, args=(stop, bot), name="telegram-relay", daemon=True)]
            for t in self._threads:
                t.start()

    def stop(self) -> None:
        with self._lock:
            if self._stop is not None:
                self._stop.set()
            threads, self._threads, self._stop, self._bot = self._threads, [], None, None
            self._username = ""
            self._set("off")
        for t in threads:
            if t is not threading.current_thread() and t.name == "telegram-relay":
                t.join(2.0)                                       # the poll may sit in a long request: it ends by itself

    def _attachments(self) -> Any:
        from tadamoo.attachments import AttachmentStore
        return AttachmentStore(self.attachments_dir)

    def _live(self, stop: threading.Event) -> bool:
        return self._stop is stop and not stop.is_set()

    def _relay(self, stop: threading.Event, bot: TelegramBot) -> None:
        while not stop.wait(self.relay_s):
            try:
                bot.pump()
            except Exception as exc:
                self.log(f"the relay failed once: {type(exc).__name__}")

    def _poll(self, stop: threading.Event, api: BotApi, bot: TelegramBot) -> None:
        failures, known = 0, False
        while self._live(stop):
            try:
                if not known:
                    self._username = str((api.call("getMe", timeout=15) or {}).get("username") or "")
                    self._skip_backlog(api)
                    known = True
                    self._set("running")
                params: Dict[str, Any] = {"timeout": int(self.poll_s), "allowed_updates": ["message", "callback_query"]}
                offset = self.offset.get()
                if offset:
                    params["offset"] = offset
                updates = api.call("getUpdates", params, timeout=self.poll_s + 15)
                if not self._live(stop):
                    return
                self._set("running")
                failures = 0
                for update in updates if isinstance(updates, list) else []:
                    self._handle(bot, update)
            except TelegramError as exc:
                if not self._live(stop):
                    return
                wait = self._failed(exc, failures)
                failures += 1
                if stop.wait(wait):
                    return
            except Exception as exc:                              # one bad round does not end the listener
                self.log(f"a poll round failed: {type(exc).__name__}")
                self._set("error", type(exc).__name__)
                if stop.wait(WAITS[min(failures, len(WAITS) - 1)]):
                    return
                failures += 1

    def _failed(self, exc: TelegramError, failures: int) -> float:
        """The status for a failed call and how long to wait before the next."""
        if exc.kind == "conflict":
            self._set("conflict", CONFLICT_TEXT)
            return CONFLICT_WAIT_S
        if exc.kind == "auth":
            self._set("bad_token", str(exc))
            return AUTH_WAIT_S
        if exc.kind == "rate":
            return min(max(exc.retry_after, 1.0), RATE_WAIT_MAX_S)
        self._set("error", str(exc))
        return WAITS[min(failures, len(WAITS) - 1)]

    def _skip_backlog(self, api: BotApi) -> None:
        """The first time: what was sent while nobody listened is not acted on, and the next poll starts after it."""
        if self.offset.get() is not None:
            return
        last = api.call("getUpdates", {"offset": -1, "timeout": 0}, timeout=15)
        self.offset.set(int(last[-1]["update_id"]) + 1 if isinstance(last, list) and last else 0)

    def _handle(self, bot: TelegramBot, update: Any) -> None:
        if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
            return
        try:
            bot.handle_update(update)
        except Exception as exc:                                  # an update that fails is not asked for again
            self.log(f"an update could not be handled: {type(exc).__name__}")
        self.offset.set(update["update_id"] + 1)

    # -- what the page does

    def set_settings(self, body: Any) -> Dict[str, Any]:
        if not isinstance(body, dict):
            raise TelegramServiceError("send an object")
        unknown = sorted(k for k in body if k not in ("enabled", "home", "allowed"))
        if unknown:
            raise TelegramServiceError("unknown setting: " + ", ".join(map(str, unknown)), "unknown_field")
        changes = {k: body[k] for k in ("enabled", "home", "allowed") if k in body}
        if not changes:
            raise TelegramServiceError("say what to change: enabled, home or allowed")
        before = self.settings.read()
        try:
            after = self.settings.write(**changes)
        except ValueError as exc:
            raise TelegramServiceError(str(exc)) from None
        if after["enabled"] != before["enabled"]:
            self.start()
        return self.overview()

    def own_sessions(self) -> List[str]:
        """The chats' own conversations, which follow Telegram's level; a conversation moved here from the page is not one of them."""
        return sorted(set(self.settings.chats().values()) - set(self._sessions().telegram_moved()))

    def set_token(self, value: Any) -> Dict[str, Any]:
        try:
            self.token.set(value)
        except ValueError as exc:
            raise TelegramServiceError(str(exc)) from None
        self.start()
        return self.overview()

    def allow(self, body: Any) -> Dict[str, Any]:
        body = body if isinstance(body, dict) else {}
        try:
            if "add" in body:
                self.settings.allow(body["add"])
            elif "remove" in body:
                self.settings.disallow(body["remove"])
            else:
                raise ValueError("say add or remove with an id")
        except ValueError as exc:
            raise TelegramServiceError(str(exc)) from None
        return self.overview()

    def pairing(self, body: Any) -> Dict[str, Any]:
        body = body if isinstance(body, dict) else {}
        try:
            if "authorise" in body:
                self.settings.authorise(body["authorise"])
            elif "dismiss" in body:
                self.settings.dismiss(body["dismiss"])
            else:
                raise ValueError("say authorise or dismiss with an id")
        except KeyError:
            raise TelegramServiceError("that sender is not in the list", "not_found", 404) from None
        except ValueError as exc:
            raise TelegramServiceError(str(exc)) from None
        return self.overview()

    def import_hermes(self) -> Dict[str, Any]:
        """Reads Hermes' `.env` (only reads it): the token goes to the credential store, the user ids join the allow-list, the home channel is set.
        The answer says which values were found, never what they are."""
        found = tg.read_hermes(self._hermes_env())
        report = {"file": found["present"], "token": bool(found["token"]), "token_valid": False, "allowed": len(found["allowed"]), "skipped": found["skipped"], "home": bool(found["home"])}
        if found["present"]:
            if found["token"]:
                try:
                    self.token.set(found["token"])
                    report["token_valid"] = True
                except ValueError:
                    pass
            for user in found["allowed"]:
                try:
                    self.settings.allow(user)
                except ValueError:
                    break
            if found["home"]:
                self.settings.write(home=found["home"])
            if report["token_valid"]:
                self.start()
        return {"imported": report, **self.overview()}

    def test(self) -> Dict[str, Any]:
        token = self.token.get()
        if not token:
            return {"ok": False, "message": "no token is set"}
        try:
            me = BotApi(token, self.api_base).call("getMe", timeout=15) or {}
        except TelegramError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "bot": str(me.get("username") or "")}

    def send_test(self, text: Any = "") -> Dict[str, Any]:
        token, home = self.token.get(), self.settings.read()["home"]
        if not token:
            return {"ok": False, "message": "no token is set"}
        if not home:
            return {"ok": False, "message": "no home channel is set"}
        words = str(text or "").strip()[:500] or "TaDaMoo test message."
        try:
            BotApi(token, self.api_base).call("sendMessage", {"chat_id": home, "text": words}, timeout=15)
        except TelegramError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    def move(self, sid: str, chat: Any) -> Dict[str, Any]:
        """Moves the conversation to a chat of an allowed person (told there first, so a chat Telegram refuses is not recorded), or brings it back."""
        mgr = self._sessions()
        if chat is None:
            before = mgr.get(sid).get("telegram_chat")
            rec = mgr.set_telegram(sid, None)
            bot = self._bot
            if bot is not None and before:
                try:
                    bot.announce(sid, before, False)
                except TelegramError:
                    pass
            return rec
        try:
            chat = tg.check_id(chat)
        except ValueError as exc:
            raise TelegramServiceError(str(exc), "bad_chat") from None
        bot = self._bot
        if bot is None or self._state != "running":
            raise TelegramServiceError("the Telegram bot is not running", "not_running", 409)
        if chat not in [t["id"] for t in self.targets()]:
            raise TelegramServiceError("that chat is not one of an allowed person", "bad_chat")
        try:
            bot.announce(sid, chat, True)
        except TelegramError as exc:
            raise TelegramServiceError(f"Telegram refused: {exc}", "telegram", 502) from None
        rec = mgr.set_telegram(sid, chat)
        bot.track(sid)
        return rec
