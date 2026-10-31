"""The client repeats a connect that does not go through, and never a request that was sent."""
import http.server
import socket
import threading

import pytest

from harness import client as hc


class Echo(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        Echo.hits += 1
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    Echo.hits = 0
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_a_connect_that_fails_twice_is_made_again_and_the_request_goes_once(server, monkeypatch):
    real, tries = socket.create_connection, []

    def flaky(address, timeout=None, source_address=None):
        tries.append(timeout)
        if len(tries) < 3:
            raise TimeoutError("the connection did not go through")
        return real(address, timeout, source_address)
    monkeypatch.setattr(hc.socket, "create_connection", flaky)
    assert hc.BasaltClient(base_url=server)._get("/x", 10.0) == {"ok": True}
    assert len(tries) == 3 and max(tries) <= hc.CONNECT_S and Echo.hits == 1


def test_a_server_that_is_not_there_gives_up_after_the_tries_and_says_so(monkeypatch):
    tries = []

    def never(address, timeout=None, source_address=None):
        tries.append(1)
        raise TimeoutError("nothing answers")
    monkeypatch.setattr(hc.socket, "create_connection", never)
    with pytest.raises(hc.BasaltError) as err:
        hc.BasaltClient(base_url="http://127.0.0.1:9")._get("/x", 10.0)
    assert err.value.status == 0 and len(tries) == hc.CONNECT_TRIES
