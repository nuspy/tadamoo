"""A stand-in for the Telegram Bot API on 127.0.0.1, for tests: it never reaches the network.

It answers `getMe`, `getUpdates` (long poll with a short wait), `sendMessage`, `editMessageText`, `sendChatAction`, `answerCallbackQuery`
and `getFile` and the file download, keeps every call it got, and can be told to fail the next calls of a method (409, 429, 401...)."""
import collections
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = "123456789:AAFakeFakeFakeFakeFakeFakeFakeFake_x"


class FakeTelegram:
    def __init__(self, token: str = TOKEN, wait: float = 0.15):
        self.token, self.wait = token, wait
        self.calls = []                          # (method, params) in the order they came
        self.updates = []
        self.failures = {}                       # method -> list of (status, body) used one per call
        self.files = {}                          # file_id -> (path, bytes)
        self.next_message = 100
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body, raw=False):
                data = body if raw else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                prefix = f"/file/bot{outer.token}/"
                if not self.path.startswith(prefix):
                    return self._send(404, {"ok": False, "error_code": 404, "description": "Not Found"})
                for path, data in outer.files.values():
                    if prefix + path == self.path:
                        return self._send(200, data, raw=True)
                self._send(404, {"ok": False, "error_code": 404, "description": "Not Found"})

            def do_POST(self):
                parts = self.path.strip("/").split("/")
                params = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if len(parts) != 2 or parts[0] != f"bot{outer.token}":
                    return self._send(404, {"ok": False, "error_code": 404, "description": "Not Found"})
                method = parts[1]
                with outer.lock:
                    outer.calls.append((method, params))
                    failing = outer.failures.get(method)
                    if failing:
                        status, body = failing.pop(0)
                        return self._send(status, body)
                if method == "getMe":
                    return self._send(200, {"ok": True, "result": {"id": 42, "is_bot": True, "username": "tadamoo_test_bot"}})
                if method == "getUpdates":
                    deadline = time.monotonic() + min(float(params.get("timeout") or 0), outer.wait)
                    while True:
                        with outer.lock:
                            offset = params.get("offset")
                            if offset == -1:
                                found = outer.updates[-1:]
                            else:
                                found = [u for u in outer.updates if offset is None or u["update_id"] >= offset]
                        if found or time.monotonic() >= deadline:
                            return self._send(200, {"ok": True, "result": found})
                        time.sleep(0.02)
                if method in ("sendMessage", "editMessageText"):
                    with outer.lock:
                        outer.next_message += 1
                        mid = params.get("message_id") or outer.next_message
                    return self._send(200, {"ok": True, "result": {"message_id": mid, "chat": {"id": params.get("chat_id")}, "text": params.get("text")}})
                if method == "getFile":
                    found = outer.files.get(params.get("file_id"))
                    if not found:
                        return self._send(400, {"ok": False, "error_code": 400, "description": "Bad Request: wrong file_id"})
                    return self._send(200, {"ok": True, "result": {"file_id": params["file_id"], "file_path": found[0], "file_size": len(found[1])}})
                return self._send(200, {"ok": True, "result": True})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.02), daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def push(self, update):
        with self.lock:
            self.updates.append(update)

    def fail(self, method, status, description="", retry_after=0):
        body = {"ok": False, "error_code": status, "description": description or f"error {status}"}
        if retry_after:
            body["parameters"] = {"retry_after": retry_after}
        self.failures.setdefault(method, []).append((status, body))

    def sent(self, method):
        with self.lock:
            return [p for m, p in self.calls if m == method]


def message(update_id, user, text=None, chat=None, **extra):
    """An update of a person writing: `user` is their id, `chat` defaults to the same (a private chat)."""
    msg = {"message_id": update_id, "date": int(time.time()), "from": {"id": user, "is_bot": False, "username": extra.pop("username", "someone"),
           "language_code": extra.pop("language_code", "en")}, "chat": {"id": chat if chat is not None else user, "type": "private"}}
    if text is not None:
        msg["text"] = text
    msg.update(extra)
    return {"update_id": update_id, "message": msg}


def press(update_id, user, data, chat=None, message_id=101):
    return {"update_id": update_id, "callback_query": {"id": f"cb{update_id}", "from": {"id": user, "is_bot": False}, "data": data,
                                                       "message": {"message_id": message_id, "chat": {"id": chat if chat is not None else user}}}}


class FakeSessions:
    """The part of the Chat's manager the bot uses, with events the test writes."""

    def __init__(self):
        self.recs, self.log, self.sent, self.answers, self.cancelled, self.modes = {}, collections.defaultdict(list), [], [], [], []
        self.n = 0

    def create(self, workspace, mode="default", **kw):
        self.n += 1
        sid = f"s{self.n}"
        self.recs[sid] = {"id": sid, "title": "", "state": "idle", "mode": mode, "telegram_chat": None}
        return dict(self.recs[sid])

    def get(self, sid):
        return dict(self.recs[sid])

    def emit(self, sid, ev):
        self.log[sid].append({**ev, "seq": len(self.log[sid]) + 1})

    def send(self, sid, text, attachments=None):
        self.sent.append((sid, text, attachments))
        turn = f"t{len(self.sent)}"
        self.emit(sid, {"event": "user", "turn": turn, "text": text})
        return {"turn": turn}

    def events(self, sid, after=0):
        return {"events": [e for e in self.log[sid] if e["seq"] > after], "next": len(self.log[sid])}

    def cancel(self, sid):
        self.cancelled.append(sid)

    def answer(self, sid, ask, value):
        self.answers.append((sid, ask, value))

    def set_mode(self, sid, mode):
        self.modes.append((sid, mode))

    def set_telegram(self, sid, chat):
        self.recs[sid]["telegram_chat"] = chat
        return dict(self.recs[sid])

    def telegram_moved(self):
        return {s: r["telegram_chat"] for s, r in self.recs.items() if r["telegram_chat"]}


class FakeAttachments:
    def __init__(self):
        self.added = []

    def add(self, sid, name, path):
        self.added.append((sid, name, Path(path).read_bytes()))
        return {"id": f"{len(self.added):012x}"}
