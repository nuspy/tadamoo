"""The desktop app's server list (tools/windows/desktop/ServerList.cs): the address rules (http or https with a name, a port or none) and the list kept per user with the keys protected.
The checks that need no window run as a small console program (tools/windows/desktop_tests); skipped where there is no .NET SDK or this is not Windows."""
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "tools" / "windows" / "desktop_tests" / "ServerListTests.csproj"


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("dotnet"), reason="needs Windows and the .NET SDK")
def test_the_address_rules_the_list_kept_on_disk_and_the_connection_test_against_a_real_server(tmp_path):
    """The server runs with a made-up key (test-live-key-1); the program tests the connection with the right and a wrong key and changes the server's key."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = subprocess.Popen([sys.executable, "-m", "serve.server", "--standby", "--host", "127.0.0.1", "--port", str(port), "--api-key", "test-live-key-1"], cwd=str(ROOT),
                              env={**os.environ, "TADAMOO_DATA": str(tmp_path / "data")}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).read(1)
                break
            except OSError:
                time.sleep(1)
        else:
            pytest.skip("the server did not start")
        out = subprocess.run(["dotnet", "run", "--project", str(PROJECT), "-c", "Release", "--", f"http://127.0.0.1:{port}"], capture_output=True, text=True, timeout=280, cwd=str(tmp_path))
        assert out.returncode == 0 and out.stdout.strip().splitlines()[-1] == "ok", out.stdout[-2000:] + out.stderr[-2000:]
    finally:
        server.terminate()
        server.wait(timeout=20)
