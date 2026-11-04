"""Every place the technical session is used, with its switch and its numbers, kept in one file: side_uses.json in the user data folder.

    {"judge": {"on": false}, "triage": {"params": {"timeout_s": 45}}, "chat_abstract": {"system": "..."}}

A use with no entry takes its defaults (all on, the numbers below). The file is read again whenever it changes, so a value reaches the
next call in every process without a restart. A use that is off sends no `cache_slot`: its calls run in an ordinary slot like the ones
of any server without a session, except for the uses that need the session (`needs_session`), which are then not made at all.

Each use has its own system prompt, written for its task, or none: a call of the session never carries the conversation's. The
prompts of the uses whose answer is free text can be replaced here; the ones whose answer the harness reads by its form cannot.

Long jobs (a text read in parts) register here so that the settings page can show how far they are and stop them: a job writes a
small file in `side_jobs/` while it runs, and "stop" writes the time to `side_stop.json`; a job that started before that time ends."""
from __future__ import annotations

import importlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FILE = "side_uses.json"
STOP_FILE = "side_stop.json"
JOBS_DIR = "side_jobs"
JOB_STALE_S = 120.0                # a job file not touched for this long belongs to a process that is gone
MAX_SYSTEM_CHARS = 4000
EXTERNAL_WINDOW = 30000            # the most a route to another provider is asked to read in one call


@dataclass(frozen=True)
class Param:
    key: str
    default: float
    lo: float
    hi: float
    integer: bool = True


@dataclass(frozen=True)
class Use:
    id: str
    needs_session: bool = False      # without the session there is no such call at all (it would take the conversation's own slot)
    params: Tuple[Param, ...] = ()
    prompt: str = ""                 # "module:NAME" of the system prompt it shows; empty: the prompt is built for each call
    editable: bool = False           # the answer is free text, so the prompt may be replaced
    default_on: bool = True          # a use that changes what the model reads starts off, until it is measured


USES: Tuple[Use, ...] = (
    Use("triage", params=(Param("max_tokens", 600, 100, 4000), Param("timeout_s", 30, 5, 600, False)), prompt="tadamoo.intake:TRIAGE_PROMPT"),
    Use("judge"),
    Use("router", params=(Param("timeout_s", 30, 5, 600, False),), prompt="tadamoo.router:JUDGE_PROMPT"),
    Use("optimize", needs_session=True, params=(Param("timeout_s", 8, 2, 120, False),), prompt="tadamoo.optimize:SYSTEM_PROMPT"),
    Use("suggestions", params=(Param("max_tokens", 480, 100, 2000), Param("timeout_s", 12, 3, 120, False), Param("turns", 4, 0, 20)), prompt="tadamoo.suggestions:SYSTEM"),
    Use("compaction", params=(Param("answer_tokens", 1500, 200, 4000), Param("chunk_tokens", 10000, 2000, 24000)),
        prompt="tadamoo.compaction:SUMMARY_PROMPT"),
    Use("chat_summary", params=(Param("answer_tokens", 1536, 200, 6000), Param("chunk_tokens", 10000, 2000, 24000)),
        prompt="serve.chatctx:SUMMARY_PROMPT", editable=True),
    Use("chat_abstract", needs_session=True, params=(Param("min_chars", 4000, 400, 60000),), prompt="serve.chatctx:ABSTRACT_PROMPT", editable=True),
    Use("code_abstract", needs_session=True, params=(Param("min_lines", 6, 2, 200),), prompt="serve.chatcode:CODE_ABSTRACT_PROMPT", editable=True),
    Use("longform"),
    Use("memory_extract", params=(Param("max_tokens", 900, 200, 4000), Param("timeout_s", 120, 10, 600, False)), prompt="tadamoo.memory_extract:SYSTEM"),
    Use("conversation_card", needs_session=True,
        params=(Param("max_tokens", 600, 200, 2000), Param("timeout_s", 60, 10, 600, False), Param("idle_s", 300, 30, 7200), Param("gap_s", 20, 5, 600)),
        prompt="tadamoo.cards:SYSTEM"),
    Use("memory_rank", needs_session=True, params=(Param("candidates", 12, 4, 40), Param("timeout_s", 8, 2, 60, False)), prompt="tadamoo.memory_rank:SYSTEM"),
    Use("progress", needs_session=True, params=(Param("every_steps", 8, 2, 60), Param("timeout_s", 10, 2, 60, False)), prompt="tadamoo.progress:SYSTEM"),
    Use("planner", params=(Param("timeout_s", 120, 10, 600, False),)),
    Use("tool_extract", needs_session=True, default_on=False,
        params=(Param("min_output_chars", 20000, 4000, 400000), Param("answer_tokens", 1200, 200, 4000), Param("chunk_tokens", 8000, 2000, 24000)),
        prompt="tadamoo.tool_extract:SYSTEM"),
    Use("lessons", needs_session=True, params=(Param("timeout_s", 60, 5, 600, False),), prompt="tadamoo.lessons:POLISH_SYSTEM"),
    Use("completion_check", needs_session=True, params=(Param("timeout_s", 10, 2, 60, False),), prompt="tadamoo.completion:SYSTEM"),
    Use("request_review", needs_session=True, params=(Param("timeout_s", 20, 2, 120, False),), prompt="tadamoo.review:SYSTEM"),
    Use("auto_approve", needs_session=True, default_on=False, params=(Param("max_chars", 3000, 500, 12000), Param("timeout_s", 8, 2, 60, False)),
        prompt="tadamoo.auto_approve:SYSTEM"),
    Use("inject_screen", needs_session=True, params=(Param("max_chars", 6000, 1000, 30000), Param("timeout_s", 8, 2, 60, False)),
        prompt="tadamoo.inject_screen:SYSTEM"),
)
BY_ID: Dict[str, Use] = {u.id: u for u in USES}

_lock = threading.Lock()
_cache: Dict[str, Any] = {"key": None, "values": {}}


def _dir() -> Path:
    from serve.userdata import user_data_dir
    return user_data_dir()


def file_path() -> Path:
    return _dir() / FILE


def _read() -> Dict[str, Any]:
    p = file_path()
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(p), None, None)
    with _lock:
        if _cache["key"] == key:
            return _cache["values"]
        values: Dict[str, Any] = {}
        if key[1] is not None:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    values = data
            except (OSError, ValueError):
                values = {}
        _cache.update(key=key, values=values)
        return values


def external_on() -> bool:
    """Whether the technical session may run on a provider outside this TaDaMoo (a route the person chose for the function `technical`). Off
    until the person switches it on: each job is a call that costs money or leaves the PC."""
    entry = _read().get("_external")
    return isinstance(entry, dict) and entry.get("on") is True


def set_external(on: bool) -> None:
    if not isinstance(on, bool):
        raise ValueError("external: true or false")
    values = dict(_read())
    values["_external"] = {"on": on}
    _write(values)


def session_on() -> bool:
    """The person's master switch for the whole technical session (the chat's own switch): on unless they turned it off. Every use is off while it is."""
    entry = _read().get("_session")
    return not (isinstance(entry, dict) and entry.get("on") is False)


def set_session(on: bool) -> None:
    if not isinstance(on, bool):
        raise ValueError("session: true or false")
    values = dict(_read())
    values["_session"] = {"on": on}
    _write(values)


def _entry(use: str) -> Dict[str, Any]:
    entry = _read().get(use)
    return entry if isinstance(entry, dict) else {}


def enabled(use: str) -> bool:
    """Whether `use` is made in the technical session (and, for the uses that need it, made at all)."""
    if use not in BY_ID:
        return True
    if not session_on():
        return False
    on = _entry(use).get("on")
    return BY_ID[use].default_on if on is None else on is True


def _spec(use: str, key: str) -> Param:
    for p in BY_ID[use].params:
        if p.key == key:
            return p
    raise KeyError(f"{use}: no parameter {key}")


def _clean(p: Param, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ValueError(f"{p.key}: a number from {p.lo:g} to {p.hi:g}")
    if not p.lo <= value <= p.hi:
        raise ValueError(f"{p.key}: a number from {p.lo:g} to {p.hi:g}, not {value:g}")
    return int(value) if p.integer else float(value)


def param(use: str, key: str) -> float:
    """The number `key` of `use` as it applies now: the file's, when it is in range, else the default."""
    p = _spec(use, key)
    given = (_entry(use).get("params") or {}).get(key)
    if given is None:
        return p.default
    try:
        return _clean(p, given)
    except ValueError:
        return p.default


def system(use: str, default: str) -> str:
    """The system prompt of `use`: the replacement when the person wrote one and the use allows it, else `default`."""
    text = _entry(use).get("system") if BY_ID.get(use) and BY_ID[use].editable else None
    return text if isinstance(text, str) and text.strip() else default


def prompt_text(use: str) -> str:
    """The shipped system prompt of `use`, "" when it is built for each call."""
    where = BY_ID[use].prompt
    if not where:
        return ""
    module, name = where.split(":")
    try:
        return str(getattr(importlib.import_module(module), name))
    except Exception:
        return ""


def _write(values: Dict[str, Any]) -> None:
    p = file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(values, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def save(use: str, on: Optional[bool] = None, params: Optional[Dict[str, Any]] = None, text: Optional[str] = None) -> None:
    """Change `use`. ValueError for an unknown use or number, a number out of range, or a prompt on a use that does not allow one."""
    if use not in BY_ID:
        raise ValueError(f"unknown use: {use}")
    entry = dict(_entry(use))
    if on is not None:
        if not isinstance(on, bool):
            raise ValueError("on: true or false")
        entry["on"] = on
    if params:
        kept = dict(entry.get("params") or {})
        for key, value in params.items():
            try:
                spec = _spec(use, key)
            except KeyError:
                raise ValueError(f"{use}: no parameter {key}") from None
            kept[key] = _clean(spec, value)
        entry["params"] = kept
    if text is not None:
        if not BY_ID[use].editable:
            raise ValueError(f"{use}: its prompt cannot be replaced")
        if not isinstance(text, str) or len(text) > MAX_SYSTEM_CHARS:
            raise ValueError(f"system: text up to {MAX_SYSTEM_CHARS} characters")
        if text.strip():
            entry["system"] = text
        else:
            entry.pop("system", None)
    values = dict(_read())
    values[use] = entry
    _write(values)


def reset(use: Optional[str] = None) -> None:
    """Back to the defaults: one use, or all of them."""
    if use is not None and use not in BY_ID:
        raise ValueError(f"unknown use: {use}")
    values = {k: v for k, v in _read().items() if k in ("_external", "_session")} if use is None else {k: v for k, v in _read().items() if k != use}
    _write(values)


def view() -> List[Dict[str, Any]]:
    """Every use as the settings page shows it."""
    out = []
    for u in USES:
        entry = _entry(u.id)
        out.append({
            "id": u.id, "on": enabled(u.id), "needs_session": u.needs_session, "editable": u.editable,
            "prompt": system(u.id, prompt_text(u.id)) if u.editable else prompt_text(u.id),
            "prompt_default": prompt_text(u.id), "prompt_changed": bool(u.editable and entry.get("system")),
            "params": [{"key": p.key, "value": param(u.id, p.key), "default": p.default, "min": p.lo, "max": p.hi, "integer": p.integer} for p in u.params],
            "default_on": u.default_on,
            "changed": bool(entry.get("params")) or entry.get("on") not in (None, u.default_on) or bool(entry.get("system")),
        })
    return out


# -- long jobs -------------------------------------------------------------------------------------------------------

class Job:
    """A long piece of work in the session, such as a text read in parts: shows how far it is and ends when the person says stop."""

    def __init__(self, use: str, label: str = "") -> None:
        self.id = uuid.uuid4().hex[:12]
        self.use, self.label, self.started = use, label, time.time()
        self.done, self.total = 0, 0
        self._path: Optional[Path] = None
        try:
            folder = _dir() / JOBS_DIR
            folder.mkdir(parents=True, exist_ok=True)
            self._path = folder / f"{self.id}.json"
        except OSError:
            self._path = None
        self._write()

    def _write(self) -> None:
        if self._path is None:
            return
        data = {"id": self.id, "use": self.use, "label": self.label, "started": self.started, "done": self.done, "total": self.total, "pid": os.getpid()}
        try:
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            self._path = None

    def progress(self, done: int, total: int) -> None:
        self.done, self.total = done, total
        self._write()                       # once a part, and a part is a model call: no need to hold back

    def stopped(self) -> bool:
        try:
            at = float(json.loads((_dir() / STOP_FILE).read_text(encoding="utf-8")).get("at") or 0)
        except (OSError, ValueError, AttributeError):
            return False
        return at >= self.started

    def close(self) -> None:
        if self._path is not None:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass

    def __enter__(self) -> "Job":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def job(use: str, label: str = "") -> Job:
    return Job(use, label)


def running() -> List[Dict[str, Any]]:
    """The jobs at work now, in every process."""
    out: List[Dict[str, Any]] = []
    folder = _dir() / JOBS_DIR
    now = time.time()
    try:
        files = list(folder.glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            if now - f.stat().st_mtime > JOB_STALE_S:
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return sorted(out, key=lambda d: d.get("started") or 0)


def stop_all() -> None:
    """Ends the jobs running now (a job that starts after this is not affected)."""
    path = _dir() / STOP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": time.time()}), encoding="utf-8")
