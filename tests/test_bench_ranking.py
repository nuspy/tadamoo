"""Leaderboard entries, the place among them and the interval (harness/bench/ranking.py). CPU only."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.bench import ranking

CSV = """Model,Harness,Interception Rate,Reward Rate,Date
alpha,Hermes,45.0%,30.5,2026-01-01
beta,Hermes,20,10,2026-01-02
gamma,Hermes,5,2,2026-01-03
delta,Hermes,20,,2026-01-04
"""


def test_load_csv_reads_names_and_numeric_columns():
    entries = ranking.load_csv(CSV)
    assert [e.name for e in entries] == ["alpha", "beta", "gamma", "delta"]
    assert entries[0].metrics == {"interception_rate": 45.0, "reward_rate": 30.5}
    assert "date" not in entries[0].metrics and "reward_rate" not in entries[3].metrics


def test_load_csv_from_a_file_and_with_a_bom(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("﻿name,score\nx,1\n", encoding="utf-8")
    assert ranking.load_csv(p)[0].metrics == {"score": 1.0}
    assert ranking.load_csv(str(p))[0].name == "x"
    assert ranking.load_csv("") == [] and ranking.load_csv("name,score\n") == []


def test_rank_position_percentile_and_neighbours():
    entries = ranking.load_csv(CSV)
    r = ranking.rank({"reward_rate": 0.15}, entries, "reward_rate")
    assert r["position"] == 2 and r["of"] == 4 and r["entries_compared"] == 3 and r["entries_without_metric"] == 1
    assert r["our_value"] == pytest.approx(15.0) and r["percentile"] == pytest.approx(66.7, abs=0.1)
    assert r["nearest_above"] == {"name": "alpha", "value": 30.5}
    assert r["nearest_below"] == {"name": "beta", "value": 10.0}


def test_rank_ties_share_a_position_and_ends():
    entries = ranking.load_csv(CSV)
    tie = ranking.rank({"interception_rate": 20.0}, entries, "interception_rate", scale="percent")
    assert tie["position"] == 2 and tie["percentile"] == pytest.approx(50.0, abs=0.1)
    top = ranking.rank({"interception_rate": 90}, entries, "interception_rate")
    assert top["position"] == 1 and top["nearest_above"] is None
    bottom = ranking.rank({"interception_rate": 0.01}, entries, "interception_rate")
    assert bottom["position"] == 5 and bottom["of"] == 5 and bottom["nearest_below"] is None and bottom["percentile"] == 0.0


def test_scales_fraction_and_percent_are_matched():
    frac = ranking.load_csv("name,rate\na,0.5\nb,0.2\n")
    assert ranking.rank({"rate": 0.3}, frac, "rate")["position"] == 2
    assert ranking.rank({"rate": 30}, frac, "rate")["position"] == 2                    # percent given, fractions in the file
    assert ranking.rank({"rate": 0.3}, ranking.load_csv("name,rate\na,50\nb,20\n"), "rate")["position"] == 2


def test_rank_with_no_entries_and_a_missing_metric():
    r = ranking.rank({"m": 1.0}, [], "m")
    assert r["position"] == 1 and r["of"] == 1 and r["percentile"] is None
    with pytest.raises(KeyError, match="no metric"):
        ranking.rank({"x": 1}, [], "m")


def test_wilson_interval():
    i = ranking.interval(5, 10)
    assert 0.23 < i["low"] < 0.24 and 0.76 < i["high"] < 0.77 and i["n"] == 10
    assert ranking.interval(0, 0) == {"low": 0.0, "high": 0.0, "n": 0}


def test_simulate_with_entries_and_the_official_judge():
    entries = ranking.load_csv(CSV)
    s = ranking.simulate({"reward_rate": 0.15}, entries, "reward_rate", official_judge=True, successes=3, trials=20)
    assert s["basis"] == "entries" and s["provisional"] is False and s["position"] == 2
    assert s["position_range"]["best"] <= s["position"] <= s["position_range"]["worst"]
    assert s["interval"]["n"] == 20 and "Unofficial" not in s["note"]


def test_simulate_without_the_official_judge_is_provisional():
    entries = ranking.load_csv(CSV)
    s = ranking.simulate({"reward_rate": 0.15}, entries, "reward_rate", official_judge=False)
    assert s["provisional"] and "Unofficial judge: not comparable with the board" in s["note"]


def test_simulate_without_entries_gives_only_the_local_score():
    s = ranking.simulate({"resolution_rate": 0.2}, [], "resolution_rate", official_judge=True, successes=2, trials=10)
    assert s["basis"] == "local_only" and s["position"] is None and s["interval"]["n"] == 10 and "cannot be shown" in s["note"]
