"""The Download panel of the Loader: its files are served, its place is in the page, the model picker's hint is wired, and
every string it uses exists in English and Italian. The browser behaviour is checked by hand against a mock-engine
server. Mock engine only."""
import json
import shutil
import subprocess
import unittest
import urllib.request
from pathlib import Path

from serve import catalog
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "serve" / "web"


class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "Hello.", max_context=4096), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8")

    def test_the_panel_is_in_the_loader_and_its_script_is_served_after_the_models(self):
        _, _, html = self.get("/")
        self.assertIn('id="ld-download"', html)
        self.assertLess(html.index('id="ld-models"'), html.index('id="ld-download"'))
        self.assertLess(html.index('web/models.js'), html.index('web/download.js'))
        code, ctype, js = self.get("/web/download.js")
        self.assertEqual(code, 200)
        self.assertIn("javascript", ctype)
        for call in ("api/models/catalog", "api/models/download-plan", "api/models/download", "api/models/download-cancel",
                     "api/models/hf-token", "api/models/hf"):
            self.assertIn(call, js)

    def test_the_picker_shows_the_catalogs_hint_and_the_styles_exist(self):
        _, _, models_js = self.get("/web/models.js")
        for needle in ("BasaltCatalog", "rerender: render", "mdl-tip"):
            self.assertIn(needle, models_js)
        _, _, css = self.get("/web/app.css")
        for cls in (".lddownload", ".dl-body", ".dl-steps", ".dl-ack"):
            self.assertIn(cls, css)

    def test_the_catalog_and_the_models_list_agree_on_ids(self):
        _, _, body = self.get("/api/models/catalog")
        ids = {e["id"] for e in json.loads(body)["entries"]}
        self.assertEqual(ids, {e["id"] for e in catalog.CATALOG})
        _, _, listing = self.get("/api/models")
        for m in json.loads(listing)["models"]:
            self.assertTrue(m["catalog"] is None or m["catalog"] in ids)


@unittest.skipUnless(shutil.which("node"), "node is needed to read the page's strings")
class Strings(unittest.TestCase):
    def test_english_and_italian_have_the_same_keys(self):
        script = (
            "const fs=require('fs');const src=fs.readFileSync(process.argv[1],'utf8');"
            "const a=src.indexOf('const S = {'), b=src.indexOf('const t = (key');"
            "const S=eval('('+src.slice(a+'const S = '.length, b).trim().replace(/;$/,'')+')');"
            "const keys=(o,p='')=>Object.entries(o).flatMap(([k,v])=>typeof v==='object'?keys(v,p+k+'.'):[p+k]);"
            "console.log(JSON.stringify({en:keys(S.en).sort(),it:keys(S.it).sort(),"
            "empty:[...keys(S.en),...keys(S.it)].length}))")
        out = subprocess.run(["node", "-e", script, str(WEB / "download.js")], capture_output=True, text=True, check=True)
        k = json.loads(out.stdout)
        self.assertGreater(len(k["en"]), 60)
        self.assertEqual(k["en"], k["it"])


if __name__ == "__main__":
    unittest.main()
