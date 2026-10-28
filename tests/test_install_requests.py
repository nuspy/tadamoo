"""The queue of install requests: what the harness found by itself is installed only when the person authorises one candidate."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.discovery.installer import InstallError, Installer, IntegrityError  # noqa: E402
from harness.discovery.requests import FILE, InstallRequests, UnknownRequest  # noqa: E402
from serve import handler  # noqa: E402
from serve.capabilities_api import CapabilityApi  # noqa: E402
from test_discovery_api import Api  # noqa: E402
from test_discovery_install import Runner, env, npm_candidate  # noqa: E402,F401
from test_e2e_mock_server import answer, start_server  # noqa: E402,F401


def queue_of(env, **kw):
    q = InstallRequests(env.caps)
    return q, q.add("search the web", [env.store.add(npm_candidate(**kw)), env.store.add(npm_candidate(name="other-mcp", package="@acme/other-mcp", bins={"other-mcp": "x.js"}))], 50, ["a note"])


def test_a_request_keeps_the_candidates_with_their_cards_and_the_comparison_and_is_shared_through_a_file(env):
    q, r = queue_of(env)
    assert r["status"] == "pending" and r["need"] == "search the web" and [c["card"]["name"] for c in r["candidates"]] == ["search-mcp", "other-mcp"]
    assert r["candidates"][0]["card"]["source"] == "registry" and r["candidates"][0]["spec"]["package"] == "@acme/search-mcp"
    q.explain(r["id"], [{"n": 2, "summary": "Smaller", "pros": "light", "cons": "few users"}, {"n": 7, "summary": "x"}, "junk"])
    other_process = InstallRequests(env.caps)
    got = other_process.get(r["id"])
    assert got["candidates"][1]["pros"] == "light" and got["candidates"][0]["summary"] == "" and other_process.pending()[0]["id"] == r["id"]
    assert "approve" not in json.dumps(json.loads((env.caps / FILE).read_text(encoding="utf-8")))
    with pytest.raises(UnknownRequest):
        q.get("r000")


def test_nothing_is_installed_until_a_candidate_is_authorised_and_then_only_that_one(env):
    q, r = queue_of(env)
    assert env.runner.calls == [] and env.index.get("mcp:search-mcp") is None
    done = q.approve(r["id"], 1, env.inst.reading)
    assert done["status"] == "installed" and done["installed"] == "mcp:search-mcp"
    assert env.index.get("mcp:search-mcp")["state"] == "approved" and env.index.get("mcp:other-mcp") is None
    with pytest.raises(InstallError, match="already installed"):
        q.approve(r["id"], 2, env.inst.reading)


def test_a_rejected_request_installs_nothing_and_cannot_be_approved(env):
    q, r = queue_of(env)
    assert q.reject(r["id"])["status"] == "rejected"
    with pytest.raises(InstallError, match="already rejected"):
        q.approve(r["id"], 1, env.inst.reading)
    assert env.runner.calls == [] and q.pending() == []


def test_a_wrong_number_a_damaged_request_and_a_failed_install_say_why(env):
    q, r = queue_of(env)
    for number in (0, 3, "1", None):
        with pytest.raises(InstallError, match="Choose one"):
            q.approve(r["id"], number, env.inst.reading)
    path = env.caps / FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["candidates"][0]["spec"]["package"] = "evil; rm -rf /"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(InstallError, match="cannot be installed"):                    # the same strict checks as straight after a search
        q.approve(r["id"], 1, env.inst.reading)
    data["items"][0]["candidates"][0]["spec"] = {"nonsense": 1}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(InstallError, match="damaged"):
        q.approve(r["id"], 1, env.inst.reading)
    assert env.runner.calls == []
    bad = Installer(env.caps, env.store, index=env.index, runner=Runner(integrity="sha512-OTHER"), which=lambda n: "npm")
    q2, r2 = queue_of(env, name="third-mcp", package="@acme/third-mcp", bins={"third-mcp": "x.js"})
    with pytest.raises(IntegrityError):
        q2.approve(r2["id"], 1, bad.reading)
    assert q2.get(r2["id"])["status"] == "pending"                                # the person may go on with "install anyway"
    assert q2.approve(r2["id"], 1, bad.reading, anyway=True)["status"] == "installed"
    broken = Installer(env.caps, env.store, index=env.index, runner=Runner(fail=lambda c: True), which=lambda n: "npm")
    q3, r3 = queue_of(env, name="fourth-mcp", package="@acme/fourth-mcp")
    with pytest.raises(InstallError):
        q3.approve(r3["id"], 1, broken.reading)
    assert q3.get(r3["id"])["status"] == "failed" and q3.get(r3["id"])["detail"]


def test_the_queue_keeps_only_the_latest_requests(env):
    q = InstallRequests(env.caps)
    for i in range(45):
        q.add(f"need {i}", [env.store.add(npm_candidate())])
    assert len(q.listing()) == 40 and q.listing()[0]["need"] == "need 44"


@pytest.fixture
def served(env, tmp_path, monkeypatch, start_server):  # noqa: F811
    capi = CapabilityApi(env.caps, tmp_path / "ws", home=tmp_path / "home", appdata=tmp_path / "none")
    monkeypatch.setattr(handler, "_CAPS", capi)
    monkeypatch.setattr(capi, "_installer_for", lambda store: Installer(env.caps, store, index=capi.index, runner=Runner(), which=lambda n: "npm"))
    return Api(start_server([answer("x")])), env


def test_the_page_lists_authorises_and_rejects_requests(served):
    api, env = served
    q, r = queue_of(env)
    q.explain(r["id"], [{"n": 1, "summary": "Searches", "pros": "popular", "cons": "needs a key"}])
    code, view = api("GET", "/api/capabilities/requests")
    assert code == 200 and view["requests"][0]["id"] == r["id"] and view["requests"][0]["candidates"][0]["cons"] == "needs a key"
    assert api("POST", "/api/capabilities/requests/approve", {"id": "r000", "number": 1})[0] == 409
    assert api("POST", "/api/capabilities/requests/approve", {"id": r["id"], "number": 9})[1]["error"]["code"] == "bad_request"
    code, body = api("POST", "/api/capabilities/requests/approve", {"id": r["id"], "number": 2})
    assert code == 200, body
    assert body["requests"][0]["status"] == "installed" and any(e["id"] == "mcp:other-mcp" and e["state"] == "approved" for e in body["entries"])
    q2, r2 = queue_of(env, name="third-mcp", package="@acme/third-mcp", bins={"third-mcp": "x.js"})
    code, body = api("POST", "/api/capabilities/requests/reject", {"id": r2["id"]})
    assert code == 200 and [x["status"] for x in body["requests"]] == ["rejected", "installed"]
    assert api("POST", "/api/capabilities/requests/reject", {"id": "r000"})[0] == 409
