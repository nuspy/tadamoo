"""Every feature the app has is reachable from the GUI: a list of the controls that must exist in the page, by feature and place. A feature that is built but not on this list is not finished:
add its control here when it is built. The list is checked against the page's own files (index.html, the scripts and the strings in both languages)."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
FILES = {p.name: p.read_text(encoding="utf-8") for p in WEB.glob("*") if p.suffix in (".js", ".html")}
ALL = "\n".join(FILES.values())
I18N = FILES["i18n.js"]

# (feature, where in the GUI, file, a token that must be in it)
FEATURES = [
    ("2FA: import from Google Authenticator", "Settings > Wallet", "wallet.js", "api/wallet/authenticator/preview"),
    ("2FA: scan the Authenticator export", "Settings > Wallet", "wallet.js", "api/wallet/authenticator/scan"),
    ("Browser: which driver (agent-browser or TaDaMoo's own)", "Settings > Wallet > Browser", "wallet.js", 'select(["agent-browser", "tadamoo"]'),
    ("Browser for the dark web (default, own pilot or agent-browser)", "Chat settings > Dark web", "darkwebset.js", '["default", "own", "agent-browser"]'),
    ("Dark web: switch, search, read, Tor", "Chat settings > Dark web", "index.html", 'id="dw-root"'),
    ("Dark web kept by saved inference profiles", "Chat settings > presets", "chatset.js", 'darkweb_navigation: dw("darkweb_navigation")'),
    ("Connectors: skills, MCP servers, MCP apps, plugins, providers", "Connectors", "connectors.js", 'const SUBTABS = ["skill", "mcp_server", "mcp_app", "plugin", "providers"]'),
    ("Connectors: personal skills and external ones", "Connectors > scope", "connectors.js", "cx.scope"),
    ("Connectors: review before installing", "Connectors > item", "connectors.js", "api/connectors/review"),
    ("Connectors: install", "Connectors > item", "connectors.js", "api/connectors/install"),
    ("Connectors: uninstall", "Connectors > installed item", "connectors.js", "api/connectors/uninstall"),
    ("Connectors: configure an installed server (keys to the vault)", "Connectors > installed item", "connectors.js", "api/connectors/configure"),
    ("Connectors: check for updates and update", "Connectors", "connectors.js", "api/connectors/updates"),
    ("Dark web: watching pages", "Chat settings > Dark web", "darkwebset.js", "dw-watches"),
    ("Dark web: published leak archives (list, add, remove)", "Chat settings > Dark web", "darkwebset.js", "dw-leaks-on"),
    ("Connectors: dependency tree of a plugin", "Connectors > plugin", "connectors.js", "treeDialog"),
    ("Connectors: folder for the downloaded originals", "Connectors > Providers", "connectors.js", "api/connectors/settings"),
    ("Knowledge: projects", "Knowledge", "rag.js", "api/rag/projects"),
    ("Knowledge: tree of documents with schedules", "Knowledge > archive", "ragsync.js", "freqDialog"),
    ("Knowledge: add by drop, by browsing this PC, by words", "Knowledge > archive", "rag.js", "browseDialog"),
    ("Knowledge: add from a request in words", "Knowledge > archive", "ragdiscover.js", "api/rag/archives/"),
    ("Knowledge: web address with depth", "Knowledge > archive > sources", "ragsync.js", "rs.f.depth"),
    ("Knowledge: a wiki source follows its own links too, within the wiki", "Knowledge > archive > sources", "ragsync.js", "rs.f.wikidepth"),
    ("Knowledge: images store (folder, MinIO)", "Knowledge > Images", "ragimages.js", "api/rag/images"),
    ("Knowledge: database", "Knowledge > Database", "ragdb.js", "api/rag/db/config"),
    ("Knowledge: figures of a document and of a search hit", "Knowledge", "rag.js", "hit.figures"),
    ("Settings: skills, memories, personalities, phones, network, benchmarks", "Settings", "index.html", 'data-sub="skills"'),
    ("Plans and routines", "Plans, Cron Jobs", "index.html", 'id="view-plans"'),
    ("Models: loader, converter, voice services", "Models", "index.html", 'id="view-loader"'),
    ("Web search: custom search engines and page directories (add, edit, switch, remove)", "Settings > Web search", "websearch.js", "api/websearch/custom"),
    ("Page reader and browser override for a preset's conversations", "Chat settings", "index.html", 'id="cs-browsing"'),
    ("Connectors: review mode (our own, NVIDIA SkillEvaluator, both) and its Tier 3", "Connectors > Providers", "connectors.js", "api/connectors/nvidia"),
    ("Dark web browser follows the default browser unless overridden", "Chat settings > Dark web", "darkwebset.js", '"default", "own", "agent-browser"'),
    ("Dark web: own pinned onion search engines and page directories (add, enable, remove)", "Chat settings > Dark web", "darkwebset.js", "api/darkweb/custom"),
    ("Connectors: more than one account per installed item (add, remove, choose the default)", "Connectors > installed item > Configure", "connectors.js", "api/connectors/accounts"),
    ("Connectors: ask for guided help filling in the settings (field names only, never a value)", "Connectors > installed item > Configure", "connectors.js", "api/connectors/configure/ask"),
    ("Connectors: three-way merge of an update (what stays, what changes, what conflicts) with a risk review of the diff", "Connectors > installed item > update", "connectors.js", "api/connectors/update/plan"),
    ("Knowledge: a separate PostgreSQL of our own, installed, started and stopped by TaDaMoo", "Knowledge", "ragpostgres.js", "api/rag/postgres"),
    ("Knowledge > Database: point a project at TaDaMoo's own PostgreSQL with one button", "Knowledge > Database", "ragdb.js", "api/rag/db/use-own-postgres"),
    ("Knowledge: install the embeddings/Docling Python environment on request", "Knowledge > semantic search", "rag.js", "api/rag/embedding/env/install"),
    ("Connectors: a GitHub token in the vault (rate limit, private repositories)", "Connectors > Providers", "connectors.js", "api/connectors/github-token"),
    ("Knowledge: try a question against an older version of the archive", "Knowledge > archive > try a question", "rag.js", "rg.try.version"),
    ("Dark web: browse an onion page with a real browser, over Tor", "Chat settings > Dark web", "darkwebset.js", "dw-browse"),
]


class EveryFeatureIsInTheGui(unittest.TestCase):
    def test_each_feature_has_its_control_in_the_page(self):
        missing = [f"{name} ({where}): {token!r} not in {fname}" for name, where, fname, token in FEATURES if token not in FILES.get(fname, "")]
        self.assertEqual(missing, [])

    def test_the_page_loads_every_script_and_style_it_ships(self):
        html = FILES["index.html"]
        for p in WEB.glob("*.js"):
            self.assertIn(f'src="web/{p.name}"', html, p.name)
        for p in WEB.glob("*.css"):
            self.assertIn(f'href="web/{p.name}"', html, p.name)

    def test_every_string_a_script_asks_for_exists_in_both_languages(self):
        asked = set()
        for name, text in FILES.items():
            if name in ("i18n.js", "index.html"):
                continue
            asked |= set(re.findall(r'\bT\("([a-z][a-z0-9_]*\.[a-z0-9_.\-]+)"', text))
        missing = sorted(k for k in asked if not k.endswith(".") and I18N.count(f'"{k}":') < 2)
        self.assertEqual(missing[:40], [], f"{len(missing)} strings are asked for and not written in both languages")


if __name__ == "__main__":
    unittest.main()
