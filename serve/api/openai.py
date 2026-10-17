"""OpenAI chat completion chunks, their non-streaming collection and structured-output buffering."""
from __future__ import annotations

import json
import time
import uuid

from serve.frontend import Event
from serve.service import Service
from serve.api.repair import Repair, build_repair
from serve.api.structured import validated_json


def openai_chunks(svc: Service, req: dict, ids, thinking, tools, max_new, cancel, force=None):
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())
    model = svc.model

    def chunk(delta, finish=None):
        return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    yield chunk({"role": "assistant", "content": ""})
    calls = 0
    streamed = {}                                  # tool call id -> index, for calls sent piece by piece
    finished = set()                               # ... and the ones whose final tool_call came
    for kind, x in svc.run(ids, thinking, tools, max_new, req, cancel, force=force):
        if kind == "ping":
            yield None
        elif kind == "event":
            ev: Event = x
            if ev.kind == "reasoning" and ev.text:
                yield chunk({"reasoning_content": ev.text})
            elif ev.kind == "content" and ev.text:
                yield chunk({"content": ev.text})
            elif ev.kind == "tool_start":
                streamed[ev.call.id] = calls
                calls += 1
                yield chunk({"tool_calls": [{"index": streamed[ev.call.id], "id": ev.call.id, "type": "function",
                                             "function": {"name": ev.call.name, "arguments": ""}}]})
            elif ev.kind == "tool_args":
                yield chunk({"tool_calls": [{"index": streamed[ev.call.id], "function": {"arguments": ev.text}}]})
            elif ev.kind == "tool_call" and ev.call.id in streamed:
                finished.add(ev.call.id)
            elif ev.kind == "tool_call":
                yield chunk({"tool_calls": [{"index": calls, "id": ev.call.id, "type": "function",
                                             "function": {"name": ev.call.name,
                                                          "arguments": json.dumps(ev.call.arguments, ensure_ascii=False)}}]})
                calls += 1
        else:
            # a call whose final tool_call never came was cut off: not "tool_calls"
            whole = calls and streamed.keys() <= finished
            finish = "tool_calls" if whole and x["finish"] == "stop" else {"cancel": "stop"}.get(x["finish"], x["finish"])
            last = chunk({}, finish)
            last["usage"] = {"prompt_tokens": len(ids), "completion_tokens": x["completion_tokens"],
                             "total_tokens": len(ids) + x["completion_tokens"],
                             # prompt tokens served from the conversation cache
                             "prompt_tokens_details": {"cached_tokens": min(x.get("reused") or 0, len(ids))}}
            if x.get("timings"):
                last["timings"] = x["timings"]          # llama.cpp's field: the speed its clients show
            yield last


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def openai_collect(chunks) -> dict:
    content, reasoning, by_index, last = [], [], {}, None
    for c in chunks:
        if c is None:                              # a heartbeat
            continue
        if (c.get("structured_output") or {}).get("discarded") is True:
            content.clear()                        # a rejected try: only the last one is the answer
            reasoning.clear()
            by_index.clear()
            continue
        d = c["choices"][0]["delta"]
        content.append(d.get("content") or "")
        reasoning.append(d.get("reasoning_content") or "")
        for tc in d.get("tool_calls") or []:       # streamed calls arrive in pieces: merge them by index
            cur = by_index.setdefault(tc.get("index", len(by_index)), {"id": None, "type": "function",
                                                                        "function": {"name": "", "arguments": ""}})
            cur["id"] = tc.get("id") or cur["id"]
            fn = tc.get("function") or {}
            cur["function"]["name"] += fn.get("name") or ""
            cur["function"]["arguments"] += fn.get("arguments") or ""
        last = c
    calls = [by_index[i] for i in sorted(by_index)]
    if last["choices"][0]["finish_reason"] != "tool_calls":
        calls = [c for c in calls if _is_json(c["function"]["arguments"])]   # a call the output ended inside
    msg = {"role": "assistant", "content": "".join(content) or None}
    if "".join(reasoning):
        msg["reasoning_content"] = "".join(reasoning)
    if calls:
        msg["tool_calls"] = calls
    out = {"id": last["id"], "object": "chat.completion", "created": last["created"], "model": last["model"],
           "choices": [{"index": 0, "message": msg, "finish_reason": last["choices"][0]["finish_reason"]}],
           "usage": last["usage"]}
    if last.get("timings"):
        out["timings"] = last["timings"]
    if last.get("structured_output"):
        out["structured_output"] = last["structured_output"]
    return out


STRUCTURED_RETRIES = 2              # extra tries after an answer that fails its JSON check
STRUCTURED_RETRIES_MAX = 5


def structured_retries(req: dict) -> int:
    """The request's `structured_retries` (0..5, default 2); ValueError for anything else."""
    n = req.get("structured_retries", STRUCTURED_RETRIES)
    if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= STRUCTURED_RETRIES_MAX:
        raise ValueError(f"structured_retries: a whole number 0..{STRUCTURED_RETRIES_MAX}, not {n!r}")
    return n


def retry_messages(messages: list[dict], bad: str, error: str, repair: Repair | None = None) -> list[dict]:
    """The conversation plus one user message for the next try: the error and where the answer went wrong. The
    rejected text itself is never sent back, so a model that looped is not shown its loop again."""
    return [*messages, {"role": "user", "content": (repair or build_repair(error, bad)).message}]


def repair_note(repair: Repair) -> str:
    """Where a rejected answer broke and what it repeated, in a few words for the server window."""
    note = f"at {repair.path}"
    if repair.repeat:
        note += f", {repair.repeat['kind']} written {repair.repeat['count']} times"
    return note


def repeat_facts(repair: Repair) -> dict | None:
    """The worst repetition of a rejected answer, without the list of every position."""
    r = repair.repeat
    if not r:
        return None
    facts = {k: r[k] for k in ("text", "count", "path", "kind", "total")}
    if r["positions"]:
        facts["first"], facts["last"] = r["positions"][0], r["positions"][-1]
    return facts


def _chunk_of(base: dict, delta: dict, **extra) -> dict:
    return {"id": base["id"], "object": base["object"], "created": base["created"], "model": base["model"],
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}], **extra}


def structured_retry_chunks(start, validator, retries: int, note=None):
    """Hold each try's answer, validate it, and on a rejected answer start again with the error fed back (at most
    `retries` more times). `start(feedback)` gives a try's chunks: feedback is None first, then (rejected text, error,
    repair). Only the answer is held; reasoning chunks go to the client as they arrive. A rejected try is announced by
    a chunk with an empty delta and `structured_output.discarded`; the final chunk carries only the validated answer
    and says how many tries it took (`structured_output`). Generation itself is not constrained.
    `note(kind, data, text)` is told each try's outcome ("attempt") and each repair message ("repair")."""
    from serve.api.structured import StructuredOutputError
    feedback, attempt, first, discarded = None, 0, None, []

    def stamp(chunk):                          # every chunk of the stream carries the first try's id and time
        return chunk if chunk["id"] == first["id"] else {**chunk, "id": first["id"], "created": first["created"]}

    while True:
        attempt += 1
        chunks = start(feedback)
        held, heartbeat = [], time.monotonic()
        try:
            for chunk in chunks:
                if chunk is not None:
                    delta = chunk["choices"][0]["delta"]
                    if first is None:
                        first = chunk
                        held.append(chunk)
                        yield chunk                                       # the role chunk opens the stream
                    elif delta.get("reasoning_content"):
                        yield stamp(_chunk_of(chunk, {"reasoning_content": delta["reasoning_content"]}))
                        rest = {k: v for k, v in delta.items() if k != "reasoning_content"}
                        if rest:
                            held.append({**chunk, "choices": [{**chunk["choices"][0], "delta": rest}]})
                    else:
                        held.append(chunk)
                if chunk is None or time.monotonic() - heartbeat >= 1:
                    heartbeat = time.monotonic()
                    yield None
        finally:
            chunks.close()
        result = openai_collect(held)
        choice = result["choices"][0]
        reply = choice["message"]["content"] or ""
        try:
            content = validated_json(reply, validator, choice["finish_reason"])
        except StructuredOutputError as e:
            retrying = attempt <= retries and choice["finish_reason"] == "stop"
            info = {"attempt": attempt, "ok": False, "error": str(e), "finish_reason": choice["finish_reason"],
                    "discarded": True, "retrying": retrying}
            if not retrying:
                if note:
                    note("attempt", info, "")
                yield stamp(_chunk_of(first, {}, structured_output={"attempt": attempt, "discarded": True,
                                                                    "reason": str(e), "retrying": False}))
                raise StructuredOutputError(f"{e} (after {attempt} tr{'y' if attempt == 1 else 'ies'})") from None
            repair = build_repair(str(e), reply)
            print(f"[server] structured output try {attempt} rejected ({e}; {repair_note(repair)}); trying again",
                  flush=True)
            if note:
                note("attempt", info, "")
                note("repair", {"attempt": attempt, "path": repair.path, "cut": repair.cut,
                                "valid_json": repair.valid_json, "repeated": repeat_facts(repair)}, repair.message)
            discarded.append({"attempt": attempt, "reason": str(e)})
            feedback = (reply, str(e), repair)
            yield stamp(_chunk_of(first, {}, structured_output={"attempt": attempt, "discarded": True,
                                                                "reason": str(e), "retrying": True}))
            continue
        if note:
            note("attempt", {"attempt": attempt, "ok": True, "error": None, "finish_reason": choice["finish_reason"],
                             "discarded": False, "retrying": False}, "")
        yield stamp(_chunk_of(held[-1], {"content": content}))
        facts = {"validated": True, "constrained": False, "attempts": attempt}
        if discarded:
            facts["discarded"] = discarded
        yield stamp({**held[-1], "structured_output": facts})
        return
