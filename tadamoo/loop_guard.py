"""Catches an agent that goes round in circles by what its calls bring back, not only by what it asks.

Each executed call is noted as a pair: the call (tool and arguments) and its outcome (a hash of the result with times, durations,
process ids and addresses taken out). From the pairs the guard tells four things:

* the same call with the same outcome again and again,
* a cycle of 2 to 4 calls whose outcomes repeat (A, B, A, B, A, B),
* one outcome coming back for many different calls (the arguments change, the result does not),
* a tool failing again and again with different errors (tools that are meant to fail while work goes on, such as a shell, are told
  of it only).

A change to the world (a file edited) is progress: it clears what was counted. A result that is long and the same as an earlier one
of the same call is replaced by a short stub that points at the first. The guard decides nothing about permissions and runs nothing: it
answers with a note for the model, a stub, and whether the call counts as a strike toward ending the run.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Optional, Set, Tuple

REPEAT_NOTICE, REPEAT_STOP = 3, 5       # the same call with the same outcome this many times in a row
CYCLE_ROUNDS = 3                        # a pattern of 2 to 4 calls seen this many times in a row is a cycle; one round more is a strike
CYCLE_MAX = 4
CHURN_NOTICE, CHURN_STOP = 4, 12        # different calls of one tool with one outcome
FAILURE_NOTICE, FAILURE_STOP = 5, 8     # failures of one tool in a row, whatever the errors
STUB_MIN_CHARS = 400                    # a repeated result shorter than this is left as it is
HISTORY = 40
FAILURE_TOLERANT = frozenset(("shell_command", "run_code", "run_tests"))     # red runs are part of the work: they get the note and never a strike
EXEMPT = frozenset(("ask_user", "todo_write", "present_plan"))               # tools that are asked again by design

_VOLATILE = (
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<time>"),
    (re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b"), "<time>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|secs|seconds|min)\b"), "<duration>"),
    (re.compile(r"\bpid[ =:]*\d+", re.IGNORECASE), "pid <n>"),
    (re.compile(r"0x[0-9a-fA-F]{6,}"), "<address>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<id>"),
)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def outcome_hash(text: str, is_error: bool) -> str:
    """A fingerprint of a result: what varies from run to run (times, durations, process ids, addresses) is taken out first."""
    for pattern, stand_in in _VOLATILE:
        text = pattern.sub(stand_in, text)
    return digest(("E" if is_error else "O") + text)


def call_signature(name: str, args: Any) -> str:
    return digest(json.dumps({"tool": name, "args": args}, sort_keys=True, default=str))


@dataclass
class Verdict:
    note: str = ""          # said to the model after the result
    stub: str = ""          # replaces a long result that is the same as an earlier one
    strike: bool = False    # counts toward ending the run


class OutcomeGuard:
    def __init__(self, failure_tolerant: frozenset = FAILURE_TOLERANT, exempt: frozenset = EXEMPT) -> None:
        self.failure_tolerant, self.exempt = failure_tolerant, exempt
        self.restart()

    def restart(self) -> None:
        """A new request of the person: nothing is counted and no earlier result is pointed at."""
        self._first: Dict[Tuple[str, str], str] = {}
        self.progress()

    def progress(self) -> None:
        """The world changed: what was counted so far no longer says the agent is stuck."""
        self._history: Deque[Tuple[str, str, str]] = deque(maxlen=HISTORY)       # (call, outcome, tool)
        self._streak = 0
        self._last: Optional[Tuple[str, str]] = None
        self._by_outcome: Dict[Tuple[str, str], Set[str]] = {}
        self._failures: Dict[str, int] = {}
        self._noted: Set[str] = set()

    def observe(self, call_id: str, name: str, args: Any, text: str, is_error: bool, mutated: bool = False, pure: bool = False,
                held: Optional[Callable[[str], bool]] = None) -> Verdict:
        """One executed call and what it brought back. `mutated`: it changed something; `pure`: the tool only reads; `held`: whether an
        earlier result, by its call id, is still in the conversation."""
        verdict = Verdict()
        if name in self.exempt:
            return verdict
        if mutated and not is_error:
            self.progress()
        sig, out = call_signature(name, args), outcome_hash(text, is_error)
        pair = (sig, out)
        self._history.append((sig, out, name))

        self._streak = self._streak + 1 if pair == self._last else 1
        self._last = pair
        if self._streak >= REPEAT_STOP:
            verdict.strike = True
            verdict.note = self._once("repeat-stop", f"[harness] This is the {self._streak}th identical call with the same result. Stop repeating it: change what you ask or take another way, or say what blocks you.")
        elif self._streak == REPEAT_NOTICE:
            verdict.note = self._once("repeat", f"[harness] The last {REPEAT_NOTICE} calls were the same and brought back the same result. Repeating it will not help: change what you ask or take another way.")

        first = self._first.get(pair)
        if first is None:
            self._first[pair] = call_id
        elif pure and not is_error and len(text) >= STUB_MIN_CHARS and (held is None or held(first)):
            verdict.stub = f"[Same result as call {first} above: nothing changed since. The text is not repeated; read it there.]"

        rounds = self._cycle_rounds()
        if rounds:
            names = ", ".join(dict.fromkeys(n for _, _, n in list(self._history)[-rounds[1]:]))
            line = f"[harness] The last calls repeat a pattern of {rounds[1]} ({names}) with the same results each time, {rounds[0]} times now. Break out of it: do something different, or say what blocks you."
            if rounds[0] > CYCLE_ROUNDS:
                verdict.strike = True
                verdict.note = verdict.note or self._once(f"cycle-stop{rounds[1]}", line)
            else:
                verdict.note = verdict.note or self._once(f"cycle{rounds[1]}", line)

        seen = self._by_outcome.setdefault((name, out), set())
        seen.add(sig)
        if len(seen) >= CHURN_STOP and name not in self.failure_tolerant:
            verdict.strike = True
        if len(seen) >= CHURN_NOTICE:
            verdict.note = verdict.note or self._once(f"churn-{name}-{out}", f"[harness] '{name}' has brought back this same result for {len(seen)} different calls. Another variation is unlikely to differ: change the approach.")

        if is_error:
            self._failures[name] = self._failures.get(name, 0) + 1
            count = self._failures[name]
            if count >= FAILURE_STOP and name not in self.failure_tolerant:
                verdict.strike = True
            if count >= FAILURE_NOTICE:
                verdict.note = verdict.note or self._once(f"fail-{name}-{count // FAILURE_NOTICE}", f"[harness] '{name}' has failed {count} times in a row. Read the errors together before the next call: something you assume is wrong.")
        else:
            self._failures[name] = 0
        return verdict

    def _once(self, key: str, text: str) -> str:
        """The note the first time something is seen, and nothing after, until progress clears it."""
        if key in self._noted:
            return ""
        self._noted.add(key)
        return text

    def _cycle_rounds(self) -> Optional[Tuple[int, int]]:
        """(how many times the pattern has repeated, its length) for the shortest cycle of 2 to CYCLE_MAX calls that ends the history, or None."""
        pairs = [(s, o) for s, o, _ in self._history]
        for period in range(2, CYCLE_MAX + 1):
            if len(pairs) < period * CYCLE_ROUNDS:
                continue
            pattern = pairs[-period:]
            if len(set(pattern)) < 2:
                continue
            rounds = 1
            while len(pairs) >= period * (rounds + 1) and pairs[-period * (rounds + 1):len(pairs) - period * rounds] == pattern:
                rounds += 1
            if rounds >= CYCLE_ROUNDS:
                return rounds, period
        return None
