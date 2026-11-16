"""Zoom and pan of the plan diagram in a real headless Chrome or Edge on a mock server: Ctrl and the wheel zoom around the pointer, the middle button pans with an open or a closed
hand, steps still drag, links are still picked, the keyboard zooms, and on a phone two fingers pinch and one finger pans; at three screen sizes, in English and in Italian.
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
WEB = ROOT / "serve" / "web"


class PlanZoomInARealBrowserTest(unittest.TestCase):
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
        cls.tmp = tempfile.mkdtemp(prefix="pzc-data-")
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
        from tadamoo.planner import PlanDAG
        from test_planner import node
        dag = PlanDAG(plan_id="zoom-check", title="Zoom check", target_type="occasional")
        spots = {"a1": (20, 20), "a2": (300, 20), "a3": (580, 20), "b1": (20, 160), "b2": (300, 160), "b3": (580, 160), "c1": (20, 300), "c2": (300, 300), "c3": (580, 300)}
        from tadamoo.planner import NodeType
        dag.add_node(node("start", NodeType.START, title="Start", spec={"number": 1, "launch": "manual"}))        # placed left of the origin, as the briefing plan's start is
        for nid in spots:
            dag.add_node(node(nid, title=nid))
        dag.add_edge("start", "a1")
        for a, b in (("a1", "a2"), ("a2", "a3"), ("b1", "b2"), ("b2", "b3"), ("c1", "c2"), ("c2", "c3"), ("a1", "b1"), ("b2", "c2")):
            dag.add_edge(a, b)
        for nid, at in {**spots, "start": (-230, 0)}.items():
            dag.update_node(nid, position={"x": at[0], "y": at[1]})
        req = urllib.request.Request(cls.url + "api/plans", data=json.dumps({"plan": dag.to_dict(), "created_by": "test"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=30).read()

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.terminate()
            cls.server.wait(timeout=20)
        if cls.tmp:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_zoom_and_pan_on_a_phone_and_on_a_desktop_in_both_languages(self):
        self.maxDiff = None
        results, log = [], ""
        for screen in ("desktop 1280x800", "phone 360x640", "phone 390x844"):                # one browser for each screen size and language: mouse and touch input do not share a session
            for lang in ("en", "it"):
                out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "plan_zoom_dom_check.js"), self.url, "Zoom check", self.browser, screen, lang], capture_output=True, text=True, timeout=400)
                results += json.loads(out.stdout.strip().splitlines()[-1])
                log += out.stderr[-1000:]
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [], log)
        self.assertEqual(len(results), 5 * 2 + 3 * 2 * 2, results)                       # the mouse checks (4) on the desktop and the keyboard and touch checks on the phones, in each language

    def test_the_wiring_is_read_from_the_files(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        plans = (WEB / "plans.js").read_text(encoding="utf-8")
        css = (WEB / "plans.css").read_text(encoding="utf-8")
        i18n = (WEB / "i18n.js").read_text(encoding="utf-8")
        self.assertLess(html.index("web/planzoom.js"), html.index("web/plans.js"))
        self.assertIn("PlanZoom.attach(ui.graph", plans)
        self.assertIn("ui.zoom.size(root_)", plans)
        for key in ("pl.zoom.in", "pl.zoom.out", "pl.zoom.reset", "pl.zoom.label"):
            self.assertEqual(i18n.count(f'"{key}":'), 2, key)
        for rule in (".pl-graph { cursor: grab;", ".pl-graph.pl-panning", ".pl-node { cursor: move;", ".pl-zb {"):
            self.assertIn(rule, css)


if __name__ == "__main__":
    unittest.main()
