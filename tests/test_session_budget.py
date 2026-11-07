"""How much of its day a conversation has spent: a cumulative total that only grows within one process, reset by a restart (a lower
number) or by a new day (serve/session_budget.py)."""
from serve.session_budget import SessionBudget


def test_usage_adds_up_within_one_process_lifetime():
    b = SessionBudget()
    b.record("s1", 100)
    b.record("s1", 250)
    b.record("s1", 400)
    assert b.used_today("s1") == 400


def test_a_lower_total_means_the_process_restarted_and_counts_as_new_usage():
    b = SessionBudget()
    b.record("s1", 300)
    b.record("s1", 50)                   # the process died and started again
    assert b.used_today("s1") == 350


def test_sessions_are_counted_apart():
    b = SessionBudget()
    b.record("s1", 100)
    b.record("s2", 500)
    assert b.used_today("s1") == 100 and b.used_today("s2") == 500


def test_bad_values_are_ignored():
    b = SessionBudget()
    b.record("s1", -5)
    b.record("s1", True)
    b.record("s1", "100")
    assert b.used_today("s1") == 0


def test_over_is_false_without_a_limit_and_true_once_the_limit_is_reached():
    b = SessionBudget()
    assert b.over("s1", None) is False
    b.record("s1", 1000)
    assert b.over("s1", None) is False
    assert b.over("s1", 2000) is False
    assert b.over("s1", 1000) is True
    assert b.over("s1", 500) is True


def test_a_new_day_starts_the_count_over(monkeypatch):
    import datetime
    b = SessionBudget()
    b.record("s1", 900)
    assert b.used_today("s1") == 900
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    monkeypatch.setattr(SessionBudget, "_today", staticmethod(lambda: tomorrow.isoformat()))
    assert b.used_today("s1") == 0
    b.record("s1", 50)
    assert b.used_today("s1") == 50
