"""The Plans page: a column of plans with open, duplicate and delete, four parts, links drawn between steps and rows that move up and down."""
import json
import os
import re
import sys
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "plans.js").read_text(encoding="utf-8")
CSS = (WEB / "plans.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class PlansPageTest(unittest.TestCase):
    def test_the_plan_is_chosen_in_a_column_and_not_in_a_drop_down(self):
        self.assertNotIn("pl-select", JS)
        self.assertNotIn("ui.select", JS)
        self.assertIn('class: "convs plcol"', JS)
        for route in ("/delete", "/duplicate"):
            self.assertIn(route, JS)
        self.assertIn("S.delArmed !== p.plan_id", JS)                                  # a delete asks twice
        self.assertIn('keep(COL_KEY, open ? "1" : "0")', JS)                           # the column opens and shuts like the Chat's
        self.assertRegex(HTML, r'id="view-plans"[^>]*>\s*<div class="chatrow plrow" id="plans-root">')

    def test_the_four_parts_are_tabs_in_the_order_asked_and_none_is_a_folding_box(self):
        self.assertIn('const TABS = ["aims", "details", "events", "define"];', JS)
        for key in ("aims", "details", "events", "define"):
            self.assertEqual(I18N.count(f'"pl.tab.{key}":'), 2, key)
            self.assertEqual(I18N.count(f'"pl.tab.{key}.tip":'), 2, key)
        self.assertNotIn('el("summary", { text: T("pl.hz.title")', JS)
        self.assertNotIn("ontoggle", JS)
        self.assertIn('ArrowRight: i + 1, ArrowLeft: i - 1', JS)                       # the tabs answer to the arrow keys

    def test_links_are_drawn_from_the_round_ends_picked_and_removed(self):
        self.assertIn("function startLink(", JS)
        self.assertIn('add_edges: [port ? [a, b, port] : [a, b]]', JS)                 # an edge leaves a named connector
        self.assertIn('remove_edges: [port ? [a, b, port] : [a, b]]', JS)
        self.assertIn('startLink(e, n.id, "out", b.id, "flow")', JS)                    # each branch of a decision is a connector of its own
        self.assertIn('startLink(e, n.id, "in", "rag", "input")', JS)                   # an action takes archives at a data input
        self.assertIn('ev.key === "Escape"', JS)                                       # letting go elsewhere or Escape drops the link
        self.assertIn('e.key === "Delete"', JS)                                        # a picked link is removed by the keyboard too
        for cls in (".pl-port", ".pl-edgehit", ".pl-x", ".linktarget"):
            self.assertIn(cls, CSS)
        self.assertNotIn("depsBox", JS)                                                # no "comes after" box: links are drawn, from the round ends
        self.assertNotIn('"pl.after"', JS)
        self.assertNotIn('"pl.deps"', JS)

    def test_a_step_is_moved_by_hand_and_the_rows_of_the_list_move_up_and_down(self):
        self.assertNotIn("pl-move", JS)                                                # no arrow buttons: the graph is dragged
        self.assertNotIn("pl.move.left", JS)
        self.assertIn('ARROWS[e.key]', JS)                                              # the keys on a picked step stay, for those who cannot drag
        self.assertIn('type: "plan:move", node_id: id, direction: dir', JS)
        self.assertIn('S.snap.order || S.snap.layers.flat()', JS)
        self.assertIn('"plan:move"', re.search(r"const DIRECT = \[([^\]]*)\]", JS).group(1))

    def test_every_button_the_page_makes_has_a_tooltip(self):
        for key in ("pl.tip.graph", "pl.tip.list", "pl.tip.save", "pl.tip.start", "pl.tip.done", "pl.tip.fail", "pl.tip.rewind", "pl.tip.remove",
                    "pl.tip.approve", "pl.tip.reject", "pl.tip.mermaid", "pl.tip.new", "pl.tip.open", "pl.auto.tip", "pl.auto.apply.tip", "pl.auto.cancel.tip"):
            self.assertIn(f'T("{key}")', JS, key)
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_a_kind_is_put_on_the_plan_at_once_as_a_draft_with_no_links_and_no_properties(self):
        self.assertIn("async function putKind(type, at)", JS)
        self.assertIn("dependencies: [], outputs: []", JS)                                           # not linked to anything yet
        self.assertIn("state_variables: { draft: true }", JS)                                       # the server keeps it unfinished and does not run it
        self.assertIn("await userDiff({ add_nodes: [node] }", JS)
        self.assertNotIn("pl-a-title", JS)                                                           # there is no add form to fill in
        self.assertNotIn("pl-a-exits", JS)
        self.assertIn("no_route", JS)                                                                # a decision no exit of which holds is shown with its facts

    def test_a_draft_is_set_up_in_the_properties_and_stays_a_draft_until_it_is_complete(self):
        self.assertIn("delete sv.draft", JS)
        self.assertIn('T("pl.draft.saved"', JS)
        self.assertIn(".pl-node.draft", CSS)

    def test_the_properties_live_inside_the_graphs_container_and_the_kinds_and_the_instruction_box_are_fixed_at_the_bottom(self):
        self.assertIn('el("div", { class: "pl-canvas" }, el("div", { class: "pl-stage" }, ui.graph, ui.zoom.bar, ui.list, ui.autoBtn, ui.autoBox), ui.detail)', JS)
        self.assertIn('el("div", { class: "pl-dock" }, ui.palette, ui.instr)', JS)                    # the kinds just above the instruction box
        self.assertRegex(CSS, r"\.pl-dock \{[^}]*position: sticky; bottom: 0")
        self.assertRegex(CSS, r"\.pl-detail \{[^}]*flex: 0 0 270px")

    def test_the_layout_of_the_whole_plan_is_an_icon_at_the_corner_that_closes_and_cancels_on_a_click_outside(self):
        self.assertIn("PlanKinds.layoutIcon()", JS)
        self.assertIn("async function applyAuto()", JS)
        self.assertIn("!ui.autoBox.contains(e.target)", JS)
        self.assertIn('e.key === "Escape" && S.auto', JS)
        self.assertIn("if (S.auto && S.auto.at && S.auto.at[id]) return S.auto.at[id];", JS)         # a layout being tried is drawn, not saved
        self.assertIn("update_nodes: update", JS)
        self.assertRegex(CSS, r"\.pl-autobtn \{[^}]*position: absolute; top: 6px; right: 6px")
        for kind in ("lr", "tb", "compact", "lanes"):
            self.assertEqual(I18N.count(f'"pl.auto.{kind}":'), 2, kind)
            self.assertEqual(I18N.count(f'"pl.auto.{kind}.d":'), 2, kind)
        self.assertNotIn("pl.move.reset", JS)                                                        # not a button of each step

    def test_the_page_never_writes_markup_and_the_new_texts_exist_in_both_languages(self):
        for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, JS)
        keys = {k for k in re.findall(r'T\("(pl\.[a-z.]+)"', JS) if not k.endswith(".")}
        self.assertGreater(len(keys), 60)
        for key in keys:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_the_settings_page_has_the_key_field_the_threshold_the_timeout_and_the_chunk_with_their_explanations(self):
        self.assertIn('id: "pl-s-key", type: "password", autocomplete: "new-password"', JS)         # the key is typed, never filled back in
        self.assertIn("clear_jev_key: true", JS)
        self.assertIn("body.jev_key = newKey", JS)
        for field in ("blocker_threshold", "cloud_timeout", "max_events"):
            self.assertIn(field + ":", JS)
        for key in ("pl.set.key.field", "pl.set.key.remove", "pl.set.threshold", "pl.set.timeout", "pl.set.chunk"):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
            self.assertEqual(I18N.count(f'"{key}.help":'), 2 if key != "pl.set.key.remove" else 0, key)


class PortsOnThePageTest(unittest.TestCase):
    KINDS = (WEB / "plankinds.js").read_text(encoding="utf-8")

    def test_a_decision_draws_a_labelled_connector_for_each_branch_and_the_label_is_cut_with_its_whole_text_in_a_tooltip(self):
        self.assertIn("const rows = branchesOf(n);", JS)
        self.assertIn('class: "pl-br"', JS)
        self.assertIn("clip(b.label, 17)", JS)
        self.assertIn("svg(\"title\", {}, document.createTextNode(b.label))".replace('\\"', '"'), JS)
        self.assertIn('"aria-label": name', JS)                                       # the connector's name is its label
        self.assertNotIn('svg("polygon", { points: `${x + W / 2},${y}', JS)            # a decision is a box that grows with its branches, not a diamond
        self.assertIn("hOf(n)", JS)

    def test_the_connectors_are_small_and_the_touch_area_is_a_larger_one_that_draws_nothing(self):
        self.assertRegex(CSS, r"\.pl-hit \{ fill: transparent; stroke: none; pointer-events: all;")
        self.assertIn("{ dot: 5, hit: 22 }", JS)                                     # a 10 px dot, a 44 px area
        self.assertRegex(CSS, r"\.pl-t \{ font: 500 13px")
        self.assertRegex(CSS, r"\.pl-br \{[^}]*font: 400 11px")
        self.assertRegex(CSS, r"\.pl-connx \{ min-width: 44px; min-height: 44px; \}")
        self.assertRegex(CSS, r"\.pk-brbtn, \.pk-bradd \{ min-height: 44px; min-width: 44px; \}")
        self.assertIn("prefers-reduced-motion", CSS[CSS.index(".pl-port { stroke-width"):])

    def test_a_link_from_a_rag_node_is_drawn_apart_from_a_wait_and_stored_as_a_connector_not_a_dependency(self):
        self.assertIn('class: "pl-edge " + (feed ? "feed " : "")', JS.replace("svg(\"path\", { d, ", "").replace('svg("path", { d, class: "pl-edge " + (feed ? "feed " : "")', 'class: "pl-edge " + (feed ? "feed " : "")'))
        self.assertIn('"marker-end": feed ? "url(#pl-dot)" : "url(#pl-arrow)"', JS)     # a dot where the data arrives, not the arrow of "waits for"
        self.assertRegex(CSS, r"\.pl-edge\.feed \{ stroke-dasharray: 4 4; \}")
        self.assertIn("function connectionsBox(n)", JS)                                  # the phone and the keyboard connect from the panel
        self.assertIn('T("pl.link.ragonly")', JS)

    def test_a_palette_kind_with_only_optional_fields_is_not_a_draft(self):
        self.assertIn("const needs = fields.some(f => f.required) || type === \"sensor\";", JS)

    def test_every_word_the_new_parts_use_exists_in_both_languages(self):
        keys = set(re.findall(r'T\("(pl\.(?:br|feed|conn|port|link|f\.profile)[\w.]*)"', JS + self.KINDS))
        keys = {k for k in keys if not k.endswith(".")}                                   # a prefix the page completes from a table, listed below
        keys |= {f"pl.br.op.{w}" for w in ("lt", "le", "eq", "ne", "ge", "gt", "between", "in", "contains", "else", "custom")}
        keys |= {"pl.type.rag", "pl.kind.rag.d", "pl.spec.archives", "pl.spec.rag_archives", "pl.spec.branches", "pl.spec.mode", "pl.spec.input", "pl.spec.opt.first", "pl.spec.opt.all", "pl.spec.on_no_match", "pl.spec.opt.stop", "pl.spec.opt.ask", "pl.fact.nobranch"}
        self.assertGreater(len(keys), 40)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_the_plan_has_a_picker_for_its_own_archives(self):
        self.assertIn("window.ragPick.create({ T, ids: p.rag_archives || []", JS)
        self.assertIn("profile.rag_archives = S.horizonPick.get()", JS)


class DecisionGraphInARealBrowserTest(unittest.TestCase):
    """The graph of a decision, drawn by the page itself in headless Chrome or Edge on a mock server: every connector has its visible label, the header does not
    collide with the rows, the type is small. Skipped where there is no browser or no node."""

    @classmethod
    def setUpClass(cls):
        import shutil, socket, subprocess, tempfile, time, urllib.request
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
        cls.tmp = tempfile.mkdtemp(prefix="plg-data-")
        root = Path(__file__).resolve().parents[2]
        env = {**os.environ, "TADAMOO_DATA": cls.tmp + "/data", "TADAMOO_CACHE": cls.tmp + "/cache"}
        cls.server = subprocess.Popen([sys.executable, "-m", "serve.server", "--mock-engine", "--port", str(port)], cwd=str(root), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.url = f"http://127.0.0.1:{port}/"
        for _ in range(80):
            try:
                urllib.request.urlopen(cls.url, timeout=1).read(1)
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise unittest.SkipTest("the mock server did not start")
        sys.path.insert(0, str(root / "tests"))
        import test_planner_ports as ports
        dag = ports.branching()
        dag.plan_id, dag.title = "graph-check", "Decision with five outputs"
        req = urllib.request.Request(cls.url + "api/plans", data=json.dumps({"plan": dag.to_dict(), "created_by": "test"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=30).read()
        from tadamoo.planner import NodeType, PlanDAG
        from test_planner import node
        wired = PlanDAG(plan_id="connect-check", title="Connect check", target_type="occasional")
        for nid in ("a1", "a2", "t1", "t2"):
            wired.add_node(node(nid, title=nid))
        wired.add_node(node("choose", NodeType.DECISION, title="choose", spec={"mode": "first", "input": "n", "branches": [{"label": "small", "op": "<", "value": 10}, {"label": "rest", "op": "else"}]}))
        wired.add_node(node("kb", NodeType.RAG, title="kb", spec={"archives": ["boats-111111"]}))
        for nid, at in {"a1": (20, 20), "a2": (300, 20), "choose": (20, 100), "t1": (300, 100), "kb": (20, 250), "t2": (300, 250)}.items():
            wired.update_node(nid, position={"x": at[0], "y": at[1]})
        req = urllib.request.Request(cls.url + "api/plans", data=json.dumps({"plan": wired.to_dict(), "created_by": "test"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=30).read()

    @classmethod
    def tearDownClass(cls):
        import shutil
        if cls.server:
            cls.server.terminate()
            cls.server.wait(timeout=20)
        if cls.tmp:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_graph_of_a_decision(self):
        import subprocess
        out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "plan_graph_dom_check.js"), self.url, "Decision with five outputs", self.browser],
                             capture_output=True, text=True, timeout=180)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertGreaterEqual(len(results), 10)
        self.assertEqual([r for r in results if not r["ok"]], [])

    def test_a_link_dragged_from_an_output_connector_lands_in_the_plan(self):
        """Mouse and touch pointers, from a plain step, from a branch of a decision and from a RAG node, and the Connections section: the edge is in the plan the server keeps."""
        import subprocess
        out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "plan_connect_check.js"), self.url, "connect-check", "Connect check", self.browser],
                             capture_output=True, text=True, timeout=400)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(len(results), 11, results)
        self.assertEqual([r for r in results if not r["ok"]], [])


if __name__ == "__main__":
    unittest.main()
