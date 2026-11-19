"""The Images block of the Knowledge settings: wired into the page, every text in both languages, nothing written as markup, and the secret key never kept in the page."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
JS = (WEB / "ragimages.js").read_text(encoding="utf-8")
RAGSET = (WEB / "ragsettings.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class RagImagesPageTest(unittest.TestCase):
    def test_it_is_loaded_and_put_in_the_settings_of_the_page(self):
        # this is a general setting (one copy for the whole app), not a per-project one, so it lives on Settings > Knowledge
        # (ragsettings.js) next to the other general RAG settings, not on the Knowledge tab's own rag.js
        self.assertIn("web/ragimages.js", HTML)
        self.assertIn("window.RagImages.block(draw)", RAGSET)

    def test_every_string_exists_in_both_languages(self):
        keys = {k for k in re.findall(r'T\(\s*"(ri\.[a-z0-9_.]+)"', JS) if not k.endswith(".")} | {f"ri.opt.{o}" for o in ("use_existing", "install_separate")}
        keys |= {"ri.moved", "ri.done"}
        self.assertGreater(len(keys), 18)
        for key in keys:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_the_script_writes_no_markup_calls_only_the_images_routes_and_sends_the_key(self):
        for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, JS)
        self.assertNotRegex(JS, r'fetch\(\s*["`]/')
        self.assertIn("authHeaders()", JS)
        for route in ("api/rag/images", "api/rag/images/folder", "api/rag/images/use-folder", "api/rag/images/minio/existing", "api/rag/images/minio/install"):
            self.assertIn(route, JS)

    def test_the_secret_key_is_a_password_field_and_is_cleared_after_it_is_sent(self):
        self.assertIn('i.type = "password"', JS)
        self.assertIn('form.secret = ""', JS)


if __name__ == "__main__":
    unittest.main()
