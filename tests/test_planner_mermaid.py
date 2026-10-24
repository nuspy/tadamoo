"""A plan as Mermaid text: shapes, links, status in words as well as colour, and nothing in a title can break out of its label."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.planner.mermaid import to_mermaid


def plan(**nodes):
    return {"plan_id": "p", "title": "Weekend a Cervinia", "nodes": nodes}


def node(i, title, type="action", status="pending", outputs=(), assignee="agent"):
    return {"id": i, "type": type, "title": title, "status": status, "outputs": list(outputs), "assignee": assignee}


def test_shapes_links_and_status_in_words():
    text = to_mermaid(plan(s1=node("s1", "Prenota", status="completed", outputs=["s2"]), s2=node("s2", "Meteo?", "decision", outputs=["s3", "s4"]),
                           s3=node("s3", "Sci", status="in_progress", assignee="user"), s4=node("s4", "Spa", "sensor", status="failed")))
    assert text.startswith("%% Weekend a Cervinia\nflowchart TD\n")
    assert 'n_s1["Prenota · completed"]' in text and 'n_s2{"Meteo?"}' in text and 'n_s4(["Spa · failed"])' in text and 'n_s3["Sci (you) · in progress"]' in text
    assert "n_s1 --> n_s2" in text and "n_s2 --> n_s3" in text and "n_s2 --> n_s4" in text
    assert "class n_s1 done" in text and "class n_s4 failed" in text and "classDef done" in text and "classDef blocked" not in text


def test_a_title_cannot_end_its_label_or_add_markup():
    text = to_mermaid(plan(s1=node("s1", 'Evil"] ; click n_s1 call alert() <script>x</script> |x| `y` {z}\nnext line')))
    line = next(ln for ln in text.splitlines() if ln.startswith("    n_s1["))
    inner = line[len('    n_s1["'):-2]
    assert '"' not in inner and "<" not in inner and ">" not in inner and "\n" not in inner
    for ch in "[](){}|`":
        assert ch not in inner
    assert text.count("\n    click ") == 0 and "script" in inner                  # the word stays as text; nothing is a statement of its own


def test_odd_ids_get_stable_distinct_names_and_missing_links_are_skipped():
    text = to_mermaid(plan(**{"a b": node("a b", "x", outputs=["a-b", "ghost"]), "a-b": node("a-b", "y")}))
    assert "n_a_b[" in text and "n_a_b_2[" in text and "n_a_b --> n_a_b_2" in text and "ghost" not in text
    assert to_mermaid(plan(**{"a b": node("a b", "x"), "a-b": node("a-b", "y")})) == to_mermaid(plan(**{"a b": node("a b", "x"), "a-b": node("a-b", "y")}))


def test_an_empty_plan_is_still_a_valid_diagram():
    assert to_mermaid({"plan_id": "p", "title": "Vuoto", "nodes": {}}).endswith("flowchart TD\n")
