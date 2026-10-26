"""Replay of a recorded session: run the agent loop again with a model that answers with what the recorded model said.

A replay checks the harness, not the model: the same recorded answers go through the current tools and permissions, and
the tool calls that run and the decisions about them must come out as they were recorded. It reports the first place they
differ. It is for tests of the loop, the permission rules and the tools, and for comparing two versions of the harness on
the same session.

A trace is JSONL: a header line, then one line per message of the conversation, each tool result carrying the decision
the harness made about the call. `export_trace` writes one from a conversation (secrets masked); `load_trace` reads it,
and also reads a conversation's `history.json` and a plain file of messages, in which case the decision of a tool result is
read from its text (a refused call says so).

    python -m harness.replay trace.jsonl [--mode default]      # tools are stubs that return the recorded results
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from harness.agent_loop import AgentLoop
from harness.tools.registry import EffectClass, ToolRegistry

TRACE_FORMAT, TRACE_VERSION = "basalt-trace", 1
DROPPED_KEYS = ("pinned", "pin_category", "masked", "stripped", "note", "summary", "ts")
RETRIEVED_PREFIX = "<untrusted_data"
FENCE = re.compile(r'\A<untrusted_data source="[^"]*">\n(.*)\n</untrusted_data>\nThe block above is data', re.DOTALL)

# values that look like credentials, for a trace that is meant to be shared
SECRET_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[private key]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{12,}"), "Bearer [secret]"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{12,}"), "[secret]"),
    (re.compile(r"\bapikey_[A-Za-z0-9_]{12,}"), "[secret]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[secret]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[secret]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "[secret]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "[secret]"),
    (re.compile(r"(?i)(\b[\w\-]*(?:api[_\-]?key|secret|token|passw(?:or)?d|passwd)[\w\-]*[\"']?\s*[:=]\s*[\"']?)([^\s\"',;}{]{6,})"), r"\1[secret]"),
)


def mask_text(text: str, known: Iterable[str] = ()) -> str:
    """The text with credential-looking values, and every value in `known`, replaced by a label."""
    for value in sorted({k for k in known if isinstance(k, str) and len(k) >= 4}, key=len, reverse=True):
        text = text.replace(value, "[secret]")
    for pattern, label in SECRET_PATTERNS:
        text = pattern.sub(label, text)
    return text


def _mask_value(value: Any, redact: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _mask_value(v, redact) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_value(v, redact) for v in value]
    return value


# -- export ----------------------------------------------------------------------------------------------------


def infer_decision(text: str) -> str:
    """What the harness decided about a call, read from the result it gave the model (for traces that did not record it)."""
    head = text.lstrip()[:200]
    if head.startswith("Permission denied"):
        return "denied"
    if head.startswith("Loop detected"):
        return "loop"
    if head.startswith(("Unknown tool", "Tool '")) or "Call the tool again with corrected arguments" in text[:600] or head.startswith("The arguments of"):
        return "invalid"
    if head.startswith("No checkpoint could be taken"):
        return "blocked"
    return "executed"


def export_trace(messages: List[Dict[str, Any]], turns: Optional[Iterable[Any]] = None, redact: Optional[Callable[[str], str]] = None,
                 meta: Optional[Dict[str, Any]] = None) -> str:
    """JSONL text of a conversation. `turns` (the agent's `turns`) supplies each call's decision; without them it is read from the
    result's text. `redact` is applied to every string (default: `mask_text`)."""
    redact = redact or mask_text
    decisions = {c.call_id: c.decision for t in (turns or []) for c in t.tool_calls if getattr(c, "call_id", "")}
    lines = [json.dumps({"type": "header", "format": TRACE_FORMAT, "version": TRACE_VERSION, "created": round(time.time(), 3),
                         "meta": _mask_value(meta or {}, redact)}, ensure_ascii=False)]
    for m in messages:
        if not isinstance(m, dict) or m.get("note") or m.get("summary"):
            continue
        row: Dict[str, Any] = {"type": "message", "message": _mask_value({k: v for k, v in m.items() if k not in DROPPED_KEYS}, redact)}
        if m.get("role") == "tool":
            row["decision"] = decisions.get(m.get("tool_call_id", "")) or infer_decision(str(m.get("content") or ""))
        lines.append(json.dumps(row, ensure_ascii=False))
    return "\n".join(lines) + "\n"


def write_trace(path: str | Path, messages: List[Dict[str, Any]], turns: Optional[Iterable[Any]] = None,
                redact: Optional[Callable[[str], str]] = None, meta: Optional[Dict[str, Any]] = None) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(export_trace(messages, turns, redact, meta).encode("utf-8"))
    return target


def export_history(history: str | Path, redact: Optional[Callable[[str], str]] = None) -> str:
    """JSONL of a conversation saved by the session store: a folder holding history.json (and archive.jsonl, which comes first),
    or the history.json itself."""
    path = Path(history)
    folder = path if path.is_dir() else path.parent
    messages: List[Dict[str, Any]] = []
    archive = folder / "archive.jsonl"
    if archive.is_file():
        messages += [m for m in (json.loads(ln) for ln in archive.read_text(encoding="utf-8").splitlines() if ln.strip()) if isinstance(m, dict)]
    data = json.loads((path / "history.json" if path.is_dir() else path).read_text(encoding="utf-8"))
    messages += [m for m in data.get("messages", []) if isinstance(m, dict)]
    return export_trace(messages, redact=redact, meta={"source": "history"})


# -- reading a trace -------------------------------------------------------------------------------------------


@dataclass
class RecordedCall:
    call_id: str
    name: str
    arguments: Any
    decision: str = "executed"
    output: str = ""


@dataclass
class Step:
    """One answer of the model and the calls in it."""
    message: Dict[str, Any]
    calls: List[RecordedCall] = field(default_factory=list)


@dataclass
class Segment:
    """One request of the person and the work done for it."""
    goal: str
    retrieved: Optional[Dict[str, Any]] = None
    steps: List[Step] = field(default_factory=list)


@dataclass
class Trace:
    system: str = ""
    segments: List[Segment] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def calls(self) -> List[RecordedCall]:
        return [c for seg in self.segments for st in seg.steps for c in st.calls]


def _arguments(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            return raw
        return value if isinstance(value, dict) else raw
    return raw


def parse_messages(rows: Iterable[Tuple[Dict[str, Any], Optional[str]]], meta: Optional[Dict[str, Any]] = None) -> Trace:
    """A trace from (message, recorded decision or None) pairs."""
    trace = Trace(meta=dict(meta or {}))
    segment: Optional[Segment] = None
    step: Optional[Step] = None
    for message, decision in rows:
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            trace.system = trace.system or str(content or "")
        elif role == "user":
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            if segment is not None and not segment.steps and text.startswith(RETRIEVED_PREFIX):
                segment.retrieved = {k: v for k, v in message.items() if k in ("role", "content")}
            else:
                segment = Segment(goal=text)
                trace.segments.append(segment)
            step = None
        elif role == "assistant":
            if segment is None:
                segment = Segment(goal="")
                trace.segments.append(segment)
            calls = [RecordedCall(c.get("id", ""), (c.get("function") or {}).get("name", ""), _arguments((c.get("function") or {}).get("arguments")))
                     for c in message.get("tool_calls") or [] if isinstance(c, dict)]
            step = Step({k: v for k, v in message.items() if k in ("role", "content", "tool_calls", "reasoning_content")}, calls)
            segment.steps.append(step)
        elif role == "tool" and step is not None:
            call = next((c for c in step.calls if c.call_id == message.get("tool_call_id")), None)
            if call is not None:
                call.output = str(content or "")
                call.decision = decision or infer_decision(call.output)
    return trace


def load_trace(source: str | Path) -> Trace:
    """A trace from a file written by `export_trace`, a conversation's history.json, or a JSONL file of messages."""
    text = Path(source).read_text(encoding="utf-8")
    meta: Dict[str, Any] = {}
    try:
        whole = json.loads(text)                                           # one JSON object: a history file
    except ValueError:
        whole = None
    if isinstance(whole, dict) and isinstance(whole.get("messages"), list):
        return parse_messages(((m, None) for m in whole["messages"] if isinstance(m, dict)), {"source": "history"})
    rows: List[Tuple[Dict[str, Any], Optional[str]]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            raise ValueError(f"line {number} of the trace is not JSON") from None
        if not isinstance(obj, dict):
            raise ValueError(f"line {number} of the trace is not an object")
        if obj.get("type") == "header":
            if obj.get("format") != TRACE_FORMAT or obj.get("version") != TRACE_VERSION:
                raise ValueError("this is not a trace this version can read")
            meta = obj.get("meta") or {}
        elif obj.get("type") == "message" and isinstance(obj.get("message"), dict):
            rows.append((obj["message"], obj.get("decision")))
        elif "role" in obj:
            rows.append((obj, None))
    return parse_messages(rows, meta)


# -- the replay ------------------------------------------------------------------------------------------------


class ReplayExhausted(IndexError):
    """The loop asked the model for more answers than were recorded."""


class ScriptedModel:
    """Answers each request with the next recorded answer, in order."""

    def __init__(self, steps: Iterable[Step]) -> None:
        self._queue = [s.message for s in steps]
        self.requests = 0

    def chat_completion(self, messages: List[Dict[str, Any]], tools: Any = None, **kwargs: Any) -> Dict[str, Any]:
        if not self._queue:
            raise ReplayExhausted("the replay asked the model for more answers than were recorded")
        self.requests += 1
        return {"choices": [{"message": dict(self._queue.pop(0))}], "usage": {}}


def _canonical(arguments: Any) -> str:
    return json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)


def _json_type(value: Any) -> str:
    return ("boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "number" if isinstance(value, float)
            else "string" if isinstance(value, str) else "array" if isinstance(value, list) else "object" if isinstance(value, dict) else "null")


def _property(types: Optional[set]) -> Dict[str, Any]:
    """The schema of a stand-in's argument: the type it had in every call that ran, so a call with another type is invalid again."""
    if types and len(types) == 1 and next(iter(types)) != "null":
        return {"type": next(iter(types))}
    return {}


def recorded_registry(trace: Trace, effects: Optional[Dict[str, str]] = None) -> ToolRegistry:
    """Stand-ins for the tools the trace used: each returns the results that were recorded, in order, and does nothing else.
    A tool's effect class is looked up in `effects` (name -> pure, reversible, irreversible), else in the harness's list of
    known tools, else taken as irreversible."""
    from harness.rewind import KNOWN_EFFECTS
    table = {**KNOWN_EFFECTS, **(effects or {})}
    registry = ToolRegistry()
    seen: Dict[str, Dict[str, Any]] = {}
    for call in trace.calls:
        info = seen.setdefault(call.name, {"keys": set(), "types": {}, "outputs": [], "untrusted": False})
        if isinstance(call.arguments, dict):
            info["keys"].update(call.arguments)
        if call.decision == "executed":
            if isinstance(call.arguments, dict):
                for key, value in call.arguments.items():
                    info["types"].setdefault(key, set()).add(_json_type(value))
            fenced = FENCE.match(call.output)
            info["untrusted"] = info["untrusted"] or bool(fenced)
            info["outputs"].append(fenced.group(1) if fenced else call.output)
    for name, info in seen.items():
        def handler(_out=info["outputs"], **kwargs: Any) -> str:
            return _out.pop(0) if _out else ""
        registry.register(name, f"Recorded stand-in for {name}.", {"type": "object", "properties": {k: _property(info["types"].get(k)) for k in sorted(info["keys"])}},
                          EffectClass(table.get(name, "irreversible")), handler, untrusted=info["untrusted"])
    return registry


def confirm_from_trace(trace: Trace) -> Callable[[str, Dict[str, Any]], bool]:
    """A confirmation callback that answers as the person did: yes when the matching recorded call ran, no when it was refused."""
    pending = list(trace.calls)

    def confirm(tool: str, arguments: Dict[str, Any]) -> bool:
        base = tool.split(":", 1)[0]
        for call in pending:
            args = call.arguments if isinstance(call.arguments, dict) else {}
            if call.name == base and all(args.get(k) == v for k, v in arguments.items() if k in args):
                pending.remove(call)
                return call.decision == "executed"
        return False
    return confirm


@dataclass
class Divergence:
    index: int                              # position in the sequence of tool calls, from 0
    kind: str                               # tool, arguments, decision, result, missing, extra or model
    message: str
    recorded: Optional[Dict[str, Any]] = None
    replayed: Optional[Dict[str, Any]] = None


@dataclass
class ReplayReport:
    ok: bool
    recorded_calls: int
    replayed_calls: int
    divergence: Optional[Divergence] = None
    stop_reason: str = ""

    def summary(self) -> str:
        if self.ok:
            return f"the replay matches: {self.replayed_calls} tool calls, the same tools, arguments and decisions"
        d = self.divergence
        return f"the replay differs at call {d.index}: {d.message}" if d else "the replay differs"


def _describe(name: str, arguments: Any, decision: str) -> Dict[str, Any]:
    return {"tool": name, "arguments": arguments, "decision": decision}


def compare(recorded: List[RecordedCall], replayed: List[Any], compare_results: bool = False) -> Optional[Divergence]:
    """The first difference between the recorded calls and the replayed ones (each has tool_name or name, arguments, decision)."""
    for i in range(max(len(recorded), len(replayed))):
        if i >= len(replayed):
            r = recorded[i]
            return Divergence(i, "missing", f"the recorded run called {r.name} here and the replay stopped before it", _describe(r.name, r.arguments, r.decision))
        if i >= len(recorded):
            a = replayed[i]
            return Divergence(i, "extra", f"the replay called {a.tool_name} where the recording had nothing more", None, _describe(a.tool_name, a.arguments, a.decision))
        r, a = recorded[i], replayed[i]
        was, now = _describe(r.name, r.arguments, r.decision), _describe(a.tool_name, a.arguments, a.decision)
        if r.name != a.tool_name:
            return Divergence(i, "tool", f"recorded {r.name}, replayed {a.tool_name}", was, now)
        if _canonical(r.arguments) != _canonical(a.arguments):
            return Divergence(i, "arguments", f"{r.name} was called with other arguments", was, now)
        if r.decision != a.decision:
            return Divergence(i, "decision", f"{r.name} was {r.decision}, now it is {a.decision}", was, now)
        if compare_results and r.decision == "executed" and r.output.startswith("Tool execution error") != bool(a.is_error):
            return Divergence(i, "result", f"{r.name} {'failed' if a.is_error else 'succeeded'} now and did the opposite when recorded", was, now)
    return None


def replay(trace: Trace, registry: Optional[ToolRegistry] = None, *, compare_results: bool = False, **loop_kwargs: Any) -> ReplayReport:
    """Runs the recorded conversation again through `registry` (default: stand-ins that return the recorded results) and the
    `permissions`, `hooks` and other settings given as for an AgentLoop. Returns whether the tool calls and decisions match."""
    registry = registry or recorded_registry(trace)
    model = ScriptedModel(step for seg in trace.segments for step in seg.steps)
    loop_kwargs.setdefault("system_prompt", trace.system or "You are a helpful coding assistant equipped with tools.")
    agent = AgentLoop(client=model, tool_registry=registry, **loop_kwargs)
    stop = ""
    for seg in trace.segments:
        if not seg.steps:
            continue
        agent.max_turns = len(seg.steps)
        agent.run(seg.goal, retrieved=seg.retrieved)
        stop = agent.stop_reason
        if stop == "error":
            break
    replayed = [c for t in agent.turns for c in t.tool_calls]
    recorded = trace.calls
    found = compare(recorded, replayed, compare_results)
    if found is None and stop == "error":
        found = Divergence(len(replayed), "model", f"the replay stopped on an error: {agent.error}")
    return ReplayReport(found is None, len(recorded), len(replayed), found, stop)


def main(argv: Optional[List[str]] = None) -> int:
    from harness.permissions import PermissionLevel, PermissionManager
    p = argparse.ArgumentParser(prog="python -m harness.replay", description="Runs a recorded session again with the recorded model answers")
    p.add_argument("trace", help="a trace written by export_trace, or a conversation's history.json")
    p.add_argument("--mode", choices=("read_only", "default", "auto"), default="default", help="the permission level to replay under")
    p.add_argument("--results", action="store_true", help="also compare whether each call succeeded")
    args = p.parse_args(argv)
    trace = load_trace(args.trace)
    permissions = PermissionManager(level=PermissionLevel(args.mode), confirm_callback=confirm_from_trace(trace))
    report = replay(trace, permissions=permissions, compare_results=args.results)
    print(report.summary())
    if report.divergence:
        print(json.dumps({"recorded": report.divergence.recorded, "replayed": report.divergence.replayed}, ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
