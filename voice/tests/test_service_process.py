"""The real entry point, `python -m voice.service`, started as a process on a free loopback port: ready line, info file, token, clean stop."""
import json
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _run(tmp_path, token_text="tok", extra=()):
    token = tmp_path / "svc.token"
    if token_text is not None:
        token.write_text(token_text, encoding="utf-8")
    info = tmp_path / "voice-service.json"
    cmd = [sys.executable, "-m", "voice.service", "--settings", str(tmp_path / "voice.json"), "--port", "0", "--token-file", str(token),
           "--info-file", str(info), "--no-desktop", *extra]       # no real hotkey hook, clipboard monitor or window in a test
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {}
    proc = subprocess.Popen(cmd, cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)
    return proc, info


def _get(url, token=None):
    """(status, json body) of a GET; the body is None when it is not JSON."""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def _stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("the service did not stop on the signal")
    proc.stdout.close()
    proc.stderr.close()


def test_refuses_to_start_without_a_token(tmp_path):
    for text in (None, "", "  \n"):
        proc, info = _run(tmp_path, text)
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 2 and "token" in err and out == "", (text, err)
        assert not info.exists()


def test_refuses_a_host_that_is_not_loopback(tmp_path):
    proc, _ = _run(tmp_path, "tok", ("--host", "0.0.0.0"))
    _, err = proc.communicate(timeout=60)
    assert proc.returncode == 2 and "loopback" in err


def test_starts_serves_and_stops_cleanly(tmp_path):
    proc, info = _run(tmp_path, "real-token")
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready == {"ready": True, "port": ready["port"], "pid": proc.pid} and ready["port"] > 0
        written = json.loads(info.read_text(encoding="utf-8"))
        assert written["host"] == "127.0.0.1" and written["port"] == ready["port"] and written["pid"] == proc.pid and written["started"]
        base = f"http://127.0.0.1:{ready['port']}"
        assert _get(base + "/health") == (200, {"status": "ok"})
        assert _get(base + "/status")[0] == 401
        assert _get(base + "/status", "wrong")[0] == 401
        code, body = _get(base + "/status", "real-token")
        assert code == 200 and body["state"] == "standby" and body["pid"] == proc.pid
        assert body["offline"] is True and all(not s["installed"] for s in body["services"].values())
    finally:
        _stop(proc)
    assert proc.returncode is not None
    assert not info.exists()


def test_the_phone_gateway_listens_on_loopback_with_its_own_port_and_only_opens_for_the_token(tmp_path):
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect
    proc, info = _run(tmp_path, "real-token")
    try:
        ready = json.loads(proc.stdout.readline())
        written = json.loads(info.read_text(encoding="utf-8"))
        port = written["gateway_port"]
        assert port > 0 and port != ready["port"]
        code, body = _get(f"http://127.0.0.1:{ready['port']}/status", "real-token")
        assert body["gateway"]["port"] == port and body["gateway"]["host"] == "127.0.0.1" and body["gateway"]["clients"] == 0
        assert _get(f"http://127.0.0.1:{ready['port']}/gateway", "real-token")[1]["voice"] is False
        with pytest.raises(InvalidStatus) as refused:
            connect(f"ws://127.0.0.1:{port}/ws", additional_headers={"Authorization": "Bearer wrong"}, open_timeout=10)
        assert refused.value.response.status_code == 401
        with connect(f"ws://127.0.0.1:{port}/ws", additional_headers={"Authorization": "Bearer real-token"}, open_timeout=10) as ws:
            ws.send(json.dumps({"type": "auth"}))
            assert json.loads(ws.recv(timeout=10))["type"] == "auth_ok"
            assert json.loads(ws.recv(timeout=10))["voice"] is False       # standby: nothing is loaded
    finally:
        _stop(proc)
    assert not info.exists()


def test_the_gateway_can_be_left_off(tmp_path):
    proc, info = _run(tmp_path, "real-token", extra=("--no-gateway",))
    try:
        ready = json.loads(proc.stdout.readline())
        assert json.loads(info.read_text(encoding="utf-8"))["gateway_port"] == 0
        assert _get(f"http://127.0.0.1:{ready['port']}/status", "real-token")[1]["gateway"] is None
        assert _get(f"http://127.0.0.1:{ready['port']}/gateway", "real-token")[0] == 404
    finally:
        _stop(proc)
