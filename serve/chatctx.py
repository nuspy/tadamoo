"""The chat page's context policies (POST /api/chat/context): count a conversation with the server's own tokenizer and
template, and when it outgrows the context either stop, drop the oldest turns, or compact the middle turns into one
summary written by the model. The system prompt and the first user message always stay at the head, untouched, and
the newest turns stay verbatim."""
from __future__ import annotations

import threading

from serve.frontend import images_of, openai_to_messages
from serve.service import CTX_SLACK

POLICIES = ("stop", "rolling", "compact")
DEFAULT_RESERVE = 1024           # room kept for the answer when the request leaves max_tokens open
IMAGE_TOKENS = 1280              # an image's tokens before it is encoded: a conservative stand-in
COMPACT_THRESHOLD = 0.8          # compact when the conversation passes this share of the context
KEEP_TURNS = 2                   # the newest turns compaction keeps verbatim
SUMMARY_MAX_TOKENS = 1536
SUMMARY_PREFIX = "[Summary of the earlier conversation, compacted to save context]\n"
SUMMARY_PROMPT = (
    "You compact the middle of a conversation so it can go on in less context. Write one summary that replaces "
    "the messages you are given.\n"
    "- Keep every fact, figure, name, requirement and preference the user stated, and every decision with its "
    "reason.\n"
    "- Keep open questions, unfinished tasks and promised follow-ups.\n"
    "- Copy code, commands, file paths, identifiers, URLs, numbers and error messages verbatim.\n"
    "- Drop greetings, repetition and abandoned attempts, unless an attempt explains a decision.\n"
    "- Write in the conversation's own language, as short notes under these headings: Facts, Decisions, "
    "Open questions, Code and identifiers. Leave out a heading with nothing under it.\n"
    "- Do not answer the conversation, do not add anything that is not in it.")


def _role(m) -> str:
    return m.get("role") if isinstance(m, dict) else ""


def head_of(messages: list[dict]) -> list[int]:
    """The indices that never move: a leading system message and the first user message."""
    head = [0] if messages and _role(messages[0]) in ("system", "developer") else []
    first_user = next((i for i, m in enumerate(messages) if _role(m) == "user"), None)
    if first_user is not None:
        head.append(first_user)
    return head


def turns_after(messages: list[dict], start: int) -> list[list[int]]:
    """The messages from `start` on, grouped in turns: a user message and the replies that follow it."""
    turns: list[list[int]] = []
    for i in range(start, len(messages)):
        if _role(messages[i]) == "user" or not turns:
            turns.append([i])
        else:
            turns[-1].append(i)
    return turns


class Counter:
    """Prompt tokens of a conversation as the server would render it for the request's reasoning setting."""

    def __init__(self, svc, req: dict):
        self.svc = svc
        self.extra = {k: req[k] for k in ("reasoning_effort", "chat_template_kwargs") if k in req}

    def __call__(self, messages: list[dict]) -> int:
        msgs, tools, kw = openai_to_messages({"messages": messages, **self.extra})
        images = images_of(msgs)
        return len(self.svc.encode_prompt(msgs, tools, kw)) + IMAGE_TOKENS * len(images)


def _limit(svc) -> int:
    ctx = int(getattr(svc.engine, "max_context", 0) or 0)
    return ctx if ctx > 0 else int(getattr(svc.engine, "known_ctx", 0) or 0)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in ("text", "input_text"):
                out.append(str(p.get("text", "")))
            elif isinstance(p, dict):
                out.append("[image]")
        return "\n".join(out)
    return ""


def transcript(messages: list[dict]) -> str:
    names = {"user": "USER", "assistant": "ASSISTANT", "system": "SYSTEM", "tool": "TOOL"}
    return "\n\n".join(f"{names.get(_role(m), str(_role(m)).upper())}: {_text(m.get('content'))}" for m in messages)


def summarize(svc, middle: list[dict], limit: int, count) -> tuple[str, int]:
    """(summary, messages left out because even the transcript did not fit) from one model call."""
    dropped, room = 0, min(SUMMARY_MAX_TOKENS, limit // 4)
    while True:
        prompt = [{"role": "system", "content": SUMMARY_PROMPT},
                  {"role": "user", "content": "The messages to compact:\n\n" + transcript(middle[dropped:])}]
        n = count(prompt)
        if n + room + CTX_SLACK <= limit or dropped >= len(middle) - 1:
            break
        dropped += 1
    svc.load()
    msgs, _, kw = openai_to_messages({"messages": prompt, "reasoning_effort": "none"})
    ids, thinking, max_new = svc.prepare(msgs, None, kw, min(room, max(1, limit - n - CTX_SLACK)))
    parts = []
    for kind, x in svc.run(ids, thinking, None, max_new, {"temperature": 0.3, "top_p": 0.9}, threading.Event()):
        if kind == "event" and x.kind == "content":
            parts.append(x.text or "")
    return "".join(parts).strip(), dropped


def _number(req, key, default, lo, hi, kind=float):
    v = req.get(key, default)
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ValueError(f"{key}: a number from {lo} to {hi}, not {v!r}")
    return kind(v)


def fit(svc, req: dict) -> dict:
    """Apply the request's policy to its messages; see the module docstring. ValueError for a bad request."""
    if not isinstance(req, dict):
        raise ValueError("send a JSON object")
    messages = req.get("messages")
    if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
        raise ValueError("messages: a non-empty list of chat messages")
    policy = req.get("policy", "stop")
    if policy not in POLICIES:
        raise ValueError(f"policy: one of {', '.join(POLICIES)}, not {policy!r}")
    limit = _limit(svc)
    if limit <= 0:
        raise ValueError("the model is not loaded yet: its context is unknown")
    reserve = _number(req, "max_tokens", 0, 0, 1 << 31, int) or DEFAULT_RESERVE
    threshold = _number(req, "threshold", COMPACT_THRESHOLD, 0.3, 1.0)
    keep_turns = _number(req, "keep_turns", KEEP_TURNS, 1, 64, int)
    count = Counter(svc, req)
    budget = limit - CTX_SLACK - reserve
    used = count(messages)
    out = {"limit": limit, "reserve": reserve, "used": used, "policy": policy, "action": "none",
           "keep": list(range(len(messages)))}
    if policy == "stop" or (used <= budget and (policy != "compact" or used <= threshold * limit)):
        if used > budget:
            out["action"] = "over"
        return out
    head = head_of(messages)
    start = (max(head) + 1) if head else 0
    turns = turns_after(messages, start)
    if policy == "rolling":
        keep_turns_idx = list(turns)
        while keep_turns_idx:
            keep = head + [i for t in keep_turns_idx for i in t]
            n = count([messages[i] for i in keep])
            if n <= budget:
                out.update(action="dropped" if len(keep) < len(messages) else "none", keep=keep, used=n,
                           dropped=[i for i in range(len(messages)) if i not in keep])
                return out
            if len(keep_turns_idx) == 1:
                break
            keep_turns_idx = keep_turns_idx[1:]
        out["action"] = "over"
        return out
    # compact: the middle turns become one summary; the newest `keep_turns` turns stay as they are
    tail = turns[-keep_turns:] if len(turns) > keep_turns else turns[-1:]
    middle_turns = turns[:len(turns) - len(tail)]
    middle = [i for t in middle_turns for i in t]
    if not middle:
        out["action"] = "over" if used > budget else "none"
        return out
    text, left_out = summarize(svc, [messages[i] for i in middle], limit, count)
    if not text:
        raise ValueError("the model wrote an empty summary; try again or use the rolling window")
    summary = {"role": "user", "content": SUMMARY_PREFIX + text}
    tail_idx = [i for t in tail for i in t]
    new = [messages[i] for i in head] + [summary] + [messages[i] for i in tail_idx]
    n = count(new)
    out.update(action="compacted" if n <= budget else "over", used=n, used_before=used,
               keep=head + tail_idx, summary={"message": summary, "text": text, "replaces": middle,
                                               "not_summarized": middle[:left_out]})
    return out
