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
        self.assertIn('el("div", { class: "pl-canvas" }, el("div", { class: "pl-stage" }, ui.graph, ui.list, ui.autoBtn, ui.autoBox), ui.detail)', JS)
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


if __name__ == "__main__":
    unittest.main()
