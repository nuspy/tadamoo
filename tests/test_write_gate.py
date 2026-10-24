"""What /api/* changes needs the server's secret: in a header for programs, in the cookie the page gets when it loads."""
import http.client
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import control_secret
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[1]
SECRET = "s3cret-of-this-server"


class _NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("BASALT_DATA", str(tmp_path / "data"))
    tok = ByteTokenizer()
    svc = Service(MockEngine(tok, ["</think>\n\nok"], max_context=16384), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
    svc.telemetry = _NoTelemetry()
    svc.control_token = SECRET
    httpd = serve(svc, port=0)
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    data = None if body is None else json.dumps(body)
    c.request(method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})})
    r = c.getresponse()
    raw = r.read()
    out = (r.status, json.loads(raw.decode() or "{}") if r.getheader("Content-Type", "").startswith("application/json") else raw, r)
    c.close()
    return out


WRITES = [("/api/tasks", {"id": "t1", "status": "running"}), ("/api/plans", {"plan": {"title": "x", "nodes": {}}}),
          ("/api/agent/sessions", {"mode": "default"}), ("/api/profiles", {"action": "noop"}), ("/api/settings", {})]


@pytest.mark.parametrize("path,body", WRITES)
def test_a_change_without_the_secret_is_refused_before_the_route_looks_at_it(server, path, body):
    code, data, _ = call(server, "POST", path, body)
    assert code == 403 and data["error"]["code"] == "control_secret" and "reload the page" in data["error"]["message"]
    code, data, _ = call(server, "POST", path, body, {"X-Basalt-Control": "wrong"})
    assert code == 403 and data["error"]["code"] == "control_secret"


@pytest.mark.parametrize("path,body", WRITES)
def test_the_secret_in_the_header_lets_a_program_through(server, path, body):
    code, data, _ = call(server, "POST", path, body, {"X-Basalt-Control": SECRET})
    assert data.get("error", {}).get("code") != "control_secret" if isinstance(data, dict) else True


def test_the_page_gets_the_cookie_and_changes_things_with_it(server):
    code, page, resp = call(server, "GET", "/")
    cookie = resp.getheader("Set-Cookie")
    assert code == 200 and cookie.startswith(f"basalt_control_{server}={SECRET}") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    sent = cookie.split(";")[0]
    code, data, _ = call(server, "POST", "/api/tasks", {"id": "t2", "status": "running"}, {"Cookie": sent})
    assert code == 200
    code, data, _ = call(server, "POST", "/api/tasks", {"id": "t2", "status": "running"}, {"Cookie": f"basalt_control_{server}=nope"})
    assert code == 403
    code, data, _ = call(server, "POST", "/api/tasks", {"id": "t2", "status": "running"}, {"Cookie": f"basalt_control_{server + 1}={SECRET}"})
    assert code == 403                                                                           # another server's cookie name


def test_reading_stays_open_and_so_do_the_model_routes(server):
    assert call(server, "GET", "/api/prefs")[0] == 200
    assert call(server, "GET", "/api/tasks")[0] == 200
    code, data, _ = call(server, "POST", "/v1/vram", {"reserve_mib": "x"})
    assert code != 403 or data["error"].get("code") != "control_secret"                          # /v1/vram is not gated


def test_a_big_body_without_the_secret_gets_a_clear_answer(server):
    code, data, _ = call(server, "POST", "/api/tasks", {"id": "t3", "pad": "x" * 200000})
    assert code in (403, 413)


def test_a_program_on_this_pc_finds_the_secret_in_the_data_folder(server, tmp_path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / f"control-{server}.token").write_text(SECRET, encoding="ascii")
    h = control_secret.header_for(f"http://127.0.0.1:{server}")
    assert h == {"X-Basalt-Control": SECRET}
    code, data, _ = call(server, "POST", "/api/tasks", {"id": "t4", "status": "running"}, h)
    assert code == 200
    assert control_secret.header_for("https://example.com") == {} and control_secret.header_for("http://192.168.1.5:7232") == {}
    assert control_secret.header_for(f"http://127.0.0.1:{server + 7}") == {}                    # no file for that port
