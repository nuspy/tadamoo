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

A routine is created enabled; a disabled one never runs on its own. Overdue interval routines run once when the program
starts, and so does a fixed time that passed while the program was off (once, late). Routines, results and pending requests
are kept in files in the folder given at construction (the user data folder).
"""
from __future__ import annotations

import datetime as dt
import json
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
NO_STEP_LIMIT = -1                  # `max_turns` the conversation path reads as no limit
KEEP_RUNS = 200
NAME_MAX, PROMPT_MAX, SUMMARY_MAX = 80, 4000, 4000
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


def outcome_of(outcome: Dict[str, Any]) -> str:
    """The one word a run leaves for the next one: the `OUTCOME:` line the agent ended with, else what the status says."""
    given = str(outcome.get("outcome") or "")
    if given in OUTCOMES:
        return given
    found = _OUTCOME.findall(str(outcome.get("summary") or ""))
    if found and found[-1].lower() in OUTCOMES:
        return found[-1].lower()
    return {"done": "done", "parked": "blocked"}.get(str(outcome.get("status")), "")


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

    def previous(self, moment: float) -> float:
        """The latest fixed time at or before `moment` (0 for an interval, which has no fixed times)."""
        if self.interval_s:
            return 0.0
        start = dt.datetime.fromtimestamp(moment)
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
        self.meter = meter                                                   # makes an EnergyMeter (harness/energy.py) for each run
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._queue: List[str] = []                                          # routines waiting to start, each at most once
        self._manual: set = set()                                            # queued because the person asked, enabled or not
        self._due: Dict[str, float] = {}
        self._active: Dict[str, Dict[str, Any]] = {}                         # routine id -> the cancel and yield events and run id of its run
        self._workers: List[threading.Thread] = []
        self.routines: List[Dict[str, Any]] = [self._normalize(r) for r in self._load(ROUTINES_FILE, "routines")]
        self.results: List[Dict[str, Any]] = [{**r, "status": "parked" if r.get("status") == "asked" else r.get("status")}
                                              for r in self._load(RUNS_FILE, "runs")]
        self.pending_requests: List[Dict[str, Any]] = self._load(PENDING_FILE, "pending")
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
        return r

    def _save(self) -> None:
        _write_json(self.directory / ROUTINES_FILE, {"schema": "1.0", "routines": self.routines})

    def _save_runs(self) -> None:
        _write_json(self.directory / RUNS_FILE, {"schema": "1.0", "runs": self.results[-KEEP_RUNS:]})

    def _save_pending(self) -> None:
        _write_json(self.directory / PENDING_FILE, {"schema": "1.0", "pending": self.pending_requests})

    def _first_due(self, routine: Dict[str, Any], now: float) -> float:
        """When the program starts: an interval counts from the last run (so an overdue one is due at once); a fixed time that
        passed since the routine last ran (or was made or turned on) is due at once, once; otherwise the next time."""
        schedule = Schedule.parse(routine["schedule"])
        if schedule.interval_s:
            return float(routine["last_run"]) + schedule.interval_s if routine.get("last_run") else schedule.next_after(now)
        since = max(float(routine.get(k) or 0) for k in ("last_run", "created", "enabled_at"))
        return now if schedule.previous(now) > since else schedule.next_after(now)

    # -- the list --------------------------------------------------------------------------------

    def previous(self, routine_id: str) -> List[Dict[str, Any]]:
        """The outcome and the first words of the last runs of a routine, oldest first: what the next run is told."""
        done = [r for r in self.results if r.get("routine") == routine_id and r.get("status") not in ("skipped",)]
        return [{"at": r.get("started"), "outcome": r.get("outcome") or "", "status": r.get("status"), "summary": str(r.get("summary") or "")[:300]} for r in done[-PREVIOUS_RUNS:]]

    def view(self) -> Dict[str, Any]:
        with self._lock:
            return {"routines": [{**r, "next_run": self._due.get(r["id"]) if r.get("enabled") else None} for r in self.routines],
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
            skill: Any = "", values: Any = None, yield_s: Any = None, index_past: Any = False, check: Any = "") -> Dict[str, Any]:
        """A new routine, enabled. With a `skill` it runs that personal skill with `values` instead of a prompt.
        `max_turns` and `max_minutes` limit the steps and the time of a run, `yield_s` how long it may go on once a conversation
        of the person's is at work; empty means no limit. `index_past` adds what the routine does to the index of earlier conversations (off unless the person asks).
        `check` is a command run before the model is woken: exit code 0 goes on (what it prints is given to the model), 1 means nothing to do and the run ends there, anything else fails the run."""
        routine = self._check(name, prompt, workspace, schedule, max_turns, max_minutes, skill, values, yield_s, index_past, check)
        now = self.clock()
        routine.update(id=secrets.token_hex(3), enabled=True, enabled_at=now, created=now, last_run=0, last_status="")
        with self._lock:
            self.routines.append(routine)
            self._due[routine["id"]] = self._advance(routine, now)
            self._save()
        return dict(routine)

    def update(self, routine_id: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            current = self._find(routine_id)
            merged = {**current, **{k: v for k, v in fields.items() if k in ("name", "prompt", "workspace", "schedule", "max_turns", "max_minutes", "skill", "values", "yield_s", "index_past", "check")}}
            checked = self._check(merged["name"], merged["prompt"], merged["workspace"], merged["schedule"], merged.get("max_turns"),
                                  merged.get("max_minutes"), merged.get("skill", ""), merged.get("values"), merged.get("yield_s"), merged.get("index_past", False), merged.get("check", ""))
            current.update(checked)
            self._due[routine_id] = self._advance(current, self.clock())
            self._save()
            return dict(current)

    @staticmethod
    def _check(name: Any, prompt: Any, workspace: Any, schedule: Any, max_turns: Any = None, max_minutes: Any = None,
               skill: Any = "", values: Any = None, yield_s: Any = None, index_past: Any = False, check: Any = "") -> Dict[str, Any]:
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
        if not isinstance(workspace, str) or not Path(workspace.strip()).is_absolute() or not Path(workspace.strip()).is_dir():
            raise RoutineError("Give the full path of an existing folder.", "bad_folder")
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
        return {"yield_s": yield_s, "index_past": index_past, "check": check.strip(), "name": name.strip(), "prompt": prompt.strip(), "workspace": str(Path(workspace.strip()).resolve()),
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
            self._save()
            return dict(r)

    def remove(self, routine_id: str) -> None:
        with self._lock:
            self._find(routine_id)
            self.routines = [r for r in self.routines if r["id"] != routine_id]
            self._due.pop(routine_id, None)
            if routine_id in self._queue:
                self._queue.remove(routine_id)
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
            self._dispatch()

    def _dispatch(self) -> None:
        """Starts every queued routine that is not already running; each run has its own thread."""
        with self._lock:
            for rid in list(self._queue):
                if rid in self._active or self._stop.is_set() or self.stopped():
                    continue
                self._queue.remove(rid)
                routine = next((dict(r) for r in self.routines if r["id"] == rid), None)
                manual = rid in self._manual
                self._manual.discard(rid)
                if routine is None or (not routine.get("enabled") and not manual):
                    continue
                state = {"cancel": threading.Event(), "yielded": threading.Event(), "run": secrets.token_hex(4)}
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
                outcome = self.runner.run({**routine, "previous": self.previous(routine["id"]), "check_output": note}, state["cancel"])
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
                self._save()
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

    def __init__(self, manager: Any, poll_s: float = 0.5, cancel_grace_s: float = 30.0) -> None:
        self.manager, self.poll_s, self.cancel_grace_s = manager, poll_s, cancel_grace_s      # how long a stopped conversation has to wind down
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

    def run(self, routine: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
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
