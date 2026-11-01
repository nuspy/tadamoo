"""ReAct agent loop: model call, tool-call parsing, guarded execution, loop detection and budgets.

Every tool call goes through the same gate: argument validation, pre-tool hooks, the permission
manager, an optional checkpoint, execution, secret masking, untrusted-content fencing, post-tool
hooks and an audit record. Any piece left out of the constructor is simply not applied.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from harness.audit import AuditLedger
from harness.checkpoints import CheckpointManager, call_paths
from harness.client import BasaltClient, BasaltError, StreamCancelled, extract_reasoning_and_content
from harness.context import ContextManager, fence_untrusted, wire_messages
from harness.continuation import cut_point, join_continuation
from harness.gate import VerifyGate, event_data, read_result
from harness.loop_guard import OutcomeGuard, Verdict
from harness.repeat_guard import RepeatGuard
from harness.rule_of_two import CONTENT_TOOLS, leak_question
from harness import inject_screen, outward_calls
from harness.hooks import AskPerson, HookManager
from harness.output_guard import RepetitionAbort, RepetitionWatch, raised_penalty, repetition_dominated
from harness.permissions import PermissionManager
from harness.pipeline import Card, Pipeline, feedback
from harness.sandbox import cancel_scope
from harness.secrets_broker import SecretsBroker
from harness.tools.registry import EffectClass, ToolArgumentError, ToolRegistry
from harness.tools.repair import repair_json

TOOL_CALL_REGEX = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
FUNCTION_REGEX = re.compile(r"<function=([^>\s]+)>(.*?)</function>", re.DOTALL)
PARAMETER_REGEX = re.compile(r"<parameter=([^>\s]+)>\n?(.*?)\n?</parameter>", re.DOTALL)
FINAL, MAX_TURNS, CANCELLED, BUDGET, LOOP, ERROR = "final", "max_turns", "cancelled", "budget", "loop", "error"
LENGTH = "length"          # a tool call was cut by the output limit again and again; nothing was run
EMPTY = "empty"            # the model's thinking used the whole output and no answer came, also after a second try
DEGENERATE = "degenerate"  # the model's output fell into a repetition loop, also after a second try
SUSPENDED = "suspended"   # put aside at a safe point for something more urgent; it can go on from where it stands
ASKED = "asked"       # the turn ended on a question for the person; their answer is the next message
VERIFY_FAILED = "verify_failed"   # the project's checks still failed after the fixes allowed, or could not be run
MAX_CALLS_PER_MESSAGE = 16     # tool calls run from one model message; the rest are answered "not run"
MAX_COMPLETION_ROUNDS = 2      # times the completion check may send the model back to finish, per request
MAX_STOP_DENIALS = 2           # times a stop hook may send the model back to work, per request
BUDGET_WARN_AT = 0.9           # the share of a budget used at which the model is told it is nearly spent
BUDGET_WARNING = ("[harness] The budget for this answer is nearly used up. Finish what matters most now, and let your last message say what "
                  "is done and what is not.")
GRACE_NOTE = ("[harness] The budget for this answer is used up. Do not call any tool. Write your final report now: what is done, what is "
              "not done, and what the next step is.")
TODO_NOTE = ("[harness] Your checklist still has unfinished items:\n{items}\nFinish them, or update the list with todo_write if they no longer "
             "apply, then answer again.")
MAX_CONTINUATIONS = 2          # times a reply cut by the output limit is asked to go on, per request
MAX_CUT_RETRIES = 3            # times a tool call cut by the output limit is asked for again, each with twice the room
RETRY_TOKENS = 8192            # the room a cut tool call is asked again with when neither the setting nor the answer says how much it had
RETRY_TOKENS_MAX = 131072
CONTINUE_NOTE = ("[harness] Your reply was cut by the output limit. Continue exactly where you stopped: do not repeat what is already "
                 "written, and do not start over.")
SHORT_ANSWER_NOTE = ("[harness] Your last reply had no answer: the thinking used up the whole output. Think very briefly now and write "
                     "the answer itself, in a few sentences.")
EMPTY_NOTICE = ("The model produced no answer: its thinking used up the whole output, also on a second try with less thinking. "
                "Ask again, more narrowly, or lower the thinking level.")
DEGENERATE_NOTICE = "The model's output fell into a repetition loop, also on a second try with a higher repetition penalty; it was stopped."
LENGTH_NOTICE = ("A tool call was cut by the output limit, also with {tokens} tokens of room, so it was not run. "
                 "Raise the answer limit (--max-tokens) or ask for smaller steps.")
LOWER_EFFORT = {"high": "low", "medium": "low", "low": "none", "none": "none", "": "low"}      # thinking level for the second try, by the session's level
_call_note = threading.local()    # the person's sentence for the call this thread is running


def current_explanation() -> str:
    """The sentence the model gave the person for the tool call now running in this thread (empty if none): for the questions that call asks."""
    return getattr(_call_note, "text", "")


def local_now() -> datetime:
    """The time on this PC's clock, with its timezone."""
    return datetime.now().astimezone()


def local_time_line(now: datetime) -> str:
    """What the model is told before each request: the person's date, time and timezone (offset, and the zone's name when there is one)."""
    offset = now.strftime("%z")
    zone = f"UTC{offset[:3]}:{offset[3:]}" if len(offset) == 5 else "UTC"
    name = now.tzname() or ""
    return f"[harness] The person's local date and time: {now:%A %Y-%m-%d %H:%M} ({zone}{', ' + name if name and name != zone else ''})."


@dataclass
class ToolExecutionResult:
    tool_name: str
    arguments: Dict[str, Any]
    output: Any
    is_error: bool = False
    call_id: str = ""
    decision: str = ""      # "executed", "denied", "blocked", "invalid", "loop" or "skipped"
    description: str = ""   # the model's own sentence for the person; display text only
    repaired: bool = False  # the call's name, JSON or value types were mended before it ran (harness/tools/repair.py)
    written: Any = None     # the arguments as the model wrote them, before the value types were mended
    feedback: str = ""      # what the post-tool hooks said about the call, for the model


@dataclass
class AgentTurn:
    turn_index: int
    user_input: str
    reasoning: str
    assistant_content: str
    tool_calls: List[ToolExecutionResult] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    seconds: float = 0.0
    finish_reason: str = ""
    cut: bool = False         # the output limit ended this reply (it was asked to go on, or delivered as it is)
    continues: bool = False   # this reply goes on from the end of the one before it
    raw_content: str = ""     # the text as written, for joining a reply that went on


@dataclass
class ParsedCall:
    call_id: str
    name: str
    arguments: Any
    error: str = ""         # set when the call could not be read at all
    repaired: bool = False  # its name or its JSON was mended (harness/tools/repair.py)


def answer_text(turns: List[AgentTurn]) -> str:
    """The text of the last answer: the last turn that has text and no tool call, with the turns it goes on from put before it."""
    for i in range(len(turns) - 1, -1, -1):
        if not turns[i].tool_calls and turns[i].assistant_content:
            pieces = [turns[i]]
            while pieces[0].continues and i > 0 and not turns[i - 1].tool_calls:
                i -= 1
                pieces.insert(0, turns[i])
            if len(pieces) == 1:
                return pieces[0].assistant_content
            text = pieces[0].raw_content or pieces[0].assistant_content
            for piece in pieces[1:]:
                text = join_continuation(text, piece.raw_content or piece.assistant_content)
            return text.strip()
    return ""


class LoopDetector:
    """Detects cycles and infinite loops in tool execution: the same call again and again, and the same error from the same tool."""

    def __init__(self, max_consecutive_repeats: int = 2) -> None:
        self.max_repeats = max_consecutive_repeats
        self.history: List[str] = []
        self._last_error: Optional[str] = None
        self.error_repeats = 0                    # how many calls in a row ended with this same error

    def progress(self) -> None:
        """Something in the workspace changed: the same call again is no longer the same situation, and the failures counted so far are history."""
        self.history.clear()
        self._last_error, self.error_repeats = None, 0

    def check_and_record(self, tool_name: str, args: Any) -> bool:
        """Returns True if a forbidden loop is detected."""
        canonical = json.dumps({"tool": tool_name, "args": args}, sort_keys=True, default=str)
        self.history.append(hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        if len(self.history) > self.max_repeats:
            return len(set(self.history[-self.max_repeats - 1:])) == 1
        return False

    def record_result(self, tool_name: str, is_error: bool, message: str) -> int:
        """Notes how a call ended; returns how many calls in a row have failed with this tool and this message (0 if it did not fail)."""
        if not is_error:
            self._last_error, self.error_repeats = None, 0
            return 0
        key = hashlib.sha256(f"{tool_name}\x00{message}".encode("utf-8", "replace")).hexdigest()
        self.error_repeats = self.error_repeats + 1 if key == self._last_error else 1
        self._last_error = key
        return self.error_repeats


def _fence_untrusted(name: str, text: str) -> str:
    return fence_untrusted(name, text)


@dataclass
class _Completion:
    """What one call for the model's reply came to, with the usage of every attempt it took."""
    message: Dict[str, Any] = field(default_factory=dict)
    finish: str = ""
    ceiling: Optional[int] = None       # the most the reply could write, when it was set
    degenerate: bool = False            # the output was a repetition loop, also on the second try
    cut_call: bool = False              # the output limit ended a tool call inside its arguments, also after the retries
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    def add_usage(self, usage: Dict[str, Any]) -> None:
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.cached_tokens += int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)


class AgentLoop:
    def __init__(
        self,
        client: BasaltClient,
        tool_registry: ToolRegistry,
        max_turns: int = 15,
        system_prompt: str = "You are a helpful coding assistant equipped with tools.",
        on_turn_end: Optional[Callable[[AgentTurn], None]] = None,
        permissions: Optional[PermissionManager] = None,
        hooks: Optional[HookManager] = None,
        audit: Optional[AuditLedger] = None,
        checkpoints: Optional[CheckpointManager] = None,
        secrets: Optional[SecretsBroker] = None,
        output_filter: Optional[Callable[[str, str], str]] = None,
        max_total_tokens: Optional[int] = None,
        max_seconds: Optional[float] = None,
        cancel: Optional[threading.Event] = None,
        max_loop_strikes: int = 2,
        context: Optional[ContextManager] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        sampling: Optional[Dict[str, Any]] = None,
        on_delta: Optional[Callable[[str, str], None]] = None,
        before_inference: Optional[Callable[[], Optional[str]]] = None,
        batch_interrupt: Optional[Callable[[], str]] = None,
        pipeline: Optional[Pipeline] = None,
        clock: Optional[Callable[[], datetime]] = None,
        max_tokens: Optional[int] = None,
        gate: Optional[VerifyGate] = None,
        changes: Optional[Callable[[], int]] = None,
        budget_grace: bool = True,
        subagent: bool = False,
        role: Optional[Any] = None,
    ) -> None:
        self.client = client
        self.tools = tool_registry
        self.max_turns = max_turns
        self.system_prompt = system_prompt
        self.on_turn_end = on_turn_end
        self.permissions = permissions
        self.hooks = hooks
        self.audit = audit
        self.checkpoints = checkpoints
        self.secrets = secrets
        self.output_filter = output_filter
        self.max_total_tokens = max_total_tokens
        self.max_seconds = max_seconds
        self.cancel = cancel or threading.Event()
        self.max_loop_strikes = max_loop_strikes
        self.context = context
        self.on_event = on_event
        self.on_delta = on_delta                  # the model's text as it is written ("content" or "reasoning"), when set
        self.before_inference = before_inference  # looked at before every model call; it may add messages or ask to be put aside
        self.completion: Optional[Callable[[str, str, str], str]] = None      # "work until done": the note that sends the model back to finish, or "" (harness/completion.py)
        self.lessons_for: Optional[Callable[[str], str]] = None               # the lessons of earlier work that bear on a request, as a note, or "" (harness/lessons.py)
        self.learn: Optional[Callable[[Dict[str, Any], List[Dict[str, Any]]], None]] = None   # told how a request went and what it taught, when its run ends
        self._recorder: Any = None
        self.course: Any = None                   # the watch on the course of a request (harness/course.py): notes to the agent, then a card to the person
        self.mission: Dict[str, Any] = {}         # the request in hand and what the harness knows of it, for self_status
        self.batch_interrupt = batch_interrupt    # looked at before each call of a message's batch: a reason to leave the rest of the batch unrun, or ""
        self.pipeline = pipeline                  # checks the final answer against the request's card (harness/pipeline.py)
        self.clock = clock                        # when set: each request is preceded by the local time, and each message carries its time (`ts`)
        self._card: Optional[Card] = None
        self._retried = False
        self.sampling = dict(sampling or {})      # temperature, seed, top_p...; what is not set is the model file's own
        self.max_tokens = max_tokens              # the most one reply may write; None: the server's own limit
        self.gate = gate                          # the project's own checks, run before a turn ends after edits (harness/gate.py)
        self.changes = changes                    # how many file changes have been recorded: a change is progress, and what the gate looks at
        self.budget_grace = budget_grace          # a budget that runs out first warns, then allows one last report with no tools
        self.role = role                          # what the conversation is for (harness/roles.py): refuses the calls outside it, adds its note to each request
        self.subagent = subagent                  # a subagent has no gate, stop hook or last report of its own: its parent's turn has them
        self._edits_seen = changes() if changes else 0
        self.loop_detector = LoopDetector()
        self.outcome_guard = OutcomeGuard()       # loops seen in what calls bring back, not only in what they ask (harness/loop_guard.py)
        self.repeat_guard = RepeatGuard()         # a web read made twice in one request is not made again (harness/repeat_guard.py)
        self.messages: List[Dict[str, Any]] = [self._system_message()]
        self.turns: List[AgentTurn] = []
        self.stop_reason = ""
        self.error = ""
        self.error_status: Optional[int] = None       # the HTTP status of a failed model call (0: unreachable)
        self.total_tokens = 0
        self._started = False
        self._loop_strikes = 0
        self.last_user_text = ""                      # the words of the person's latest message
        self.untrusted_since_user = False             # content from outside came in after that message
        self.screen: Optional[Callable[[str, str], Optional[str]]] = None      # a second reader of untrusted text (harness/inject_screen.py): a notice or None
        self._end_turn = False
        self.length_stops = 0                         # replies the output limit ended, in the latest run
        self.cut = False                              # the answer delivered by the latest run was ended by the output limit
        self._restart_request()

    def _restart_request(self) -> None:
        """What a person's request is allowed once is allowed again."""
        self._continuations = 0
        self._empty_retried = False
        self._loop_retried = False
        self._ceiling = self.max_tokens               # raised for the rest of a request by a tool call that was cut
        self._effort_once = ""                        # thinking level for the next model call only
        self._continuing = False                      # the next reply goes on from a cut one
        self._invalid_strike = False
        self._stop_denials = 0                        # times a stop hook sent the model back to work
        self._todo_nudged = False
        self._completion_rounds = 0
        if self.gate is not None:
            self.gate.restart()

    def fork(self) -> "AgentLoop":
        """Another conversation with the same tools, permissions, audit and model but its own messages: for a request that has
        nothing to do with this one."""
        return AgentLoop(
            client=self.client, tool_registry=self.tools, max_turns=self.max_turns, system_prompt=self.system_prompt,
            on_turn_end=self.on_turn_end, permissions=self.permissions, hooks=self.hooks, audit=self.audit, checkpoints=self.checkpoints,
            secrets=self.secrets, output_filter=self.output_filter, max_total_tokens=self.max_total_tokens, max_seconds=self.max_seconds,
            cancel=threading.Event(), max_loop_strikes=self.max_loop_strikes, context=self.context, on_event=self.on_event,
            sampling=self.sampling, on_delta=self.on_delta, pipeline=self.pipeline, clock=self.clock, max_tokens=self.max_tokens,
            gate=self.gate.clone() if self.gate is not None else None, changes=self.changes, budget_grace=self.budget_grace,
            role=self.role)

    def append(self, message: Dict[str, Any]) -> None:
        """Puts a message at the end of the conversation; with a clock it carries the time it was made, which never goes to the server."""
        if self.clock is not None and "ts" not in message:
            message["ts"] = self.clock().timestamp()
        self.messages.append(message)

    def add_user_message(self, text: str, origin: str = "user", steer: bool = False, shown: Optional[str] = None) -> None:
        """Puts a message at the end of the conversation. From the person it is also what their next confirmation may quote and
        the request the answer is checked against (a steering message adds to the constraints already found); a note from the
        planner is neither: nothing in it can stand for the person's words. With a clock, the local time goes in first, as a note of
        the harness's: it is the one thing in the conversation that changes with every request, so it comes after everything that is kept."""
        if self.clock is not None:
            self.append({"role": "user", "content": local_time_line(self.clock()), "note": True})
        if origin != "user":
            self.append({"role": "user", "content": text})
            self.last_user_text = ""
            self.taint(f"{origin} note")                                       # text made from an event is no one's instruction
            return
        self.loop_detector = LoopDetector()                              # a request of the person starts the count again: asking for the same thing twice is not a loop
        self.outcome_guard.restart()
        self._restart_request()
        self.repeat_guard.restart(len(self.turns))
        rewrite = getattr(self.context, "rewrite", None)
        words = shown if shown is not None else text                     # `shown`: the person's own words, when `text` also holds their files
        self.append({"role": "user", "content": rewrite(text) if rewrite and not steer and shown is None else text})
        self.last_user_text, self.untrusted_since_user = words, False
        if not steer:
            self.mission = {"request": words[:300], "completion_rounds": 0}
            self._begin_learning(words)
            if self.course is not None:
                self.course.restart()
        if self.pipeline:
            card = self.pipeline.card(words)
            if steer and self._card is not None:
                have = {(c.kind, str(c.value)) for c in self._card.constraints}
                card.constraints = self._card.constraints + [c for c in card.constraints if (c.kind, str(c.value)) not in have]
            self._card, self._retried = card, False

    def _course_step(self, step: int) -> bool:
        """Before a model call: what the watch of the course wants done. A note goes to the agent; a card goes to the person, and True comes back when the
        turn must end there to wait for their answer."""
        action = self.course.check(self, step)
        if action is None:
            return False
        self._audit("course_check", {"kind": action.kind, "stage": action.stage, "problem": action.problem[:200]})
        if action.stage == "card":
            ask = self.tools.get("ask_user")
            if ask is not None:
                self._emit({"type": "course_check", "kind": action.kind, "stage": "card", "problem": action.problem[:200]})
                said = ask.handler(**action.card())
                if self._end_turn:
                    self._end_turn, self.stop_reason = False, ASKED
                    return True
                self.append({"role": "user", "content": "[harness] Course check: " + str(said), "note": True})
                return False
        self._emit({"type": "course_check", "kind": action.kind, "stage": "note", "problem": action.problem[:200]})
        self.append({"role": "user", "content": action.note, "note": True})
        return False

    def _begin_learning(self, request: str) -> None:
        """A request of the person starts: what earlier work taught about it is added after it, and what happens is recorded to teach the next one."""
        if self.subagent:
            return
        if self.lessons_for is not None:
            try:
                block = self.lessons_for(request)
            except Exception:
                block = ""
            if block:
                self.append({"role": "user", "content": block, "note": True})
                self.mission["lessons"] = block.count(chr(10))
        if self.learn is not None:
            from harness.lessons import Recorder
            self._recorder = Recorder(mask=self.secrets.mask_secrets if self.secrets is not None else None)
            self._learn_from = request
            self._learn_started = time.monotonic()

    def _end_learning(self) -> None:
        recorder, self._recorder = self._recorder, None
        if recorder is None or self.learn is None:
            return
        try:
            from harness.lessons import result_for
            record, candidates = recorder.finish(result_for(self.stop_reason, cancelled_by_person=self.cancel.is_set()), self._learn_from, steps=len(self.turns),
                                                 tokens=self.total_tokens, seconds=round(time.monotonic() - self._learn_started, 1), loop_strikes=self._loop_strikes)
            self.learn(record, candidates)
        except Exception:
            pass                                              # learning never gets in the way of the answer

    def taint(self, source: str, private: bool = False) -> None:
        """Content from outside the person's control came into the conversation (`private`: it is also the person's own data): the Rule of Two
        counts it, and nothing the person said earlier stands for what follows."""
        self.untrusted_since_user = True
        if self.permissions:
            self.permissions.note_untrusted_input(source)
            if private:
                self.permissions.note_private_data(source)

    def request_end_turn(self) -> None:
        """For a tool: end this run when the tools of the current model turn are done (a question was put to the person)."""
        self._end_turn = True

    def _emit(self, event: Dict[str, Any]) -> None:
        """Tells an observer what is happening (assistant text, tool start and end); it can never affect the run."""
        if self._recorder is not None:
            self._recorder.observe(event)
        if self.course is not None:
            self.course.observe(event)
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass

    def _system_message(self) -> Dict[str, Any]:
        return {"role": "system", "content": self.system_prompt, "pinned": True, "pin_category": "system_core"}

    # -- parsing ---------------------------------------------------------------------------------

    def _decode_parameters(self, name: str, body: str) -> Dict[str, Any]:
        tool = self.tools.get(name)
        props = (tool.parameters.get("properties") if tool else None) or {}
        args: Dict[str, Any] = {}
        for key, raw in PARAMETER_REGEX.findall(body):
            if (props.get(key) or {}).get("type") == "string":
                args[key] = raw
            else:
                try:
                    args[key] = json.loads(raw)
                except json.JSONDecodeError:
                    args[key] = raw
        return args

    def _read_arguments(self, name: str, raw: str, repair: bool) -> "tuple[Any, str, bool]":
        """(arguments, error, repaired) for arguments written as JSON text. Text that is not valid is mended when the mending is not in doubt
        (harness/tools/repair.py); a string that holds the JSON of an object is read as that object."""
        try:
            value: Any = json.loads(raw or "{}")
            fixed = False
        except json.JSONDecodeError as exc:
            value = repair_json(raw) if repair else None
            if not isinstance(value, dict):
                return raw, (f"The arguments of '{name}' are not valid JSON ({exc.msg} at position {exc.pos}). "
                             "Call the tool again with a valid JSON object."), False
            fixed = True
        if isinstance(value, str):
            try:
                inner = json.loads(value)
            except ValueError:
                inner = None
            if isinstance(inner, dict):
                value, fixed = inner, True
        return value, "", fixed

    def _fix_name(self, name: str) -> "tuple[str, bool]":
        """The registered tool a written name stands for, when it is not in doubt; else the name as it was."""
        if not isinstance(name, str) or self.tools.get(name) is not None:
            return name, False
        found, _ = self.tools.resolve(name)
        return (found, True) if found else (name, False)

    def parse_calls(self, message: Dict[str, Any], content: str, repair: bool = True) -> List[ParsedCall]:
        """Tool calls of one assistant message: the structured field if present, else tags in `content`.

        `content` is the visible text without reasoning, so a call quoted inside <think> never runs. A name or arguments that can be mended
        without a guess are (the call is marked `repaired`); with `repair` off nothing is."""
        calls: List[ParsedCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name, renamed = self._fix_name(fn.get("name") or "") if repair else (fn.get("name") or "", False)
            raw_args = fn.get("arguments", "{}")
            call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
            if isinstance(raw_args, str):
                args, error, fixed = self._read_arguments(name, raw_args, repair)
                if error:
                    calls.append(ParsedCall(call_id, name, args, error))
                    continue
            else:
                args, fixed = raw_args, False
            calls.append(ParsedCall(call_id, name, args, repaired=renamed or fixed))
        if calls:
            return calls

        for body in TOOL_CALL_REGEX.findall(content):
            call_id = f"call_{uuid.uuid4().hex[:12]}"
            fn = FUNCTION_REGEX.search(body)
            if fn:
                name, renamed = self._fix_name(fn.group(1)) if repair else (fn.group(1), False)
                calls.append(ParsedCall(call_id, name, self._decode_parameters(name, fn.group(2)), repaired=renamed))
                continue
            try:
                parsed = json.loads(body)
                fixed = False
            except json.JSONDecodeError as exc:
                parsed = repair_json(body) if repair else None
                fixed = isinstance(parsed, dict)
                if not fixed:
                    calls.append(ParsedCall(call_id, "", body, f"A tool call was not valid JSON ({exc.msg}). Use "
                                            '{"name": ..., "arguments": {...}}.'))
                    continue
            if not isinstance(parsed, dict) or not parsed.get("name"):
                calls.append(ParsedCall(call_id, "", body, 'A tool call needs a "name".'))
                continue
            name, renamed = self._fix_name(parsed["name"]) if repair else (parsed["name"], False)
            arguments = parsed.get("arguments", {})
            if isinstance(arguments, str) and repair:
                arguments, error, again = self._read_arguments(name, arguments, repair)
                if error:
                    calls.append(ParsedCall(call_id, name, arguments, error))
                    continue
                fixed = fixed or again
            calls.append(ParsedCall(call_id, name, arguments, repaired=renamed or fixed))
        if not calls and "<tool_call>" in content:        # opened and never closed: the answer was cut off, it is not a final answer
            calls.append(ParsedCall(f"call_{uuid.uuid4().hex[:12]}", "", content.partition("<tool_call>")[2],
                                    "A tool call was cut off: it has no closing </tool_call>. Write the whole call again."))
        return calls

    def parse_tool_calls(self, message: Dict[str, Any]) -> List[Any]:
        """(name, arguments) pairs for one message; kept for callers that only need the calls."""
        _, content = extract_reasoning_and_content(message)
        return [(c.name, c.arguments) for c in self.parse_calls(message, content) if not c.error]

    # -- one guarded tool call ------------------------------------------------------------------

    def _audit(self, action: str, details: Dict[str, Any]) -> None:
        if self.audit:
            self.audit.record("agent", action, details)

    def _audit_call(self, action: str, name: str, args: Any, permission: str, outcome: str, started: float,
                    reason: str = "", effect: str = "", repaired: bool = False) -> None:
        """One record about one tool call: the arguments only as a hash and a short masked preview (see AuditLedger.call_details)."""
        if self.audit:
            self.audit.record("agent", action, self.audit.call_details(
                name, args, permission=permission, outcome=outcome, duration_s=time.monotonic() - started, effect=effect, reason=reason,
                repaired=repaired))

    def _stringify(self, name: str, output: Any) -> str:
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
        if self.secrets:
            text = self.secrets.mask_secrets(text)
        if self.output_filter:
            text = self.output_filter(name, text)
        return text

    def call_tool(self, name: str, arguments: Dict[str, Any], checkpointed: Optional[List[Any]] = None) -> "tuple[bool, str]":
        """A tool call made for the person by something other than the model, such as a personal skill's script. It goes through the same
        gate as the model's own (arguments checked, hooks, permissions, a checkpoint, the audit record), is shown and kept in the
        conversation the same way, and is not taken for a loop when it repeats. Returns whether it worked and its answer as text."""
        call = ParsedCall("call_" + uuid.uuid4().hex[:10], name, arguments)
        tool = self.tools.get(name)
        self.append({"role": "assistant", "content": "", "tool_calls": [{"id": call.call_id, "type": "function",
                                                                       "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]})
        self._emit({"type": "tool_start", "call_id": call.call_id, "name": name, "arguments": arguments, "effect": tool.effect.value if tool else None})
        result = self._execute(call, checkpointed if checkpointed is not None else [False], loop_check=False)
        text = self._stringify(name, result.output)
        worked = result.decision == "executed" and not result.is_error
        if worked:
            text = self._screened(name, text, tool)
        if result.feedback:
            text += "\n[harness] " + result.feedback
        self._emit({"type": "tool_end", "call_id": call.call_id, "name": name, "status": "completed" if worked else "failed",
                    "decision": result.decision, "output": text})
        self.append({"role": "tool", "tool_call_id": call.call_id, "name": name, "content": text})
        return worked, text

    def _screened(self, name: str, text: str, tool: Any) -> str:
        """A tool's answer as the model gets it. Content from outside is fenced as data; the answer of a tool that reads files or runs code is
        only looked at. Text that speaks to an AI is marked, and the permission manager asks the person before anything goes on that changes or sends."""
        outside = tool is not None and tool.untrusted
        if not outside and name not in CONTENT_TOOLS:
            return text
        if outside:
            notice = self.screen(name, text) if self.screen else (inject_screen.NOTICE if inject_screen.detect(text) else None)
            self.untrusted_since_user = True
        else:
            notice = inject_screen.NOTICE if inject_screen.detect(text, strict=True) else None
        if notice and self.permissions:
            self.permissions.note_instructions(name)
        return (_fence_untrusted(name, text) if outside else text) + (chr(10) + notice if notice else "")

    def _execute(self, call: ParsedCall, checkpointed: List[Any], loop_check: bool = True) -> ToolExecutionResult:
        """One guarded call. The model's sentence for the person is taken off the arguments first: what is checked, counted, recorded
        and run never sees it, and it never changes a decision; it is kept on the result for display."""
        args, description = self.tools.split_description(call.name, call.arguments)
        _call_note.text = description
        try:
            result = (loop_check and self._repeated(call, args)) or self._guarded(call, args, checkpointed, loop_check)
        finally:
            _call_note.text = ""
        result.description = description
        if loop_check and result.decision == "executed" and not result.is_error:
            self.repeat_guard.remember(call.name, args, call.call_id, result.output, len(self.turns))
        return result

    def _repeated(self, call: ParsedCall, args: Any) -> Optional[ToolExecutionResult]:
        """A web read already made with these arguments in this request is not run again: the result says where its text is. It is recorded
        as refused, and it is neither a loop strike nor a call that went through the permission gate."""
        if call.error:
            return None
        said = self.repeat_guard.again(call.name, args, self.messages)
        if said is None:
            return None
        self._audit_call("tool_repeat", call.name, args, "not_required" if self.permissions is None else "not_reached", "refused", time.monotonic(), reason=said)
        return ToolExecutionResult(call.name, args if isinstance(args, dict) else {}, said, False, call.call_id, "skipped")

    def _guarded(self, call: ParsedCall, args: Any, checkpointed: List[Any], loop_check: bool) -> ToolExecutionResult:
        name = call.name
        started = time.monotonic()
        permission = "not_required" if self.permissions is None else "not_reached"
        repaired, written = call.repaired, args

        def refused(decision: str, message: str, audit_action: str) -> ToolExecutionResult:
            self._audit_call(audit_action, name, args, permission, "refused", started, reason=message, repaired=repaired)
            return ToolExecutionResult(name, args if isinstance(args, dict) else {}, message, True, call.call_id, decision, repaired=repaired, written=written)

        if call.error:
            return refused("invalid", call.error, "tool_invalid")
        tool = self.tools.get(name)
        if tool is None:
            _, near = self.tools.resolve(name)
            hint = f"Did you mean {', '.join(near)}?" if near else "No tool has a similar name; use only the tools in your tool list."
            return refused("invalid", f"Unknown tool '{name}'. {hint}", "tool_invalid")
        args, coerced = self.tools.coerce(name, args)
        repaired = repaired or coerced
        if self.role is not None:
            why = self.role.check(name, args)
            if why:
                return refused("blocked", why, "role_denied")
        try:
            self.tools.validate(name, args)
        except ToolArgumentError as exc:
            return refused("invalid", f"{exc} Call the tool again with corrected arguments.", "tool_invalid")

        if loop_check and self.loop_detector.check_and_record(name, args):
            self._loop_strikes += 1
            return refused("loop", f"Loop detected: '{name}' was called repeatedly with identical arguments. "
                                   "Stop repeating it and change your approach.", "tool_loop")

        if tool.outward and self.permissions:
            two = self.permissions.two
            held = two.leak(args, self.secrets) if two.level != "off" else ""
            if held:
                allowed, reason = self.permissions.ask_leak(name, args, two.hide(args, self.secrets), leak_question(name, held, args))
                if not allowed:
                    permission = "denied"
                    return refused("denied", f"Permission denied: {reason}", "permission_denied")
        if self.hooks:
            blocked = self.hooks.trigger_pre_tool(name, args)
            if isinstance(blocked, AskPerson):
                if not (self.permissions and self.permissions.ask_by_hook(name, args, str(blocked))):
                    return refused("blocked", f"Not run: a hook of the person's asks before this call and it was not allowed ({blocked})", "tool_blocked")
            elif blocked:
                return refused("blocked", blocked, "tool_blocked")
        if self.permissions:
            allowed, reason = self.permissions.evaluate(name, tool.effect, args, outward=tool.outward)
            permission = "allowed" if allowed else "denied"
            if not allowed:
                return refused("denied", f"Permission denied: {reason}", "permission_denied")
            self._audit_call("permission_granted", name, args, permission, "started", started, reason=reason, effect=tool.effect.value, repaired=repaired)
        cp_note = ""
        mode = self.checkpoints.effective_mode(self.permissions.level.value if self.permissions else "default") if self.checkpoints else "off"
        if mode != "off" and tool.effect != EffectClass.PURE:
            try:
                paths = call_paths(self.checkpoints.workspace, name, args)
                if isinstance(checkpointed[0], str):
                    self.checkpoints.add(checkpointed[0], paths)
                else:
                    checkpointed[0] = self.checkpoints.create(f"before {name}", paths)
                    self._audit("checkpoint", {"id": checkpointed[0]})
            except Exception as exc:
                if mode == "on":
                    return refused("blocked", f"No checkpoint could be taken ({exc}); the action was not run.", "tool_blocked")
                self._audit("checkpoint_failed", {"tool": name, "error": str(exc)})
                cp_note = f"No checkpoint could be taken for this action ({exc}); it ran without one."

        ran = time.monotonic()
        if tool.outward:
            outward_calls.note(name, args)
        try:
            with cancel_scope(self.cancel) as scope:             # Stop also ends a command that is running
                output = self.tools.execute(name, args)
            is_error = False
        except Exception as exc:
            output, is_error = f"Tool execution error: {type(exc).__name__}: {exc}", True
        cancelled = scope.cancelled or (is_error and self.cancel.is_set())
        if cp_note:
            output = f"{output}\n\n[{cp_note}]" if isinstance(output, str) else {**output, "checkpoint": cp_note} if isinstance(output, dict) else output
        if self.permissions:
            self.permissions.two.observe(name, args, output, untrusted=tool.untrusted, secrets=self.secrets)
        feedback = (self.hooks.trigger_post_tool(name, args, output) or "") if self.hooks else ""
        self._audit_call("tool_cancelled" if cancelled else "tool_error" if is_error else "tool_executed", name, args, permission,
                         "cancelled" if cancelled else "error" if is_error else "ok", ran, effect=tool.effect.value,
                         reason=str(output) if is_error else "", repaired=repaired)
        return ToolExecutionResult(name, args, output, is_error, call.call_id, "executed", repaired=repaired, written=written, feedback=feedback)

    # -- main loop -------------------------------------------------------------------------------

    def _out_of_budget(self, started: float) -> bool:
        if self.max_total_tokens is not None and self.total_tokens >= self.max_total_tokens:
            return True
        return self.max_seconds is not None and time.monotonic() - started >= self.max_seconds

    def reset(self) -> None:
        """Forgets the conversation (the permission and audit state stay)."""
        self.messages = [self._system_message()]
        self.turns = []
        self.loop_detector = LoopDetector()
        self.outcome_guard.restart()
        self.repeat_guard.restart(0)
        self.total_tokens = 0
        self._restart_request()

    def close(self) -> None:
        if self.hooks and self._started:
            self.hooks.trigger_event("session_end", {"turns": len(self.turns), "stop_reason": self.stop_reason,
                                                     "permission_denials": self.permissions.denied if self.permissions else 0})
        self._started = False

    def run(self, user_goal: Optional[str], retrieved: Optional[Dict[str, Any]] = None) -> List[AgentTurn]:
        """Runs until the model answers without tool calls, a budget ends, it is cancelled or a loop persists.

        The conversation continues across calls; `stop_reason` and `error` tell why this call ended. With no goal
        nothing is added to the conversation: it goes on from where it stands, as after a failed model call."""
        started = time.monotonic()
        self.stop_reason, self.error, self.error_status, self._loop_strikes = "", "", None, 0
        self.length_stops, self.cut, self._invalid_strike = 0, False, False
        warned = False
        if not self._started:
            self._started = True
            if self.hooks:
                self.hooks.trigger_event("session_start", {"goal": user_goal})
        if user_goal is not None:
            self.add_user_message(user_goal)
        if retrieved:
            self.append(retrieved)     # memories and wiki text go after the goal, at the tail
            self.taint("memories", private=bool(retrieved.get("private")))
        run_turns: List[AgentTurn] = []

        turn_idx = 0
        grace = ""                                                            # the budget that ran out, while its last report is being written
        while True:
            turn_idx += 1
            over = MAX_TURNS if 0 < (self.max_turns or 0) < turn_idx else ""    # below 1: no limit on the steps; the other guards below stay
            if not over:
                if self.cancel.is_set():
                    self.stop_reason = CANCELLED
                    break
                if self._out_of_budget(started):
                    over = BUDGET
            if over:
                if grace or not self.budget_grace or self.subagent or self.cancel.is_set():
                    self.stop_reason = over
                    break
                grace = over                                                  # one more call, with no tools, to say where things stand
                self.append({"role": "user", "content": GRACE_NOTE, "note": True})
                self._emit({"type": "budget_grace", "reason": over})
                self._audit("budget_grace", {"reason": over, "turns": len(self.turns)})
            elif not warned and self.budget_grace and not self.subagent:
                warned = self._budget_warning(started, turn_idx)

            if self.before_inference is not None and self.before_inference() == SUSPENDED:
                self.stop_reason = SUSPENDED          # between model calls and tools: nothing is half done
                break

            if self.course is not None and not self.subagent and not grace and self._course_step(turn_idx):
                break

            t0 = time.monotonic()
            try:
                if self.context:
                    self._prepare_context()
                done = self._complete()
                self._note_usage(done)
            except StreamCancelled:                     # the person stopped the answer while it was being written
                self.stop_reason = CANCELLED
                break
            except (BasaltError, KeyError, IndexError, TypeError) as exc:
                self.stop_reason, self.error = ERROR, str(exc) or type(exc).__name__
                self.error_status = exc.status if isinstance(exc, BasaltError) else None
                break
            msg, finish = done.message, done.finish
            reasoning, content = extract_reasoning_and_content(msg)
            turn = AgentTurn(
                turn_index=turn_idx,
                user_input=(user_goal or "") if turn_idx == 1 else "",
                reasoning=reasoning,
                assistant_content=content,
                prompt_tokens=done.prompt_tokens,
                completion_tokens=done.completion_tokens,
                cached_tokens=done.cached_tokens,
                finish_reason=finish,
                continues=self._continuing,
                raw_content=msg.get("content") or "",
            )
            self._continuing = False
            self.total_tokens += turn.prompt_tokens + turn.completion_tokens

            if done.degenerate:                          # a loop, twice: what was written is not kept
                self.stop_reason, self.error = DEGENERATE, DEGENERATE_NOTICE
                self._emit({"type": "answer_degenerate"})
                self._audit("output_degenerate", {"final": True})
                self._finish_turn(turn, run_turns)
                break
            if done.cut_call:                            # a tool call ended inside its arguments, at every size tried: nothing of it runs
                self.stop_reason, self.error = LENGTH, LENGTH_NOTICE.format(tokens=done.ceiling)
                self._emit({"type": "tool_call_cut", "final": True, "max_tokens": done.ceiling})
                self._audit("tool_call_cut", {"final": True, "max_tokens": done.ceiling})
                self._finish_turn(turn, run_turns)
                break

            self._emit({"type": "assistant", "reasoning": reasoning, "content": content, **({"finish_reason": finish} if finish == "length" else {})})
            calls = self.parse_calls(msg, content)
            if grace:                                    # the last report: what it says is kept, any call in it is not run
                if content:
                    self.append({"role": "assistant", "content": content})
                turn.seconds = round(time.monotonic() - t0, 3)
                self._finish_turn(turn, run_turns)
                self.stop_reason = grace
                break
            if not calls:
                if not content and (reasoning or finish == "length"):
                    if self._answer_missing(turn, run_turns, t0, finish):
                        continue
                    break
                if finish == "length" and content:
                    if self._answer_cut(turn, run_turns, t0, content):
                        continue
                    self.stop_reason = FINAL
                    break
                self.append({"role": "assistant", "content": content})
                turn.seconds = round(time.monotonic() - t0, 3)
                self._finish_turn(turn, run_turns)
                if self._check_answer(answer_text(self.turns) if turn.continues else content):
                    continue                         # one more try, with what was not respected
                if self._before_end(answer_text(self.turns) if turn.continues else content):
                    continue                         # the project's checks, the checklist or a stop hook sent the model back to work
                self.stop_reason = self.stop_reason or FINAL
                break

            self.append({
                "role": "assistant",
                "content": content,
                "tool_calls": [{
                    "id": c.call_id, "type": "function",
                    "function": {"name": c.name,
                                 "arguments": c.arguments if isinstance(c.arguments, str) else json.dumps(c.arguments, ensure_ascii=False)},
                } for c in calls],
            })
            checkpointed = [False]
            seen: Dict[str, str] = {}                 # the calls of this message already run: what they were, and their id
            ran = repeated = surplus = interrupted_n = 0
            interrupted = ""
            for call in calls:
                if self.cancel.is_set():
                    self.append({"role": "tool", "tool_call_id": call.call_id, "name": call.name,
                                 "content": "Cancelled before it ran."})
                    self._audit_call("tool_cancelled", call.name, self.tools.split_description(call.name, call.arguments)[0], "not_reached", "cancelled", time.monotonic(),
                                     reason="cancelled before it ran")
                    continue
                shown_args, description = self.tools.split_description(call.name, call.arguments)
                same = self._call_key(call, shown_args)
                skip = ""
                if ran and not interrupted and self.batch_interrupt is not None:
                    interrupted = self.batch_interrupt() or ""
                if interrupted:
                    skip = interrupted
                    interrupted_n += 1
                elif same and same in seen:
                    skip, repeated = f"Not run again: this repeats call {seen[same]} above (same tool, same arguments).", repeated + 1
                elif ran >= MAX_CALLS_PER_MESSAGE:
                    skip, surplus = f"Not run: more than {MAX_CALLS_PER_MESSAGE} calls in one message.", surplus + 1
                if skip:                              # the protocol needs one result per call id; it is short and nothing ran
                    turn.tool_calls.append(ToolExecutionResult(call.name, shown_args if isinstance(shown_args, dict) else {}, skip, True,
                                                               call.call_id, "skipped", description))
                    self.append({"role": "tool", "tool_call_id": call.call_id, "name": call.name, "content": skip, "skipped": True})
                    continue
                ran += 1
                if same:
                    seen[same] = call.call_id
                tool = self.tools.get(call.name)
                self._emit({"type": "tool_start", "call_id": call.call_id, "name": call.name,
                            "arguments": shown_args if isinstance(shown_args, dict) else {},
                            "effect": tool.effect.value if tool else None, **({"description": description} if description else {})})
                result = self._execute(call, checkpointed)
                turn.tool_calls.append(result)
                text = result_text = self._stringify(call.name, result.output)
                if result.decision != "loop":
                    text += self._same_error_note(call.name, result, str(result.output))
                seen_by_guard = self._observe(call, shown_args, result, tool, result_text)       # the result as it is, without the notes added to it
                if seen_by_guard.stub:
                    text = seen_by_guard.stub
                if result.decision == "executed" and not result.is_error:
                    text = self._screened(call.name, text, tool)
                if seen_by_guard.note:
                    text += "\n" + seen_by_guard.note
                if result.feedback:
                    text += "\n[harness] " + result.feedback
                self._emit({"type": "tool_end", "call_id": call.call_id, "name": call.name,
                            "status": "completed" if result.decision == "executed" and not result.is_error else "failed",
                            "decision": result.decision, "output": text})
                self.append({"role": "tool", "tool_call_id": call.call_id, "name": call.name, "content": text,
                             **({"explain": description} if description else {})})
            if interrupted_n:                         # the person wrote something that changes or cancels the work: said once, the rest of the batch was not run
                self._emit({"type": "batch_interrupted", "skipped": interrupted_n})
                self._audit("batch_interrupted", {"skipped": interrupted_n, "ran": ran})
            if repeated or surplus:                   # one strike for the message, and the model is told once
                self._loop_strikes += 1
                note = self._skipped_note(repeated, surplus)
                for m in reversed(self.messages):
                    if m.get("role") == "tool":
                        m["content"] += note
                        break
                self._emit({"type": "calls_skipped", "repeated": repeated, "surplus": surplus})
                self._audit("calls_skipped", {"repeated": repeated, "surplus": surplus, "ran": ran})

            news = getattr(self.tools, "jobs", None) and self.tools.jobs.news()
            if news:                                  # a background job ended: the model reads it with the results it is about to get
                for m in reversed(self.messages):
                    if m.get("role") == "tool":
                        m["content"] += chr(10) + news
                        break
            if self._invalid_strike:                  # a call that could not be read counts against the model only when none of the turn's calls could
                self._invalid_strike = False
                if all(r.decision == "invalid" for r in turn.tool_calls):
                    self._loop_strikes += 1
            turn.seconds = round(time.monotonic() - t0, 3)
            self._finish_turn(turn, run_turns)
            if self._end_turn:                       # a tool asked the person something: nothing goes on until they answer
                self._end_turn, self.stop_reason = False, ASKED
                break
            if self._loop_strikes > self.max_loop_strikes:
                self.stop_reason = LOOP
                break
        self._end_learning()
        return run_turns

    @staticmethod
    def _call_key(call: ParsedCall, args: Any) -> str:
        """What makes two calls in one message the same call: the tool and its arguments, not the sentence for the person. Empty for a call that could not be read."""
        if call.error or not isinstance(args, dict):
            return ""
        return json.dumps({"tool": call.name, "args": args}, sort_keys=True, default=str)

    @staticmethod
    def _skipped_note(repeated: int, surplus: int) -> str:
        """Said to the model once, after the results of a message that had calls repeated or beyond the limit."""
        parts = []
        if repeated:
            parts.append(f"{repeated} call(s) repeated an earlier call of the same message and were not run again")
        if surplus:
            parts.append(f"{surplus} call(s) beyond the {MAX_CALLS_PER_MESSAGE} allowed in one message were not run")
        return f"\n[harness] In this message {' and '.join(parts)}. Send each call once, and no more than {MAX_CALLS_PER_MESSAGE} at a time; make the ones that were not run in a later message if you still need them."

    def _same_error_note(self, name: str, result: ToolExecutionResult, message: str) -> str:
        """Said to the model when a tool has failed with the same error twice in a row; from the third time it counts as a loop strike."""
        repeats = self.loop_detector.record_result(name, result.is_error, message)
        if repeats < 2:
            return ""
        if repeats >= 3:
            if result.decision == "invalid":
                self._invalid_strike = True
            else:
                self._loop_strikes += 1
        return (f"\n[harness] '{name}' has now failed {repeats} times in a row with this same error. Repeating it will not help: "
                "read the error, then change what you ask or take another way.")

    # -- one model call, and what its ending means -----------------------------------------------

    def _ask(self, ceiling: Optional[int], extra: Dict[str, Any], watch: Optional[RepetitionWatch]) -> Dict[str, Any]:
        """One request to the model for the conversation as it stands."""
        temperature = extra.pop("temperature", None)
        kwargs: Dict[str, Any] = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if ceiling:
            kwargs["max_tokens"] = ceiling
        if extra:
            kwargs["extra"] = extra
        if self.on_delta:
            shown, guard = self.on_delta, watch

            def on_delta(kind: str, text: str) -> None:
                shown(kind, text)
                if guard is not None and guard.feed(kind, text):
                    raise RepetitionAbort(guard.kind)
            kwargs["on_delta"] = on_delta
        messages = wire_messages(self.messages)
        note = self.role.note() if self.role is not None else ""
        if note:
            messages.append({"role": "user", "content": note})          # the role's note ends the request and is not kept, so the start of the prompt never changes
        return self.client.chat_completion(messages=messages, tools=self.tools.schemas(), **kwargs)

    def _complete(self) -> "_Completion":
        """The model's reply for the conversation as it stands. Two failures are put right without touching the conversation: a stream
        that fell into a repetition loop (asked again once per request, with a higher repetition penalty) and a tool call that the
        output limit ended inside its arguments (asked again up to MAX_CUT_RETRIES times, each time with twice the room). The usage of
        every attempt is added up."""
        extra = dict(self.sampling)
        if self._effort_once:
            extra["reasoning_effort"], self._effort_once = self._effort_once, ""
        ceiling, cut_tries = self._ceiling, 0
        done = _Completion()
        while True:
            watch = RepetitionWatch() if self.on_delta else None
            try:
                resp = self._ask(ceiling, dict(extra), watch)
            except RepetitionAbort:
                if self._loop_retried:
                    done.degenerate = True
                    return done
                self._restart_after_loop(extra, "stream")
                continue
            choice = resp["choices"][0]
            msg, finish = choice["message"], choice.get("finish_reason") or ""
            done.add_usage(resp.get("usage") or {})
            if finish == "length":
                self.length_stops += 1
                if self._cut_in_call(resp, msg):
                    if cut_tries >= MAX_CUT_RETRIES:
                        done.cut_call, done.ceiling = True, ceiling
                        return done
                    cut_tries += 1
                    ceiling = min(RETRY_TOKENS_MAX, 2 * max(ceiling or 0, int((resp.get("usage") or {}).get("completion_tokens") or 0), RETRY_TOKENS // 2))
                    self._ceiling = ceiling
                    self._emit({"type": "tool_call_cut", "final": False, "attempt": cut_tries, "max_tokens": ceiling})
                    self._audit("tool_call_cut", {"final": False, "attempt": cut_tries, "max_tokens": ceiling})
                    continue
                _, text = extract_reasoning_and_content(msg)
                if text and not msg.get("tool_calls") and repetition_dominated(text):
                    if self._loop_retried:
                        done.degenerate = True
                        return done
                    self._restart_after_loop(extra, "reply")
                    continue
            done.message, done.finish, done.ceiling = msg, finish, ceiling
            return done

    def _restart_after_loop(self, extra: Dict[str, Any], where: str) -> None:
        self._loop_retried = True
        extra["repetition_penalty"] = raised_penalty(extra.get("repetition_penalty"))
        self._emit({"type": "stream_restart", "reason": "repetition"})
        self._audit("output_degenerate", {"final": False, "in": where, "repetition_penalty": extra["repetition_penalty"]})

    @staticmethod
    def _cut_in_call(resp: Dict[str, Any], msg: Dict[str, Any]) -> bool:
        """Whether a reply that the output limit ended was ended inside a tool call: the client left one out because it was unfinished, a
        call's arguments are not complete JSON, or a <tool_call> tag was never closed."""
        if resp.get("cut_calls"):
            return True
        for tc in msg.get("tool_calls") or []:
            raw = (tc.get("function") or {}).get("arguments")
            if isinstance(raw, str):
                try:
                    json.loads(raw or "{}")
                except ValueError:
                    return True
        _, text = extract_reasoning_and_content(msg)
        return text.count("<tool_call>") > text.count("</tool_call>")

    def _answer_missing(self, turn: AgentTurn, run_turns: List[AgentTurn], t0: float, finish: str) -> bool:
        """The reply has no text although the model thought (or the output limit ended it): the thinking used the output. Asked again once
        per request, at a lower thinking level and with a note to answer shortly; True when the run goes on. Otherwise the run ends as
        `empty`, with a notice in `error`."""
        turn.seconds = round(time.monotonic() - t0, 3)
        if not self._empty_retried and not self.cancel.is_set():
            self._empty_retried = True
            self._effort_once = LOWER_EFFORT.get(str(self.sampling.get("reasoning_effort") or ""), "low")
            self.append({"role": "user", "content": SHORT_ANSWER_NOTE, "note": True})
            self._emit({"type": "answer_empty", "final": False, "effort": self._effort_once})
            self._audit("answer_empty", {"final": False, "finish_reason": finish, "effort": self._effort_once})
            self._finish_turn(turn, run_turns)
            return True
        self.stop_reason, self.error = EMPTY, EMPTY_NOTICE
        self._emit({"type": "answer_empty", "final": True})
        self._audit("answer_empty", {"final": True, "finish_reason": finish})
        self._finish_turn(turn, run_turns)
        return False

    def _answer_cut(self, turn: AgentTurn, run_turns: List[AgentTurn], t0: float, content: str) -> bool:
        """The output limit ended an answer that has text and no tool call. It is asked to go on, up to MAX_CONTINUATIONS times per request,
        unless the text is a repetition loop; then it is delivered as it is, flagged `cut`. True when the run goes on."""
        turn.cut = True
        degenerate = repetition_dominated(content)
        go_on = self._continuations < MAX_CONTINUATIONS and not degenerate and not self.cancel.is_set()
        self._emit({"type": "answer_truncated", "final": not go_on, "continuation": self._continuations + 1 if go_on else self._continuations,
                    "of": MAX_CONTINUATIONS, "degenerate": degenerate})
        self._audit("answer_truncated", {"final": not go_on, "continuations": self._continuations, "degenerate": degenerate, "chars": len(content)})
        self.append({"role": "assistant", "content": content[:cut_point(content)] if go_on else content})     # a continuation starts at a word boundary
        turn.seconds = round(time.monotonic() - t0, 3)
        self._finish_turn(turn, run_turns)
        if not go_on:
            self.cut = True
            return False
        self._continuations += 1
        self._continuing = True
        self.append({"role": "user", "content": CONTINUE_NOTE, "note": True})
        return True

    def _check_answer(self, content: str) -> bool:
        """Measures the answer against the request's card; True when it asks for a second try (once per request)."""
        card = self._card
        if not self.pipeline or not card or not card.constraints or self.cancel.is_set():
            return False
        first = not self._retried
        verdict = self.pipeline.verify(card, content, judged=first)
        retry = first and not verdict.ok
        self._emit({"type": "verify", "mode": verdict.mode, "ok": verdict.ok, "violations": verdict.violations, "retry": retry,
                    "checked": verdict.checked, "judge_calls": verdict.judge_calls, "judge_unknown": verdict.judged_unknown,
                    "seconds": verdict.seconds, "constraints": [c.text for c in card.constraints]})
        if retry:
            self._retried = True
            self.append({"role": "user", "content": feedback(verdict.violations)})
        return retry

    # -- what is looked at before a turn ends, and while it goes on ---------------------------------------

    def _observe(self, call: ParsedCall, args: Any, result: ToolExecutionResult, tool: Any, text: str) -> Verdict:
        """Lets the loop guard see an executed call and what it brought back. A change to the files is progress and clears what was counted;
        a strike counts toward ending the run."""
        if result.decision != "executed" or tool is None:
            return Verdict()
        if call.name == "todo_write" and not result.is_error and getattr(self.tools, "todo", None) is not None:
            self._emit({"type": "todo", "items": self.tools.todo.items})
        recorded = self.changes() if self.changes else None
        if recorded is not None:
            mutated, self._edits_seen = recorded != self._edits_seen, recorded
        else:
            mutated = tool.effect == EffectClass.REVERSIBLE and not result.is_error
        if mutated:
            self.loop_detector.progress()
        verdict = self.outcome_guard.observe(call.call_id, call.name, args, text, result.is_error, mutated=mutated,
                                             pure=tool.effect == EffectClass.PURE and not tool.untrusted, held=self._held)
        if verdict.strike:
            self._loop_strikes += 1
            self._audit("loop_guard", {"tool": call.name, "note": verdict.note[:200]})
        return verdict

    def _held(self, call_id: str) -> bool:
        """Whether the result of this call is still in the conversation as it was written (not masked to save room)."""
        return any(m.get("role") == "tool" and m.get("tool_call_id") == call_id and not m.get("masked") for m in self.messages)

    def _budget_warning(self, started: float, turn_idx: int) -> bool:
        """Tells the model once, in a hidden note, when a budget is nine tenths used. True when it did."""
        if turn_idx < 2:
            return False
        spent = []
        if self.max_turns and self.max_turns > 0 and turn_idx >= math.ceil(BUDGET_WARN_AT * self.max_turns):
            spent.append("steps")
        if self.max_total_tokens and self.total_tokens >= BUDGET_WARN_AT * self.max_total_tokens:
            spent.append("tokens")
        if self.max_seconds and time.monotonic() - started >= BUDGET_WARN_AT * self.max_seconds:
            spent.append("time")
        if not spent:
            return False
        self.append({"role": "user", "content": BUDGET_WARNING, "note": True})
        self._emit({"type": "budget_warning", "spent": spent})
        self._audit("budget_warning", {"spent": spent, "turns": len(self.turns)})
        return True

    def _prepare_context(self) -> None:
        """Keeps the conversation inside its budget; the compaction hooks hear of it when the context is over its threshold and gets smaller.
        A summary of the older steps (harness/compaction.py) is announced with its report: what was replaced and what the server will read again."""
        ctx = self.context
        due, before = False, 0
        if self.hooks is not None:
            try:
                before = ctx.estimate_tokens(self.messages)
                due = before > ctx.budget_tokens * ctx.mask_at or bool(getattr(ctx, "compaction_due", lambda m: False)(self.messages))
            except (AttributeError, TypeError):
                due = False
        if due:
            self.hooks.trigger_event("pre_compact", {"tokens": before, "messages": len(self.messages)})
        self.messages = ctx.prepare(self.messages)
        report = getattr(ctx, "take_compaction", lambda: None)()
        if report:
            report["prefix"]["tools_sha"] = hashlib.sha256(json.dumps(self.tools.schemas(), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
            self._emit({"type": "compaction", **report})
            self._audit("compaction", {k: report[k] for k in ("n", "reason", "tokens_before", "tokens_after", "replaced", "summary_by")})
        if due:
            after = ctx.estimate_tokens(self.messages)
            if after < before:
                self.hooks.trigger_event("post_compact", {"tokens_before": before, "tokens_after": after, "messages": len(self.messages),
                                                          "actions": list(getattr(ctx, "last_actions", None) or []), **({"compaction": report} if report else {})})

    def _note_usage(self, done: "_Completion") -> None:
        """Tells the context what the server counted for the request just made; the first request after a compaction reports what it read from cache."""
        note = getattr(self.context, "note_usage", None)
        if note is None or not done.prompt_tokens:
            return
        seen = note(self.messages, done.prompt_tokens, done.cached_tokens)
        if seen:
            self._emit({"type": "compaction_cache", "n": seen["n"], **seen["observed"]})

    def _before_end(self, content: str) -> bool:
        """The answer is final unless the project's checks fail, the checklist has items open or a stop hook denies it: then a hidden message
        says why and the model works again (True). A gate that gives up ends the run as `verify_failed` and False is returned."""
        if self.subagent or self.cancel.is_set():
            return False
        if self._run_gate():
            return True
        if self.stop_reason == VERIFY_FAILED:
            return False
        todo = getattr(self.tools, "todo", None)
        if todo is not None and not self._todo_nudged and todo.open_items():
            self._todo_nudged = True
            self.append({"role": "user", "content": TODO_NOTE.format(items=todo.render(open_only=True)), "note": True})
            self._emit({"type": "todo_open", "count": len(todo.open_items())})
            return True
        if self.completion is not None and self._completion_rounds < MAX_COMPLETION_ROUNDS:
            note = self.completion(self.last_user_text, content, self._work_line())
            if note:
                self._completion_rounds += 1
                self.mission["completion_rounds"] = self._completion_rounds
                self.append({"role": "user", "content": note, "note": True})
                self._emit({"type": "completion_check", "round": self._completion_rounds, "of": MAX_COMPLETION_ROUNDS, "note": note[:300]})
                self._audit("completion_check", {"round": self._completion_rounds, "note": note[:300]})
                return True
        if self.hooks is None:
            return False
        reason = self.hooks.trigger_stop({"final": content[:2000], "turns": len(self.turns), "stop_hook_active": self._stop_denials > 0})
        if not reason:
            return False
        if self._stop_denials >= MAX_STOP_DENIALS:
            self._emit({"type": "stop_denied", "final": True, "reason": reason[:300]})
            self._audit("stop_denied", {"final": True, "denials": self._stop_denials})
            return False
        self._stop_denials += 1
        self.append({"role": "user", "content": f"[harness] {reason}\nContinue the work, then answer again.", "note": True})
        self._emit({"type": "stop_denied", "final": False, "n": self._stop_denials, "reason": reason[:300]})
        self._audit("stop_denied", {"final": False, "denials": self._stop_denials})
        return True

    def _work_line(self) -> str:
        """One line about what this request's work did, for the reader that checks whether it is met."""
        calls = [c for t in self.turns for c in t.tool_calls]
        errors = sum(1 for c in calls if c.is_error)
        changed = (self.changes() - self._edits_seen) if self.changes else 0
        return f"{len(calls)} tool calls, {errors} failed, {max(changed, 0)} file changes recorded"

    def _run_gate(self) -> bool:
        """The verify gate (harness/gate.py): when files changed since it last looked, each listed command runs as a shell call of this
        conversation. True when the checks failed and the model was told to fix them."""
        gate = self.gate
        if gate is None or not gate.due():
            return False
        if self.tools.get("shell_command") is None:
            return False
        results = []
        for command in gate.commands:
            results.append(self._gate_command(command))
            if self.cancel.is_set():
                return False
        gate.looked()
        self._edits_seen = self.changes() if self.changes else self._edits_seen
        decision, text = gate.verdict(results)
        self._emit({"type": "gate", "decision": decision, "round": gate.rounds, "of": gate.max_rounds, "results": event_data(results)})
        self._audit("verify_gate", {"decision": decision, "round": gate.rounds, "commands": [r.command[:120] for r in results],
                                    "failed": [r.command[:120] for r in results if not r.ok]})
        if decision == "retry":
            self.append({"role": "user", "content": text, "note": True})
            return True
        if decision == "stop":
            self.stop_reason, self.error = VERIFY_FAILED, text
        return False

    def _gate_command(self, command: str) -> "Any":
        """One of the person's check commands, through the same gate as any shell call; its start and end are shown like the model's calls."""
        gate = self.gate
        args = {"command": command, "timeout": gate.timeout}
        if self.permissions is not None:
            self.permissions.approve("shell_command", args)       # the person listed this exact command: it runs without asking, deny rules still hold
        call = ParsedCall("call_gate_" + uuid.uuid4().hex[:10], "shell_command", args)
        self._emit({"type": "tool_start", "call_id": call.call_id, "name": "shell_command", "arguments": args, "effect": "irreversible", "gate": True})
        result = self._execute(call, [False], loop_check=False)
        found = read_result(command, result.output, result.decision, result.is_error)
        shown = result.output if isinstance(result.output, str) else json.dumps(result.output, ensure_ascii=False, default=str)
        self._emit({"type": "tool_end", "call_id": call.call_id, "name": "shell_command", "status": "completed" if found.ok else "failed",
                    "decision": result.decision, "output": self._stringify("shell_command", shown), "gate": True, "ok": found.ok})
        return found

    def _finish_turn(self, turn: AgentTurn, run_turns: List[AgentTurn]) -> None:
        self.turns.append(turn)
        run_turns.append(turn)
        if self.on_turn_end:
            self.on_turn_end(turn)
        if self.hooks:
            self.hooks.trigger_event("turn_end", {"turn": turn.turn_index, "tool_calls": len(turn.tool_calls)})
