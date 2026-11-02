"""serve/billion_context.py: the setting, the process manager and the hand-over of a streamed request, with a stand-in
proxy (a small Python program on the loopback) in the place of the Node.js add-on."""
import io
import json
import os
import sys
import time

import pytest

from serve import billion_context as bc

FAKE = r'''
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
marker = os.environ.get("FAKE_DIE_ONCE")
if marker and not os.path.exists(marker):
    open(marker, "w").close()
    sys.exit(3)
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    def log_message(self, *a): pass
    def _json(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/__bili/health": self._json(200, {"ok": True, "version": "t"})
        elif self.path == "/__bili/stats":
            self._json(200, {"sessions": [{"requests": 2, "contextTokens": 10, "contextWindow": 100, "lastSeen": "2026-01-01"},
                                          {"requests": 3, "contextTokens": 40, "contextWindow": 100, "lastSeen": "2026-01-02"}]})
        elif self.path == "/__bili/env": self._json(200, {k: v for k, v in os.environ.items() if k.startswith(("ACP_", "BILI_", "XDG_"))})
        else: self._json(404, {})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = json.loads(self.rfile.read(n) or b"{}")
        if body.get("model") == "refuse": return self._json(502, {"error": {"message": "no"}})
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        self.wfile.write(("PATH " + self.path + " HOP " + str(self.headers.get("X-Basalt-Bili-Hop")) + " AUTH " + str(self.headers.get("Authorization")) + "\n").encode()); self.wfile.flush()
        for i in range(200 if body.get("model") == "slow" else 3):
            time.sleep(0.2 if body.get("model") == "slow" else 0.05); self.wfile.write(("tok%d\n" % i).encode()); self.wfile.flush()
ThreadingHTTPServer(("127.0.0.1", int(os.environ["ACP_PORT"])), H).serve_forever()
'''


def free_port():
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


@pytest.fixture(autouse=True)
def data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("BASALT_DATA", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_script(tmp_path):
    p = tmp_path / "fake_proxy.py"
    p.write_text(FAKE, encoding="utf-8")
    return str(p)


def until(cond, seconds=40.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def manager(fake_script):
    made = []

    def make(command=None, **kw):
        m = bc.Manager(command=command or (lambda: [sys.executable, fake_script]), poll_s=0.1, backoff_first_s=0.1, **kw)
        made.append(m)
        return m
    yield make
    for m in made:
        m.stop()


def test_the_setting_defaults_to_on_and_a_bad_value_is_refused():
    assert bc.load() == {"enabled": True, "port": bc.DEFAULT_PORT, "compress": False}
    assert bc.save({"enabled": False, "port": "20000"}) == {"enabled": False, "port": 20000, "compress": False}
    assert bc.load() == {"enabled": False, "port": 20000, "compress": False}
    for bad in (80, 70000, "x", True, None):
        with pytest.raises(ValueError):
            bc.save({"port": bad})
    with pytest.raises(ValueError):
        bc.save({"enabled": "yes"})
    with pytest.raises(ValueError):
        bc.save({"compress": 1})
    assert bc.load()["port"] == 20000


def test_the_proxy_only_keeps_things_aside_unless_the_model_may_fold_old_turns():
    archive, folding = bc.proxy_config(False)["compress"], bc.proxy_config(True)["compress"]
    assert archive["injectTool"] is False and archive["injectNudge"] is False
    assert "injectTool" not in folding and "injectNudge" not in folding
    assert archive["maxContextLimit"] == folding["maxContextLimit"] == "90%"
    assert bc.save({"compress": True})["compress"] is True and bc.load()["compress"] is True


def test_a_damaged_file_gives_the_defaults(data_folder):
    (data_folder / bc.FILE).write_text("{not json", encoding="utf-8")
    assert bc.load() == {"enabled": True, "port": bc.DEFAULT_PORT, "compress": False}


def test_without_the_add_on_the_status_says_so_and_nothing_is_routed(manager):
    m = manager(command=lambda: None)
    m.apply()
    assert until(lambda: m.view()["status"] == "not_installed")
    v = m.view()
    assert v["installed"] is False and v["running"] is False and m.target() is None


def test_it_starts_serves_stats_and_stops_with_the_server(manager, data_folder):
    port = free_port()
    bc.save({"port": port})
    m = manager(context=lambda: 32768)
    m.apply()
    assert until(lambda: m.target() == port), (m.view(False), (bc.state_dir() / "proxy.log").read_text(errors="replace")[-600:] if (bc.state_dir() / "proxy.log").exists() else "")
    v = m.view()
    assert v["status"] == "running" and v["running"] is True
    assert until(lambda: m.view()["stats"] == {"sessions": 2, "requests": 5, "context_tokens": 40, "context_window": 100})
    env = bc._http_json(port, "/__bili/env")
    assert env["ACP_PORT"] == str(port) and env["ACP_HOST"] == "127.0.0.1" and env["BILI_STRICT_PORT"] == "1"
    assert env["ACP_AUTO_UPDATE"] == "0" and env["BILI_ADVISORY_CHECK"] == "0" and env["BILI_RELEASE_NOTES_CHECK"] == "0"
    assert env["ACP_MODEL_CONTEXT_LIMIT"] == "32768"
    assert json.loads(open(env["BILI_CONFIG_FILE"], encoding="utf-8").read())["compress"]["maxContextLimit"] == "90%"
    for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        assert env[k].startswith(str(data_folder))                      # the proxy keeps its files under the user data folder
    proc = m._proc
    m.stop()
    assert proc.poll() is not None and m.target() is None and bc._http_json(port, "/__bili/health") is None


def test_the_environment_of_other_proxies_is_not_inherited(manager, monkeypatch):
    monkeypatch.setenv("ACP_PORT", "18787")
    monkeypatch.setenv("BILI_CONFIG_FILE", "elsewhere.json")
    env = manager()._env(20001)
    assert env["ACP_PORT"] == "20001" and env["BILI_CONFIG_FILE"] == str(bc.state_dir() / bc.CONFIG_NAME)


def test_switching_it_off_stops_the_process_and_on_starts_it_again(manager):
    port = free_port()
    bc.save({"port": port})
    m = manager()
    m.apply()
    assert until(lambda: m.target() == port)
    bc.save({"enabled": False})
    m.apply()
    assert until(lambda: m.target() is None and bc._http_json(port, "/__bili/health") is None)
    assert m.view()["status"] == "disabled"
    bc.save({"enabled": True})
    m.apply()
    assert until(lambda: m.target() == port)


def test_a_proxy_that_dies_is_started_again(manager, tmp_path, monkeypatch):
    port = free_port()
    bc.save({"port": port})
    monkeypatch.setenv("FAKE_DIE_ONCE", str(tmp_path / "died"))
    m = manager()
    m.apply()
    assert until(lambda: m.target() == port)
    assert (tmp_path / "died").exists() and m.view()["restarts"] >= 1


def test_the_servers_own_port_is_refused(manager):
    port = free_port()
    bc.save({"port": port})
    m = manager(server_port=port)
    m.apply()
    assert until(lambda: m.view()["status"] == "error")
    assert "this server" in m.view()["detail"] and m.target() is None


class FakeHandler:
    """What forward() uses of the request handler."""

    def __init__(self, headers=None):
        self.headers = headers or {}
        self.wfile = io.BytesIO()
        self.sent = []
        self.watch_done = None

    def _watch_client(self, cancel):
        pass

    def _cors(self):
        pass

    def send_response(self, code):
        self.sent.append(code)

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass


@pytest.fixture
def running(manager, monkeypatch):
    port = free_port()
    bc.save({"port": port})
    m = manager()
    monkeypatch.setattr(bc, "_MANAGER", m)
    m.apply()
    assert until(lambda: m.target() == port)
    return m


def body(**kw):
    return json.dumps({"model": "m", "stream": True, "messages": [], **kw}).encode()


def test_a_streamed_request_goes_through_the_proxy_and_comes_back_whole(running):
    h = FakeHandler({"Authorization": "Bearer k"})
    assert bc.forward(h, body(), 7232) is True
    out = h.wfile.getvalue().decode()
    assert h.sent == [200]
    assert out.startswith("PATH /bili/http://127.0.0.1:7232/v1/chat/completions HOP 1 AUTH Bearer k")
    assert out.endswith("tok0\ntok1\ntok2\n")


def test_what_cannot_go_through_is_answered_directly(running, manager):
    assert bc.forward(FakeHandler(), body(stream=False), 7232) is False         # not streamed
    assert bc.forward(FakeHandler(), b'{"messages": []}', 7232) is False
    assert bc.forward(FakeHandler(), b"not json with \"stream\"", 7232) is False
    h = FakeHandler()
    assert bc.forward(h, body(model="refuse"), 7232) is False and h.sent == []   # refused before the first byte: nothing was sent
    running.stop()
    assert bc.forward(FakeHandler(), body(), 7232) is False                      # no proxy


def test_a_proxy_that_cannot_be_reached_is_marked_and_the_request_goes_direct(running):
    running._port = free_port()                                                    # nothing listens there
    assert bc.forward(FakeHandler(), body(), 7232) is False
    assert running.target() is None


def test_a_client_that_hangs_up_stops_the_stream_at_once(running):
    import threading
    h = FakeHandler()
    h._watch_client = lambda cancel: threading.Timer(0.6, cancel.set).start()
    t0 = time.time()
    assert bc.forward(h, body(model="slow"), 7232) is True
    assert time.time() - t0 < 5 and b"tok0" in h.wfile.getvalue()                  # the stream would have run 40 s


def test_only_a_request_that_asks_is_handed_over_and_never_twice():
    assert bc.wants({bc.OPT_HEADER: "1"}) is True
    assert bc.wants({}) is False
    assert bc.wants({bc.OPT_HEADER: "1", bc.HOP_HEADER: "1"}) is False
    assert bc.wants({bc.OPT_HEADER: "1", bc.PROXY_HOP: "abc"}) is False


def test_summarize_tolerates_anything():
    assert bc.summarize(None) == {} and bc.summarize({"sessions": "x"}) == {"sessions": 0, "requests": 0}
    assert bc.summarize({"sessions": [{"requests": "n"}, 5]})["sessions"] == 1


def test_the_post_saves_and_a_port_change_restarts(manager, monkeypatch):
    m = manager()
    monkeypatch.setattr(bc, "_MANAGER", m)
    a, b = free_port(), free_port()
    v = bc.set_enabled_port({"enabled": True, "port": a})
    assert v["port"] == a and until(lambda: m.target() == a)
    bc.set_enabled_port({"port": b})
    assert until(lambda: m.target() == b)
    with pytest.raises(ValueError):
        bc.set_enabled_port({"port": 5})
    assert bc.load()["port"] == b


def test_the_agent_client_asks_only_when_the_server_started_it_for_a_conversation(monkeypatch):
    from harness.client import BasaltClient
    monkeypatch.delenv("BASALT_COMPRESS", raising=False)
    assert bc.OPT_HEADER not in BasaltClient("http://x")._headers()
    monkeypatch.setenv("BASALT_COMPRESS", "1")
    assert BasaltClient("http://x")._headers()[bc.OPT_HEADER] == "1"


def test_the_page_has_the_controls_and_every_text_in_both_languages():
    import re
    from pathlib import Path
    web = Path(__file__).resolve().parent.parent / "web"
    html, js, i18n = [(web / n).read_text(encoding="utf-8") for n in ("index.html", "billioncontext.js", "i18n.js")]
    assert 'src="web/billioncontext.js"' in html
    for ident in re.findall(r'\$\("#(cs-bc[\w-]*)"\)', js):
        assert f'id="{ident}"' in html
    keys = set(re.findall(r'data-t="(bc\.[\w.]+)"', html)) | set(re.findall(r'T\("(bc\.[\w.]+)"', js)) | {f"bc.st.{s}" for s in ("running", "starting", "disabled", "stopped", "not_installed", "error")}
    assert len(keys) >= 12
    for key in (k for k in keys if not k.endswith(".")):
        assert i18n.count(f'"{key}":') == 2, key                       # one English text and one Italian
    assert 'X-Basalt-Compress' in (web / "chat.js").read_text(encoding="utf-8")
