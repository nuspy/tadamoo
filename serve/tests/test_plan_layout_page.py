"""The Plans page layout in a real headless Chrome or Edge on a mock server: a fixed bar and tabs, a fixed pane (diagram and inspector) a third of the window high by default and set with a
divider that is kept, one lower part that scrolls, one-line empty fields that grow, compact empty blocks, left-aligned one-line history, readable settings panels; at three screen sizes, in English and in Italian.
Skipped where there is no browser or no node."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class PlanLayoutInARealBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = None
        cls.tmp = ""
        candidates = (os.environ.get("TADAMOO_TEST_BROWSER", ""), r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      shutil.which("google-chrome") or "", shutil.which("chromium") or "")
        cls.browser = next((p for p in candidates if p and os.path.exists(p)), None)
        cls.node = shutil.which("node")
        if not cls.browser or not cls.node:
            raise unittest.SkipTest("no headless browser or no node")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        cls.tmp = tempfile.mkdtemp(prefix="plc-data-")
        env = {**os.environ, "TADAMOO_DATA": cls.tmp + "/data", "TADAMOO_CACHE": cls.tmp + "/cache"}
        cls.server = subprocess.Popen([sys.executable, "-m", "serve.server", "--mock-engine", "--port", str(port)], cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.url = f"http://127.0.0.1:{port}/"
        for _ in range(80):
            try:
                urllib.request.urlopen(cls.url, timeout=1).read(1)
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise unittest.SkipTest("the mock server did not start")
        sys.path.insert(0, str(ROOT / "tests"))
        from tadamoo.planner import NodeType, PlanDAG
        from test_planner import node
        dag = PlanDAG(plan_id="layout-check", title="Layout check", target_type="occasional")
        dag.add_node(node("start", NodeType.START, title="Start", spec={"number": 1, "launch": "manual"}))
        spots = {"a1": (20, 20), "a2": (300, 20), "a3": (580, 20), "b1": (20, 160), "b2": (300, 160), "b3": (580, 160), "c1": (20, 300), "c2": (300, 300), "c3": (580, 300)}
        for nid in spots:
            dag.add_node(node(nid, title=nid, description="A step of a long plan " * 6))
        dag.add_edge("start", "a1")
        for a, b in (("a1", "a2"), ("a2", "a3"), ("b1", "b2"), ("b2", "b3"), ("c1", "c2"), ("c2", "c3"), ("a1", "b1"), ("b2", "c2")):
            dag.add_edge(a, b)
        for nid, at in {**spots, "start": (-230, 0)}.items():
            dag.update_node(nid, position={"x": at[0], "y": at[1]})
        cls.post("api/plans", {"plan": dag.to_dict(), "created_by": "test"})
        for i in range(3):                                          # a plan with a history
            proposal = cls.post("api/plans/layout-check/events", {"type": "plan:propose_diff", "title": f"Add step {i}", "source": "user",
                                                                  "diff": {"add_nodes": [{"id": f"x{i}", "type": "action", "title": f"Extra {i}", "description": "", "assignee": "agent", "dependencies": [], "outputs": []}]}})
            cls.post("api/plans/layout-check/events", {"type": "plan:approve", "proposal_id": proposal["proposal_id"]})

    @classmethod
    def post(cls, path, body):
        req = urllib.request.Request(cls.url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.terminate()
            cls.server.wait(timeout=20)
        if cls.tmp:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_layout_on_a_phone_and_on_a_desktop_in_both_languages(self):
        self.maxDiff = None
        out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "plan_layout_dom_check.js"), self.url, "Layout check", self.browser], capture_output=True, text=True, timeout=1500)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [], out.stderr[-3000:])
        self.assertEqual(len(results), 7 * 3 * 2, results)


if __name__ == "__main__":
    unittest.main()
