"""The Plans page: a column of plans with open, duplicate and delete, four parts, links drawn between steps and rows that move up and down."""
import re
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
        self.assertIn('add_edges: [[a, b]]', JS)
        self.assertIn('remove_edges: [[a, b]]', JS)
        self.assertIn('port.addEventListener("pointerdown", e => startLink(e, n.id, side))', JS)
        self.assertIn('ev.key === "Escape"', JS)                                       # letting go elsewhere or Escape drops the link
        self.assertIn('e.key === "Delete"', JS)                                        # a picked link is removed by the keyboard too
        for cls in (".pl-port", ".pl-edgehit", ".pl-x", ".linktarget"):
            self.assertIn(cls, CSS)
        self.assertIn("function depsBox(", JS)                                         # the same by boxes, for the keyboard and the list

    def test_the_move_arrows_are_for_the_graph_and_the_rows_of_the_list_move_up_and_down(self):
        self.assertIn('S.mode === "graph" ? el("div", { class: "pl-acts pl-move"', JS)
        self.assertIn('type: "plan:move", node_id: id, direction: dir', JS)
        self.assertIn('S.snap.order || S.snap.layers.flat()', JS)
        self.assertIn('"plan:move"', re.search(r"const DIRECT = \[([^\]]*)\]", JS).group(1))

    def test_every_button_the_page_makes_has_a_tooltip(self):
        for key in ("pl.tip.graph", "pl.tip.list", "pl.tip.add", "pl.tip.save", "pl.tip.start", "pl.tip.done", "pl.tip.fail", "pl.tip.rewind", "pl.tip.remove",
                    "pl.tip.approve", "pl.tip.reject", "pl.tip.reset", "pl.tip.mermaid", "pl.tip.new", "pl.tip.open"):
            self.assertIn(f'T("{key}")', JS, key)
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        for sym in ("pl.move.left", "pl.move.up", "pl.move.down", "pl.move.right"):
            self.assertIn(f'title: T(key)', JS)
            self.assertEqual(I18N.count(f'"{sym}":'), 2)

    def test_a_decision_is_added_with_at_least_two_exits_each_with_a_condition(self):
        self.assertIn('id: "pl-a-exits"', JS)
        self.assertIn('if (rows.length < 2 || rows.some(r => !r.title || !r.cond))', JS)             # refused here, as the server refuses it
        self.assertIn("add[0].conditions[xid] = r.cond", JS)
        self.assertIn('document.getElementById("pl-a-exits").hidden = ', JS)                          # only a decision shows the exits
        self.assertIn("if (!(await userDiff({ add_nodes: add }, T(\"pl.add\")))) return;", JS)          # a refused change does not reset the form
        self.assertIn("#pl-a-exits[hidden]", CSS)
        self.assertIn("no_route", JS)                                                                # a decision no exit of which holds is shown with its facts

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


if __name__ == "__main__":
    unittest.main()
