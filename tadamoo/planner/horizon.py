"""What a plan says about time and money: the long-term aims of a constant plan, the dates and cost of an occasional one.

A constant plan ("keep healthy", "save for a house") has no end: it carries aims in words, an optional budget (per month or in all),
the periods in which the person has already committed their time, and how often it must be looked at again (at most a month). An
occasional plan ("a weekend in Cervinia") has a start, an end and a cost. These are what `alignment` compares; nothing else about a
plan is read as a number.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

MAX_GOALS, MAX_COMMITMENTS, TEXT_CHARS = 12, 30, 300
MAX_REVIEW_DAYS = 30
PERIODS = ("month", "total")


def _date(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError(f"{what} is a date written like 2026-11-24") from None


def _money(value: Any, what: str, period: bool = False) -> Optional[Dict[str, str]]:
    if value in (None, "", {}):
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{what} is an amount and a currency")
    try:
        amount = Decimal(str(value.get("amount", "")).strip().replace(",", "."))
    except InvalidOperation:
        raise ValueError(f"{what}: the amount is a number") from None
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        raise ValueError(f"{what}: the amount is positive with at most two decimals")
    currency = str(value.get("currency", "")).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError(f"{what}: the currency is a three-letter code")
    out = {"amount": f"{amount:.2f}", "currency": currency}
    if period:
        per = str(value.get("period", "month")).strip().lower()
        if per not in PERIODS:
            raise ValueError(f"{what}: the period is month or total")
        out["period"] = per
    return out


@dataclass
class PlanProfile:
    goals: List[str] = field(default_factory=list)                  # a constant plan: what it is for, in the person's words
    review_every_days: int = MAX_REVIEW_DAYS
    last_review: str = ""
    budget: Optional[Dict[str, str]] = None                         # a constant plan: what it lets be spent
    commitments: List[Dict[str, str]] = field(default_factory=list)  # a constant plan: periods already taken, {"text", "from", "to"}
    starts_on: str = ""                                             # an occasional plan
    ends_on: str = ""
    cost: Optional[Dict[str, str]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"goals": list(self.goals), "review_every_days": self.review_every_days, "last_review": self.last_review, "budget": self.budget,
                "commitments": [dict(c) for c in self.commitments], "starts_on": self.starts_on, "ends_on": self.ends_on, "cost": self.cost}

    @classmethod
    def from_dict(cls, data: Any) -> "PlanProfile":
        if data in (None, {}):
            return cls()
        if not isinstance(data, dict):
            raise ValueError("the profile is an object")
        unknown = sorted(set(data) - {"goals", "review_every_days", "last_review", "budget", "commitments", "starts_on", "ends_on", "cost"})
        if unknown:
            raise ValueError(f"the profile has no field: {', '.join(unknown)}")
        goals = data.get("goals") or []
        if not isinstance(goals, list) or len(goals) > MAX_GOALS or any(not isinstance(g, str) or not g.strip() or len(g) > TEXT_CHARS for g in goals):
            raise ValueError(f"goals are up to {MAX_GOALS} lines of text, each up to {TEXT_CHARS} characters")
        review = data.get("review_every_days", MAX_REVIEW_DAYS)
        if isinstance(review, bool) or not isinstance(review, int) or not 1 <= review <= MAX_REVIEW_DAYS:
            raise ValueError(f"a long-term plan is looked at again every 1 to {MAX_REVIEW_DAYS} days")
        commitments = data.get("commitments") or []
        if not isinstance(commitments, list) or len(commitments) > MAX_COMMITMENTS:
            raise ValueError(f"commitments are up to {MAX_COMMITMENTS} periods")
        clean = []
        for c in commitments:
            if not isinstance(c, dict) or not str(c.get("text", "")).strip() or len(str(c["text"])) > TEXT_CHARS:
                raise ValueError("a commitment says what it is, and from when to when")
            start, end = _date(c.get("from"), "a commitment's start"), _date(c.get("to"), "a commitment's end")
            if not start or not end or start > end:
                raise ValueError("a commitment goes from a day to the same or a later day")
            clean.append({"text": str(c["text"]).strip(), "from": start, "to": end})
        starts, ends = _date(data.get("starts_on"), "the start"), _date(data.get("ends_on"), "the end")
        if starts and ends and starts > ends:
            raise ValueError("the plan cannot end before it starts")
        return cls(goals=[g.strip() for g in goals], review_every_days=review, last_review=_date(data.get("last_review"), "the last review"),
                   budget=_money(data.get("budget"), "the budget", period=True), commitments=clean, starts_on=starts, ends_on=ends,
                   cost=_money(data.get("cost"), "the cost"))

    def review_due(self, today: Optional[datetime.date] = None) -> bool:
        """A long-term plan not looked at for longer than it said it should be (never looked at counts as due)."""
        if not self.last_review:
            return True
        today = today or datetime.date.today()
        return (today - datetime.date.fromisoformat(self.last_review)).days > self.review_every_days
