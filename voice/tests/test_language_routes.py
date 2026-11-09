"""The language routes of the real control app over the real service Session, the real push-to-talk manager and a real push-to-talk session
(nothing is run: no audio, no model)."""
import json
import types

import pytest

from callbridge import ptt
from voice import service

TOKEN = "t0ken-for-tests"
AUTH = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def env(tmp_path):
    settings = tmp_path / "voice.json"
    settings.write_text(json.dumps({"models_dir": str(tmp_path / "models"), "language": {"enabled": ["it", "en", "fr"]}}), encoding="utf-8")
    app, state = service.build_app(settings, TOKEN)
    return types.SimpleNamespace(app=app, state=state, client=app.test_client())


def start_session(env, language="it", voice="if_sara"):
    """A real manager and a real session in it, as a started push-to-talk session would be."""
    manager = ptt.PushToTalkManager(env.state.config, env.state.brains, desktop=types.SimpleNamespace())
    session = ptt.PushToTalkSession(env.state.config, env.state.brains.fork(), None, None, 16000, voice, language, half_duplex=False)
    manager._session = session
    env.state.session._ptt = manager
    return session


def test_without_a_session_the_language_route_answers_and_says_so(env):
    r = env.client.get("/session/language", headers=AUTH)
    assert r.status_code == 200
    body = r.get_json()
    assert (body["language"], body["active"], body["languages"]) == ("", False, [])
    assert env.client.get("/session/state", headers=AUTH).status_code == 200


def test_with_a_session_it_gives_the_language_the_mode_and_the_enabled_languages(env):
    start_session(env)
    r = env.client.get("/session/language", headers=AUTH)
    assert r.status_code == 200
    body = r.get_json()
    assert (body["language"], body["active"], body["mode"], body["follow"]) == ("it", True, "follow", True)
    assert [x["code"] for x in body["languages"]] == ["it", "en", "fr"] and body["warnings"] == [] and body["engine_fallback"] is False
    state = env.client.get("/session/state", headers=AUTH).get_json()
    assert (state["language"], state["language_mode"]) == ("it", "follow")


def test_the_panel_can_ask_for_a_language_and_for_automatic(env):
    session = start_session(env)
    assert env.client.post("/session/language", json={"language": "fr"}, headers=AUTH).get_json() == {"status": "ok", "language": "fr"}
    assert session._lang_request == "fr"
    assert env.client.post("/session/language", json={"language": "auto"}, headers=AUTH).status_code == 200
    assert session._lang_request == "auto"
    bad = env.client.post("/session/language", json={"language": "de"}, headers=AUTH)
    assert bad.status_code == 400 and bad.get_json()["code"] == "not_enabled"


def test_the_routes_ask_for_the_token(env):
    assert env.client.get("/session/language").status_code == 401
    assert env.client.post("/session/language", json={"language": "auto"}).status_code == 401
