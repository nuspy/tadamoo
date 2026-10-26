"""Plans in the left column: a plan is duplicated or deleted, and the steps of its list move up and down."""
from serve.tests import test_api_plans as base

CARD, PLAN = base.CARD, base.PLAN


def chain(plan_id, count=3):
    nodes = {}
    for i in range(count):
        name = f"s{i}"
        nodes[name] = {"id": name, "type": "action", "title": f"Step {i}", "description": "", "assignee": "agent",
                       "dependencies": [f"s{i - 1}"] if i else [], "outputs": [f"s{i + 1}"] if i < count - 1 else []}
    return {"plan_id": plan_id, "title": "Chain", "target_type": "occasional", "nodes": nodes}


def side_by_side(plan_id):
    nodes = {n: {"id": n, "type": "action", "title": n.upper(), "description": "", "assignee": "agent", "dependencies": [], "outputs": []}
             for n in ("a", "b", "c")}
    return {"plan_id": plan_id, "title": "Side by side", "target_type": "occasional", "nodes": nodes}


class PlansManageTest(base.PlansApiBase):
    def move(self, plan_id, node, direction, **extra):
        return self.call("POST", f"/api/plans/{plan_id}/events", {"type": "plan:move", "node_id": node, "direction": direction, **extra})

    def test_a_duplicate_has_a_new_id_the_same_steps_and_the_title_the_page_gave(self):
        self.call("POST", "/api/plans", {"plan": {**PLAN, "plan_id": "dup_a1b2"}, "card": CARD})
        code, copy = self.call("POST", "/api/plans/dup_a1b2/duplicate", {"title": "Copia di Weekend"})
        self.assertEqual(code, 201)
        self.assertNotEqual(copy["plan"]["plan_id"], "dup_a1b2")
        self.assertTrue(copy["plan"]["plan_id"].startswith("dup_"))
        self.assertEqual(copy["plan"]["title"], "Copia di Weekend")
        self.assertEqual(sorted(copy["plan"]["nodes"]), ["hotel", "partenza"])
        self.assertEqual(copy["card"]["titolo"], "Copia di Weekend")
        self.assertEqual([h["type"] for h in copy["history"]], ["create"])                    # its own history
        self.assertEqual(self.call("GET", "/api/plans/dup_a1b2")[1]["plan"]["title"], "Weekend a Cervinia")   # the original is untouched
        code, other = self.call("POST", "/api/plans/dup_a1b2/duplicate", {})
        self.assertEqual((code, other["plan"]["title"]), (201, "Weekend a Cervinia (copy)"))
        self.assertNotEqual(other["plan"]["plan_id"], copy["plan"]["plan_id"])
        self.assertEqual(self.call("POST", "/api/plans/ghost/duplicate", {})[0], 404)

    def test_a_deleted_plan_is_gone_from_the_list_and_cannot_be_deleted_twice(self):
        self.call("POST", "/api/plans", {"plan": {**PLAN, "plan_id": "to_delete"}, "card": CARD})
        self.assertEqual(self.call("POST", "/api/plans/to_delete/delete", {}), (200, {"deleted": "to_delete"}))
        self.assertNotIn("to_delete", [p["plan_id"] for p in self.call("GET", "/api/plans")[1]["plans"]])
        self.assertEqual(self.call("GET", "/api/plans/to_delete")[0], 404)
        self.assertEqual(self.call("POST", "/api/plans/to_delete/delete", {})[0], 404)

    def test_steps_side_by_side_change_places_and_the_order_is_kept(self):
        self.call("POST", "/api/plans", {"plan": side_by_side("sbs")})
        self.assertEqual(self.call("GET", "/api/plans/sbs")[1]["order"], ["a", "b", "c"])
        code, res = self.move("sbs", "c", -1)
        self.assertEqual((code, res["order"]), (200, ["a", "c", "b"]))
        self.assertEqual(self.move("sbs", "a", 1)[1]["order"], ["c", "a", "b"])
        self.assertEqual(self.call("GET", "/api/plans/sbs")[1]["order"], ["c", "a", "b"])
        code, res = self.move("sbs", "c", -1)                                                 # already at the top
        self.assertEqual(code, 400)
        self.assertIn("top", res["error"])
        self.assertIn("bottom", self.move("sbs", "b", 1)[1]["error"])
        code, res = self.move("sbs", "ghost", 1)
        self.assertEqual(code, 400)
        self.assertIn("not found", res["error"])
        self.assertEqual(self.move("sbs", "a", 2)[0], 400)
        self.assertEqual(self.move("sbs", "a", True)[0], 400)

    def test_a_plain_chain_is_redrawn_when_two_steps_change_places(self):
        self.call("POST", "/api/plans", {"plan": chain("chain3")})
        code, res = self.move("chain3", "s2", -1)
        self.assertEqual((code, res["order"]), (200, ["s0", "s2", "s1"]))
        nodes = self.call("GET", "/api/plans/chain3")[1]["plan"]["nodes"]
        self.assertEqual((nodes["s0"]["outputs"], nodes["s2"]["dependencies"], nodes["s2"]["outputs"], nodes["s1"]["dependencies"]),
                         (["s2"], ["s0"], ["s1"], ["s2"]))
        self.assertEqual(self.call("GET", "/api/plans/chain3")[1]["layers"], [["s0"], ["s2"], ["s1"]])
        self.assertEqual(self.move("chain3", "s0", 1)[1]["order"], ["s2", "s0", "s1"])         # the first step goes down: s2 -> s0 -> s1

    def test_a_step_that_branches_or_has_started_does_not_change_places_and_nothing_changes(self):
        branching = chain("branch3")
        branching["nodes"]["s0"]["outputs"] = ["s1", "s2"]
        branching["nodes"]["s2"]["dependencies"] = ["s0", "s1"]
        branching["nodes"]["s1"]["outputs"] = ["s2"]
        self.call("POST", "/api/plans", {"plan": branching})
        before = self.call("GET", "/api/plans/branch3")[1]["plan"]["nodes"]
        code, res = self.move("branch3", "s1", -1)
        self.assertEqual(code, 400)
        self.assertIn("linked to others", res["error"])
        self.assertEqual(self.call("GET", "/api/plans/branch3")[1]["plan"]["nodes"], before)
        self.call("POST", "/api/plans", {"plan": chain("started")})
        self.call("POST", "/api/plans/started/events", {"type": "plan:mark", "node_id": "s0", "status": "in_progress"})
        code, res = self.move("started", "s1", -1)
        self.assertEqual(code, 400)
        self.assertIn("started or finished", res["error"])

    def test_a_move_made_on_an_old_reading_is_refused(self):
        self.call("POST", "/api/plans", {"plan": side_by_side("stale_sbs")})
        rev = self.call("GET", "/api/plans/stale_sbs")[1]["rev"]
        self.assertEqual(self.move("stale_sbs", "c", -1, base_rev=rev)[0], 200)
        code, res = self.move("stale_sbs", "b", -1, base_rev=rev)
        self.assertEqual(code, 400)
        self.assertTrue(res["stale"])
