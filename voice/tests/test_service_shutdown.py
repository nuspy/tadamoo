"""POST /shutdown: the route build_app() registers when it is given a stop Event (served.py passes its own), the one TaDaMoo's server asks for
when it restarts itself and wants the voice service to restart too."""
import json
import socket
import threading
import urllib.request

import pytest

from callbridge import models_store as ms
from voice import service

TOKEN = "t0ken-for-shutdown-tests"
AUTH = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", ms.ROOT_ENV):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(ms, "_service_env", {})


def _app(tmp_path, stop=None):
    settings = tmp_path / "voice.json"
    settings.write_text(json.dumps({"models_dir": str(tmp_path / "models")}), encoding="utf-8")
    app, _ = service.build_app(settings, TOKEN, stop=stop)
    return app


def test_without_a_stop_event_the_route_does_not_exist(tmp_path):
    client = _app(tmp_path).test_client()
    assert client.post("/shutdown", headers=AUTH).status_code == 404


def test_with_a_stop_event_the_route_sets_it_and_answers(tmp_path):
    stop = threading.Event()
    client = _app(tmp_path, stop=stop).test_client()
    r = client.post("/shutdown", headers=AUTH)
    assert r.status_code == 200
    assert r.get_json()["status"] == "stopping"
    assert stop.is_set()


def test_the_wrong_token_is_refused_and_does_not_set_it(tmp_path):
    stop = threading.Event()
    client = _app(tmp_path, stop=stop).test_client()
    r = client.post("/shutdown", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert not stop.is_set()
