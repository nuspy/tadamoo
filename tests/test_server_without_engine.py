"""The harness alone: the server in standby starts with no model and no engine binary, answers, and names the hosts it accepts."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port, path, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def test_standby_alone_starts_answers_and_keeps_the_host_rule(tmp_path):
    port = free_port()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"server": {"allowed-hosts": ["tadamoo.example.org"]}}), encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "serve.server", "--standby", "--host", "127.0.0.1", "--port", str(port), "--config", str(cfg)], cwd=ROOT,
                            env={**os.environ, "TADAMOO_DATA": str(tmp_path / "data")}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                status, body = get(port, "/health")
                break
            except OSError:
                time.sleep(1)
        else:
            raise AssertionError("the server did not answer")
        health = json.loads(body)
        assert status == 200 and health["standby"] is True and health["loaded"] is False and health["service"] == "tadamoo"
        assert health["engine_installed"] is False                                      # no engine binary: the page hides the Loader, and Load says so
        assert get(port, "/", host="tadamoo.example.org")[0] == 200 and get(port, "/", host="other.example.org")[0] == 403
    finally:
        proc.terminate()
        proc.wait(timeout=20)
