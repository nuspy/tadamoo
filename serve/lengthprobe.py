"""How long a reply this model writes by itself, measured once when the model is first loaded and raised by use.

The probe asks for a chapter of growing length (and last a whole book), with the thinking off, and keeps for each try the words and tokens
that came, how it ended (the model stopped, the cap or the context was reached) and whether the text fell into a loop. The record is a JSON
file by the hash of the model file, with the identity of the model. The number it gives, `reliable_tokens`, is where the harness starts to
write a text in checked parts; nothing here cuts a reply. A reply (thinking and answer together) that ends well above it raises it."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

VERSION = 1
RUNGS = (2000, 5000, 10000, 20000, 60000)      # the words each try asks for; the last is a whole book
ENOUGH = 0.6                                   # a try that gives less than this share of the words asked ends the ladder
SLICE = 16_384                                 # characters looked at once for a loop
SAVE_EVERY_S = 5.0                             # the least time between two writes of a raised number
TOPICS = ("the history of a river harbour town and the families who lived there",
          "a long journey by train across a continent, told stage by stage",
          "how a small company builds, tests and ships a product over one year")

PROMPT = ("Write one chapter of a book about {topic}. It must be about {words} words long. Write continuous prose in full paragraphs, "
          "with no outline, no notes about the length and no summary at the end. Keep writing until you reach about {words} words.")


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


def looped(text: str) -> bool:
    """True when a stretch of the text is mostly the same units over and over (the harness's own check, over slices)."""
    from tadamoo.output_guard import repetition_dominated
    return any(repetition_dominated(text[i:i + SLICE]) for i in range(0, max(1, len(text)), SLICE))


def judge(asked: int, text: str, tokens: int, finish: str, repeated: bool, seconds: float) -> dict:
    """One try as the record keeps it. `clean`: no loop. `natural`: the model ended the text itself."""
    words = len(text.split())
    bad = bool(repeated) or looped(text)
    return {"asked_words": asked, "words": words, "tokens": int(tokens), "finish": finish, "looped": bad, "clean": not bad,
            "natural": finish == "stop" and not bad, "seconds": round(seconds, 1)}


def summarize(rungs: List[dict]) -> dict:
    """The numbers the rest of the program uses, from the tries so far: the longest clean reply in tokens and words, and the longest
    one the model ended by itself."""
    clean = [r for r in rungs if r["clean"]]
    natural = [r for r in rungs if r["natural"]]
    best = max(clean, key=lambda r: r["tokens"], default=None)
    own = max(natural, key=lambda r: r["tokens"], default=None)
    return {"reliable_tokens": best["tokens"] if best else 0, "reliable_words": best["words"] if best else 0,
            "natural_tokens": own["tokens"] if own else 0, "natural_words": own["words"] if own else 0}


def measure(ask: Callable[[str], dict], stop: threading.Event | None = None, on_step: Callable[[dict, list], None] | None = None,
            rungs=RUNGS) -> dict:
    """Run the ladder. `ask(prompt)` returns {"text", "tokens", "finish", "repeated", "seconds"}. It stops at the first try that
    loops, that is cut by the cap or the context, or that gives much less than was asked. Returns {"rungs", "complete", "stopped_by"}."""
    done: List[dict] = []
    why = "ladder"
    for i, words in enumerate(rungs):
        if stop is not None and stop.is_set():
            why = "stopped"
            break
        got = ask(PROMPT.format(topic=TOPICS[i % len(TOPICS)], words=words))
        if got.get("finish") == "cancel":
            why = "stopped"
            break
        r = judge(words, got.get("text") or "", got.get("tokens") or 0, got.get("finish") or "", got.get("repeated"), got.get("seconds") or 0.0)
        done.append(r)
        if on_step is not None:
            on_step(r, done)
        if not r["clean"]:
            why = "loop"
            break
        if r["finish"] != "stop":
            why = "cap"                                        # the context or the cap ended it: nothing longer can be asked
            break
        if r["words"] < ENOUGH * words:
            why = "short"                                      # the model ended by itself well before the length asked
            break
    return {"rungs": done, "complete": why in ("ladder", "cap", "short", "loop"), "stopped_by": why}


def new_record(model: dict, context: int, engine: str | None, result: dict) -> dict:
    sums = summarize(result["rungs"])
    return {"version": VERSION, "model": model, "engine": engine, "context": context, "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
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
        """Tokens of a reply (thinking and answer) that this model has been seen to write well; 0 while unknown."""
        r = self.record
        return int(max(r.get("reliable_tokens") or 0, r.get("observed_max_tokens") or 0)) if r else 0

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
        return {"known": True, "limit_tokens": self.limit_tokens(), "reliable_tokens": r.get("reliable_tokens"),
                "reliable_words": r.get("reliable_words"), "natural_words": r.get("natural_words"),
                "observed_max_tokens": r.get("observed_max_tokens"), "measured_at": r.get("measured_at"),
                "complete": r.get("complete"), "stopped_by": r.get("stopped_by"), "model": r.get("model"),
                "rungs": [{k: x[k] for k in ("asked_words", "words", "tokens", "finish", "clean")} for x in r.get("rungs", [])]}
