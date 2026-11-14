"""The Parameters tab, Start plan and Stop plan in a real headless Chrome or Edge on a mock server: the parameters are not steps, they are listed under their headings with the widget of
their kind and their default, a change is saved (only what changed) and kept by the plan, Start plan and Stop plan are in the header and the step has no Start button, at three screen sizes
in English and in Italian. Skipped where there is no browser or no node."""
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
SCREENS = ("desktop 1280x800", "phone 360x640", "phone 390x844")


class PlanParametersInARealBrowserTest(unittest.TestCase):
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
        cls.tmp = tempfile.mkdtemp(prefix="ppc-data-")
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
        cls.plans = {}

    @classmethod
    def make(cls, plan_id):
        sys.path.insert(0, str(ROOT / "tests"))
        from tadamoo.planner import NodeType, PlanDAG
        from test_planner import node
        dag = PlanDAG(plan_id=plan_id, title=f"Params check {plan_id}", target_type="occasional")
        dag.add_node(node("s", NodeType.START, title="Start", spec={"number": 1, "launch": "manual"}))
        dag.add_node(node("a", title="a", description="A step"))
        dag.add_node(node("e", NodeType.END, title="End"))
        dag.add_edge("s", "a")
        dag.add_edge("a", "e")
        shared = {"group": "Gruppo A", "group_en": "Group A"}
        specs = [("p_brief", {"prompt": "Tema", "prompt_en": "Topic", "into": "brief", "kind": "text", **shared}),
                 ("p_stories", {"prompt": "Quante notizie", "prompt_en": "How many stories", "into": "stories", "kind": "number", "default": 3, **shared}),
                 ("p_edition", {"prompt": "Edizione", "prompt_en": "Edition", "into": "edition", "kind": "choice", "default": "evening", "group": "Gruppo B", "group_en": "Group B",
                                "choices": [{"value": "morning", "label": "Mattina", "label_en": "Morning"}, {"value": "evening", "label": "Sera", "label_en": "Evening"},
                                            {"value": "night", "label": "Notte", "label_en": "Night"}]}),
                 ("p_label", {"prompt": "Scritta", "prompt_en": "Label", "into": "ai_label", "kind": "choice", "default": "no", "group": "Gruppo B", "group_en": "Group B",
                              "choices": [{"value": "yes", "label": "Sì", "label_en": "Yes"}, {"value": "no", "label": "No", "label_en": "No"}]})]
        for nid, spec in specs:
            dag.add_node(node(nid, NodeType.PARAMETER, title=nid, spec=spec))
        cls.post("api/plans", {"plan": dag.to_dict(), "created_by": "test"})

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

    def test_parameters_start_plan_and_stop_plan_on_a_phone_and_on_a_desktop_in_both_languages(self):
        self.maxDiff = None
        results, log = [], ""
        for screen in SCREENS:
            for lang in ("en", "it"):
                plan_id = f"params-{screen.split()[0]}-{screen.split()[1].split('x')[0]}-{lang}"          # one plan for each browser: each one starts it and stops it
                self.make(plan_id)
                out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "plan_params_dom_check.js"), self.url, plan_id, f"Params check {plan_id}", self.browser, screen, lang],
                                     capture_output=True, text=True, timeout=400)
                results += json.loads(out.stdout.strip().splitlines()[-1])
                log += out.stderr[-800:]
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [], log)
        self.assertEqual(len(results), 4 * 3 * 2, results)


if __name__ == "__main__":
    unittest.main()
