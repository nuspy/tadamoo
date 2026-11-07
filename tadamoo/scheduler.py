"""Routines: recurring tasks for the agent ("every morning run the tests and summarise", "check the pull requests").

A routine is a prompt (or a personal skill), a folder and a schedule. When its time comes it starts by itself, in Automatic:
nothing is asked of the person while it runs. Routines run in parallel; the same routine never overlaps with itself (a
second due time waits for the run in progress). A routine runs through the agent's conversation path
(`SessionRoutineRunner` uses the server's `SessionManager`), so its tools, audit and checkpoints are the ones of any
conversation. Time and steps limits are optional per routine (empty: none), and so is `yield_s`, how long a run goes on once
a conversation of the person's is at work (empty: it is never stopped for one).

Only a request that is the person's alone to settle (another person, a booking, a resource the agent does not control) is
parked, in a list of pending requests; the rest of the run goes on. A run that can only continue once the person answers
ends with the status "parked", not "failed".

Schedules are written as text:
  every 30s | every 30m | every 2h | every 1d       an interval
  daily 08:30 | weekdays 08:30 | weekends 10:00 | mon,wed,fri 18:15       fixed local times
  cron 50 14 * * 1-5                                                        a cron expression: minute hour day-of-month month day-of-week
  at 2026-12-24 18:00                                                       one moment, once

A routine is created enabled; a disabled one never runs on its own. Overdue interval routines run once when the program
starts, and so does a fixed time that passed while the program was off (once, late). Routines, results and pending requests
are kept in files in the folder given at construction (the user data folder).

Every scheduled fire has a run id worked out from the routine and the planned local time, and is written to the fire log
before it starts, so a restart never runs the same fire twice. A routine with `ttl_min` does not run a fire that is later
than that (the log says `skipped_late`). Routines that share a `dedup_key` never have two fires in flight at once.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

ROUTINES_FILE = "routines.json"
RUNS_FILE = "routine_runs.json"
PENDING_FILE = "routine_pending.json"
FIRES_FILE = "fires.json"
KEEP_FIRES = 500
KEY_MAX = 80
NO_STEP_LIMIT = -1                  # `max_turns` the conversation path reads as no limit
KEEP_RUNS = 200
NAME_MAX, PROMPT_MAX, SUMMARY_MAX = 80, 4000, 4000
MESSAGE_MAX = 4000
DELIVER_KINDS = ("new_session", "session", "plan")
CHECK_MAX, CHECK_TIMEOUT_S, CHECK_OUTPUT_MAX = 1000, 300, 2000
STOP_FILE = "routines.stop"         # while this file exists in the data folder no routine starts and the ones running are stopped
OUTCOMES = ("nothing_new", "progress", "done", "blocked")
_OUTCOME = re.compile(r"^\s*OUTCOME:\s*([a-z_]+)\s*$", re.I | re.M)
PREVIOUS_RUNS = 3                   # earlier runs whose outcome a run is told about
YIELD_MAX_S = 3600                  # the longest a routine may go on once a conversation of the person's is at work
VALUES_MAX = 8000                                                     # what is kept of a skill's values: names and short values
_SKILL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_INTERVAL = re.compile(r"^every\s+(\d+)\s*([smhd])$")
_FIXED = re.compile(r"^([a-z,\s]+?)\s+(\d{1,2}):(\d{2})$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_AT = re.compile(r"^at\s+(\d{4})-(\d{2})-(\d{2})[ t](\d{1,2}):(\d{2})$")
_KEY = re.compile(r"^[A-Za-z0-9_.:-]*$")
_CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_CRON_HELP = "A cron schedule is five fields: minute hour day-of-month month day-of-week, for example 'cron 50 14 * * 1-5'."
SEARCH_DAYS = 366 * 8                # how far a cron expression is searched before it is called a schedule that never comes


def outcome_of(outcome: Dict[str, Any]) -> str:
    """The one word a run leaves for the next one: the `OUTCOME:` line the agent ended with, else what the status says."""
    given = str(outcome.get("outcome") or "")
    if given in OUTCOMES:
        return given
    found = _OUTCOME.findall(str(outcome.get("summary") or ""))
    if found and found[-1].lower() in OUTCOMES:
        return found[-1].lower()
    return {"done": "done", "parked": "blocked"}.get(str(outcome.get("status")), "")


def fire_id(routine_id: str, planned: float) -> str:
    """The id of one scheduled fire: the routine and its planned local time, so the hour that repeats when the clocks go back is one fire."""
    local = dt.datetime.fromtimestamp(planned).replace(microsecond=0).isoformat()
    return hashlib.sha1(f"{routine_id}|{local}".encode("utf-8")).hexdigest()[:12]


def _cron_field(text: str, low: int, high: int) -> "tuple[set, bool]":
    """The values one cron field names, and whether it starts with '*' (day-of-month and day-of-week then combine differently)."""
    values: set = set()
    for part in text.split(","):
        body, slash, step = part.partition("/")
        if slash and (not step.isdigit() or int(step) < 1):
            raise ValueError(part)
        stride = int(step) if slash else 1
        if body == "*":
            first, last = low, high
        elif "-" in body:
            a, _, b = body.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(part)
            first, last = int(a), int(b)
        elif body.isdigit():
            first = int(body)
            last = high if slash else first
        else:
            raise ValueError(part)
        if first < low or last > high or first > last:
            raise ValueError(part)
        values.update(range(first, last + 1, stride))
    return values, text.startswith("*")


class RoutineError(ValueError):
    """A routine that cannot be saved or found: a message fit for the person, and a code."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class Schedule:
    """`interval_s` for "every ..."; the days of the week (0 is Monday) and the time of day for a fixed time; `cron` for a cron
    expression; `once` for one moment (a timestamp)."""

    def __init__(self, text: str, interval_s: int = 0, days: Optional[List[int]] = None, at: Optional[dt.time] = None,
                 cron: Optional[Dict[str, Any]] = None, once: Optional[float] = None) -> None:
        self.text, self.interval_s, self.days, self.at, self.cron, self.once = text, interval_s, days or [], at, cron, once

    @classmethod
    def parse(cls, text: Any) -> "Schedule":
        if not isinstance(text, str) or not text.strip():
            raise RoutineError("Write when it runs, for example 'every 2h' or 'weekdays 08:30'.", "bad_schedule")
        norm = " ".join(text.lower().split())
        if norm.startswith("cron "):
            return cls._parse_cron(norm)
        m = _AT.match(norm)
        if m:
            try:
                when = dt.datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]))
            except ValueError:
                raise RoutineError("The moment is written YYYY-MM-DD HH:MM, in local time.", "bad_schedule") from None
            return cls(norm, once=when.timestamp())
        m = _INTERVAL.match(norm)
        if m:
            seconds = int(m.group(1)) * _UNITS[m.group(2)]
            if seconds < 1:
                raise RoutineError("An interval is at least one second.", "bad_schedule")
            return cls(norm, interval_s=seconds)
        m = _FIXED.match(norm)
        if m:
            hour, minute = int(m.group(2)), int(m.group(3))
            if hour > 23 or minute > 59:
                raise RoutineError("The time is HH:MM, from 00:00 to 23:59.", "bad_schedule")
            names = m.group(1).replace(" ", "")
            days = {"daily": list(range(7)), "weekdays": list(range(5)), "weekends": [5, 6]}.get(names)
            if days is None:
                parts = names.split(",")
                if any(p not in DAYS for p in parts):
                    raise RoutineError("Days are daily, weekdays, weekends or a list such as mon,wed,fri.", "bad_schedule")
                days = sorted({DAYS.index(p) for p in parts})
            return cls(norm, days=days, at=dt.time(hour, minute))
        raise RoutineError("Write when it runs, for example 'every 2h' or 'weekdays 08:30'.", "bad_schedule")

    @classmethod
    def _parse_cron(cls, norm: str) -> "Schedule":
        fields = norm.split()[1:]
        if len(fields) != 5:
            raise RoutineError(_CRON_HELP, "bad_schedule")
        try:
            (minutes, _), (hours, _), (dom, dom_star), (months, _), (dow, dow_star) = [_cron_field(f, lo, hi) for f, (lo, hi) in zip(fields, _CRON_RANGES)]
        except ValueError:
            raise RoutineError(_CRON_HELP, "bad_schedule") from None
        return cls(norm, cron={"times": sorted((h, m) for h in hours for m in minutes), "dom": dom, "months": months, "dow": {d % 7 for d in dow},
                               "dom_star": dom_star, "dow_star": dow_star})

    def _cron_day(self, day: dt.date) -> bool:
        """Whether a day passes the date fields; when both day-of-month and day-of-week are restricted, either one is enough."""
        c = self.cron
        if day.month not in c["months"]:
            return False
        dom_ok, dow_ok = day.day in c["dom"], (day.weekday() + 1) % 7 in c["dow"]
        return dom_ok and dow_ok if c["dom_star"] or c["dow_star"] else dom_ok or dow_ok

    def occurrences(self, since: float, now: float, cap: int = 1000) -> int:
        """How many times this schedule came in (since, now]."""
        if self.interval_s:
            return int(max(0.0, now - since) // self.interval_s)
        if self.once is not None:
            return int(since < self.once <= now)
        count, moment = 0, self.previous(now)
        while moment > since and count < cap:
            count += 1
            moment = self.previous(moment - 1)
        return count

    def next_after(self, moment: float) -> float:
        """The next time to run after `moment`, as a timestamp (infinity for a single moment that has passed)."""
        if self.interval_s:
            return moment + self.interval_s
        if self.once is not None:
            return self.once if self.once > moment else math.inf
        start = dt.datetime.fromtimestamp(moment)
        if self.cron:
            for offset in range(SEARCH_DAYS):
                day = start.date() + dt.timedelta(days=offset)
                if self._cron_day(day):
                    for hour, minute in self.cron["times"]:
                        when = dt.datetime.combine(day, dt.time(hour, minute)).timestamp()
                        if when > moment:
                            return when
            raise RoutineError("This schedule never comes.", "bad_schedule")
        for offset in range(8):
            day = start.date() + dt.timedelta(days=offset)
            when = dt.datetime.combine(day, self.at)
            if day.weekday() in self.days and when.timestamp() > moment:
                return when.timestamp()
        raise RoutineError("This schedule never comes.", "bad_schedule")

    def previous(self, moment: float) -> float:
        """The latest fixed time at or before `moment` (0 for an interval, which has no fixed times)."""
        if self.interval_s:
            return 0.0
        if self.once is not None:
            return self.once if self.once <= moment else 0.0
        start = dt.datetime.fromtimestamp(moment)
        if self.cron:
            for offset in range(SEARCH_DAYS):
                day = start.date() - dt.timedelta(days=offset)
                if self._cron_day(day):
                    for hour, minute in reversed(self.cron["times"]):
                        when = dt.datetime.combine(day, dt.time(hour, minute)).timestamp()
                        if when <= moment:
                            return when
            return 0.0
        for offset in range(8):
            day = start.date() - dt.timedelta(days=offset)
            when = dt.datetime.combine(day, self.at).timestamp()
            if day.weekday() in self.days and when <= moment:
                return when
        return 0.0


class Runner(Protocol):
    def run(self, routine: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
        """{"status": ..., "summary": ..., "session": ...}"""


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    finally:
        if Path(tmp).exists():
            Path(tmp).unlink(missing_ok=True)


class Scheduler:
    def __init__(self, directory: str | Path, runner: Runner, busy: Optional[Callable[[], bool]] = None,
                 clock: Callable[[], float] = time.time, tick_s: float = 5.0, poll_s: float = 1.0,
                 meter: Optional[Callable[[], Any]] = None) -> None:
        self.directory = Path(directory)
        self.runner = runner
        self.busy = busy or getattr(runner, "busy", None) or (lambda: False)
        self.clock, self.tick_s, self.poll_s = clock, tick_s, poll_s
        self.meter = meter                                                   # makes an EnergyMeter (tadamoo/energy.py) for each run
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._queue: List[str] = []                                          # routines waiting to start, each at most once
        self._claims: Dict[str, str] = {}                                    # routine id -> run id of the scheduled fire waiting in the queue
        self._manual: set = set()                                            # queued because the person asked, enabled or not
        self._due: Dict[str, float] = {}
        self._active: Dict[str, Dict[str, Any]] = {}                         # routine id -> the cancel and yield events and run id of its run
        self._workers: List[threading.Thread] = []
        self.routines: List[Dict[str, Any]] = [self._normalize(r) for r in self._load(ROUTINES_FILE, "routines")]
        self.results: List[Dict[str, Any]] = [{**r, "status": "parked" if r.get("status") == "asked" else r.get("status")}
                                              for r in self._load(RUNS_FILE, "runs")]
        self.pending_requests: List[Dict[str, Any]] = self._load(PENDING_FILE, "pending")
        self.fires: List[Dict[str, Any]] = self._load(FIRES_FILE, "fires")
        interrupted = [f for f in self.fires if f.get("state") == "claimed"]          # claimed by a run that never got to say how it ended
        for f in interrupted:
            f["state"] = "interrupted"
        if interrupted:
            self._save_fires()
        if hasattr(runner, "park"):
            runner.park = self._park
        now = self.clock()
        for r in self.routines:
            self._due[r["id"]] = self._first_due(r, now)
        self._threads = [threading.Thread(target=self._tick_loop, name="routine-clock", daemon=True),
                         threading.Thread(target=self._work_loop, name="routine-worker", daemon=True)]

    # -- files -----------------------------------------------------------------------------------

    def _load(self, name: str, key: str) -> List[Dict[str, Any]]:
        try:
            data = json.loads((self.directory / name).read_text(encoding="utf-8"))
            items = data.get(key) if isinstance(data, dict) else None
        except (OSError, ValueError):
            items = None
        return [i for i in items or [] if isinstance(i, dict) and i.get("id")]

    @staticmethod
    def _normalize(routine: Dict[str, Any]) -> Dict[str, Any]:
        """A stored routine in the current shape: no mode or allow rules (every run is Automatic), limits empty or a whole number."""
        r = {k: v for k, v in routine.items() if k not in ("mode", "allow")}
        for key in ("max_turns", "max_minutes"):
            v = r.get(key)
            r[key] = v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else None
        if r.get("last_status") == "asked":
            r["last_status"] = "parked"
        ttl = r.get("ttl_min")
        r["ttl_min"] = ttl if isinstance(ttl, int) and not isinstance(ttl, bool) and ttl >= 1 else None
        r["dedup_key"] = r["dedup_key"] if isinstance(r.get("dedup_key"), str) else ""
        r["last_planned"] = float(r.get("last_planned") or 0)
        deliver_to = r.get("deliver_to")
        r["deliver_to"] = deliver_to if isinstance(deliver_to, dict) and deliver_to.get("kind") in DELIVER_KINDS else {"kind": "new_session", "ref": ""}
        r["message"] = r["message"] if isinstance(r.get("message"), str) else ""
        link = r.get("link")
        r["link"] = {"plan": str(link["plan"])} if isinstance(link, dict) and isinstance(link.get("plan"), str) and link["plan"] else {}
        return r

    def _save(self) -> None:
        _write_json(self.directory / ROUTINES_FILE, {"schema": "1.0", "routines": self.routines})

    def _save_runs(self) -> None:
        _write_json(self.directory / RUNS_FILE, {"schema": "1.0", "runs": self.results[-KEEP_RUNS:]})

    def _save_fires(self) -> None:
        _write_json(self.directory / FIRES_FILE, {"schema": "1.0", "fires": self.fires[-KEEP_FIRES:]})

    def _save_pending(self) -> None:
        _write_json(self.directory / PENDING_FILE, {"schema": "1.0", "pending": self.pending_requests})

    def _first_due(self, routine: Dict[str, Any], now: float) -> float:
        """When the program starts: an interval counts from the last run or fire (so an overdue one is due at once); a fixed time that
        passed since the routine last ran or fired (or was made or turned on) is due at once, once, and keeps its planned time (so a
        routine with a time to live can tell it is late); otherwise the next time."""
        schedule = Schedule.parse(routine["schedule"])
        if schedule.interval_s:
            base = max(float(routine.get("last_run") or 0), float(routine.get("last_planned") or 0))
            return base + schedule.interval_s if base else schedule.next_after(now)
        since = max(float(routine.get(k) or 0) for k in ("last_run", "last_planned", "created", "enabled_at"))
        missed = schedule.previous(now)
        return missed if missed > since else schedule.next_after(now)

    # -- the list --------------------------------------------------------------------------------

    def previous(self, routine_id: str) -> List[Dict[str, Any]]:
        """The outcome and the first words of the last runs of a routine, oldest first: what the next run is told."""
        done = [r for r in self.results if r.get("routine") == routine_id and r.get("status") not in ("skipped", "skipped_paused")]
        return [{"at": r.get("started"), "outcome": r.get("outcome") or "", "status": r.get("status"), "summary": str(r.get("summary") or "")[:300]} for r in done[-PREVIOUS_RUNS:]]

    def view(self) -> Dict[str, Any]:
        with self._lock:
            return {"routines": [{**r, "next_run": self._due.get(r["id"]) if r.get("enabled") and math.isfinite(self._due.get(r["id"], math.inf)) else None} for r in self.routines],
                    "running": list(self._active), "queued": list(self._queue), "pending": [dict(p) for p in self.pending_requests], "stopped": self.stopped()}

    def get(self, routine_id: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._find(routine_id))

    def _find(self, routine_id: str) -> Dict[str, Any]:
        r = next((r for r in self.routines if r["id"] == routine_id), None)
        if r is None:
            raise RoutineError("That routine is not in the list any more.", "gone")
        return r

    def add(self, name: Any, prompt: Any, workspace: Any, schedule: Any, max_turns: Any = None, max_minutes: Any = None,
            skill: Any = "", values: Any = None, yield_s: Any = None, index_past: Any = False, check: Any = "",
            ttl_min: Any = None, dedup_key: Any = "", deliver_to: Any = None, message: Any = "", link: Any = None) -> Dict[str, Any]:
        """A new routine, enabled. With a `skill` it runs that personal skill with `values` instead of a prompt.
        `max_turns` and `max_minutes` limit the steps and the time of a run, `yield_s` how long it may go on once a conversation
        of the person's is at work; empty means no limit. `index_past` adds what the routine does to the index of earlier conversations (off unless the person asks).
        `check` is a command run before the model is woken: exit code 0 goes on (what it prints is given to the model), 1 means nothing to do and the run ends there, anything else fails the run.
        `ttl_min` is how late a scheduled fire may start (empty: any time); `dedup_key` keeps routines that share it from having two fires in flight.
        `deliver_to` ({"kind": "new_session"|"session"|"plan", "ref": name}) sends `message` (or `prompt` when it is empty) to an existing
        conversation instead of starting one; `link` ({"plan": plan_id}) is which plan a "plan" delivery speaks for."""
        routine = self._check(name, prompt, workspace, schedule, max_turns, max_minutes, skill, values, yield_s, index_past, check, ttl_min, dedup_key,
                              deliver_to, message, link)
        now = self.clock()
        routine.update(id=secrets.token_hex(3), enabled=True, enabled_at=now, created=now, last_run=0, last_planned=0, last_status="")
        with self._lock:
            self.routines.append(routine)
            self._due[routine["id"]] = self._advance(routine, now)
            self._save()
        return dict(routine)

    def update(self, routine_id: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            current = self._find(routine_id)
            merged = {**current, **{k: v for k, v in fields.items() if k in ("name", "prompt", "workspace", "schedule", "max_turns", "max_minutes", "skill", "values", "yield_s", "index_past", "check", "ttl_min",
                                                    "dedup_key", "deliver_to", "message", "link")}}
            checked = self._check(merged["name"], merged["prompt"], merged["workspace"], merged["schedule"], merged.get("max_turns"),
                                  merged.get("max_minutes"), merged.get("skill", ""), merged.get("values"), merged.get("yield_s"), merged.get("index_past", False),
                                  merged.get("check", ""), merged.get("ttl_min"), merged.get("dedup_key", ""), merged.get("deliver_to"),
                                  merged.get("message", ""), merged.get("link"))
            current.update(checked)
            self._due[routine_id] = self._advance(current, self.clock())
            self._save()
            return dict(current)

    @staticmethod
    def _check(name: Any, prompt: Any, workspace: Any, schedule: Any, max_turns: Any = None, max_minutes: Any = None,
               skill: Any = "", values: Any = None, yield_s: Any = None, index_past: Any = False, check: Any = "",
               ttl_min: Any = None, dedup_key: Any = "", deliver_to: Any = None, message: Any = "", link: Any = None) -> Dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name) > NAME_MAX:
            raise RoutineError(f"Give the routine a name of at most {NAME_MAX} characters.", "bad_name")
        if skill:
            if not isinstance(skill, str) or not _SKILL.match(skill):
                raise RoutineError("Name one of your personal skills.", "bad_skill")
            if values is not None and (not isinstance(values, dict) or len(values) > 40 or len(json.dumps(values, default=str)) > VALUES_MAX
                                       or any(not isinstance(k, str) or len(k) > 60 for k in values)):
                raise RoutineError("The values of the skill are a short list of names and values.", "bad_values")
            prompt = prompt if isinstance(prompt, str) and prompt.strip() else f"Skill: {skill}"
        elif values:
            raise RoutineError("Values go with a skill.", "bad_values")
        if not isinstance(prompt, str) or not prompt.strip():
            raise RoutineError("Write what the agent should do.", "bad_prompt")
        if len(prompt) > PROMPT_MAX:
            raise RoutineError(f"The task is too long ({len(prompt)} characters; at most {PROMPT_MAX}).", "bad_prompt")
        kind = deliver_to.get("kind") if isinstance(deliver_to, dict) else "new_session"
        if kind == "new_session" or check:
            if not isinstance(workspace, str) or not Path(workspace.strip()).is_absolute() or not Path(workspace.strip()).is_dir():
                raise RoutineError("Give the full path of an existing folder.", "bad_folder")
            workspace = str(Path(workspace.strip()).resolve())
        else:
            workspace = str(workspace or "").strip()           # not used: this fire only delivers a message, nothing runs in a folder
        Schedule.parse(schedule)
        if max_turns is not None and (not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1):
            raise RoutineError("The number of steps is a whole number from 1, or empty for no limit.", "bad_steps")
        if max_minutes is not None and (not isinstance(max_minutes, int) or isinstance(max_minutes, bool) or max_minutes < 1):
            raise RoutineError("The time of a run is a whole number of minutes from 1, or empty for no limit.", "bad_minutes")
        if yield_s is not None and (not isinstance(yield_s, int) or isinstance(yield_s, bool) or not 0 <= yield_s <= YIELD_MAX_S):
            raise RoutineError(f"The time to go on once a conversation of yours is at work is from 0 to {YIELD_MAX_S} seconds, or empty for never.", "bad_yield")
        if not isinstance(index_past, bool):
            raise RoutineError("Indexing is on or off.", "bad_index")
        if not isinstance(check, str) or len(check) > CHECK_MAX:
            raise RoutineError(f"The check is a command of at most {CHECK_MAX} characters, or empty.", "bad_check")
        if ttl_min is not None and (not isinstance(ttl_min, int) or isinstance(ttl_min, bool) or ttl_min < 1):
            raise RoutineError("How late a fire may start is a whole number of minutes from 1, or empty for any time.", "bad_ttl")
        if not isinstance(dedup_key, str) or len(dedup_key) > KEY_MAX or not _KEY.match(dedup_key):
            raise RoutineError(f"The key that keeps fires apart is letters, digits and _.:- (at most {KEY_MAX}), or empty.", "bad_key")
        deliver_to = deliver_to if deliver_to is not None else {"kind": "new_session", "ref": ""}
        if not isinstance(deliver_to, dict) or deliver_to.get("kind") not in DELIVER_KINDS:
            raise RoutineError("Where a fire goes is 'new_session', 'session' or 'plan', each with a 'ref' (empty for new_session).", "bad_deliver_to")
        ref = deliver_to.get("ref", "")
        if deliver_to["kind"] != "new_session" and (not isinstance(ref, str) or not ref.strip()):
            raise RoutineError("Name the conversation this fire is delivered to.", "bad_deliver_to")
        if deliver_to["kind"] == "new_session" and ref:
            raise RoutineError("A new-session routine has no 'ref'.", "bad_deliver_to")
        if not isinstance(message, str) or len(message) > MESSAGE_MAX:
            raise RoutineError(f"The message is at most {MESSAGE_MAX} characters.", "bad_message")
        if link is not None and (not isinstance(link, dict) or set(link) - {"plan"} or (("plan" in link) and not isinstance(link["plan"], str))):
            raise RoutineError("The link is {\"plan\": a plan id}, or empty.", "bad_link")
        return {"ttl_min": ttl_min, "dedup_key": dedup_key, "deliver_to": {"kind": deliver_to["kind"], "ref": ref.strip() if isinstance(ref, str) else ""},
                "message": message.strip(), "link": {"plan": link["plan"]} if link and link.get("plan") else {}, "yield_s": yield_s, "index_past": index_past, "check": check.strip(), "name": name.strip(), "prompt": prompt.strip(), "workspace": workspace,
                "schedule": " ".join(str(schedule).lower().split()), "max_turns": max_turns, "max_minutes": max_minutes,
                **({"skill": skill, "values": dict(values or {})} if skill else {"skill": "", "values": {}})}

    def set_enabled(self, routine_id: str, enabled: bool) -> Dict[str, Any]:
        with self._lock:
            r = self._find(routine_id)
            if enabled and not r.get("enabled"):
                r["enabled_at"] = self.clock()
            r["enabled"] = bool(enabled)
            self._due[routine_id] = self._advance(r, self.clock())
            if not enabled and routine_id in self._queue:
                self._queue.remove(routine_id)
                self._settle(self._claims.pop(routine_id, ""), "cancelled")
            self._save()
            return dict(r)

    def remove(self, routine_id: str) -> None:
        with self._lock:
            self._find(routine_id)
            self.routines = [r for r in self.routines if r["id"] != routine_id]
            self._due.pop(routine_id, None)
            if routine_id in self._queue:
                self._queue.remove(routine_id)
                self._settle(self._claims.pop(routine_id, ""), "cancelled")
            if any(p.get("routine") == routine_id for p in self.pending_requests):
                self.pending_requests = [p for p in self.pending_requests if p.get("routine") != routine_id]
                self._save_pending()
            self._save()

    def runs(self, routine_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            items = [r for r in self.results if not routine_id or r.get("routine") == routine_id]
            return [dict(r) for r in reversed(items[-max(1, min(int(limit), KEEP_RUNS)):])]

    # -- pending requests ------------------------------------------------------------------------

    def _park(self, routine_id: str, request: Dict[str, Any]) -> None:
        """A request only the person can settle: kept in the list while the rest of the run goes on."""
        with self._lock:
            routine = next((r for r in self.routines if r["id"] == routine_id), None)
            if routine is None:
                return
            key = f"{request.get('session', '')}:{request.get('key', '')}"
            if request.get("key") and any(p.get("key") == key for p in self.pending_requests):
                return
            options = [str(o) for o in request.get("options") or []][:20]
            self.pending_requests.append({"id": secrets.token_hex(4), "routine": routine_id, "name": routine["name"],
                                          "run": (self._active.get(routine_id) or {}).get("run", ""), "session": str(request.get("session") or ""),
                                          "key": key if request.get("key") else "", "kind": str(request.get("kind") or "ask"),
                                          "question": str(request.get("question") or "")[:SUMMARY_MAX], "options": options,
                                          "created": self.clock()})
            self._save_pending()

    def pending(self, routine_id: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(p) for p in self.pending_requests if not routine_id or p.get("routine") == routine_id]

    def dismiss(self, pending_id: str) -> None:
        with self._lock:
            if not any(p.get("id") == pending_id for p in self.pending_requests):
                raise RoutineError("That request is not in the list any more.", "gone")
            self.pending_requests = [p for p in self.pending_requests if p.get("id") != pending_id]
            self._save_pending()

    # -- running ---------------------------------------------------------------------------------

    def run_now(self, routine_id: str) -> Dict[str, Any]:
        """The person asks for a run: it goes in the queue even if the routine is disabled, and starts at once unless the same routine is running."""
        with self._lock:
            r = self._find(routine_id)
            if routine_id not in self._queue:
                self._queue.append(routine_id)
                self._manual.add(routine_id)
        self._wake.set()
        return dict(r)

    def stopped(self) -> bool:
        return (self.directory / STOP_FILE).exists()

    def emergency_stop(self, on: bool) -> None:
        """The kill switch: with it on, nothing starts and what runs is stopped; a file in the data folder, so a script can set it too."""
        path = self.directory / STOP_FILE
        if on:
            path.write_text(str(self.clock()), encoding="utf-8")
            self.cancel_running()
        elif path.exists():
            path.unlink()
        self._wake.set()

    def cancel_running(self) -> None:
        with self._lock:
            for state in self._active.values():
                state["cancel"].set()

    def check(self, now: Optional[float] = None) -> None:
        """One look at the clock (public so the tests can drive it): every enabled routine that is due goes in the queue once."""
        now = self.clock() if now is None else now
        if self.stopped():
            self.cancel_running()
            return
        with self._lock:
            for r in self.routines:
                rid = r["id"]
                if r.get("enabled") and self._due.get(rid, now + 1) <= now and rid not in self._queue:
                    self._claim(r, self._due[rid], now)
                    self._due[rid] = self._advance(r, now)
        if self._queue:
            self._wake.set()

    def _claim(self, routine: Dict[str, Any], planned: float, now: float) -> None:
        """Decides one scheduled fire: already handled, too late, a duplicate of one in flight, or queued. The decision is written down
        before anything starts, so a restart finds it."""
        rid, run_id, key = routine["id"], fire_id(routine["id"], planned), routine.get("dedup_key") or ""
        if any(f["id"] == run_id for f in self.fires):
            return
        schedule = Schedule.parse(routine["schedule"])
        before = max(float(routine.get(k) or 0) for k in ("last_run", "last_planned", "created", "enabled_at"))
        record = {"id": run_id, "routine": rid, "name": routine["name"], "planned": planned, "claimed": now, "key": key}
        ttl = routine.get("ttl_min")
        routine["last_planned"] = planned
        if ttl and now - planned > ttl * 60:
            self.fires.append({**record, "state": "skipped_late", "missed": max(1, schedule.occurrences(before, now))})
            self._retire_once(routine, schedule)
        elif key and any(f.get("state") == "claimed" and f.get("key") == key for f in self.fires):
            self.fires.append({**record, "state": "skipped_dup"})
            self._retire_once(routine, schedule)
        else:
            self.fires.append({**record, "state": "claimed"})
            self._queue.append(rid)
            self._claims[rid] = run_id
        self._save()
        self._save_fires()

    def _settle(self, run_id: str, state: str, **extra: Any) -> None:
        """Records how a scheduled fire ended (a manual run has no entry in the log)."""
        fire = next((f for f in reversed(self.fires) if f["id"] == run_id), None) if run_id else None
        if fire is not None:
            fire.update(state=state, **extra)
            self._save_fires()

    @staticmethod
    def _retire_once(routine: Dict[str, Any], schedule: Schedule) -> None:
        if schedule.once is not None:
            routine["enabled"] = False

    def fires_log(self, routine_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        """The latest scheduled fires and what became of each, newest first."""
        with self._lock:
            items = [f for f in self.fires if not routine_id or f.get("routine") == routine_id]
            return [dict(f) for f in reversed(items[-max(1, min(int(limit), KEEP_FIRES)):])]

    def _advance(self, routine: Dict[str, Any], now: float) -> float:
        return Schedule.parse(routine["schedule"]).next_after(now)

    def _tick_loop(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self.check()
            except Exception:
                pass

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            self._dispatch()

    def _dispatch(self) -> None:
        """Starts every queued routine that is not already running; each run has its own thread."""
        with self._lock:
            for rid in list(self._queue):
                if rid in self._active or self._stop.is_set() or self.stopped():
                    continue
                self._queue.remove(rid)
                fire = self._claims.pop(rid, "")
                routine = next((dict(r) for r in self.routines if r["id"] == rid), None)
                manual = rid in self._manual
                self._manual.discard(rid)
                if routine is None or (not routine.get("enabled") and not manual):
                    self._settle(fire, "cancelled")
                    continue
                state = {"cancel": threading.Event(), "yielded": threading.Event(), "run": fire or secrets.token_hex(4)}
                self._active[rid] = state
                worker = threading.Thread(target=self._run_one, args=(routine, state), name=f"routine-{rid}", daemon=True)
                self._workers = [w for w in self._workers if w.is_alive()] + [worker]
                worker.start()

    def _run_one(self, routine: Dict[str, Any], state: Dict[str, Any]) -> None:
        started = self.clock()
        meter = self.meter().start() if self.meter else None
        finished = threading.Event()
        if routine.get("yield_s") is not None:
            threading.Thread(target=self._yield_watch, args=(float(routine["yield_s"]), state, finished), name="routine-yield", daemon=True).start()
        try:
            woken, note, skipped = self._check_first(routine)
            if skipped is not None:
                outcome = skipped
            else:
                outcome = self.runner.run({**routine, "previous": self.previous(routine["id"]), "check_output": note, "fire_run": state["run"]}, state["cancel"])
        except Exception as exc:
            outcome = {"status": "failed", "summary": f"{type(exc).__name__}: {exc}"}
        finished.set()
        if state["yielded"].is_set():
            outcome = {**outcome, "status": "yielded", "summary": "Stopped because a conversation of yours needed the model. " + str(outcome.get("summary") or "")}
        energy = meter.stop() if meter else {}
        if energy:
            outcome = {**outcome, "energy": energy}
        self._finish(routine["id"], outcome, started, state["run"])

    def _check_first(self, routine: Dict[str, Any]) -> "tuple[bool, str, Optional[Dict[str, Any]]]":
        """Runs the routine's own check, which costs no model. (woken, what it printed, an outcome when the run ends here)."""
        command = routine.get("check") or ""
        if not command:
            return True, "", None
        try:
            done = subprocess.run(command, shell=True, cwd=routine["workspace"], capture_output=True, text=True, errors="replace", timeout=CHECK_TIMEOUT_S, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, "", {"status": "failed", "summary": f"The check took more than {CHECK_TIMEOUT_S} seconds."}
        except OSError as exc:
            return False, "", {"status": "failed", "summary": f"The check could not run: {exc}"}
        printed = (done.stdout or "").strip()[:CHECK_OUTPUT_MAX]
        if done.returncode == 0:
            return True, printed, None
        if done.returncode == 1:
            return False, printed, {"status": "skipped", "outcome": "nothing_new", "summary": printed or "The check found nothing to do."}
        return False, "", {"status": "failed", "summary": f"The check ended with code {done.returncode}: {((done.stderr or printed) or '').strip()[:300]}"}

    def _yield_watch(self, grace_s: float, state: Dict[str, Any], finished: threading.Event) -> None:
        """While a routine runs: once a conversation of the person's has been at work for `grace_s` seconds, the routine is asked to stop."""
        since = None
        while not finished.wait(self.poll_s):
            if not self.busy():
                since = None
                continue
            since = since if since is not None else time.monotonic()
            if time.monotonic() - since >= grace_s:
                state["yielded"].set()
                state["cancel"].set()
                return

    def _finish(self, routine_id: str, outcome: Dict[str, Any], started: float, run_id: str = "") -> None:
        ended = self.clock()
        with self._lock:
            routine = next((r for r in self.routines if r["id"] == routine_id), None)
            record = {"id": run_id or secrets.token_hex(4), "routine": routine_id, "name": routine["name"] if routine else "",
                      "started": started, "ended": ended, "status": str(outcome.get("status") or "failed"),
                      "summary": str(outcome.get("summary") or "")[:SUMMARY_MAX], "session": str(outcome.get("session") or ""),
                      "outcome": outcome_of(outcome),
                      **{k: outcome[k] for k in ("seconds", "energy") if k in outcome}}
            if record["status"] == "timeout":                 # a warning, not a failure: the routine is tried again at its next time
                record["warning"] = "timed out"
            self.results.append(record)
            self.results = self.results[-KEEP_RUNS:]
            if routine is not None:
                routine["last_run"], routine["last_status"] = started, record["status"]
                self._retire_once(routine, Schedule.parse(routine["schedule"]))
                self._save()
            self._settle(run_id, "done", status=record["status"])
            self._active.pop(routine_id, None)
            self._save_runs()
        self._wake.set()

    # -- life ------------------------------------------------------------------------------------

    def start(self) -> "Scheduler":
        for t in self._threads:
            if not t.is_alive() and not self._stop.is_set():
                try:
                    t.start()
                except RuntimeError:
                    pass
        return self

    def shutdown(self) -> None:
        self._stop.set()
        self.cancel_running()
        self._wake.set()
        with self._lock:
            workers = list(self._workers)
        for t in self._threads + workers:
            if t.is_alive() and t is not threading.current_thread():
                t.join(5)


def _pending_options(source: Dict[str, Any]) -> List[str]:
    return [str(o.get("label") or o.get("id") or "") if isinstance(o, dict) else str(o) for o in source.get("options") or []]


def with_history(routine: Dict[str, Any]) -> str:
    """The routine's task with what the earlier runs left (their outcome words and first lines), what its check printed, and how to end."""
    nl = chr(10)
    parts = [routine["prompt"]]
    previous = [p for p in routine.get("previous") or [] if (p.get("outcome") or p.get("summary")) and p.get("at")]
    if previous:
        parts.append("Earlier runs of this routine, oldest first:" + nl + nl.join(
            f"- {time.strftime('%Y-%m-%d %H:%M', time.localtime(float(p['at'])))} {p.get('outcome') or p.get('status')}: {str(p.get('summary') or '')[:200]}" for p in previous))
    if routine.get("check_output"):
        parts.append("What the routine's check found:" + nl + routine["check_output"])
    if len(parts) > 1 or routine.get("history_on"):
        parts.append("End your final answer with one line: OUTCOME: nothing_new, progress, done or blocked.")
    return (nl + nl).join(parts)


class SessionRoutineRunner:
    """Runs a routine as an Automatic conversation of the agent through the server's `SessionManager`, then closes it."""

    park: Optional[Callable[[str, Dict[str, Any]], None]] = None            # set by the Scheduler: keeps a request for the person

    def __init__(self, manager: Any, poll_s: float = 0.5, cancel_grace_s: float = 30.0, plan_paused: Optional[Callable[[str], bool]] = None,
                 plan_context: Optional[Callable[[str], Dict[str, Any]]] = None) -> None:
        self.manager, self.poll_s, self.cancel_grace_s = manager, poll_s, cancel_grace_s      # how long a stopped conversation has to wind down
        self.plan_paused = plan_paused                  # plan_id -> paused, for a "plan" delivery whose plan has its schedule stopped
        self.plan_context = plan_context                # plan_id -> {"plan_title", "ready_step", ...its shared values}, for the message template
        self._own: set = set()

    def busy(self) -> bool:
        """True while a conversation that is not a routine's is at work."""
        sessions = self.manager.view().get("sessions", [])
        return any(s.get("state") in ("busy", "starting") and s["id"] not in self._own for s in sessions)

    def _park(self, routine: Dict[str, Any], sid: str, key: str, kind: str, source: Dict[str, Any], default: str) -> str:
        text = str(source.get("question") or source.get("detail") or source.get("reason") or default)
        if self.park is not None:
            self.park(routine["id"], {"session": sid, "key": key, "kind": kind, "question": text, "options": _pending_options(source)})
        return text

    def _deliver(self, routine: Dict[str, Any], deliver_to: Dict[str, Any]) -> Dict[str, Any]:
        """A fire that speaks to an existing conversation instead of starting one: the message is left for it (tadamoo/provenance.py),
        and this run ends here whether or not it was taken yet (the delivery's own state, pending/delivered/dead, lives in the queue)."""
        from tadamoo.cron_templates import render_template
        started = time.monotonic()
        link = routine.get("link") or {}
        if deliver_to["kind"] == "plan" and link.get("plan") and self.plan_paused is not None and self.plan_paused(link["plan"]):
            return {"status": "skipped_paused", "outcome": "nothing_new", "summary": f"The plan {link['plan']} is paused: the fire was skipped.",
                    "seconds": round(time.monotonic() - started, 1)}
        context = {"date": time.strftime("%Y-%m-%d")}
        if link.get("plan"):
            context["plan_id"] = link["plan"]
            if self.plan_context is not None:
                context.update(self.plan_context(link["plan"]) or {})
        text = render_template(routine.get("message") or routine["prompt"], context)
        provenance = f"plan:{link['plan']}" if deliver_to["kind"] == "plan" and link.get("plan") else "cron:" + routine["id"]
        try:
            result = self.manager.deliver(deliver_to["ref"], text, provenance=provenance, run_id=routine.get("fire_run") or "")
        except Exception as exc:
            return {"status": "failed", "summary": f"Could not deliver to {deliver_to['ref']}: {exc}", "seconds": round(time.monotonic() - started, 1)}
        delivery = result.get("delivery") or {}
        state = delivery.get("state", "pending")
        if state == "dead":
            return {"status": "failed", "summary": f"Not delivered to {deliver_to['ref']}: {delivery.get('reason', 'unreachable')}.",
                    "seconds": round(time.monotonic() - started, 1)}
        return {"status": "done", "summary": f"Message {state} to {deliver_to['ref']}.", "seconds": round(time.monotonic() - started, 1)}

    def run(self, routine: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
        deliver_to = routine.get("deliver_to") or {"kind": "new_session", "ref": ""}
        if deliver_to.get("kind") in ("session", "plan") and deliver_to.get("ref"):
            return self._deliver(routine, deliver_to)
        mgr = self.manager
        started = time.monotonic()
        limit_s = routine["max_minutes"] * 60 if routine.get("max_minutes") else None
        sid = mgr.create(routine["workspace"], "auto", None, False, f"Routine: {routine['name']}"[:80],
                         routine.get("max_turns") or NO_STEP_LIMIT, worktree="off", unattended=True)["id"]
        self._own.add(sid)
        getattr(mgr, "keep_out_of_memory", lambda _sid: None)(sid)          # a routine is not the person talking: nothing in it is learned
        getattr(mgr, "routine_run", lambda *_a: None)(sid, routine["name"], bool(routine.get("index_past")))      # indexed for later search only when the routine says so
        try:
            if routine.get("skill"):                          # the values are worked out now: "today" is the day of this run
                try:
                    mgr.run_skill(sid, routine["skill"], routine.get("values") or {})
                except Exception as exc:
                    return {"status": "failed", "summary": f"The skill could not start: {exc}", "session": sid, "seconds": round(time.monotonic() - started, 1)}
            else:
                mgr.send(sid, with_history(routine))
            seen, cancelled_at, assumed = 0, None, []
            while True:
                data = mgr.events(sid, seen)
                seen = data.get("next", seen)
                for event in data["events"]:
                    if event.get("event") == "auto_answered" and event.get("kind") == "question" and event.get("chosen") in ("recommended", "default", "none"):
                        assumed.append(f"{str(event.get('question') or '')[:120]} -> {event.get('answer') or '(no answer)'}")      # decided by the harness, for the person to see
                    if event.get("event") == "done":
                        stop = event.get("stop_reason") or ""
                        status = "done" if stop == "final" else "parked" if stop == "asked" else "incomplete"
                        if cancelled_at is not None:
                            status = "cancelled" if cancel.is_set() else "timeout"
                        question = ""
                        if stop == "asked" and cancelled_at is None:
                            question = self._park(routine, sid, "question", "question", mgr.get(sid).get("question") or {}, "It needed an answer.")
                        summary = event.get("final") or question or event.get("error") or f"Stopped: {stop}"
                        if assumed:
                            summary += "\n\nAssumptions made (nobody was there to ask):\n" + "\n".join(f"- {a}" for a in assumed[:10])
                        return {"status": status, "summary": summary, "session": sid, "seconds": round(time.monotonic() - started, 1)}
                state = data.get("state")
                if state == "asking":                         # the run goes on; the request waits in the list for the person
                    ask = mgr.get(sid).get("ask") or {}
                    self._park(routine, sid, str(ask.get("id") or "ask"), "ask", ask, "It needed an answer.")
                elif state == "blocked":                      # only the person can lift it: this run ends parked, not failed
                    text = self._park(routine, sid, "blocked", "blocked", mgr.get(sid).get("blocked") or {}, "It needed the person.")
                    return {"status": "parked", "summary": text, "session": sid, "seconds": round(time.monotonic() - started, 1)}
                elif state == "closed":
                    return {"status": "failed", "summary": "The conversation was closed.", "session": sid}
                if cancel.is_set() or (limit_s is not None and time.monotonic() - started > limit_s):
                    if cancelled_at is None:
                        cancelled_at = time.monotonic()
                        mgr.cancel(sid)
                    elif time.monotonic() - cancelled_at > self.cancel_grace_s:
                        return {"status": "cancelled" if cancel.is_set() else "timeout", "summary": "It was stopped.", "session": sid}
                time.sleep(self.poll_s)
        finally:
            try:
                mgr.close(sid)
            except Exception:
                pass
            self._own.discard(sid)
