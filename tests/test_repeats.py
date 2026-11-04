"""Work that comes back: the signature of a request, the groups in the outcome log, and the one-time hint to make a skill of it."""
import json

from tadamoo import repeats
from tadamoo.lessons import OutcomeLog
from tadamoo.stack import AgentConfig, build_agent


def record(request="Update the weekly sales report for the north region", steps=10, result="done", **kw):
    return {"result": result, "steps": steps, "request": request, **kw}


def test_a_signature_ignores_word_order_filler_words_case_and_accents():
    a = repeats.request_signature("Update the weekly sales report for the north region")
    assert a and a == repeats.request_signature("please UPDATE the report: sales weekly, north region")
    assert repeats.request_signature("Aggiorna il rapporto settimanale delle vendite") == repeats.request_signature("aggiorna rapporto settimanale vendite")
    assert repeats.request_signature("Caffè") == ""                                          # one word does not tell requests apart
    assert repeats.request_signature("") == repeats.request_signature(None) == ""
    assert repeats.request_signature("The user answered your questions:\n1. Which? -> yes") == ""
    assert repeats.request_signature("Update the weekly sales report") != repeats.request_signature("Update the monthly sales report")


def test_only_long_finished_work_counts_and_three_times_makes_a_group():
    log = [record(), record(steps=12), record(steps=7), record(result="failed"), record(result="stopped"), record("Fix the failing build on windows", steps=20)]
    assert repeats.groups(log) == []                                                          # two long, finished runs of the sales report
    log.append(record(steps=8))
    (g,) = repeats.groups(log)
    assert g["count"] == 3 and g["steps_avg"] == 10.0 and g["request"].startswith("Update the weekly") and g["signature"]
    assert repeats.groups(log, min_count=1)[0]["signature"] == g["signature"] and len(repeats.groups(log, min_count=1)) == 2
    assert repeats.groups([{"steps": "x"}, "junk", {"result": "done", "steps": True, "request": "a b c"}]) == []


def test_the_hint_comes_once_when_the_third_long_run_is_logged(tmp_path):
    log = OutcomeLog(tmp_path)
    hints = []
    for i in range(5):
        saved = log.append(record(steps=9 + i))
        hints.append(repeats.hint_after(log, saved))
    assert hints[:2] == [None, None] and hints[3:] == [None, None]
    assert hints[2]["count"] == 3 and hints[2]["steps_avg"] == 10.0 and hints[2]["request"].startswith("Update the weekly")
    assert json.loads((tmp_path / ".tadamoo" / "repeats.json").read_text(encoding="utf-8"))["told"] == [hints[2]["signature"]]


def test_a_short_or_unfinished_run_never_raises_the_hint_and_other_requests_have_their_own(tmp_path):
    log = OutcomeLog(tmp_path)
    for _ in range(3):
        assert repeats.hint_after(log, log.append(record(steps=7))) is None
        assert repeats.hint_after(log, log.append(record(result="failed"))) is None
    other = "Rename the exported invoices by customer name"
    got = [repeats.hint_after(log, log.append(record(other, steps=11))) for _ in range(3)]
    assert got[:2] == [None, None] and got[2]["request"] == other


def test_a_damaged_state_file_is_the_same_as_none(tmp_path):
    (tmp_path / ".tadamoo").mkdir()
    (tmp_path / ".tadamoo" / "repeats.json").write_text("{not json", encoding="utf-8")
    log = OutcomeLog(tmp_path)
    got = [repeats.hint_after(log, log.append(record())) for _ in range(3)]
    assert got[2] is not None


def test_a_conversation_tells_the_page_once_through_its_events(tmp_path):
    (tmp_path / "ws").mkdir()
    built = build_agent(AgentConfig(workspace=str(tmp_path / "ws"), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False, permission_mode="default",
                                    lessons_dir=str(tmp_path / "lessons")), client=object())
    seen = []
    built.agent.on_event = seen.append
    for steps in (9, 10, 11, 12):
        built.agent.learn(record(steps=steps), [])
    hints = [e for e in seen if e.get("type") == "script_hint"]
    assert len(hints) == 1 and hints[0]["count"] == 3 and hints[0]["request"].startswith("Update the weekly")
    assert len(OutcomeLog(tmp_path / "ws").read()) == 4


def test_the_lessons_view_lists_the_repeated_work(tmp_path):
    from serve.lessons_api import LessonsApi
    project = tmp_path / "proj"
    project.mkdir()
    log = OutcomeLog(project)
    for _ in range(3):
        log.append(record())
    view = LessonsApi(lambda: [("proj", project)], tmp_path / "glob").overview(str(project))
    assert [(g["count"], g["request"][:6]) for g in view["repeats"]] == [(3, "Update")]
