"""Routines: recurring tasks for the agent ("every morning run the tests and summarise", "check the pull requests").

A routine is a prompt, a folder, a schedule and a permission profile. When its time comes it goes in a queue; one routine
runs at a time (the model serves one conversation at a time), and only while no conversation of the person is at work: a
routine that is due waits for the conversation, never the other way round. A routine runs through the agent's conversation
path (`SessionRoutineRunner` uses the server's `SessionManager`), so its tools, permissions, audit and checkpoints are the
ones of any conversation. The profile is small by default: read-only, with the allow rules the person wrote. A question the
agent cannot put to anyone ends the run with the status "asked"; nothing is decided for the person.

Schedules are written as text:
  every 30m | every 2h | every 1d       an interval (at least one minute)
  daily 08:30 | weekdays 08:30 | weekends 10:00 | mon,wed,fri 18:15       fixed local times

A routine never runs unless it is enabled, and it is created disabled. Overdue interval routines run once when the program
starts; a fixed time that passed while the program was off is skipped, not made up. Routines and results are kept in files
in the folder given at construction (the user data folder).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

MODES = ("plan", "read_only", "default", "auto")
ROUTINES_FILE = "routines.json"
RUNS_FILE = "routine_runs.json"
KEEP_RUNS = 200
NAME_MAX, PROMPT_MAX, SUMMARY_MAX = 80, 4000, 4000
MIN_INTERVAL_S = 60
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_INTERVAL = re.compile(r"^every\s+(\d{1,4})\s*([mhd])$")
_FIXED = re.compile(r"^([a-z,\s]+?)\s+(\d{1,2}):(\d{2})$")
_UNITS = {"m": 60, "h": 3600, "d": 86400}


class RoutineError(ValueError):
    """A routine that cannot be saved or found: a message fit for the person, and a code."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class Schedule:
    """`interval_s` for "every ..."; otherwise the days of the week (0 is Monday) and the time of day."""

    def __init__(self, text: str, interval_s: int = 0, days: Optional[List[int]] = None, at: Optional[dt.time] = None) -> None:
        self.text, self.interval_s, self.days, self.at = text, interval_s, days or [], at

    @classmethod
    def parse(cls, text: Any) -> "Schedule":
        if not isinstance(text, str) or not text.strip():
            raise RoutineError("Write when it runs, for example 'every 2h' or 'weekdays 08:30'.", "bad_schedule")
        norm = " ".join(text.lower().split())
        m = _INTERVAL.match(norm)
        if m:
            seconds = int(m.group(1)) * _UNITS[m.group(2)]
            if seconds < MIN_INTERVAL_S:
                raise RoutineError("An interval is at least one minute.", "bad_schedule")
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

    def next_after(self, moment: float) -> float:
        """The next time to run after `moment`, as a timestamp."""
        if self.interval_s:
            return moment + self.interval_s
        start = dt.datetime.fromtimestamp(moment)
        for offset in range(8):
            day = start.date() + dt.timedelta(days=offset)
            when = dt.datetime.combine(day, self.at)
            if day.weekday() in self.days and when.timestamp() > moment:
                return when.timestamp()
        raise RoutineError("This schedule never comes.", "bad_schedule")


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
                 clock: Callable[[], float] = time.time, tick_s: float = 5.0, max_wait_s: float = 3600.0, poll_s: float = 1.0,
                 meter: Optional[Callable[[], Any]] = None) -> None:
        self.directory = Path(directory)
        self.runner = runner
        self.busy = busy or getattr(runner, "busy", None) or (lambda: False)
        self.clock, self.tick_s, self.max_wait_s, self.poll_s = clock, tick_s, max_wait_s, poll_s
        self.meter = meter                                                   # makes an EnergyMeter (harness/energy.py) for each run
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._queue: List[str] = []
        self._manual: set = set()                                            # queued because the person asked, enabled or not
        self._due: Dict[str, float] = {}
        self.running: Optional[str] = None
        self.routines: List[Dict[str, Any]] = self._load(ROUTINES_FILE, "routines")
        self.results: List[Dict[str, Any]] = self._load(RUNS_FILE, "runs")
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

    def _save(self) -> None:
        _write_json(self.directory / ROUTINES_FILE, {"schema": "1.0", "routines": self.routines})

    def _save_runs(self) -> None:
        _write_json(self.directory / RUNS_FILE, {"schema": "1.0", "runs": self.results[-KEEP_RUNS:]})

    def _first_due(self, routine: Dict[str, Any], now: float) -> float:
        """When the program starts: an interval counts from the last run (so an overdue one is due at once), a fixed time from now."""
        schedule = Schedule.parse(routine["schedule"])
        if schedule.interval_s and routine.get("last_run"):
            return float(routine["last_run"]) + schedule.interval_s
        return schedule.next_after(now)

    # -- the list --------------------------------------------------------------------------------

    def view(self) -> Dict[str, Any]:
        with self._lock:
            return {"routines": [{**r, "next_run": self._due.get(r["id"]) if r.get("enabled") else None} for r in self.routines],
                    "running": self.running, "queued": list(self._queue)}

    def get(self, routine_id: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._find(routine_id))

    def _find(self, routine_id: str) -> Dict[str, Any]:
        r = next((r for r in self.routines if r["id"] == routine_id), None)
        if r is None:
            raise RoutineError("That routine is not in the list any more.", "gone")
        return r

    def add(self, name: Any, prompt: Any, workspace: Any, schedule: Any, mode: Any = "read_only", allow: Any = None,
            max_turns: Any = 10, confirm_auto: bool = False) -> Dict[str, Any]:
        """A new routine, disabled: the person turns it on."""
        routine = self._check(None, name, prompt, workspace, schedule, mode, allow, max_turns, confirm_auto)
        routine.update(id=secrets.token_hex(3), enabled=False, created=self.clock(), last_run=0, last_status="")
        with self._lock:
            self.routines.append(routine)
            self._due[routine["id"]] = self._advance(routine, self.clock())
            self._save()
        return dict(routine)

    def update(self, routine_id: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            current = self._find(routine_id)
            merged = {**current, **{k: v for k, v in fields.items() if k in ("name", "prompt", "workspace", "schedule", "mode", "allow", "max_turns", "confirm_auto")}}
            checked = self._check(routine_id, merged["name"], merged["prompt"], merged["workspace"], merged["schedule"], merged["mode"],
                                  merged.get("allow"), merged["max_turns"], merged.get("confirm_auto") is True)
            current.update(checked)
            self._due[routine_id] = self._advance(current, self.clock())
            self._save()
            return dict(current)

    @staticmethod
    def _check(routine_id: Optional[str], name: Any, prompt: Any, workspace: Any, schedule: Any, mode: Any, allow: Any,
               max_turns: Any, confirm_auto: bool) -> Dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name) > NAME_MAX:
            raise RoutineError(f"Give the routine a name of at most {NAME_MAX} characters.", "bad_name")
        if not isinstance(prompt, str) or not prompt.strip():
            raise RoutineError("Write what the agent should do.", "bad_prompt")
        if len(prompt) > PROMPT_MAX:
            raise RoutineError(f"The task is too long ({len(prompt)} characters; at most {PROMPT_MAX}).", "bad_prompt")
        if not isinstance(workspace, str) or not Path(workspace.strip()).is_absolute() or not Path(workspace.strip()).is_dir():
            raise RoutineError("Give the full path of an existing folder.", "bad_folder")
        Schedule.parse(schedule)
        if mode not in MODES:
            raise RoutineError("The mode must be one of: " + ", ".join(MODES) + ".", "bad_mode")
        if mode == "auto" and confirm_auto is not True:
            raise RoutineError("Automatic mode runs commands without asking. Confirm that you want that.", "auto_confirm")
        rules = allow if isinstance(allow, list) else []
        if len(rules) > 10 or any(not isinstance(r, str) or not r.strip() or len(r) > 200 or "\n" in r for r in rules):
            raise RoutineError("Allow rules are lines of text, at most 10.", "bad_rules")
        if not isinstance(max_turns, int) or isinstance(max_turns, bool) or not 1 <= max_turns <= 50:
            raise RoutineError("The number of steps must be between 1 and 50.", "bad_steps")
        return {"name": name.strip(), "prompt": prompt.strip(), "workspace": str(Path(workspace.strip()).resolve()),
                "schedule": " ".join(str(schedule).lower().split()), "mode": mode, "allow": [r.strip() for r in rules],
                "max_turns": max_turns, "confirm_auto": bool(confirm_auto and mode == "auto")}

    def set_enabled(self, routine_id: str, enabled: bool) -> Dict[str, Any]:
        with self._lock:
            r = self._find(routine_id)
            r["enabled"] = bool(enabled)
            self._due[routine_id] = self._advance(r, self.clock())
            if not enabled and routine_id in self._queue:
                self._queue.remove(routine_id)
            self._save()
            return dict(r)

    def remove(self, routine_id: str) -> None:
        with self._lock:
            self._find(routine_id)
            self.routines = [r for r in self.routines if r["id"] != routine_id]
            self._due.pop(routine_id, None)
            if routine_id in self._queue:
                self._queue.remove(routine_id)
            self._save()

    def runs(self, routine_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            items = [r for r in self.results if not routine_id or r.get("routine") == routine_id]
            return [dict(r) for r in reversed(items[-max(1, min(int(limit), KEEP_RUNS)):])]

    # -- running ---------------------------------------------------------------------------------

    def run_now(self, routine_id: str) -> Dict[str, Any]:
        """The person asks for a run: it goes in the queue even if the routine is disabled, and waits its turn like any other."""
        with self._lock:
            r = self._find(routine_id)
            if routine_id not in self._queue and self.running != routine_id:
                self._queue.append(routine_id)
                self._manual.add(routine_id)
        self._wake.set()
        return dict(r)

    def cancel_running(self) -> None:
        self._cancel.set()

    def check(self, now: Optional[float] = None) -> None:
        """One look at the clock (public so the tests can drive it): every enabled routine that is due goes in the queue once."""
        now = self.clock() if now is None else now
        with self._lock:
            for r in self.routines:
                rid = r["id"]
                if r.get("enabled") and self._due.get(rid, now + 1) <= now and rid not in self._queue and self.running != rid:
                    self._queue.append(rid)
                    self._due[rid] = self._advance(r, now)
        if self._queue:
            self._wake.set()

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
            while not self._stop.is_set():
                with self._lock:
                    rid = self._queue[0] if self._queue else None
                if rid is None:
                    break
                self._run_one(rid)

    def _run_one(self, routine_id: str) -> None:
        waited_from = time.monotonic()
        while self.busy() and not self._stop.is_set():                       # the person's conversation goes first
            if time.monotonic() - waited_from > self.max_wait_s:
                return self._finish(routine_id, {"status": "skipped", "summary": "The agent was busy with a conversation for too long."}, self.clock())
            self._stop.wait(self.poll_s)
        if self._stop.is_set():
            return
        with self._lock:
            routine = next((dict(r) for r in self.routines if r["id"] == routine_id), None)
            manual = routine_id in self._manual
            if routine is None or (not routine.get("enabled") and not manual):
                if routine_id in self._queue:
                    self._queue.remove(routine_id)
                return
            self.running = routine_id
        self._cancel.clear()
        started = self.clock()
        meter = self.meter().start() if self.meter else None
        try:
            outcome = self.runner.run(routine, self._cancel)
        except Exception as exc:
            outcome = {"status": "failed", "summary": f"{type(exc).__name__}: {exc}"}
        energy = meter.stop() if meter else {}
        if energy:
            outcome = {**outcome, "energy": energy}
        self._finish(routine_id, outcome, started)

    def _finish(self, routine_id: str, outcome: Dict[str, Any], started: float) -> None:
        ended = self.clock()
        with self._lock:
            routine = next((r for r in self.routines if r["id"] == routine_id), None)
            record = {"id": secrets.token_hex(4), "routine": routine_id, "name": routine["name"] if routine else "",
                      "started": started, "ended": ended, "status": str(outcome.get("status") or "failed"),
                      "summary": str(outcome.get("summary") or "")[:SUMMARY_MAX], "session": str(outcome.get("session") or ""),
                      **{k: outcome[k] for k in ("seconds", "energy") if k in outcome}}
            self.results.append(record)
            self.results = self.results[-KEEP_RUNS:]
            if routine is not None and outcome.get("status") != "skipped":
                routine["last_run"], routine["last_status"] = started, record["status"]
                self._save()
            if routine_id in self._queue:
                self._queue.remove(routine_id)
            self._manual.discard(routine_id)
            self.running = None
            self._save_runs()

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
        self._cancel.set()
        self._wake.set()
        for t in self._threads:
            if t.is_alive() and t is not threading.current_thread():
                t.join(5)


class SessionRoutineRunner:
    """Runs a routine as a conversation of the agent through the server's `SessionManager`, then closes it."""

    def __init__(self, manager: Any, poll_s: float = 0.5, max_seconds: float = 1800.0) -> None:
        self.manager, self.poll_s, self.max_seconds = manager, poll_s, max_seconds
        self._own: set = set()

    def busy(self) -> bool:
        """True while a conversation that is not a routine's is at work."""
        sessions = self.manager.view().get("sessions", [])
        return any(s.get("state") in ("busy", "asking", "starting") and s["id"] not in self._own for s in sessions)

    def run(self, routine: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
        mgr = self.manager
        started = time.monotonic()
        sid = mgr.create(routine["workspace"], routine["mode"], routine.get("allow"), False, f"Routine: {routine['name']}"[:80],
                         routine.get("confirm_auto") is True, routine.get("max_turns", 10), worktree="off")["id"]
        self._own.add(sid)
        try:
            mgr.send(sid, routine["prompt"])
            seen, cancelled_at = 0, None
            while True:
                data = mgr.events(sid, seen)
                seen = data.get("next", seen)
                for event in data["events"]:
                    if event.get("event") == "done":
                        stop = event.get("stop_reason") or ""
                        status = "done" if stop == "final" else "asked" if stop == "asked" else "incomplete"
                        if cancelled_at is not None:
                            status = "cancelled" if cancel.is_set() else "timeout"
                        question = (mgr.get(sid).get("question") or {}).get("question") if stop == "asked" else ""
                        summary = event.get("final") or question or event.get("error") or f"Stopped: {stop}"
                        return {"status": status, "summary": summary, "session": sid, "seconds": round(time.monotonic() - started, 1)}
                if data.get("state") in ("asking", "blocked"):
                    rec = mgr.get(sid)
                    why = (rec.get("ask") or rec.get("blocked") or {})
                    text = why.get("question") or why.get("detail") or why.get("reason") or "It needed an answer nobody could give."
                    return {"status": "asked", "summary": str(text), "session": sid, "seconds": round(time.monotonic() - started, 1)}
                if data.get("state") == "closed":
                    return {"status": "failed", "summary": "The conversation was closed.", "session": sid}
                if cancel.is_set() or time.monotonic() - started > self.max_seconds:
                    if cancelled_at is None:
                        cancelled_at = time.monotonic()
                        mgr.cancel(sid)
                    elif time.monotonic() - cancelled_at > 30:
                        return {"status": "cancelled" if cancel.is_set() else "timeout", "summary": "It was stopped.", "session": sid}
                time.sleep(self.poll_s)
        finally:
            try:
                mgr.close(sid)
            except Exception:
                pass
            self._own.discard(sid)
