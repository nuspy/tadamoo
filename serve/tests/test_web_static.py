"""Static checks of the web UI: translations, contrast, markup and the scripts that build it.

They need no browser. Translation keys are read by running i18n.js in node when node is available."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "serve" / "web"
NEW_SCRIPTS = ("plans.js", "tasks.js", "changes.js", "shell.js", "agent.js")
# keys the scripts build at run time, as prefix + each value
DYNAMIC = {
    "pl.type.": ["action", "sensor", "decision", "merge"],
    "pl.status.": ["pending", "in_progress", "completed", "failed", "blocked", "skipped"],
    "pl.assignee.": ["agent", "user", "third_party"],
    "pl.assignee.short.": ["agent", "user", "third_party"],
    "pl.chg.": ["added", "updated", "removed"],
    "pl.conn.": ["live", "retry", "off"],
    "pl.t.source.": ["cloud", "offline", "error"],
    "pl.card.": ["titolo", "date", "destinazione", "partecipanti", "logistica_e_prenotazioni", "profilo_e_vincoli_partecipanti"],
    "tk.status.": ["running", "done", "failed", "closed", "pending", "blocked"],
    "ag.status.": ["running", "done", "failed", "stopped", "interrupted"],
    "ag.mode.": ["plan", "read_only", "default", "auto"],
    "ag.e.": ["folder_missing", "folder_relative", "folder_not_found", "folder_drive", "goal_missing", "goal_long", "bad_mode", "auto_confirm", "bad_rules", "bad_steps", "busy", "start_failed", "not_running", "gone", "approval", "offline", "key", "local"],
    "ch.state.": ["pending", "partial", "kept", "reverted", "superseded"],
    "tab.": ["chat", "agent", "plans", "stats", "loader", "hardware", "logs", "settings"],
}
NODE = shutil.which("node")


def strings():
    script = ("const vm=require('vm'),fs=require('fs');"
              "const ctx={document:{documentElement:{dataset:{}}},location:{search:''},URLSearchParams,localStorage:{getItem(){return null},setItem(){}}};"
              "vm.createContext(ctx);"
              f"vm.runInContext(fs.readFileSync({json.dumps(str(WEB / 'i18n.js'))},'utf8')+';this.S=STRINGS;',ctx);"
              "console.log(JSON.stringify(ctx.S))")
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    if out.returncode:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout)


def luminance(hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def css_vars():
    found = {}
    for name in ("app.css", "plans.css"):
        for block in re.finditer(r":root\s*\{([^}]*)\}", (WEB / name).read_text(encoding="utf-8")):
            for var, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block.group(1)):
                found[var] = value.strip()
    for _ in range(3):                                  # resolve var(--x) references
        for k, v in list(found.items()):
            m = re.fullmatch(r"var\((--[\w-]+)\)", v)
            if m and m.group(1) in found:
                found[k] = found[m.group(1)]
    return found


@unittest.skipUnless(NODE, "node is needed to read the translations")
class TranslationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = strings()

    def used_keys(self):
        keys = set()
        for name in NEW_SCRIPTS:
            text = (WEB / name).read_text(encoding="utf-8")
            keys |= {k for k in re.findall(r'\bT\(\s*"([\w.]+)"', text) if not k.endswith(".")}      # "pl.type." + x is dynamic
        for prefix, values in DYNAMIC.items():
            keys |= {prefix + v for v in values}
        keys |= set(re.findall(r'data-t="([\w.]+)"', (WEB / "index.html").read_text(encoding="utf-8")))
        return keys

    def test_every_key_the_new_ui_uses_exists_in_both_languages(self):
        used = self.used_keys()
        for lang in ("en", "it"):
            missing = sorted(k for k in used if k not in self.s[lang])
            self.assertEqual(missing, [], f"missing in {lang}")

    def test_the_two_languages_have_the_same_placeholders(self):
        for key, en in self.s["en"].items():
            if key in self.s["it"]:
                self.assertEqual(sorted(re.findall(r"\{\d+\}", en)), sorted(re.findall(r"\{\d+\}", self.s["it"][key])), key)

    def test_the_new_keys_are_translated_not_copied_where_it_matters(self):
        for key in ("pl.triage.go", "pl.approve", "pl.reject", "pl.set.cloud", "tk.empty", "pl.none"):
            self.assertNotEqual(self.s["en"][key], self.s["it"][key], key)


class MarkupTest(unittest.TestCase):
    html = (WEB / "index.html").read_text(encoding="utf-8")

    def test_every_script_and_stylesheet_exists(self):
        for src in re.findall(r'<script[^>]+src="web/([^"]+)"', self.html) + re.findall(r'<link[^>]+href="web/([^"]+\.css)"', self.html):
            self.assertTrue((WEB / src).is_file(), src)

    def test_no_dead_scripts_remain(self):
        for name in ("canvas.js", "dag_viewer.js", "artifacts.js"):
            self.assertFalse((WEB / name).exists(), name)

    def test_the_plans_view_is_wired_like_the_others(self):
        self.assertIn('id="tab-plans"', self.html)
        self.assertIn('id="view-plans"', self.html)
        self.assertIn('id="plans-root"', self.html)
        views = re.search(r"const VIEWS = \[([^\]]*)\]", (WEB / "app.js").read_text(encoding="utf-8")).group(1)
        for tab in re.findall(r'data-view="(\w+)"', self.html):
            self.assertIn(f'"{tab}"', views, tab)
            self.assertIn(f'id="view-{tab}"', self.html, tab)

    def test_navigation_order_and_the_palette_and_the_notice_exist(self):
        tabs = re.findall(r'<button class="tab" role="tab" id="tab-(\w+)" data-view=', self.html)
        self.assertEqual(tabs, ["chat", "agent", "plans", "stats", "loader", "hardware", "logs", "settings"])
        for needle in ('id="palette"', 'id="pal-input"', 'id="pal-list"', 'id="pal-open"', 'id="nomodel"', 'id="st-index"',
                       'id="st-ui-scale"', 'id="st-planner-body"', 'aria-keyshortcuts="Control+K"'):
            self.assertIn(needle, self.html, needle)
        css = (WEB / "shell.css").read_text(encoding="utf-8")
        self.assertIn("calc(100vh / var(--z, 1))", css)                    # the zoomed body still fills the window exactly
        self.assertIn("prefers-reduced-motion", css)

    def test_plan_text_is_never_inserted_as_markup(self):
        for name in NEW_SCRIPTS:
            text = (WEB / name).read_text(encoding="utf-8")
            for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
                self.assertNotIn(banned, text, f"{name}: {banned}")

    def test_the_scripts_send_the_api_key_and_use_relative_urls(self):
        for name in NEW_SCRIPTS:
            text = (WEB / name).read_text(encoding="utf-8")
            self.assertIn("authHeaders()", text, name)
            self.assertNotRegex(text, r'fetch\(\s*["`]/', name)          # an absolute path breaks behind a path prefix

    def test_diff_lines_do_not_borrow_the_downloads_list_class(self):
        css = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"(?m)^\.dl \{")                             # the downloads list owns .dl
        self.assertNotRegex((WEB / "changes.js").read_text(encoding="utf-8"), r'className = "dl[ "-]')

    def test_the_agent_view_translates_its_own_static_texts(self):
        text = (WEB / "agent.js").read_text(encoding="utf-8")
        self.assertIn('applyT($id("view-agent"))', text)

    def test_undoing_asks_twice_and_posts_to_the_review_route(self):
        text = (WEB / "changes.js").read_text(encoding="utf-8")
        self.assertIn('"api/changes/review"', text)
        self.assertIn("ch.confirm", text)
        self.assertEqual(text.count("undoButton("), 3)                      # its definition, the block's and the whole edit's
        self.assertNotIn("confirm(", text.replace("T(\"ch.confirm\")", ""))   # no browser dialog

    def test_polling_stops_when_the_view_is_hidden(self):
        for name in ("tasks.js", "changes.js"):
            text = (WEB / name).read_text(encoding="utf-8")
            self.assertIn('document.body.dataset.view === "stats"', text, name)
            self.assertIn("document.hidden", text, name)

    def test_touch_targets_and_reduced_motion_are_covered(self):
        css = (WEB / "plans.css").read_text(encoding="utf-8")
        self.assertIn("min-height: 44px", css)
        self.assertIn("prefers-reduced-motion: reduce", css)
        self.assertRegex(css, r"font-size: 16px")                          # no zoom on focus in iOS
        self.assertNotRegex(css, r"overflow-x:\s*(auto|scroll)")

    def test_the_graph_has_a_keyboard_and_screen_reader_alternative(self):
        js = (WEB / "plans.js").read_text(encoding="utf-8")
        for needle in ('tabindex: "0"', 'role: "button"', '"aria-label"', "pl-list", "aria-live", "keydown", "viewBox"):
            self.assertIn(needle, js, needle)
        self.assertIn('narrow() ? "list" : "graph"', js)


class ContrastTest(unittest.TestCase):
    def test_text_colours_reach_4_5_to_1_on_their_surfaces(self):
        v = css_vars()
        surfaces = [v["--surface"], v["--panel"], v["--raised"]]
        texts = ["--fg", "--fg-2", "--pl-done", "--pl-run", "--pl-fail", "--pl-block", "--pl-add", "--pl-upd", "--pl-del"]
        for t in texts:
            for s in surfaces[:2]:
                self.assertGreaterEqual(contrast(v[t], s), 4.5, f"{t} {v[t]} on {s}")
        self.assertGreaterEqual(contrast(v["--fg"], v["--raised"]), 4.5)
        self.assertGreaterEqual(contrast(v["--fg-3"], v["--surface"]), 4.5, "skipped steps and diffs")

    def test_graph_outlines_are_visible_against_the_canvas(self):
        v = css_vars()
        for c in ("--pl-done", "--pl-run", "--pl-fail", "--pl-block", "--pl-skip", "--pl-add", "--pl-upd", "--pl-del", "--fg-2"):
            self.assertGreaterEqual(contrast(v[c], v["--panel"]), 3.0, c)                  # non-text: 3:1


if __name__ == "__main__":
    unittest.main()
