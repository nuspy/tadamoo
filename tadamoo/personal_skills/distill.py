"""Turning requests of a conversation into a personal skill.

The recorded steps are copied from the history, not rewritten: each tool call with its arguments, in order. What changes from one run to
the next is found in three ways, without the model: the phrases the person marked with # in their requests, the values of a call that
come from their words (a file name, a number, a city), and dates that were the day of the request. The model is asked twice, and only for
what needs language: to name and explain the parameters, and, once the person has chosen them, to write the Python script that does the
same steps, using code for everything that can be code and `ai()` only where the language itself has to be understood."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from tadamoo.transcript import clip, marker

from . import markers
from .check import check
from .values import NAME, TYPES, ITEM_TYPES, check_spec

MAX_REQUESTS = 12
MAX_STEPS = 80
SHOW_RESULT = 300
SHOW_ARGUMENT = 1500
FAILED = ("Tool execution error", "Permission denied", "Unknown tool", "Loop detected", "Cancelled before")
DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
COMMON = {"true", "false", "none", "null", "yes", "no", "utf-8", "r", "w", "a"}


# -- what the conversation holds -------------------------------------------------------------------------------

def requests_of(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The person's requests in order, numbered from 1 as the page counts them: `n`, where it is in the messages, what they wrote
    (`raw`, with their # marks) and what the model was given (`text`), and when."""
    out: List[Dict[str, Any]] = []
    for i, m in enumerate(messages):
        text = m.get("content")
        if m.get("role") != "user" or not isinstance(text, str) or text.startswith(("[harness]", "[planner]")) or m.get("note") or m.get("summary"):
            continue
        raw = m.get("raw") if isinstance(m.get("raw"), str) else text
        out.append({"n": len(out) + 1, "index": i, "raw": raw, "text": markers.clean(raw), "ts": m.get("ts")})
    return out


def steps_of(messages: List[Dict[str, Any]], requests: List[Dict[str, Any]], turns: List[int]) -> List[Dict[str, Any]]:
    """What was done to answer the chosen requests: each tool call (name, arguments, what it returned, whether it worked) and each
    answer in words, in order."""
    by_n = {r["n"]: r for r in requests}
    steps: List[Dict[str, Any]] = []
    for n in sorted(set(turns)):
        req = by_n.get(n)
        if req is None:
            continue
        end = next((r["index"] for r in requests if r["n"] == n + 1), len(messages))
        steps.extend(steps_in(messages, req["index"] + 1, end, n))
    return steps


def steps_in(messages: List[Dict[str, Any]], lo: int, hi: int, turn: int) -> List[Dict[str, Any]]:
    """The tool calls and the answers in words of `messages[lo:hi]`, in order, each marked with the request (or plan step) `turn` they belong to."""
    steps: List[Dict[str, Any]] = []
    results = {m.get("tool_call_id"): str(m.get("content") or "") for m in messages[lo:hi] if m.get("role") == "tool"}
    for m in messages[lo:hi]:
        if m.get("role") != "assistant":
            continue
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            result = results.get(call.get("id"), "")
            steps.append({"turn": turn, "kind": "tool", "name": fn.get("name", ""), "arguments": args if isinstance(args, dict) else {},
                          "result": result, "ok": not result.startswith(FAILED)})
        if not m.get("tool_calls") and str(m.get("content") or "").strip():
            steps.append({"turn": turn, "kind": "answer", "text": str(m["content"]).strip()})
    return steps


def leaves(value: Any, path: str = "") -> Iterator[Tuple[str, Any]]:
    """The strings and numbers inside call arguments, with where they are (`path` or `items[0].name`)."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from leaves(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from leaves(v, f"{path}[{i}]")
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, (str, int, float)):
        yield path, value


# -- what changes from one run to the next ---------------------------------------------------------------------

def _in_text(value: str, text: str) -> bool:
    v = value.strip().lower()
    if len(v) < 2 or v in COMMON:
        return False
    t = text.lower()
    if len(v) >= 4:
        return v in t
    return re.search(r"(?<![\w])" + re.escape(v) + r"(?![\w])", t) is not None


def _day_word(day: datetime, seen: datetime) -> Optional[str]:
    offset = (day.date() - seen.date()).days
    return {0: "oggi", -1: "ieri", 1: "domani"}.get(offset)


def candidates(requests: List[Dict[str, Any]], steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The values that probably change: [{"value", "source": mark | words | date, "name_hint", "used_in": [{"tool", "argument"}], "request",
    "type", "default"?}]. A phrase marked with # comes first; then what a call took from the person's words; then dates."""
    found: Dict[str, Dict[str, Any]] = {}

    def add(value: Any, source: str, **extra: Any) -> Dict[str, Any]:
        key = str(value).strip().lower()
        item = found.get(key)
        if item is None:
            item = found[key] = {"value": value if not isinstance(value, str) else value.strip(), "source": source, "name_hint": None, "used_in": [], "request": None,
                                 "type": "number" if isinstance(value, (int, float)) else "text"}
            item.update(extra)
        elif item["source"] != "mark" and source == "mark":
            item.update(source="mark", **extra)
        return item

    for req in requests:
        for mark in markers.find(req["raw"]):
            item = add(mark.value, "mark", request=req["n"])
            item["name_hint"] = item["name_hint"] or mark.name
    for step in steps:
        if step["kind"] != "tool":
            continue
        req = next((r for r in requests if r["n"] == step["turn"]), None)
        for path, value in leaves(step["arguments"]):
            where = {"tool": step["name"], "argument": path}
            texts = " ".join(r["text"] for r in requests)
            if isinstance(value, str):
                for m in DATE.finditer(value):
                    try:
                        day = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    except ValueError:
                        continue
                    seen = datetime.fromtimestamp(req["ts"]) if req and isinstance(req.get("ts"), (int, float)) else None
                    word = _day_word(day, seen) if seen else None
                    item = add(m.group(0), "date", request=step["turn"], type="date")
                    if word and "default" not in item:
                        item["default"] = word
                    item["used_in"].append(where)
                if not _in_text(value, texts):
                    # a path or an address of which the request names the last part
                    base = re.split(r"[\\/]", value.rstrip("/\\"))[-1] if re.search(r"[\\/]", value) else ""
                    if not (base and _in_text(base, texts)):
                        continue
            elif not _in_text(str(value), texts) or value in (0, 1):
                continue
            if any(str(c["value"]).lower() in str(value).lower() for c in found.values() if c["source"] == "mark"):
                continue                                              # a phrase the person marked already covers it
            item = add(value, "words", request=step["turn"], type="number" if isinstance(value, (int, float)) else "text")
            item["used_in"].append(where)
    for item in found.values():
        if item["source"] == "mark":
            for step in steps:
                if step["kind"] != "tool":
                    continue
                for path, value in leaves(step["arguments"]):
                    if isinstance(value, str) and str(item["value"]).lower() in value.lower():
                        item["used_in"].append({"tool": step["name"], "argument": path})
    order = {"mark": 0, "words": 1, "date": 2}
    return sorted(found.values(), key=lambda c: order.get(c["source"], 3))


# -- the model's two jobs --------------------------------------------------------------------------------------

JSON_SCHEMA = '''{"name": "a short name for the skill", "description": "one sentence: what it does",
 "params": [{"name": "snake_case_identifier", "label": "short name the person sees", "explanation": "what to write here",
             "type": "text|number|date|datetime|choice|list|path|bool", "value_seen": "the value in the requests or calls this replaces",
             "default": "optional: a date in words such as yesterday, or a usual value", "choices": ["only for choice"],
             "item_type": "number|text|mixed, only for list"}]}'''


def _clip(text: str, n: int) -> str:
    return clip(text, n)[0]


def _outline(steps: List[Dict[str, Any]], argument: int = 300, result: int = SHOW_RESULT) -> str:
    lines: List[str] = []
    for i, s in enumerate(steps[:MAX_STEPS], 1):
        if s["kind"] == "answer":
            lines.append(f"{i}. [answer to request {s['turn']}] {_clip(s['text'], result * 2)}")
        else:
            args = _clip(json.dumps(s["arguments"], ensure_ascii=False), argument)
            lines.append(f"{i}. [request {s['turn']}] tool {s['name']}({args}) -> {'ok' if s['ok'] else 'FAILED'}: {_clip(s['result'].strip(), result)}")
    if len(steps) > MAX_STEPS:
        lines.append(marker(len(steps) - MAX_STEPS, "steps"))
    return "\n".join(lines)


def propose_prompt(requests: List[Dict[str, Any]], steps: List[Dict[str, Any]], found: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    asked = "\n".join(f"Request {r['n']}: {_clip(r['raw'], 2000)}" for r in requests)
    cands = "\n".join(f"- {c['value']!r} ({c['source']}; used in {', '.join(u['tool'] + '.' + u['argument'] for u in c['used_in'][:4]) or 'the answer'})" for c in found) or "(none found)"
    system = ("You turn what a person asked an assistant into a reusable program. Decide which values would change the next time the same task is "
              "done (a topic, a name, a number, a date, a file) and which stay. Phrases between two # signs were marked by the person as variable. "
              "Answer with one JSON object only, no other text.")
    user = (f"The requests:\n{asked}\n\nWhat was done:\n{_outline(steps)}\n\nValues that probably change (found by the program, not by you):\n{cands}\n\n"
            f"Name every parameter. Keep the candidates that are really variable, drop the ones that are not, and add another only if a value in the "
            f"requests or calls is clearly meant to change and is missing. Do not invent values: `value_seen` must be written in the requests or calls. "
            f"A date that was 'today' or 'yesterday' when it was asked is a date parameter with that word as its default.\n\nThe JSON:\n{JSON_SCHEMA}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_json_object(text: str) -> Dict[str, Any]:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    body = fenced.group(1) if fenced else text[text.find("{"):text.rfind("}") + 1]
    try:
        value = json.loads(body)
    except ValueError:
        raise ValueError("the model did not answer with a JSON object") from None
    if not isinstance(value, dict):
        raise ValueError("the model did not answer with a JSON object")
    return value


def clean_params(raw: Any, evidence: str) -> List[Dict[str, Any]]:
    """The parameters the model proposed, kept only when they are well formed; `evidence` is the text of the requests and calls: a value that
    is not in it is marked `unverified` (the person decides)."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = re.sub(r"[^a-z0-9_]+", "_", str(item.get("name", "")).lower()).strip("_")[:40]
        if not name or name[0].isdigit():
            name = "p_" + name if name else ""
        if not NAME.match(name) or name in seen:
            continue
        spec: Dict[str, Any] = {"name": name, "label": str(item.get("label") or name)[:60], "explanation": str(item.get("explanation") or "")[:300],
                                "type": item.get("type") if item.get("type") in TYPES else "text"}
        if spec["type"] == "choice":
            spec["choices"] = [str(c)[:80] for c in item.get("choices") or [] if str(c).strip()][:30]
        if spec["type"] == "list":
            spec["item_type"] = item.get("item_type") if item.get("item_type") in ITEM_TYPES else "text"
        if item.get("default") not in (None, ""):
            spec["default"] = item["default"]
        problems = check_spec(spec)
        if problems:
            spec.pop("default", None)
            if check_spec(spec):
                continue
        seen_value = item.get("value_seen")
        spec["value_seen"] = seen_value if isinstance(seen_value, (str, int, float)) and not isinstance(seen_value, bool) else None
        spec["unverified"] = spec["value_seen"] is not None and str(spec["value_seen"]).strip().lower() not in evidence.lower()
        seen.add(name)
        out.append(spec)
    return out[:20]


def evidence_text(requests: List[Dict[str, Any]], steps: List[Dict[str, Any]]) -> str:
    return "\n".join([r["raw"] + "\n" + r["text"] for r in requests] + [json.dumps(s.get("arguments", {}), ensure_ascii=False) for s in steps if s["kind"] == "tool"])


def propose(client: Any, requests: List[Dict[str, Any]], steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The first model call: a name, a description and the parameters, from the candidates found without the model. Returns
    {"name", "description", "params", "candidates", "steps"}; if the model fails, the candidates alone are offered as parameters."""
    found = candidates(requests, steps)
    evidence = evidence_text(requests, steps)
    proposal: Dict[str, Any] = {}
    note = ""
    try:
        resp = client.chat_completion(messages=propose_prompt(requests, steps, found), temperature=0.0, extra={"reasoning_effort": "none"}, timeout=600.0)
        content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        proposal = parse_json_object(content)
    except Exception as exc:                                              # the person still gets the candidates, to name themselves
        note = f"The model could not name the parameters ({exc}); they are listed as found."
    params = clean_params(proposal.get("params"), evidence)
    if not params:
        for i, c in enumerate(found[:8], 1):
            hint = re.sub(r"[^a-z0-9_]+", "_", str(c.get("name_hint") or "").lower()).strip("_")
            params.append({"name": hint if NAME.match(hint or "-") else f"value_{i}", "label": str(c.get("name_hint") or f"Value {i}"), "explanation": "",
                           "type": c["type"], "value_seen": c["value"], "unverified": False, **({"default": c["default"]} if c.get("default") else {})})
    return {"name": str(proposal.get("name") or "")[:60], "description": str(proposal.get("description") or "")[:500], "params": params,
            "candidates": found, "note": note}


def _substitute(value: Any, params: List[Dict[str, Any]]) -> Any:
    """The arguments of a call with each parameter's value replaced by `<<params.name>>`, so the model wires them."""
    if isinstance(value, dict):
        return {k: _substitute(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, params) for v in value]
    if isinstance(value, str):
        for p in params:
            seen = p.get("value_seen")
            if isinstance(seen, str) and seen.strip() and seen.strip() in value:
                value = value.replace(seen.strip(), f"<<params.{p['name']}>>")
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for p in params:
            if isinstance(p.get("value_seen"), (int, float)) and p["value_seen"] == value:
                return f"<<params.{p['name']}>>"
    return value


SCRIPT_RULES = '''Write a Python program that does the same job again, with the parameters as inputs.

Rules:
- Define `def main(params):` and return the result the person wanted, as text (the final answer of the last request).
- `from tadamoo_skill import tool, ai, ask, say`. `tool("name", argument=value, ...)` runs a tool and returns its answer as text, and raises
  ToolError if it is refused or fails. `ai("prompt with {inputs}", inputs=...)` asks the model; `ask("question", ["option", ...])` asks the
  person; `say("text")` tells how it is going. `params.name` holds each parameter (dates are datetime.date, moments datetime.datetime, lists lists).
- Repeat the recorded tool calls in the same order with the same arguments, replacing each <<params.x>> by params.x. Do not call tools
  that were not recorded, and do not repeat attempts that failed.
- Use code, not the model, for everything that can be code: parsing, filtering, sorting, counting, formatting, dates, joining text. Call `ai()`
  only where the language itself has to be understood or written (summarising, classifying, writing prose), and give it exactly the text it needs.
- A step that only passed on a value the person gave becomes code that uses it.
- Do the actions on files, programs and the network through tool(), so the permissions and the audit record apply; use the standard library
  for everything else.
- Answer with the program in one ```python block, nothing else.'''


PLAN_RULES = '''This job was a plan: the steps below, in this order, each with the values it reads and the values it produces. Structure the program as plain code:
- One function for each plan step, named `step_<id>`, that takes the values it reads as arguments and returns a dict of the values it produces, named as the plan names them.
- `main(params)` calls the step functions in the plan's order and passes each value on as an argument; it keeps what the steps return in ordinary variables.
- A decision is an `if`/`elif` on those values, with the plan's own condition as the Python expression. A wait is `time.sleep` for a short one, else say so with `say()`.
- A step for the person, or one that asks for a value, is `ask()`. A step whose recorded work was only words (a choice, a judgement, a summary) is an `ai()` call
  with exactly the text it needs and the values it reads: the model is used to decide what the words ask, never to find out again what to do.
- A step marked "proposed" was approved by the person but never run: write it as the call says, and keep it easy to read.'''


def script_prompt(name: str, description: str, params: List[Dict[str, Any]], requests: List[Dict[str, Any]], steps: List[Dict[str, Any]],
                  plan: Optional[str] = None) -> List[Dict[str, str]]:
    plist = "\n".join(f"- params.{p['name']} ({p['type']}{', ' + p.get('item_type', '') if p['type'] == 'list' else ''}): {p.get('explanation') or p.get('label')}; "
                      f"the value in the requests was {p.get('value_seen')!r}" for p in params) or "(no parameters)"
    asked = "\n".join(f"Request {r['n']}: {_clip(r['text'], 2000)}" for r in requests)
    recorded = []
    for i, s in enumerate(steps[:MAX_STEPS], 1):
        if s["kind"] == "answer":
            recorded.append(f"{i}. [answer to request {s['turn']}] {_clip(s['text'], 1500)}")
        elif s["ok"]:
            recorded.append(f"{i}. [request {s['turn']}]{' (proposed, never run)' if s.get('proposed') else ''} tool {s['name']} with arguments {_clip(json.dumps(_substitute(s['arguments'], params), ensure_ascii=False), SHOW_ARGUMENT)}\n"
                            f"   it returned: {_clip(s['result'].strip(), SHOW_RESULT * 2)}")
        else:
            recorded.append(f"{i}. [request {s['turn']}] tool {s['name']} FAILED and was not part of the way: {_clip(s['result'].strip(), 160)}")
    outline = f"\n\nThe plan:\n{plan}" if plan else ""
    user = (f"Skill: {name}. {description}\n\nParameters:\n{plist}\n\nThe requests:\n{asked}{outline}\n\nWhat was done, in order:\n" + "\n".join(recorded)
            + "\n\n" + SCRIPT_RULES + ("\n" + PLAN_RULES if plan else ""))
    return [{"role": "system", "content": "You write small, exact Python programs from a record of what was done. You prefer plain code to model calls."},
            {"role": "user", "content": user}]


def extract_code(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    block = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    return (block.group(1) if block else text).strip("\n") + "\n"


def write_script(client: Any, name: str, description: str, params: List[Dict[str, Any]], requests: List[Dict[str, Any]], steps: List[Dict[str, Any]],
                 repairs: int = 2, plan: Optional[str] = None) -> Dict[str, Any]:
    """The second model call: the script. When the check finds problems, the model is told them and asks again, up to `repairs` times;
    what is left is returned for the person to see. Returns {"script", "problems", "unused"}."""
    messages = script_prompt(name, description, params, requests, steps, plan)
    script, problems = "", []
    for attempt in range(repairs + 1):
        resp = client.chat_completion(messages=messages, temperature=0.0, extra={"reasoning_effort": "none"}, timeout=900.0, max_tokens=8000)
        content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        script = extract_code(content)
        problems = check(script)
        if not problems:
            break
        if attempt < repairs:
            messages = messages + [{"role": "assistant", "content": content}, {"role": "user", "content":
                                   "The program has these problems:\n" + "\n".join(f"- line {p['line']}: {p['message']}" for p in problems[:8]) +
                                   "\nReturn the whole corrected program in one ```python block."}]
    unused = [p["name"] for p in params if not re.search(r"\bparams\.%s\b|params\[['\"]%s['\"]\]" % (p["name"], p["name"]), script)]
    return {"script": script, "problems": problems, "unused": unused}
