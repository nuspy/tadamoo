"""The `long_answer` field of a chat completion: the text is written in segments by the loaded model (serve/longform.py).

Progress goes in the stream as chunks with an empty delta and a `long_answer` event; the text goes as ordinary content deltas, one
checked segment at a time. The last chunk carries the totals and every warning.
"""
from __future__ import annotations

import threading
import time
import uuid

from serve.api.grammar import REQUEST_KEY as GRAMMAR_KEY
from serve.api.logit_bias import REQUEST_KEY as BIAS_KEY, add_end_turn, checked as checked_bias
from serve.api.openai import openai_chunks, openai_collect
from serve.frontend import openai_to_messages
from serve.longform import LongAnswer, LongformError, Spec
from serve.prompt import primed
from serve.service import Service
from serve.util.text import IM_END

SAMPLING = ("temperature", "top_p", "top_k", "min_p", "repetition_penalty", "presence_penalty", "frequency_penalty", "penalty_last_n",
            "seed")        # the request's own sampling, passed on to every call of the run


class ServiceClient:
    """The `chat_completion` of harness.client.BasaltClient, answered in this process by the loaded model."""

    def __init__(self, svc: Service, sampling: dict, cancel: threading.Event) -> None:
        self.svc, self.sampling, self.cancel = svc, sampling, cancel

    def chat_completion(self, messages, tools=None, tool_choice=None, temperature=None, max_tokens=None, timeout=0.0,
                        response_format=None, extra=None, on_delta=None):
        svc = self.svc
        sub = {**self.sampling, **(extra or {}), "messages": messages, "max_tokens": max_tokens}
        if temperature is not None:
            sub["temperature"] = temperature
        msgs, _tools, kw = openai_to_messages(sub)
        svc.load()
        add_end_turn(sub, svc.tok.encode(IM_END, parse_special=True))
        sub[BIAS_KEY] = checked_bias(sub, svc.engine)
        sub[GRAMMAR_KEY] = svc.grammar_for(None, None, None, None)
        ids, thinking, max_new = svc.prepare(msgs, None, kw, int(max_tokens or 0))
        chunks = primed(openai_chunks(svc, sub, ids, thinking, None, max_new, self.cancel), ids)
        got = []
        try:
            for c in chunks:
                if c is None:
                    continue
                got.append(c)
                delta = c["choices"][0]["delta"]
                if on_delta is not None:
                    if delta.get("reasoning_content"):
                        on_delta("reasoning", delta["reasoning_content"])
                    if delta.get("content"):
                        on_delta("content", delta["content"])
        finally:
            chunks.close()
        return openai_collect(got)


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(p, dict) and p.get("type") not in ("text", None) for p in content):
            raise LongformError("long_answer: images are not supported")
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def long_request(req: dict):
    """(spec, system text, earlier turns) from a request; LongformError (a 400) for one the mode cannot take."""
    if req.get("tools") or req.get("response_format"):
        raise LongformError("long_answer does not take tools or response_format")
    raw = req.get("messages")
    if not isinstance(raw, list) or not raw:
        raise LongformError("messages: a non-empty list")
    system = "\n\n".join(text_of(m.get("content")) for m in raw if isinstance(m, dict) and m.get("role") in ("system", "developer"))
    turns = [{"role": m["role"], "content": text_of(m.get("content"))} for m in raw
             if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    if not turns or turns[-1]["role"] != "user":
        raise LongformError("long_answer: the last message must be the user's")
    spec = Spec.from_request(req["long_answer"], turns[-1]["content"])
    return spec, system, turns[:-1]


def long_answer_chunks(svc: Service, req: dict, spec: Spec, system: str, prior: list, cancel: threading.Event):
    """OpenAI chunks for a long answer: content as segments pass their checks, progress events with an empty delta, a last chunk with
    the finish reason, the usage and `long_answer` (the totals and the warnings)."""
    cid, created, model = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time()), svc.model
    svc.load()
    spec.context_tokens = spec.context_tokens or int(getattr(svc.engine, "max_context", 0) or 0)

    def chunk(delta, finish=None, **extra):
        return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}
    sampling = {k: req[k] for k in SAMPLING if req.get(k) is not None}
    run = LongAnswer(ServiceClient(svc, sampling, cancel), spec, prior=prior, system=system, cancel=cancel, sampling={})
    yield chunk({"role": "assistant", "content": ""})
    events = run.run()
    try:
        for ev in events:
            if ev is None:
                yield None
            elif ev["event"] == "delta":
                yield chunk({"content": ev["text"]})
            elif ev["event"] == "done":
                use = ev["usage"]
                last = chunk({}, ev["finish"], long_answer=ev)
                last["usage"] = {"prompt_tokens": use["prompt_tokens"], "completion_tokens": use["completion_tokens"],
                                 "total_tokens": use["prompt_tokens"] + use["completion_tokens"],
                                 "prompt_tokens_details": {"cached_tokens": use["cached_tokens"]}}
                yield last
            else:
                yield chunk({}, None, long_answer=ev)
    except GeneratorExit:
        cancel.set()                         # a consumer that left stops the call in flight
        raise
    finally:
        events.close()
