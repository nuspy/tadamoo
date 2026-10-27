"""A Basalt server with a scripted model, for the app's tests: prints `PORT <n>` and serves until it is stopped.

    python mobile/tool/mock_server.py            # then: flutter test --dart-define=BASALT_TEST_URL=http://127.0.0.1:<n>
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp(prefix="basalt-mobile-test-"))
os.environ["BASALT_DATA"] = str(tmp / "data")
os.environ["BASALT_WEB_SEARCH"] = "off"

from serve.engine.mock import MockEngine  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402


class NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


ANSWER = "</think>\n\nEcco il flusso:\n\n```mermaid\nflowchart TD\n  A --> B\n```\n\nIl file: [report.txt](basalt-path:report.txt)\n"
tok = ByteTokenizer()
svc = Service(MockEngine(tok, [ANSWER] * 20, max_context=65536), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
svc.telemetry = NoTelemetry()
folder = tmp / "progetto"
folder.mkdir()
(folder / "report.txt").write_bytes(b"ciao dal PC")
httpd = serve(svc, port=0)
print(f"PORT {httpd.server_address[1]}", flush=True)
print(f"FOLDER {folder}", flush=True)
while True:
    time.sleep(1)
