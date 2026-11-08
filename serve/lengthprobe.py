"""How long a reply this model writes, measured once when the model is first loaded and raised by use.

The model cannot count what it writes, so it is not asked for a length. It is asked for a text with no end (numbered entries, one after
the other, never concluding) under a cap in tokens that the server sets and counts; what is recorded is what came out. A model usually
closes the reply long before it matters, so each time it stops by itself it is asked to go on, with its own text before it, until it reaches
the cap, falls into a loop (where it began is recorded and the try is cancelled there) or will not go on. The first stop is kept apart: it
says how long a reply the model gives when it is asked for a long text, not how long it can write. The checkpoints at 10,000, 20,000 and
30,000 tokens say which of them were reached with a clean text. The record is a JSON file by the hash of the model file, with the identity of
the model and the settings in force. `reliable_tokens` is a limit only when something bounded it (the cap or a loop); a model that stops
going on has shown nothing about where it would degrade. Nothing here cuts a reply. A reply (thinking and answer together) that ends well
above the limit raises it."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

VERSION = 3
CHECKPOINTS = (10_000, 20_000, 30_000)        # tokens at which the text is looked at
CAP = CHECKPOINTS[-1]                         # the most one try may write
WINDOW = 6_000                                # characters looked at together for a loop
STEP = 1_500                                  # and the step between two looks
LOOK_EVERY = 4_000                            # characters written between two looks while a try runs
SERVER_LOOP_TOKENS = 2_000                    # where the server's own passage check puts the start of a loop it cut
SAVE_EVERY_S = 5.0                            # the least time between two writes of a raised number
MAX_PIECES = 60                               # the most times one text is asked to go on
DECLINE_TOKENS = 150                          # a piece shorter than this after a request to go on: the model is closing, not going on
CONTINUE = ("Go on from exactly where you stopped, with the next ones in the same form. Do not conclude, do not summarize and do not say "
            "that you are finishing.")

PROMPTS = (
    ("entries", "Write an encyclopedia of the rivers of the world. Number the entries 1, 2, 3 and so on, each of about two paragraphs, "
                "in full sentences. Never conclude, never summarize and never say that you are finishing: after each entry write the next one, "
                "and go on until you are stopped."),
    ("story", "Write a novel that has no last chapter. Number the chapters 1, 2, 3 and so on, each of several paragraphs of narrative with "
              "new characters, places and events. Never conclude and never write an ending: after each chapter write the next one, and go on "
              "until you are stopped."),
)


def record_path(meta_hash: str) -> Path:
    from serve import userdata
    return userdata.path("lengths") / f"{meta_hash}.json"


def identity(facts: dict, size: int | None = None, quant: str | None = None) -> dict:
    """What names the model file: its name, quantization, full path, size and the hash of its meta section."""
    path = facts.get("path") or ""
    try:
        st = Path(path).stat() if path else None
    except OSError:
        st = None
    return {"name": facts.get("name"), "path": str(Path(path).resolve()) if path else None, "quant": quant,
            "size_bytes": size if size is not None else (st.st_size if st else None),
            "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)) if st else None,
            "meta_hash": facts.get("meta_hash")}


def read(meta_hash: str) -> Optional[dict]:
    try:
        data = json.loads(record_path(meta_hash).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("version") == VERSION else None


def write(meta_hash: str, record: dict) -> Path:
    path = record_path(meta_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="length.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def looped_now(text: str) -> bool:
    """True when the end of the text is mostly the same units over and over (the harness's own check)."""
    from tadamoo.output_guard import repetition_dominated
    return repetition_dominated(text[-16_384:])


def loop_start(text: str) -> Optional[int]:
    """The character where a loop in the text began (the start of the first stretch that is mostly repeats), or None for a clean text."""
    from tadamoo.output_guard import repetition_dominated
    for i in range(0, max(1, len(text) - WINDOW + STEP), STEP):
        if repetition_dominated(text[i:i + WINDOW]):
            return i
    return None


def judge(prompt: str, cap: int, text: str, tokens: int, finish: str, repeated: bool, seconds: float, ended_by: str = "",
          stops: List[int] | None = None) -> dict:
    """One try as the record keeps it, from the text and the tokens the server counted. `clean_tokens`: the tokens before a loop began (all of
    them when there was none). `natural`: the model would not go on, without a loop. `stops`: the token counts at which it ended a piece by
    itself; `first_stop_tokens`: the first of them, the length of a single reply to this request."""
    tokens = int(tokens)
    start = loop_start(text)
    if start is not None:
        clean = int(tokens * start / max(1, len(text)))
    elif repeated or ended_by == "loop":
        clean = max(0, tokens - SERVER_LOOP_TOKENS)       # the server or the watch cut a loop that the text check had not placed yet
    else:
        clean = tokens
    looped = start is not None or bool(repeated) or ended_by == "loop"
    words = len(text.split())
    stops = [int(s) for s in (stops or [])]
    return {"prompt": prompt, "cap": cap, "tokens": tokens, "words": words, "finish": finish, "looped": looped, "clean": not looped,
            "pieces": len(stops) + (0 if finish == "stop" and stops else 1), "stops": stops, "first_stop_tokens": stops[0] if stops else None,
            "clean_tokens": clean, "clean_words": int(words * clean / tokens) if tokens else 0,
            "natural": finish == "stop" and not looped,
            "reached_cap": tokens >= cap * 0.98 and finish == "length" and not looped,
            "checkpoints": [{"at": c, "reached": tokens >= c, "clean": clean >= c} for c in CHECKPOINTS if c <= cap],
            "seconds": round(seconds, 1)}


def summarize(rungs: List[dict]) -> dict:
    """The numbers the rest of the program uses: the most tokens (and words) written before any loop, the most written when the model stopped by
    itself, and whether a try reached the cap clean."""
    best = max(rungs, key=lambda r: r["clean_tokens"], default=None)
    own = max((r for r in rungs if r["natural"]), key=lambda r: r["tokens"], default=None)
    reached = any(r["reached_cap"] for r in rungs)
    return {"reliable_tokens": best["clean_tokens"] if best else 0, "reliable_words": best["clean_words"] if best else 0,
            "natural_tokens": own["tokens"] if own else 0, "natural_words": own["words"] if own else 0,
            "first_stop_tokens": max((r.get("first_stop_tokens") or 0 for r in rungs), default=0),
            "reached_cap": reached, "bounded": reached or any(r["looped"] for r in rungs)}


def run_text(prompt: str, ask: Callable[[List[dict], int], dict], cap: int) -> dict:
    """One text: the model is asked, and each time it ends a piece by itself it is asked to go on, with its own text before it, until the cap,
    a loop, a stop of the person's or its refusal to go on. Returns the pieces put together, in the form `judge` takes."""
    messages = [{"role": "user", "content": prompt}]
    text: List[str] = []
    tokens, seconds, stops = 0, 0.0, []
    state = {"finish": "stop", "repeated": False, "ended_by": ""}
    for _ in range(MAX_PIECES):
        left = cap - tokens
        if left < DECLINE_TOKENS:
            state["finish"] = "length"                                 # it went as far as it was let
            break
        got = ask(list(messages), left)
        piece, n = got.get("text") or "", int(got.get("tokens") or 0)
        if got.get("cap") and int(got["cap"]) < left:
            cap = tokens + int(got["cap"])                              # the context left less than was asked
        text.append(piece)
        tokens += n
        seconds += got.get("seconds") or 0.0
        state.update(finish=got.get("finish") or "", repeated=state["repeated"] or bool(got.get("repeated")), ended_by=got.get("ended_by") or "")
        if state["ended_by"] or state["repeated"] or state["finish"] != "stop":
            break
        stops.append(tokens)
        if n < DECLINE_TOKENS:
            break
        messages += [{"role": "assistant", "content": piece}, {"role": "user", "content": CONTINUE}]
    else:
        state["finish"] = "length"
    return {"text": "".join(text), "tokens": tokens, "seconds": seconds, "stops": stops, "cap": cap, **state}


def measure(ask: Callable[[List[dict], int], dict], stop: threading.Event | None = None, on_step: Callable[[dict, list], None] | None = None,
            cap: int = CAP, prompts=PROMPTS) -> dict:
    """Run the tries. `ask(messages, cap)` returns {"text", "tokens", "finish", "repeated", "seconds", "ended_by"} with the tokens as the server
    counted them, for one piece. The first text with no end is asked to go on until the cap, a loop or the model's refusal; when it does not reach
    the cap clean, the second kind of text is tried too. Returns {"rungs", "complete", "stopped_by"}; `cap` of a rung is the cap in force when the
    context left less than asked."""
    done: List[dict] = []
    why = "ladder"
    for name, prompt in prompts:
        if stop is not None and stop.is_set():
            why = "stopped"
            break
        got = run_text(prompt, ask, cap)
        if got.get("ended_by") == "stopped" or got.get("finish") == "cancel" and got.get("ended_by") != "loop":
            why = "stopped"
            break
        r = judge(name, int(got.get("cap") or cap), got.get("text") or "", got.get("tokens") or 0, got.get("finish") or "", got.get("repeated"),
                  got.get("seconds") or 0.0, got.get("ended_by") or "", got.get("stops"))
        done.append(r)
        if on_step is not None:
            on_step(r, done)
        if r["reached_cap"]:
            why = "cap"                                        # it went as far as it was let, with a clean text
            break
    return {"rungs": done, "complete": why != "stopped", "stopped_by": why}


def new_record(model: dict, context: int, engine: str | None, result: dict, settings: dict | None = None) -> dict:
    """`settings`: what was in force while it was measured (relaxed drafting and the like change what the model writes)."""
    sums = summarize(result["rungs"])
    return {"version": VERSION, "model": model, "engine": engine, "context": context, "settings": settings or {},
            "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "complete": result["complete"], "stopped_by": result["stopped_by"], "rungs": result["rungs"], **sums,
            "observed_max_tokens": sums["reliable_tokens"]}


class Lengths:
    """The record of the model in use, held by the service: read when the model loads, raised by replies that end well."""

    def __init__(self, facts: Callable[[], dict]):
        self.facts = facts
        self.record: Optional[dict] = None
        self.lock = threading.Lock()
        self._saved_at = 0.0

    def attach(self) -> Optional[dict]:
        """Read the record of the model in use (None when it has none yet)."""
        key = self.facts().get("meta_hash")
        with self.lock:
            self.record = read(key) if key else None
            return self.record

    def limit_tokens(self) -> int:
        """Tokens of a reply (thinking and answer) that this model has been seen to write well; 0 while unknown. The measurement counts only when
        something bounded it (the cap or a loop): a model that would not go on has not shown where it degrades."""
        r = self.record
        if not r:
            return 0
        measured = r.get("reliable_tokens") or 0 if r.get("bounded") else 0
        return int(max(measured, r.get("observed_max_tokens") or 0))

    def set(self, record: dict) -> None:
        with self.lock:
            self.record = record

    def note(self, done: dict) -> bool:
        """A finished reply: when it ended by the model's own stop, without a loop or a cap, and is longer than any seen, it is the new
        limit. True when the limit rose."""
        r = self.record
        tokens = int(done.get("completion_tokens") or 0)
        if r is None or done.get("finish") != "stop" or done.get("repeated") or done.get("answer_capped") or tokens <= self.limit_tokens():
            return False
        with self.lock:
            r["observed_max_tokens"] = tokens
            r["observed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            now = time.monotonic()
            if now - self._saved_at >= SAVE_EVERY_S:
                self._saved_at = now
                key = (r.get("model") or {}).get("meta_hash")
                if key:
                    try:
                        write(key, r)
                    except OSError:
                        pass
        return True

    def flush(self) -> None:
        r = self.record
        key = ((r or {}).get("model") or {}).get("meta_hash")
        if r and key:
            with self.lock:
                write(key, r)

    def summary(self) -> Dict[str, Any]:
        r = self.record
        if not r:
            return {"known": False}
        return {"known": True, "limit_tokens": self.limit_tokens(), "reliable_tokens": r.get("reliable_tokens"), "bounded": bool(r.get("bounded")),
                "first_stop_tokens": r.get("first_stop_tokens"), "settings": r.get("settings") or {},
                "reliable_words": r.get("reliable_words"), "natural_tokens": r.get("natural_tokens"), "natural_words": r.get("natural_words"),
                "reached_cap": r.get("reached_cap"), "observed_max_tokens": r.get("observed_max_tokens"), "measured_at": r.get("measured_at"),
                "complete": r.get("complete"), "stopped_by": r.get("stopped_by"), "model": r.get("model"),
                "rungs": [{k: x.get(k) for k in ("prompt", "cap", "tokens", "clean_tokens", "finish", "clean", "natural", "checkpoints", "pieces", "first_stop_tokens")}
                          for x in r.get("rungs", [])]}
