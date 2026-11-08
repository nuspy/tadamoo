"""The Telegram bot's service and routes against a stand-in Bot API: the listener (offset, conflict, rate limit, bad token), the settings, the import from Hermes, the move of a conversation."""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from fake_telegram import FakeSessions, FakeTelegram, TOKEN, message, press  # noqa: E402

from serve import handler, telegram_service as ts  # noqa: E402
from serve.agent_sessions import SessionManager  # noqa: E402
from serve.engine.mock import MockEngine  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402
from tadamoo import telegram as tg  # noqa: E402
from tadamoo.llm.credentials import FileCredentials  # noqa: E402

STUB = str(ROOT / "serve" / "tests" / "session_stub.py")
ALLOWED = 111


def until(predicate, seconds=10, step=0.02):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out waiting")


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(ts, "CONFLICT_WAIT_S", 0.1)
    monkeypatch.setattr(ts, "AUTH_WAIT_S", 0.3)
    monkeypatch.setattr(ts, "WAITS", (0.05, 0.1))


@pytest.fixture
def rig(tmp_path):
    fake = FakeTelegram()
    store = FileCredentials(tmp_path / "cred.json")
    sessions = FakeSessions()
    settings = tg.Settings(tmp_path / "telegram.json")
    token = tg.Token(store)
    token.set(TOKEN)
    settings.write(allowed=[ALLOWED], home=ALLOWED, enabled=True)
    hermes = tmp_path / "hermes.env"
    svc = ts.TelegramService(settings, token, lambda: sessions, attachments_dir=tmp_path / "att", state_dir=tmp_path / "state", api_base=fake.base,
                             poll_s=0.1, relay_s=0.02, hermes_env=lambda: hermes)

    class Rig:
        pass
    r = Rig()
    r.fake, r.svc, r.sessions, r.settings, r.token, r.hermes, r.store = fake, svc, sessions, settings, token, hermes, store
    yield r
    svc.stop()
    fake.close()


def push(r, n, text, uid=ALLOWED):
    r.fake.push(message(n, uid, text))


# -- the listener ---------------------------------------------------------------------------------------------------


def test_the_listener_runs_with_a_token_and_handles_what_arrives_and_remembers_where_it_stopped(rig):
    rig.svc.start()
    until(lambda: rig.svc.overview()["state"] == "running" and rig.svc.overview()["bot"] == "tadamoo_test_bot")
    push(rig, 1, "hello bot")
    until(lambda: rig.sessions.sent)
    assert rig.sessions.sent[0][1] == "hello bot"
    until(lambda: rig.svc.offset.get() == 2)
    rig.svc.stop()
    assert rig.svc.overview()["state"] == "off"
    push(rig, 2, "while nobody listens")
    rig.svc.start()
    until(lambda: len(rig.sessions.sent) == 2)
    assert [t for _, t, _ in rig.sessions.sent] == ["hello bot", "while nobody listens"]       # taken up where it stopped, nothing twice
    assert {p.get("offset") for p in rig.fake.sent("getUpdates") if p.get("offset") not in (None, -1)} <= {2, 3}


def test_what_waited_while_nobody_listened_is_not_acted_on_the_first_time(rig):
    for n in (1, 2, 3):
        push(rig, n, "old order")
    rig.svc.start()
    until(lambda: rig.svc.offset.get() == 4)
    push(rig, 4, "new order")
    until(lambda: rig.sessions.sent)
    assert [t for _, t, _ in rig.sessions.sent] == ["new order"]


def test_a_conflict_with_another_reader_is_shown_plainly_and_the_bot_goes_on(rig):
    for _ in range(3):
        rig.fake.fail("getUpdates", 409, "Conflict: terminated by other getUpdates request; make sure that only one bot instance is running")
    rig.svc.start()
    seen = until(lambda: rig.svc.overview() if rig.svc.overview()["state"] == "conflict" else None)
    assert "Hermes" in seen["message"] and "Another program" in seen["message"]
    until(lambda: rig.svc.overview()["state"] == "running")                     # it never gave up
    push(rig, 1, "after the conflict")
    until(lambda: rig.sessions.sent)


def test_a_rate_limit_on_the_poll_waits_the_time_telegram_asks_and_goes_on(rig):
    rig.fake.fail("getUpdates", 429, "Too Many Requests: retry after 1", retry_after=1)
    rig.svc.start()
    until(lambda: rig.svc.overview()["state"] == "running")
    started = time.monotonic()
    push(rig, 1, "after the wait")
    until(lambda: rig.sessions.sent)
    assert any(p.get("timeout") is not None for p in rig.fake.sent("getUpdates"))
    assert time.monotonic() - started < 6


def test_a_refused_token_is_shown_and_the_message_does_not_hold_it(rig):
    rig.fake.fail("getMe", 401, f"Unauthorized {TOKEN}")
    rig.svc.start()
    seen = until(lambda: rig.svc.overview() if rig.svc.overview()["state"] == "bad_token" else None)
    assert TOKEN not in json.dumps(seen) and "***" in seen["message"]


def test_a_network_failure_is_an_error_status_and_the_bot_tries_again(rig):
    rig.fake.fail("getMe", 502, "Bad Gateway")
    rig.svc.start()
    until(lambda: rig.svc.overview()["state"] == "error")
    until(lambda: rig.svc.overview()["state"] == "running")


def test_nothing_listens_without_a_token_or_when_switched_off(rig):
    rig.settings.write(enabled=False)
    rig.svc.start()
    assert rig.svc.overview()["state"] == "off" and rig.fake.calls == []
    rig.settings.write(enabled=True)
    rig.token.set("")
    rig.svc.start()
    assert rig.svc.overview()["state"] == "no_token" and rig.fake.calls == []


def test_an_update_that_fails_is_not_asked_for_again(rig, capsys):
    rig.sessions.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    rig.svc.start()
    until(lambda: rig.svc.overview()["state"] == "running")
    push(rig, 1, "poison")
    until(lambda: rig.svc.offset.get() == 2)
    push(rig, 2, "next")
    until(lambda: rig.svc.offset.get() == 3)
    assert rig.svc.overview()["state"] == "running"


def test_the_answers_of_a_conversation_reach_the_chat_through_the_relay(rig):
    rig.svc.start()
    until(lambda: rig.svc.overview()["state"] == "running")
    push(rig, 1, "question")
    until(lambda: rig.sessions.sent)
    rig.sessions.emit("s1", {"event": "agent", "turn": "t1", "data": {"type": "assistant", "content": "the answer"}})
    rig.sessions.emit("s1", {"event": "done", "turn": "t1", "final": "the answer"})
    until(lambda: any(p["text"] == "the answer" for p in rig.fake.sent("sendMessage")))


# -- the token never leaves ---------------------------------------------------------------------------------------


def test_the_token_is_in_no_answer_and_no_log_line(rig, capsys):
    rig.fake.fail("getMe", 400, f"Bad Request near {TOKEN}")
    rig.fake.fail("sendMessage", 400, f"Bad Request near {TOKEN}")
    seen = [rig.svc.overview(), rig.svc.set_settings({"mode": "auto"}), rig.svc.test(), rig.svc.send_test("x"), rig.svc.import_hermes()]
    rig.svc.start()
    until(lambda: rig.svc.overview()["state"] in ("running", "error"))
    seen.append(rig.svc.overview())
    rig.svc.log(f"a line that holds {TOKEN}")
    rig.svc.stop()
    out = capsys.readouterr()
    for blob in [json.dumps(s) for s in seen] + [out.out, out.err]:
        assert TOKEN not in blob and TOKEN.split(":")[1] not in blob
    assert "***" in out.err
    assert rig.svc.overview()["configured"] is True


def test_the_token_lives_in_the_credential_store_and_nowhere_else(rig, tmp_path):
    assert rig.store.read(tg.TOKEN_NAME) == TOKEN
    for f in tmp_path.rglob("*"):
        if f.is_file() and f.name != "cred.json":
            assert TOKEN not in f.read_text(encoding="utf-8", errors="ignore"), f
    rig.svc.set_token("")
    assert rig.store.read(tg.TOKEN_NAME) == "" and rig.svc.overview()["configured"] is False


def test_only_a_token_of_the_right_shape_is_taken(rig):
    for bad in ("hello", "12345:short", "abc:" + "x" * 30, "1234567890:" + "x" * 30 + " y"):
        with pytest.raises(ts.TelegramServiceError):
            rig.svc.set_token(bad)
    assert rig.token.get() == TOKEN


# -- settings -------------------------------------------------------------------------------------------------------


def test_the_settings_are_checked_and_kept(rig):
    out = rig.svc.set_settings({"mode": "read_only", "home": "-1001234", "allowed": ["111", 222]})
    assert out["mode"] == "read_only" and out["home"] == -1001234 and [a["id"] for a in out["allowed"]] == [111, 222]
    for bad in ({"mode": "root"}, {"enabled": "yes"}, {"allowed": [0]}, {"allowed": ["x"]}, {"allowed": [-5]}, {"home": "abc"}, {}, {"unknown": 1}, "text"):
        with pytest.raises(ts.TelegramServiceError):
            rig.svc.set_settings(bad)
    again = tg.Settings(rig.settings.path)
    assert again.read()["mode"] == "read_only" and again.read()["allowed"] == [111, 222]


def test_the_switch_starts_and_stops_the_listener_and_the_level_reaches_the_chats_conversation(rig):
    rig.svc.set_settings({"enabled": False})
    assert rig.svc.overview()["state"] == "off"
    rig.svc.set_settings({"enabled": True})
    until(lambda: rig.svc.overview()["state"] == "running")
    push(rig, 1, "hi")
    until(lambda: rig.sessions.sent)
    rig.svc.set_settings({"mode": "plan"})
    assert rig.sessions.modes == [("s1", "plan")]
    assert tg.MODES == __import__("serve.agent_runs", fromlist=["MODES"]).MODES


def test_the_allow_list_can_grow_and_shrink_and_the_pairing_list_authorises(rig):
    rig.svc.allow({"add": "222"})
    assert [a["id"] for a in rig.svc.overview()["allowed"]] == [111, 222]
    rig.svc.allow({"remove": 111})
    assert [a["id"] for a in rig.svc.overview()["allowed"]] == [222]
    for bad in ({}, {"add": "abc"}, {"add": -1}, {"remove": 0}):
        with pytest.raises(ts.TelegramServiceError):
            rig.svc.allow(bad)
    rig.settings.see_stranger(555, "newbie", "hello there", 1000.0)
    row = rig.svc.overview()["pairing"][0]
    assert row == {"id": 555, "username": "newbie", "line": "hello there", "count": 1}
    rig.svc.pairing({"authorise": 555})
    assert [a["id"] for a in rig.svc.overview()["allowed"]] == [222, 555] and rig.svc.overview()["pairing"] == []
    rig.settings.see_stranger(666, "", "x", 1000.0)
    rig.svc.pairing({"dismiss": 666})
    assert rig.svc.overview()["pairing"] == []
    with pytest.raises(ts.TelegramServiceError) as e:
        rig.svc.pairing({"authorise": 777})
    assert e.value.status == 404


def test_test_and_send_test_go_through_the_bot_api(rig):
    assert rig.svc.test() == {"ok": True, "bot": "tadamoo_test_bot"}
    assert rig.svc.send_test("hello home") == {"ok": True}
    assert rig.fake.sent("sendMessage")[-1] == {"chat_id": ALLOWED, "text": "hello home"}
    rig.fake.fail("getMe", 401, "Unauthorized")
    assert rig.svc.test()["ok"] is False
    rig.settings.write(home="")
    assert rig.svc.send_test()["message"] == "no home channel is set"
    rig.token.set("")
    assert rig.svc.test() == {"ok": False, "message": "no token is set"}


# -- the import from Hermes ---------------------------------------------------------------------------------------


HERMES = """# the gateway of Hermes
OPENAI_API_KEY=sk-not-this-one
export TELEGRAM_BOT_TOKEN="987654321:AAFakeHermesFakeHermesFakeHermes_ab"   # the bot
TELEGRAM_ALLOWED_USERS=111, 222 ,alice,333,222
TELEGRAM_HOME_CHANNEL='-1009988776655'
  # TELEGRAM_BOT_TOKEN=commented:out
"""


def test_the_import_reads_the_hermes_file_and_says_only_what_it_found(rig):
    rig.settings.write(enabled=False)
    rig.token.set("")
    rig.hermes.write_text(HERMES, encoding="utf-8")
    before = rig.hermes.read_bytes()
    out = rig.svc.import_hermes()
    assert out["imported"] == {"file": True, "token": True, "token_valid": True, "allowed": 3, "skipped": 1, "home": True}
    assert rig.token.get() == "987654321:AAFakeHermesFakeHermesFakeHermes_ab"
    assert [a["id"] for a in out["allowed"]] == [111, 222, 333] and out["home"] == -1009988776655
    assert "AAFakeHermes" not in json.dumps(out) and "sk-not-this-one" not in json.dumps(out)
    assert rig.hermes.read_bytes() == before                                    # Hermes' file is only read
    assert not list(rig.hermes.parent.glob("*.tmp"))


def test_the_import_keeps_what_is_already_allowed_and_ignores_a_token_of_the_wrong_shape(rig):
    rig.hermes.write_text("TELEGRAM_BOT_TOKEN=not-a-token\nTELEGRAM_ALLOWED_USERS=444\n", encoding="utf-8")
    out = rig.svc.import_hermes()
    assert out["imported"]["token"] is True and out["imported"]["token_valid"] is False and rig.token.get() == TOKEN
    assert [a["id"] for a in out["allowed"]] == [111, 444]


def test_a_missing_hermes_file_is_said_and_nothing_changes(rig):
    out = rig.svc.import_hermes()
    assert out["imported"] == {"file": False, "token": False, "token_valid": False, "allowed": 0, "skipped": 0, "home": False}
    assert rig.token.get() == TOKEN


def test_the_dotenv_reader():
    got = tg.parse_dotenv('\ufeffA=1\n# c\n\nB = "two words" # note\nexport C=\'x#y\'\nD=plain # trailing\nbad line\n1X=no\nE=\n')
    assert got == {"A": "1", "B": "two words", "C": "x#y", "D": "plain", "E": ""}
    assert tg.parse_ids("1, 2 ,x,,-3,2") == ([1, 2], 2)


def test_the_hermes_path_comes_from_the_environment():
    assert tg.hermes_env_path({"TADAMOO_HERMES_ENV": "C:/t/x.env", "LOCALAPPDATA": "C:/L"}) == Path("C:/t/x.env")
    assert tg.hermes_env_path({"LOCALAPPDATA": "C:/L"}) == Path("C:/L") / "hermes" / ".env"


# -- moving a conversation ----------------------------------------------------------------------------------------


def test_a_conversation_is_moved_after_the_chat_is_told_and_brought_back(rig):
    rig.svc.start()
    until(lambda: rig.svc.overview()["state"] == "running")
    sid = rig.sessions.create(None)["id"]
    assert rig.svc.overview()["can_move"] is True
    rec = rig.svc.move(sid, ALLOWED)
    assert rec["telegram_chat"] == ALLOWED and "now here" in rig.fake.sent("sendMessage")[-1]["text"]
    rec = rig.svc.move(sid, None)
    assert rec["telegram_chat"] is None and "back on the TaDaMoo page" in rig.fake.sent("sendMessage")[-1]["text"]


def test_a_move_is_refused_when_the_bot_is_off_the_chat_is_not_allowed_or_telegram_says_no(rig):
    sid = rig.sessions.create(None)["id"]
    with pytest.raises(ts.TelegramServiceError) as e:
        rig.svc.move(sid, ALLOWED)
    assert e.value.code == "not_running" and rig.sessions.recs[sid]["telegram_chat"] is None
    rig.svc.start()
    until(lambda: rig.svc.overview()["state"] == "running")
    with pytest.raises(ts.TelegramServiceError) as e:
        rig.svc.move(sid, 424242)
    assert e.value.code == "bad_chat"
    with pytest.raises(ts.TelegramServiceError):
        rig.svc.move(sid, "abc")
    rig.fake.fail("sendMessage", 403, "Forbidden: bot can't initiate conversation with a user")
    with pytest.raises(ts.TelegramServiceError) as e:
        rig.svc.move(sid, ALLOWED)
    assert e.value.status == 502 and rig.sessions.recs[sid]["telegram_chat"] is None


def test_the_button_is_offered_only_when_the_bot_runs_and_a_chat_is_known(rig):
    assert rig.svc.overview()["can_move"] is False                              # not started
    rig.svc.start()
    until(lambda: rig.svc.overview()["state"] == "running")
    assert rig.svc.overview()["can_move"] is True
    rig.settings.write(allowed=[], home="")
    assert rig.svc.overview()["can_move"] is False and rig.svc.overview()["targets"] == []


# -- the routes -----------------------------------------------------------------------------------------------------


@pytest.fixture
def web(rig, tmp_path):
    tok = ByteTokenizer()
    svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", command=lambda sid, rec, history: [sys.executable, STUB, "--session-id", sid, "--history-dir", str(history)],
                         default_workspace=ws, tick_s=0.1)
    real = ts.TelegramService(rig.settings, rig.token, lambda: mgr, attachments_dir=tmp_path / "att", state_dir=tmp_path / "state", api_base=rig.fake.base,
                              poll_s=0.1, relay_s=0.05, hermes_env=lambda: rig.hermes)
    patches = [mock.patch.object(handler, "_TELEGRAM", real), mock.patch.object(handler, "_SESSIONS", mgr)]
    for p in patches:
        p.start()
    httpd = serve(svc, port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"} if method == "POST" else {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    class Web:
        pass
    w = Web()
    w.call, w.svc, w.mgr = call, real, mgr
    yield w
    real.stop()
    httpd.shutdown()
    httpd.server_close()
    mgr.shutdown()
    for p in patches:
        p.stop()


def test_the_routes_show_and_change_the_settings_and_never_the_token(web):
    code, body = web.call("GET", "/api/telegram")
    assert code == 200 and body["configured"] is True and body["allowed"][0]["id"] == ALLOWED and TOKEN not in json.dumps(body)
    code, body = web.call("POST", "/api/telegram/settings", {"mode": "auto", "home": 5})
    assert code == 200 and body["mode"] == "auto" and body["home"] == 5
    assert web.call("POST", "/api/telegram/settings", {"mode": "root"})[0] == 400
    assert web.call("POST", "/api/telegram/allow", {"add": "222"})[1]["allowed"][-1]["id"] == 222
    assert web.call("POST", "/api/telegram/allow", {"remove": 222})[0] == 200
    code, body = web.call("POST", "/api/telegram/token", {"token": "short"})
    assert code == 400 and "bot token" in body["error"]["message"]
    code, body = web.call("POST", "/api/telegram/token", {"token": "999888777:AAFakeFakeFakeFakeFakeFakeFakeFake_y"})
    assert code == 200 and "999888777" not in json.dumps(body)
    assert web.call("POST", "/api/telegram/token", {"token": ""})[1]["configured"] is False
    assert web.call("POST", "/api/telegram/nothing", {})[0] == 404
    assert web.call("POST", "/api/telegram/test", {})[1] == {"ok": False, "message": "no token is set"}


def test_the_routes_are_for_this_pc_only(web):
    with mock.patch.object(handler.settings, "is_loopback", lambda address: False):
        assert web.call("GET", "/api/telegram")[0] == 403
        assert web.call("POST", "/api/telegram/settings", {"mode": "auto"})[0] == 403
        assert web.call("POST", "/api/telegram/token", {"token": TOKEN})[0] == 403
        assert web.call("POST", "/api/telegram/import", {})[0] == 403
        sid = web.mgr.create(None)["id"]
        assert web.call("POST", f"/api/agent/sessions/{sid}/telegram", {"chat": ALLOWED})[0] == 403


def test_the_move_route_records_the_chat_on_the_conversation_and_the_list_shows_it(web):
    web.svc.start()
    until(lambda: web.svc.overview()["state"] == "running")
    sid = web.mgr.create(None)["id"]
    code, body = web.call("POST", f"/api/agent/sessions/{sid}/telegram", {"chat": ALLOWED})
    assert code == 200 and body["session"]["telegram_chat"] == ALLOWED
    assert [s["telegram_chat"] for s in web.mgr.view()["sessions"] if s["id"] == sid] == [ALLOWED]
    assert web.mgr.telegram_moved() == {sid: ALLOWED}
    assert web.call("POST", f"/api/agent/sessions/{sid}/telegram", {"chat": 9})[0] == 400
    other = web.mgr.create(None)["id"]
    web.call("POST", f"/api/agent/sessions/{other}/telegram", {"chat": ALLOWED})
    assert web.mgr.telegram_moved() == {other: ALLOWED}                         # one moved conversation to a chat
    code, body = web.call("POST", f"/api/agent/sessions/{other}/telegram", {"chat": None})
    assert code == 200 and body["session"]["telegram_chat"] is None and web.mgr.telegram_moved() == {}
    assert web.call("POST", f"/api/agent/sessions/{other}/telegram", {"chat": True})[0] in (400,)


def test_the_move_survives_a_restart_of_the_manager(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    def make():
        return SessionManager(tmp_path / "j", "http://127.0.0.1:1", default_workspace=ws, tick_s=0.1,
                              command=lambda s, r, h: [sys.executable, STUB, "--session-id", s, "--history-dir", str(h)])
    first = make()
    sid = first.create(None)["id"]
    first.set_telegram(sid, ALLOWED)
    first.shutdown()
    second = make()
    try:
        assert second.telegram_moved() == {sid: ALLOWED}
        for bad in (0, "5", True, 1.5):
            with pytest.raises(Exception):
                second.set_telegram(sid, bad)
    finally:
        second.shutdown()


def texts(r):
    return [p["text"] for p in r.fake.sent("sendMessage")]


def test_a_telegram_message_goes_through_the_real_manager_and_the_answer_comes_back(web, rig):
    web.svc.start()
    until(lambda: web.svc.overview()["state"] == "running")
    push(rig, 1, "hello from the phone")
    until(lambda: "echo: hello from the phone" in texts(rig))
    sid = next(iter(rig.settings.chats().values()))
    assert [e["text"] for e in web.mgr.events(sid)["events"] if e["event"] == "user"] == ["hello from the phone"]
    assert web.mgr.get(sid)["mode"] == "default"


def test_a_message_written_on_the_page_for_a_moved_conversation_and_its_answer_reach_the_chat(web, rig):
    web.svc.start()
    until(lambda: web.svc.overview()["state"] == "running")
    sid = web.mgr.create(None)["id"]
    web.call("POST", f"/api/agent/sessions/{sid}/telegram", {"chat": ALLOWED})
    web.call("POST", f"/api/agent/sessions/{sid}/send", {"text": "from the page"})
    until(lambda: "echo: from the page" in texts(rig))
    assert "From the TaDaMoo page:" + chr(10) + "from the page" in texts(rig)
    push(rig, 1, "from the chat")
    until(lambda: "echo: from the chat" in texts(rig))
    assert [e["text"] for e in web.mgr.events(sid)["events"] if e["event"] == "user"] == ["from the page", "from the chat"]
    assert rig.settings.chats() == {}                                           # the chat never needed a conversation of its own


def test_a_confirmation_of_the_agent_is_answered_from_the_chat_and_a_code_is_left_to_the_page(web, rig):
    web.svc.start()
    until(lambda: web.svc.overview()["state"] == "running")
    push(rig, 1, "ask")
    until(lambda: any("reply_markup" in p for p in rig.fake.sent("sendMessage")))
    card = next(p for p in rig.fake.sent("sendMessage") if "reply_markup" in p)
    assert "Archive it?" in card["text"]
    mid = rig.fake.next_message
    rig.fake.push(press(2, ALLOWED, "p:a1:y", message_id=mid))
    sid = next(iter(rig.settings.chats().values()))
    until(lambda: any(e["event"] == "answered" and e["ask"] == "a1" for e in web.mgr.events(sid)["events"]))
    push(rig, 3, "code")
    until(lambda: any("TaDaMoo page" in t for t in texts(rig)))
