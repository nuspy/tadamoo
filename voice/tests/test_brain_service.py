"""The service side of the brains: the settings of voice.json, TaDaMoo's API client on a loopback stand-in, the state and test-sentence routes,
and the push-to-talk manager made on first use."""
import http.server
import json
import threading
import types

import pytest

from callbridge.config import Config
from callbridge.tadamoo_api import ApiError, TadamooApi
from voice import brain_service, service, settings_bridge
from fakes import FakeApi, FakeBrain

TOKEN = "t0ken-for-tests"
AUTH = {"Authorization": "Bearer " + TOKEN}


# ------------------------------------------------------------------------------------------------ voice.json
def load(tmp_path, brain):
    p = tmp_path / "voice.json"
    p.write_text(json.dumps({"models_dir": str(tmp_path / "m"), "brain": brain}), encoding="utf-8")
    return settings_bridge.load(p)


def test_defaults_when_the_file_has_no_brain_section(tmp_path):
    (tmp_path / "voice.json").write_text("{}", encoding="utf-8")
    b = settings_bridge.load(tmp_path / "voice.json").brain
    assert (b.mode, b.persona, b.permission, b.workspace, b.base_url, b.hotkeys) == ("direct", "", "default", "", "", False)
    assert settings_bridge.load(tmp_path / "missing.json").brain == b


def test_the_brain_settings_reach_the_config(tmp_path):
    ws = str(tmp_path / "proj")
    s = load(tmp_path, {"mode": "harness", "persona": "maria-rossi", "permission": "read_only", "workspace": ws,
                        "base_url": "http://127.0.0.1:7242/", "hotkeys": True})
    cfg = Config()
    settings_bridge.apply(cfg, s)
    assert (cfg.brain_mode, cfg.brain_persona, cfg.brain_permission, cfg.brain_workspace) == ("harness", "maria-rossi", "read_only", ws)
    assert cfg.tadamoo_base_url == "http://127.0.0.1:7242" and s.brain.hotkeys is True


@pytest.mark.parametrize("brain", [
    {"mode": "hermes", "persona": "Maria Rossi", "permission": "root", "workspace": "relative/dir", "base_url": "http://example.com:80", "hotkeys": "yes"},
    {"mode": 5, "persona": "../x", "permission": None, "workspace": 3, "base_url": "https://127.0.0.1:1", "hotkeys": 1},
    "not an object",
])
def test_bad_values_give_the_defaults_and_a_server_that_is_not_this_pc_is_refused(tmp_path, brain):
    cfg = Config()
    before = cfg.tadamoo_base_url
    settings_bridge.apply(cfg, load(tmp_path, brain))
    assert (cfg.brain_mode, cfg.brain_persona, cfg.brain_permission, cfg.brain_workspace) == ("direct", "", "default", "")
    assert cfg.tadamoo_base_url == before


# ------------------------------------------------------------------------------------------------ the client
class Handler(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        Handler.seen.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path == "/api/bad":
            return self._send(400, {"error": {"type": "personas", "code": "not_found", "message": "No such personality."}})
        self._send(200, {"ok": True})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        Handler.seen.append(("POST", self.path, self.headers.get("Authorization"), body))
        if self.path == "/v1/chat/completions":
            if body.get("messages", [{}])[0].get("content") == "refuse":
                return self._send(503, {"error": {"message": "No model is loaded."}})
            pieces = [{"choices": [{"delta": {"reasoning_content": "thinking..."}}]}, {"choices": [{"delta": {"content": "Hel"}}]},
                      {"choices": [{"delta": {}}]}, {"choices": [{"delta": {"content": "lo."}}]}]
            lines = "".join(f"data: {json.dumps(p)}\n\n" for p in pieces) + "data: [DONE]\n\n"
            return self._send(200, lines.encode(), "text/event-stream")
        self._send(200, {"echo": body})


@pytest.fixture
def server():
    Handler.seen = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def test_only_this_pc_over_http_is_accepted():
    for bad in ("http://example.com:7242", "https://127.0.0.1:7242", "http://192.168.1.5:7242", "ftp://127.0.0.1", ""):
        with pytest.raises(ValueError):
            TadamooApi(bad)
    assert TadamooApi("http://127.0.0.1:7242").port == 7242
    assert TadamooApi("http://localhost:7242").host == "127.0.0.1"
    assert TadamooApi("http://[::1]:7242").host == "::1"


def test_requests_carry_the_token_and_a_json_body(server):
    api = TadamooApi(f"http://127.0.0.1:{server}", "secret-token")
    assert api.get("/api/ping") == {"ok": True}
    assert api.post("/api/x", {"a": 1}) == {"echo": {"a": 1}}
    assert [s[2] for s in Handler.seen] == ["Bearer secret-token", "Bearer secret-token"]


def test_an_error_body_becomes_an_api_error_with_the_servers_words(server):
    api = TadamooApi(f"http://127.0.0.1:{server}")
    with pytest.raises(ApiError) as e:
        api.get("/api/bad")
    assert (str(e.value), e.value.status, e.value.code) == ("No such personality.", 400, "not_found")


def test_a_server_that_is_not_there_is_an_api_error_with_status_zero():
    with pytest.raises(ApiError) as e:
        TadamooApi("http://127.0.0.1:9", timeout=1).get("/x")
    assert e.value.status == 0


def test_the_stream_gives_the_content_and_not_the_reasoning(server):
    api = TadamooApi(f"http://127.0.0.1:{server}", "tok")
    assert list(api.chat_stream([{"role": "user", "content": "hi"}], max_tokens=50, temperature=0.3)) == ["Hel", "lo."]
    sent = [s for s in Handler.seen if s[1] == "/v1/chat/completions"][0]
    assert sent[3]["stream"] is True and sent[3]["max_tokens"] == 50 and sent[3]["temperature"] == 0.3
    list(api.chat_stream([{"role": "user", "content": "hi"}]))
    assert "temperature" not in [s for s in Handler.seen if s[1] == "/v1/chat/completions"][1][3]       # the server's own default when none is asked


def test_a_refused_stream_is_an_api_error(server):
    api = TadamooApi(f"http://127.0.0.1:{server}")
    with pytest.raises(ApiError) as e:
        list(api.chat_stream([{"role": "user", "content": "refuse"}]))
    assert (str(e.value), e.value.status) == ("No model is loaded.", 503)


# ------------------------------------------------------------------------------------------------ the service routes
@pytest.fixture
def env(tmp_path):
    settings = tmp_path / "voice.json"
    settings.write_text(json.dumps({"models_dir": str(tmp_path / "models"), "brain": {"mode": "harness", "persona": "maria"}}), encoding="utf-8")
    app, state = service.build_app(settings, TOKEN)
    return types.SimpleNamespace(app=app, state=state, client=app.test_client(), settings=settings)


def test_the_service_gives_its_own_token_to_the_brains(env):
    assert env.state.config.tadamoo_token == TOKEN
    assert (env.state.config.brain_mode, env.state.config.brain_persona) == ("harness", "maria")


def test_brain_state_needs_the_token_and_shows_the_mode_and_the_session(env):
    assert env.client.get("/brain/state").status_code == 401
    st = env.client.get("/brain/state", headers=AUTH).get_json()
    assert (st["default_mode"], st["persona"], st["permission"], st["last"], st["pending"], st["hotkeys"]) == ("harness", "maria", "default", None, None, False)
    assert st["session"] == {"active": False, "mode": None, "phase": "idle", "avatar": False, "brain": None}
    env.state.brains.direct._api = FakeApi()
    "".join(env.state.brains.fork("direct").respond_stream("hello"))
    assert env.client.get("/brain/state", headers=AUTH).get_json()["last"]["user"] == "hello"


def test_the_state_follows_voice_json(env):
    env.settings.write_text(json.dumps({"brain": {"mode": "direct", "persona": "luca", "permission": "auto"}}), encoding="utf-8")
    st = env.client.get("/brain/state", headers=AUTH).get_json()
    assert (st["default_mode"], st["persona"], st["permission"]) == ("direct", "luca", "auto")


def test_a_test_sentence_needs_the_tts_loaded(env):
    r = env.client.post("/brain/say", json={"text": "hello"}, headers=AUTH)
    assert r.status_code == 409 and r.get_json()["code"] == "tts_not_loaded"


def test_a_test_sentence_is_synthesised_and_played(env, monkeypatch):
    env.state.functions._live = lambda name: "loaded"
    played = []
    monkeypatch.setattr(brain_service.Speaker, "_default_synth", lambda self, text: ([0.0] * 24000, 24000))
    monkeypatch.setattr(brain_service.Speaker, "_default_play", lambda self, samples, rate: played.append((len(samples), rate)))
    r = env.client.post("/brain/say", json={"text": " Hello there "}, headers=AUTH)
    assert r.status_code == 200 and r.get_json() == {"status": "spoken", "duration_ms": 1000}
    assert played == [(24000, 24000)]


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": 5}, {"text": "x" * 301}])
def test_a_bad_test_sentence_is_refused(env, body):
    env.state.functions._live = lambda name: "loaded"
    assert env.client.post("/brain/say", json=body, headers=AUTH).status_code == 400


def test_a_failed_synthesis_is_a_502_with_the_reason(env, monkeypatch):
    env.state.functions._live = lambda name: "loaded"

    def boom(self, text):
        raise RuntimeError("no output device")
    monkeypatch.setattr(brain_service.Speaker, "_default_synth", boom)
    r = env.client.post("/brain/say", json={"text": "hi"}, headers=AUTH)
    assert r.status_code == 502 and "no output device" in r.get_json()["error"]


def test_a_session_does_not_start_until_stt_and_tts_are_loaded(env):
    r = env.client.post("/session/start", json={"mode": "half", "brain": "harness"}, headers=AUTH)
    assert r.status_code == 409 and r.get_json()["code"] == "not_loaded" and r.get_json()["missing"] == ["stt", "tts"]


class FakeManager:
    def __init__(self):
        self.started, self.calls = [], []
        self.hotkeys_attive = False

    def start_session(self, mode, avatar=False, brain=""):
        self.started.append((mode, avatar, brain))
        return True, mode

    def stop_session(self):
        return True

    def session_state(self):
        return {"active": True, "mode": "half", "phase": "idle", "avatar": False, "brain": "harness"}

    def start(self):
        self.calls.append("start")
        self.hotkeys_attive = True

    def sospendi(self):
        self.calls.append("sospendi")
        self.hotkeys_attive = False
        return True


def test_a_session_starts_with_the_chosen_brain_once_the_functions_are_loaded(env):
    env.state.functions._live = lambda name: "loaded"
    env.state.session._make = lambda: FakeManager()
    r = env.client.post("/session/start", json={"mode": "half", "brain": "harness"}, headers=AUTH)
    assert r.status_code == 200 and r.get_json()["status"] == "started"
    assert env.state.session._ptt.started == [("half", False, "harness")]
    assert env.client.get("/session/state", headers=AUTH).get_json()["brain"] == "harness"
    assert env.client.post("/session/stop", headers=AUTH).get_json() == {"status": "stopped"}


def test_a_pc_without_the_hotkey_library_says_why(env):
    env.state.functions._live = lambda name: "loaded"

    def missing():
        raise brain_service.Unavailable("push-to-talk is not available on this PC (ImportError: pynput)")
    env.state.session._make = missing
    r = env.client.post("/session/start", json={"mode": "half"}, headers=AUTH)
    assert r.status_code == 503 and r.get_json()["code"] == "unavailable" and "pynput" in r.get_json()["error"]


def test_the_hotkeys_follow_the_setting_and_start_only_when_asked(env):
    fake = FakeManager()
    env.state.session._make = lambda: fake
    assert fake.calls == [] and env.state.session._ptt is None                  # nothing is hooked by starting the service
    env.settings.write_text(json.dumps({"brain": {"hotkeys": True}}), encoding="utf-8")
    env.client.get("/status", headers=AUTH)
    assert fake.calls == ["start"] and env.client.get("/brain/state", headers=AUTH).get_json()["hotkeys"] is True
    env.client.get("/status", headers=AUTH)
    assert fake.calls == ["start"]                                              # not started twice
    env.settings.write_text(json.dumps({"brain": {"hotkeys": False}}), encoding="utf-8")
    env.client.get("/status", headers=AUTH)
    assert fake.calls == ["start", "sospendi"]


def test_the_standby_service_imports_no_hotkey_or_audio_library():
    import subprocess
    import sys
    code = "import sys; sys.path.insert(0, 'voice'); import voice.service; print(sorted(m for m in ('pynput', 'sounddevice', 'torch', 'onnxruntime') if m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(__import__('pathlib').Path(__file__).resolve().parents[2]))
    assert out.stdout.strip() == "[]", out.stderr


def test_the_brains_state_survives_a_fake_brain_in_the_manager_api():
    assert FakeBrain().fork().set_mode("harness") is True
