"""The cookie of the change secret reaches the person's page and nothing the agent runs: adversarial cases against a server in this process."""
import http.client
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util import peer
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[1]
SECRET = "s3cret-of-this-server"
NAV = {"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none"}


class _NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


def start(tmp_path, monkeypatch, api_key=""):
    monkeypatch.setenv("BASALT_DATA", str(tmp_path / "data"))
    tok = ByteTokenizer()
    svc = Service(MockEngine(tok, ["</think>\n\nok"], max_context=16384), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
    svc.telemetry = _NoTelemetry()
    svc.control_token = SECRET
    svc.api_key = api_key
    return serve(svc, port=0)


@pytest.fixture
def server(tmp_path, monkeypatch):
    httpd = start(tmp_path, monkeypatch)
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def keyed(tmp_path, monkeypatch):
    httpd = start(tmp_path, monkeypatch, api_key="k-1234")
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    c.request(method, path, body=None if body is None else json.dumps(body),
              headers={"Content-Type": "application/json", **(headers or {})})
    r = c.getresponse()
    raw = r.read()
    out = (r.status, r.getheader("Set-Cookie"), raw)
    c.close()
    return out


def test_a_plain_program_gets_the_page_but_no_cookie(server):
    code, cookie, page = call(server, "GET", "/", headers={"Content-Type": "text/plain"})
    assert code == 200 and b"<html" in page and cookie is None


def test_the_browsers_own_navigation_gets_the_cookie(server):
    code, cookie, _ = call(server, "GET", "/", headers=NAV)
    assert code == 200 and cookie.startswith(f"basalt_control_{server}={SECRET}") and "HttpOnly" in cookie


@pytest.mark.parametrize("headers", [
    {**NAV, "Sec-Fetch-Site": "cross-site"},                       # a link from another site
    {**NAV, "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors"},    # a script's fetch
    {"Sec-Fetch-Dest": "document"},                                 # half of the headers
    {**NAV, "Host": "evil.example"},                                # DNS rebinding
])
def test_a_request_that_is_not_the_page_gets_no_cookie(server, headers):
    code, cookie, _ = call(server, "GET", "/", headers=headers)
    assert cookie is None


def test_with_an_api_key_the_page_gets_the_cookie_only_through_the_key(keyed):
    assert call(keyed, "GET", "/", headers=NAV)[1] is None          # the page loads, the secret does not come with it
    assert call(keyed, "GET", "/api/session")[0] == 401
    assert call(keyed, "GET", "/api/session", headers={"Authorization": "Bearer wrong"})[0] == 401
    code, cookie, _ = call(keyed, "GET", "/api/session", headers={"Authorization": "Bearer k-1234"})
    assert code == 200 and cookie.startswith(f"basalt_control_{keyed}={SECRET}")
    assert call(keyed, "GET", "/", headers={"Authorization": "Bearer k-1234"})[1] is not None


def test_without_an_api_key_there_is_no_session_route(server):
    assert call(server, "GET", "/api/session", headers=NAV)[0] == 404


CHILD = r"""
import http.client, json, sys
port, secret = int(sys.argv[1]), sys.argv[2]
c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
c.request("GET", "/", headers={"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none"})
r = c.getresponse(); r.read()
cookie = r.getheader("Set-Cookie")
c2 = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
c2.request("POST", "/api/tasks", body=json.dumps({"id": "t9", "status": "running"}), headers={"Content-Type": "application/json", "X-Basalt-Control": secret})
r2 = c2.getresponse(); body = r2.read()
print(json.dumps({"cookie": cookie, "write": r2.status, "body": body.decode()[:300]}))
"""


@pytest.fixture
def agent_is_this_process():
    here = lambda: [os.getpid()]
    peer.add_provider(here)
    yield
    peer.remove_provider(here)


def test_a_child_of_the_agent_gets_no_cookie_and_cannot_use_the_secret_it_knows(server, agent_is_this_process):
    if peer.peer_pid("127.0.0.1", 1, 1) is None and not peer.ancestors(os.getpid()):
        pytest.skip("the process tree cannot be read here")
    out = subprocess.run([sys.executable, "-c", CHILD, str(server), SECRET], capture_output=True, text=True, timeout=60)
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["cookie"] is None, got
    assert got["write"] == 403 and "agent_code" in got["body"], got
    # the agent process itself (the approval client lives there) is not its own descendant
    assert call(server, "GET", "/", headers=NAV)[1] is not None
    code, _, body = call(server, "POST", "/api/tasks", {"id": "t8", "status": "running"}, {"X-Basalt-Control": SECRET})
    assert code == 200


def test_the_cookie_of_a_page_does_not_work_from_agent_code(server, agent_is_this_process):
    cookie = call(server, "GET", "/", headers=NAV)[1].split(";")[0]
    code = r'''
import http.client, json, sys
c = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=15)
c.request("POST", "/api/tasks", body=json.dumps({"id": "t7"}), headers={"Content-Type": "application/json", "Cookie": sys.argv[2]})
print(c.getresponse().status)
'''
    out = subprocess.run([sys.executable, "-c", code, str(server), cookie], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "403"


def test_the_secret_that_approves_plans_and_questions_is_out_of_reach_of_the_agents_own_code(tmp_path, monkeypatch, server, agent_is_this_process):
    """The control token sits in a file of the data folder. Code the agent runs can neither read it nor use it if it knew it."""
    from harness.secrets_broker import PROTECTED_FILES, SecretsBroker
    from harness.tools.execute import find_runtimes, run_code
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    token_file = data / f"control-{server}.token"
    token_file.write_text(SECRET, encoding="ascii")
    broker = SecretsBroker()
    broker.protect_in(data, *PROTECTED_FILES)
    here = str(data)
    snippet = f"""
import http.client, json, os
path = os.path.join({here[:len(here) // 2]!r} + {here[len(here) // 2:]!r}, 'cont' + 'rol-{server}.tok' + 'en')
try:
    print('READ', open(path).read())
except PermissionError:
    print('file refused')
c = http.client.HTTPConnection('127.0.0.1', {server}, timeout=15)
c.request('POST', '/api/agent/approvals/abc', body=json.dumps({{'decision': 'approve'}}), headers={{'Content-Type': 'application/json', 'X-Basalt-Control': {SECRET!r}}})
r = c.getresponse()
print('approve', r.status, r.read().decode()[:120])
"""
    res = run_code("python", snippet, tmp_path, find_runtimes(), broker=broker)
    assert SECRET not in res["stdout"] and "READ" not in res["stdout"], res
    assert "file refused" in res["stdout"], res
    assert "approve 403" in res["stdout"] and "agent_code" in res["stdout"], res
