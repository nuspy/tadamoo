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
NEW_SCRIPTS = ("plans.js", "tasks.js", "changes.js", "shell.js", "agent.js", "agentchat.js", "providers.js", "patterns.js", "capabilities.js")
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
    "pl.actor.": ["user", "chat", "planner", "system"],
    "pl.kind.": ["occasional", "constant"],
    "ch.state.": ["dormant", "starting", "idle", "busy", "blocked", "asking", "closed"],
    "ch.tool.": ["running", "ok", "failed"],
    "ch.q.": ["plan_create", "plan_mark", "plan_change", "plan_archive", "permission"],
    "ch.routed.": ["steer", "queue", "urgent", "parallel"],
    "pv.data.": ["personal", "general"],
    "pt.": ["what", "pros", "cons", "when", "cache"],
    "cp.state.": ["pending", "approved", "off"],
    "ch.fam.": ["text", "data", "document", "image", "audio", "video", "pdf", "other"],
    "cp.kind.": ["skill", "mcp", "builtin"],
    "wc.": ["added", "modified", "deleted"],
    "cp.e.": ["offline", "gone", "changed", "needs_approval", "bad_choice", "bad_node"],
    "pv.e.": ["offline", "bad_type", "bad_cap", "gone", "not_allowed", "bad_function"],
    "ch.blocked.": ["refused", "error", "model_unavailable", "model_not_loaded", "process_unstable", "interrupted_irreversible", "crash", "start_failed", "other"],
    "pl.h.": ["create", "archive", "restore", "update_node", "mark", "rewind", "apply", "fallback", "update_meta"],
    "tk.status.": ["running", "done", "failed", "closed", "pending", "blocked"],
    "ag.status.": ["running", "done", "failed", "stopped", "interrupted"],
    "ag.mode.": ["plan", "read_only", "default", "auto"],
    "ag.e.": ["folder_missing", "folder_relative", "folder_not_found", "folder_drive", "goal_missing", "goal_long", "bad_mode", "auto_confirm", "bad_rules", "bad_steps", "busy", "start_failed", "not_running", "gone", "approval", "offline", "key", "local", "needs_yes", "no_checkpoint", "rewind_failed"],
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


class ReasoningBoxTest(unittest.TestCase):
    """The chat's reasoning box: a setting of this client, 20 lines tall, closed by itself only when the reader left it."""

    def test_the_setting_is_in_the_panel_and_the_chat_asks_it(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="cs-show-reasoning"', html)
        self.assertIn("showReasoning", (WEB / "chatset.js").read_text(encoding="utf-8"))
        self.assertIn("chatSettings.showReasoning()", (WEB / "chat.js").read_text(encoding="utf-8"))

    def test_the_box_shows_twenty_lines_and_scrolls_for_the_rest(self):
        css = (WEB / "app.css").read_text(encoding="utf-8")
        rule = re.search(r"\.think-b \{([^}]*)\}", css).group(1)
        self.assertIn("max-height: calc(20 * 1.55em", rule)
        self.assertIn("14px/1.55", rule)                                 # the 20 lines are counted in this line height
        self.assertIn("overflow-y: auto", rule)

    def test_reading_by_hand_stops_the_automatic_closing(self):
        js = (WEB / "chat.js").read_text(encoding="utf-8")
        for event in ("wheel", "touchmove", "pointerdown", "click"):
            self.assertIn(event, js[js.index("function watchThink"):js.index("function paintThink")])
        self.assertIn("if (!det.dataset.touched) det.open = false", js)


class ConversationChatTest(unittest.TestCase):
    """The Chat on conversations: a mode selector, its own message area, buttons for the questions, nothing from the agent as markup."""

    def test_the_page_has_the_mode_the_list_and_the_message_area(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        for needle in ('id="chatmode"', 'id="asessions"', 'id="amsgs"', 'src="web/agentchat.js"'):
            self.assertIn(needle, html)
        self.assertLess(html.index("web/chat.js"), html.index("web/agentchat.js"))          # it uses what chat.js shares

    def test_the_questions_have_buttons_that_answer_or_send_and_the_stop_works(self):
        js = (WEB / "agentchat.js").read_text(encoding="utf-8")
        for needle in ("`/${sid}/answer`", "`/${sid}/cancel`", "send(value)", "ch.answered"):
            self.assertIn(needle, js)
        self.assertIn("agent()", (WEB / "chat.js").read_text(encoding="utf-8"))               # the composer is handed over in this mode

    def test_what_the_agent_wrote_enters_the_page_as_text(self):
        js = (WEB / "agentchat.js").read_text(encoding="utf-8")
        for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, js)


class CutReplyTest(unittest.TestCase):
    """A reply the model ended in the middle of a word is flagged, and the reader can ask again without it."""

    def test_the_chat_flags_it_and_offers_to_ask_again(self):
        js = (WEB / "chat.js").read_text(encoding="utf-8")
        self.assertIn("finish_reason", js)
        self.assertIn("m.cut = looksCut(m)", js)
        self.assertIn('class="btn ghost retry"', js)
        self.assertIn("if (m.cut) return;", js)                        # a cut reply is not sent back with the conversation
        self.assertIn("messages.splice(i - 1, 2)", js)                 # the question and the cut reply go, then it is sent again

    def test_both_languages_have_the_texts(self):
        text = (WEB / "i18n.js").read_text(encoding="utf-8")
        self.assertEqual(text.count('"chat.cut"'), 2)
        self.assertEqual(text.count('"chat.retry"'), 2)


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
