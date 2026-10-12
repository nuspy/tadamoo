"""OpenAI chat completion chunks, their non-streaming collection and structured-output buffering."""
from __future__ import annotations

import json
import time
import uuid

from serve.frontend import Event
from serve.service import Service
from serve.api.structured import validated_json


def openai_chunks(svc: Service, req: dict, ids, thinking, tools, max_new, cancel):
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())
    model = svc.model

    def chunk(delta, finish=None):
        return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    yield chunk({"role": "assistant", "content": ""})
    calls = 0
    streamed = {}                                  # tool call id -> index, for calls sent piece by piece
    finished = set()                               # ... and the ones whose final tool_call came
    for kind, x in svc.run(ids, thinking, tools, max_new, req, cancel):
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
RETRY_TEXT = ("Your previous answer was rejected: {error}. Answer again with only the corrected JSON object that "
              "matches the required JSON Schema: no Markdown, no code fences, no text outside the JSON.")


def structured_retries(req: dict) -> int:
    """The request's `structured_retries` (0..5, default 2); ValueError for anything else."""
    n = req.get("structured_retries", STRUCTURED_RETRIES)
    if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= STRUCTURED_RETRIES_MAX:
        raise ValueError(f"structured_retries: a whole number 0..{STRUCTURED_RETRIES_MAX}, not {n!r}")
    return n


def retry_messages(messages: list[dict], bad: str, error: str) -> list[dict]:
    """The conversation with the rejected answer and the reason, for the next try."""
    return [*messages, {"role": "assistant", "content": bad},
            {"role": "user", "content": RETRY_TEXT.format(error=error)}]


def structured_retry_chunks(start, validator, retries: int):
    """Buffer each try, validate it, and on a rejected answer start again with the error fed back (at most `retries`
    more times). `start(feedback)` gives a try's chunks: feedback is None first, then (rejected text, error). The
    final chunk says how many tries it took (`structured_output`); generation itself is not constrained."""
    from serve.api.structured import StructuredOutputError
    feedback, attempt = None, 0
    while True:
        attempt += 1
        chunks = start(feedback)
        buffered, heartbeat = [], time.monotonic()
        try:
            for chunk in chunks:
                if chunk is not None:
                    buffered.append(chunk)
                if chunk is None or time.monotonic() - heartbeat >= 1:
                    heartbeat = time.monotonic()
                    yield None
        finally:
            chunks.close()
        result = openai_collect(buffered)
        choice = result["choices"][0]
        try:
            content = validated_json(choice["message"]["content"], validator, choice["finish_reason"])
        except StructuredOutputError as e:
            if attempt > retries or choice["finish_reason"] != "stop":
                raise StructuredOutputError(f"{e} (after {attempt} tr{'y' if attempt == 1 else 'ies'})") from None
            print(f"[server] structured output try {attempt} rejected ({e}); trying again", flush=True)
            feedback = (choice["message"]["content"] or "", str(e))
            continue
        yield buffered[0]
        delta = {"content": content}
        if choice["message"].get("reasoning_content"):
            delta["reasoning_content"] = choice["message"]["reasoning_content"]
        yield {**buffered[0], "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        yield {**buffered[-1], "structured_output": {"validated": True, "constrained": False, "attempts": attempt}}
        return

