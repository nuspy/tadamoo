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


def test_no_web_serves_the_api_and_not_the_page(tmp_path):
    """`no-web: true` in the config file: the root says the server has no page, the files of the page and its fonts are not there, the API answers."""
    port = free_port()
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"server": {"no-web": True}}), encoding="utf-8")
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
        assert status == 200 and json.loads(body)["web"] is False and json.loads(body)["service"] == "tadamoo"
        status, body = get(port, "/")
        assert status == 200 and json.loads(body)["web"] is False                    # a proxy's check of the root passes, and says why there is no page
        assert get(port, "/web/app.js")[0] == 404 and get(port, "/web/index.html")[0] == 404 and get(port, "/fonts/x.woff2")[0] == 404
        assert get(port, "/api/network/ping")[0] == 200 and get(port, "/v1/models")[0] in (200, 503)           # the API is there
    finally:
        proc.terminate()
        proc.wait(timeout=20)


def test_a_key_changed_from_a_client_wins_over_api_key_at_the_next_start(tmp_path):
    """The key kept in the data folder (server/server_key.py) is the one in force after a restart, whatever --api-key says. The keys are made up."""
    port = free_port()
    data = tmp_path / "data"
    data.mkdir()
    (data / "server_key.json").write_text(json.dumps({"key": "test-kept-key-1"}), encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "serve.server", "--standby", "--host", "127.0.0.1", "--port", str(port), "--api-key", "test-cli-key-2"], cwd=ROOT,
                            env={**os.environ, "TADAMOO_DATA": str(data)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def status(key):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/server-key", headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    try:
        for _ in range(40):
            try:
                get(port, "/health")
                break
            except OSError:
                time.sleep(1)
        else:
            raise AssertionError("the server did not answer")
        assert status("test-kept-key-1") == 200 and status("test-cli-key-2") == 401
    finally:
        proc.terminate()
        proc.wait(timeout=20)


def test_the_page_is_served_without_no_web(tmp_path):
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "serve.server", "--standby", "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT,
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
        assert json.loads(body)["web"] is True and get(port, "/web/app.js")[0] == 200 and b"<html" in get(port, "/")[1]
    finally:
        proc.terminate()
        proc.wait(timeout=20)


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
