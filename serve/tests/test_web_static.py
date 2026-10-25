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
# the scripts the page loads, read from index.html so a new one is checked without being listed here
PAGE_SCRIPTS = tuple(re.findall(r'<script[^>]+src="web/([^"]+)"', (WEB / "index.html").read_text(encoding="utf-8")))
# older scripts that still build some of their markup as HTML strings; every other script is held to the stricter checks
LEGACY_SCRIPTS = ("i18n.js", "app.js", "chat.js", "chatset.js", "download.js", "loader.js", "models.js", "wizard.js")
NEW_SCRIPTS = tuple(n for n in PAGE_SCRIPTS if n not in LEGACY_SCRIPTS)
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
    "ch.q.": ["plan_create", "plan_mark", "plan_change", "plan_archive", "permission", "code", "payment"],
    "wl.kind.": ["card", "identity", "login", "secret"],
    "wl.field.": ["holder", "number", "expiry", "cvc", "full_name", "birth_date", "address", "city", "postal_code", "country", "phone", "email", "document_number",
                  "username", "password", "totp_secret", "value"],
    "wl.cardkind.": ["virtual", "real", "test"],
    "wl.sf.": ["none", "manual", "automatic", "none.d", "manual.d", "automatic.d"],
    "wl.credkind.": ["vault_card", "vault_login", "delegated_card", "delegated_token", "mandate"],
    "wl.act.": ["payment_authorized", "payment_confirmed", "payment_refused", "waiver_granted", "waiver_revoked", "system_registered", "system_removed"],
    "wl.w.": ["1", "2", "3"],
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
    "br.cmp.state.": ["running", "done", "interrupted"],
    "br.side.state.": ["waiting", "running", "done", "needs_you", "ended", "timeout", "failed", "interrupted"],
    "tab.": ["chat", "agent", "plans", "stats", "loader", "hardware", "logs", "wallet", "settings"],
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

    @unittest.skipUnless(NODE, "node is needed to check the scripts")
    def test_every_script_of_the_page_parses(self):
        for src in re.findall(r'<script[^>]+src="web/([^"]+)"', self.html):
            out = subprocess.run([NODE, "--check", str(WEB / src)], capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 0, f"{src}: {out.stderr}")

    def test_every_script_and_stylesheet_exists(self):
        for src in re.findall(r'<script[^>]+src="web/([^"]+)"', self.html) + re.findall(r'<link[^>]+href="web/([^"]+\.css)"', self.html):
            self.assertTrue((WEB / src).is_file(), src)

    def test_every_script_of_the_folder_is_loaded_by_the_page(self):
        orphans = sorted(p.name for p in WEB.glob("*.js") if p.name not in PAGE_SCRIPTS)
        self.assertEqual(orphans, [], "scripts that no page loads")
        for name in ("risk.js", "wallet.js", "changes.js", "tasks.js"):
            self.assertIn(name, NEW_SCRIPTS)                                # found from the page, checked as new code

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
        self.assertEqual(tabs, ["chat", "agent", "plans", "stats", "loader", "hardware", "logs", "wallet", "settings"])
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
            if "fetch(" in text:
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


class BranchesTest(unittest.TestCase):
    """The tree of branches, the comparison and the preview of the files the agent made."""
    js = (WEB / "branches.js").read_text(encoding="utf-8")
    css = (WEB / "branches.css").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")

    def test_the_dialog_is_linked_and_reachable_from_the_chat(self):
        self.assertIn('href="web/branches.css"', self.html)
        self.assertLess(self.html.index("web/agentchat.js"), self.html.index("web/branches.js"))
        for needle in ('id="branches-open"', 'id="branches"', 'role="tree"', 'id="br-tree"', 'aria-haspopup="dialog"'):
            self.assertIn(needle, self.html, needle)
        self.assertLess(self.html.index('id="chatmode"'), self.html.index('id="branches-open"'))        # in the chat's own header
        self.assertIn("basalt:open-session", (WEB / "agentchat.js").read_text(encoding="utf-8"))
        chat = (WEB / "agentchat.js").read_text(encoding="utf-8")
        self.assertIn("back-impact", chat)                                  # going back lists what stays done before it takes anything off
        self.assertIn("accept_irreversible: true", chat)

    def test_the_tree_is_a_keyboard_and_screen_reader_list(self):
        for needle in ('"treeitem"', '"group"', "aria-level", "aria-expanded", "aria-selected", "aria-label", "ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft",
                       "Home", "End", '"Enter"', "tabIndex = -1", "tabIndex = 0"):
            self.assertIn(needle, self.js, needle)
        self.assertNotIn("hover", self.js)                                  # every action works with a click and with the keyboard

    def test_a_preview_never_runs_what_it_shows(self):
        self.assertIn('frame.setAttribute("sandbox", "")', self.js)         # an empty sandbox: no scripts, no forms, no popups, no origin
        self.assertNotIn("allow-", self.js)
        self.assertIn("default-src 'none'", self.js)
        self.assertIn("img-src data:", self.js)
        self.assertNotRegex(self.js, r"createElement\(\s*[\"']script")

    def test_the_colours_are_the_apps_and_reach_4_5_to_1_and_motion_is_reduced(self):
        v = css_vars()
        used = set(re.findall(r"(?<![\w-])color:\s*var\((--[\w-]+)\)", self.css))
        self.assertTrue(used <= {"--fg", "--fg-2", "--fg-3", "--pl-fail", "--pl-add", "--pl-del"}, used)
        for name in used:
            for surface in ("--panel", "--surface"):
                self.assertGreaterEqual(contrast(v[name], v[surface]), 4.5, f"{name} on {surface}")
        self.assertNotRegex(re.sub(r"rgba?\([^)]*\)", "", self.css), r"(?<![\w-])color:\s*#")         # no colour of its own on text
        self.assertIn("prefers-reduced-motion: reduce", self.css)
        self.assertIn("min-height: 44px", self.css)


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


class WalletViewTest(unittest.TestCase):
    """The wallet page is rebuilt on a timer: that must not re-read the page to a screen reader, nor take a field away from the person typing."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "wallet.js").read_text(encoding="utf-8")

    def test_the_page_container_is_not_a_live_region_but_a_small_status_is(self):
        root = re.search(r'<div[^>]*id="wl-root"[^>]*>', self.html).group(0)
        self.assertNotIn("aria-live", root)
        self.assertRegex(self.html, r'<p[^>]*id="wl-live"[^>]*aria-live="polite"')
        self.assertNotIn('"role", "status"', self.js)                       # the rebuilt note is not a second announcer

    def test_the_timer_does_not_rebuild_a_field_in_use(self):
        self.assertIn("async function load(quiet)", self.js)
        self.assertIn("inField()", self.js)
        self.assertIn("load(true)", self.js[self.js.index("setInterval"):])
        self.assertIn("sig === shown", self.js)                             # an unchanged page is not rebuilt at all

    def test_the_pill_shows_the_limits_and_opens_a_panel_from_the_keyboard(self):
        self.assertIn('aria-expanded="false"', self.html)
        self.assertIn('id="wl-pop"', self.html)
        self.assertIn("pill.textContent = `${T(\"wl.pill\")}", self.js)
        self.assertIn('"Escape"', self.js)
        self.assertNotIn("pill.title", self.js)                             # nothing is only in a tooltip

    def test_small_targets_of_the_bar_and_the_wallet_are_40_px_tall(self):
        for name, selector in (("wallet.css", ".wl-pill"), ("shell.css", ".pal-open"), ("shell.css", ".nomodel .btn"), ("shell.css", ".st-link")):
            css = (WEB / name).read_text(encoding="utf-8")
            rule = re.search(re.escape(selector) + r" \{([^}]*)\}", css).group(1)
            self.assertIn("min-height: 40px", rule, selector)


class TopBarTest(unittest.TestCase):
    css = (WEB / "app.css").read_text(encoding="utf-8")

    def test_the_nine_tabs_wrap_to_a_second_row_instead_of_breaking_words(self):
        tabs = re.search(r"(?m)^\.tabs \{([^}]*)\}", self.css).group(1)
        self.assertIn("flex-wrap: wrap", tabs)
        self.assertIn("white-space: nowrap", re.search(r"(?m)^\.tab \{([^}]*)\}", self.css).group(1))
        bar = re.search(r"(?m)^\.bar \{([^}]*)\}", self.css).group(1)
        self.assertIn("min-height: 76px", bar)                               # a second row of tabs makes the bar taller, it does not overflow it
        self.assertNotIn("five", self.css.lower().split("max-width: 640px")[1][:600])


class ChangesViewTest(unittest.TestCase):
    def test_a_redraw_keeps_the_scroll_of_the_panel_and_of_the_open_diffs(self):
        js = (WEB / "changes.js").read_text(encoding="utf-8")
        self.assertIn("scrollTop", js)
        self.assertIn("function scroller", js)

    def test_the_text_of_the_changes_and_tasks_is_at_least_12_px(self):
        css = (WEB / "plans.css").read_text(encoding="utf-8")
        for line in css.splitlines():
            if re.match(r"\s*\.(chg|tk|dfl)", line):
                for size in re.findall(r"font(?:-size)?:\s*(?:\d{3} )?(\d+(?:\.\d+)?)px", line):
                    self.assertGreaterEqual(float(size), 12, line)

    def test_no_text_of_the_two_panels_is_written_in_the_script(self):
        for name in ("changes.js", "tasks.js"):
            js = (WEB / name).read_text(encoding="utf-8")
            self.assertNotRegex(js, r'textContent = "[^"]*[A-Za-z]{3}', name)


if __name__ == "__main__":
    unittest.main()


class RewindConfirmationTest(unittest.TestCase):
    """Going back over a step whose actions cannot be undone asks the server what would stay done and waits for an explicit yes."""
    js = (WEB / "plans.js").read_text(encoding="utf-8")

    def test_the_page_asks_for_the_impact_first_and_sends_the_acceptance_only_from_the_button(self):
        body = self.js[self.js.index("async function rewind(id)"):self.js.index("function rewindBox")]
        self.assertIn('"plan:rewind_impact"', body)
        self.assertLess(body.index('"plan:rewind_impact"'), body.index('type: "plan:rewind"'))
        self.assertEqual(self.js.count("accept_irreversible: true"), 1)                 # only the confirmed path sends it
        self.assertIn("async function rewindAnyway", self.js[self.js.index("accept_irreversible: true") - 300:self.js.index("accept_irreversible: true")])

    def test_the_confirmation_is_made_of_buttons_with_text_in_both_languages(self):
        self.assertIn('type: "button", id: "pl-rw-go"', self.js)
        self.assertIn('id: "pl-rw-no"', self.js)
        self.assertIn(".focus()", self.js[self.js.index("pl-rw-no\")"):][:200])         # the keyboard lands on the safe button
        css = (WEB / "plans.css").read_text(encoding="utf-8")
        for selector in (".pl-ptitle", ".chg-row summary"):
            self.assertIn("min-height: 40px", re.search(re.escape(selector) + r" \{([^}]*)\}", css).group(1), selector)
        text = (WEB / "i18n.js").read_text(encoding="utf-8")
        for key in ("pl.rewind.stays", "pl.rewind.anyway", "pl.rewind.cancel"):
            self.assertEqual(text.count(f'"{key}"'), 2, key)


class CanvasMoveTest(unittest.TestCase):
    """A step can be moved with the pointer, with arrow keys and with buttons; the place is saved in the step and nothing moves by itself."""
    js = (WEB / "plans.js").read_text(encoding="utf-8")
    css = (WEB / "plans.css").read_text(encoding="utf-8")

    def test_pointer_keyboard_and_buttons_all_save_the_position_through_the_node_update(self):
        for needle in ('"pointerdown"', '"pointermove"', '"pointerup"', "ArrowLeft", "ArrowDown", "ARROWS[e.key]", "moveBy(", "resetPosition(", "pl-arrow"):
            self.assertIn(needle, self.js, needle)
        save = self.js[self.js.index("async function savePosition"):self.js.index("/* by one step")]
        self.assertIn('type: "plan:update_node"', save)
        self.assertIn("position:", save)

    def test_a_click_is_not_a_move_and_the_keyboard_keeps_its_focus(self):
        self.assertIn("Math.hypot(dx, dy) < 5", self.js)
        self.assertIn("S.justDragged", self.js)
        self.assertIn("S.refocus", self.js)
        self.assertIn("S.selected === n.id && ARROWS[e.key]", self.js)             # the arrows act on the selected step

    def test_the_buttons_are_touch_sized_and_nothing_animates_for_those_who_asked_for_less_motion(self):
        self.assertIn("min-width: 44px", re.search(r"\.pl-arrow \{([^}]*)\}", self.css).group(1))
        reduce = self.css[self.css.index("prefers-reduced-motion: reduce"):]
        self.assertIn("transition: none", reduce)
        text = (WEB / "i18n.js").read_text(encoding="utf-8")
        for key in ("pl.move", "pl.move.left", "pl.move.right", "pl.move.up", "pl.move.down", "pl.move.reset", "pl.moved"):
            self.assertEqual(text.count(f'"{key}"'), 2, key)
