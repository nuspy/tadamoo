"""A personality learns from a conversation once it has ended, in the background (tadamoo/persona_learning.py does the learning).

A conversation that speaks as a personality tells this worker at the end of each of its turns; once it has been quiet for `idle_s` seconds it
is over, and the worker asks the loaded model on this PC (the technical session when the text fits it, otherwise an ordinary call while the
card is free) to write the notes. The jobs wait in the personality's own encrypted queue, so a restart does not lose them. A conversation that
is private, gone, or now speaks as another personality teaches nothing. The model-only chat hands over its whole conversation instead.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from tadamoo import side_uses
from tadamoo.persona_learning import Learner
from tadamoo.personal_memory import MAX_QUEUE
from tadamoo.personas import PersonaError, PersonaStore
from tadamoo.side import side_extra_for

USE = "persona_learning"
JOB = "learn"
POLL_S = 3.0
RETRY_S = 30.0                  # after a failed call
MAX_LINES = 40                  # lines of a model-only conversation that are handed over
LINE_CHARS = 4000


class PersonaLearningWorker:
    def __init__(self, store: PersonaStore, client: Callable[[], Any], free: Callable[[], bool], source: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
                 poll_s: float = POLL_S, clock: Callable[[], float] = time.time) -> None:
        self.store, self.client, self.free, self.poll_s, self.clock = store, client, free, poll_s, clock
        self.source = source                                  # conversation id -> {"persona", "private", "messages"}, or None when it is gone
        self.learner = Learner(store, clock)
        self._due: Dict[Tuple[str, str], float] = {}          # (personality, conversation) -> when it counts as over
        self._fresh: List[Tuple[str, str]] = []               # told, not yet written to the personality's queue
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last = 0.0                                       # when the model was last asked
        self.error = ""
        self.last_run: Optional[Dict[str, Any]] = None

    # -- what the conversations tell it ---------------------------------------------------------------

    def touch(self, conversation: str, slug: str) -> None:
        """A turn of a conversation that speaks as `slug` ended: it is over once it has been quiet for `idle_s`."""
        with self._lock:
            self._due[(slug, conversation)] = self.clock() + float(side_uses.param(USE, "idle_s"))
            self._fresh.append((slug, conversation))

    def submit(self, slug: str, conversation: str, lines: List[Dict[str, Any]]) -> bool:
        """A conversation without a harness (the model-only chat) that has ended: its lines are kept, encrypted, to be learned from."""
        kept = [{"role": l["role"], "content": str(l["text"])[:LINE_CHARS]} for l in lines[-MAX_LINES:]]
        if not self.store.memory(slug).enqueue({"kind": JOB, "conversation": conversation, "messages": kept}):
            return False
        with self._lock:
            self._due[(slug, conversation)] = self.clock()
        return True

    def resume(self) -> int:
        """The jobs that waited in the personalities' queues when the server stopped are due again."""
        found = 0
        for persona in self.store.list():
            for job in self.store.memory(persona["slug"]).queue(MAX_QUEUE):
                if job.get("kind") == JOB:
                    with self._lock:
                        self._due.setdefault((persona["slug"], str(job.get("conversation") or "")), self.clock())
                    found += 1
        return found

    # -- one unit of work -----------------------------------------------------------------------------

    def _persist(self) -> None:
        with self._lock:
            fresh, self._fresh = list(dict.fromkeys(self._fresh)), []
        for slug, conversation in fresh:
            try:
                if not self.store.get(slug)["learn"]:
                    continue                                   # it does not learn: no job to keep
                memory = self.store.memory(slug)
                if not any(j.get("kind") == JOB and j.get("conversation") == conversation for j in memory.queue(MAX_QUEUE)):
                    memory.enqueue({"kind": JOB, "conversation": conversation})
            except PersonaError:
                continue

    def _finish(self, key: Tuple[str, str], job_id: str, run: Dict[str, Any]) -> None:
        with self._lock:
            self._due.pop(key, None)
        try:
            self.store.memory(key[0]).queue_done([job_id])
        except PersonaError:
            pass
        self.last_run = {"at": self.clock(), "persona": key[0], "conversation": key[1], **run}

    def step(self) -> bool:
        """The next conversation that is over; False when there was nothing to do now (or the model could not be asked yet)."""
        self._persist()
        now = self.clock()
        with self._lock:
            ready = sorted((t, k) for k, t in self._due.items() if t <= now)
        if not ready:
            return False
        key = ready[0][1]
        slug, conversation = key
        try:
            memory = self.store.memory(slug)
            job = next((j for j in memory.queue(MAX_QUEUE) if j.get("kind") == JOB and j.get("conversation") == conversation), None)
        except PersonaError:
            job = None
        if job is None:                                        # forgotten, or the personality is gone
            with self._lock:
                self._due.pop(key, None)
            return True
        if isinstance(job.get("messages"), list):
            messages = job["messages"]
        else:
            seen = self.source(conversation) if self.source is not None else None
            if seen is None or seen.get("private") or seen.get("persona") != slug:
                self._finish(key, job["id"], {"outcome": "not_learned"})
                return True
            messages = seen.get("messages") or []
        client = self.client()
        stays = getattr(client, "stays_local", None)
        if callable(stays) and not stays() and not memory.settings()["cloud_ok"]:
            self._finish(key, job["id"], {"outcome": "not_allowed"})        # the route leaves this PC and this personality's memory does not allow it
            return True
        answer = int(side_uses.param(USE, "max_tokens"))
        captured: Dict[str, Any] = {}

        def ask(prompt: List[Dict[str, Any]]) -> str:
            side = side_extra_for(client, USE, sum(len(str(m["content"])) for m in prompt), answer)
            if not side and not self.free():
                raise _NotNow()
            captured["side"] = bool(side)
            reply = client.chat_completion(messages=prompt, temperature=0.0, max_tokens=answer, timeout=side_uses.param(USE, "timeout_s"),
                                           response_format={"type": "json_object"}, extra={"reasoning_effort": "none", **side})
            self.last = self.clock()
            return reply["choices"][0]["message"].get("content") or ""
        try:
            result = self.learner.learn(slug, conversation, messages, ask)
        except _NotNow:
            return False                                       # the card is busy: it looks again a little later
        except Exception as exc:                               # the model is unloaded or gone: the job waits
            self.error = f"{type(exc).__name__}: {exc}"[:200]
            self.last = self.clock()
            with self._lock:
                self._due[key] = self.clock() + RETRY_S
            return False
        self.error = ""
        self._finish(key, job["id"], {**result, "in_session": bool(captured.get("side"))})
        return True

    # -- the thread -----------------------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="persona-learning", daemon=True)
                self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            self.resume()
        except Exception as exc:                               # a damaged memory file stops the look back, not the worker
            self.error = f"{type(exc).__name__}: {exc}"[:200]
        while not self._stop.is_set():
            did = False
            try:
                did = self.step()
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"[:200]
            self._stop.wait(0.5 if did else self.poll_s)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            waiting = len(self._due)
        return {"waiting": waiting, "error": self.error, "last": self.last_run}


class _NotNow(Exception):
    """The card is busy with something else."""
