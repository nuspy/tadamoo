"""The chat page's context policies (POST /api/chat/context): count a conversation with the server's own tokenizer and
template, and when it outgrows the context either stop, drop the oldest turns, or compact the middle turns into one
summary written by the model. The system prompt and the first user message always stay at the head, untouched, and
the newest turns stay verbatim."""
from __future__ import annotations

import threading

from harness import compacted, digest, side_uses
from serve.frontend import images_of, openai_to_messages
from serve.service import CTX_SLACK

POLICIES = ("stop", "rolling", "compact")
DEFAULT_RESERVE = 1024           # room kept for the answer when the request leaves max_tokens open
IMAGE_TOKENS = 1280              # an image's tokens before it is encoded: a conservative stand-in
COMPACT_THRESHOLD = 0.8          # compact when the conversation passes this share of the context
KEEP_TURNS = 2                   # the newest turns compaction keeps verbatim
RECORD = "the conversation record, which the person can open"      # where the exact text of a compacted stretch is kept
SUMMARY_PROMPT = (
    "You compact the middle of a conversation so it can go on in less context. Write one summary that replaces "
    "the messages you are given.\n"
    "- Keep every fact, figure, name, requirement and preference the user stated, and every decision with its "
    "reason.\n"
    "- Keep open questions, unfinished tasks and promised follow-ups.\n"
    "- Copy code, commands, file paths, identifiers, URLs, numbers and error messages verbatim.\n"
    "- Drop greetings, repetition and abandoned attempts, unless an attempt explains a decision.\n"
    "- Compress only what the next turn no longer needs; what it may still need stays as written.\n"
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


def _generate(svc, prompt: list[dict], room: int, limit: int, count, side: bool, strict: bool = False, use: str = "") -> str:
    """One model call over `prompt`, in the technical session when `side`, else in an ordinary slot. With `strict`, an answer cut off
    raises digest.Truncated."""
    svc.load()
    msgs, _, kw = openai_to_messages({"messages": prompt, "reasoning_effort": "none"})
    ids, thinking, max_new = svc.prepare(msgs, None, kw, min(room, max(1, limit - count(prompt) - CTX_SLACK)))
    sampling = {"temperature": 0.3, "top_p": 0.9, **({"cache_slot": "side_only" if strict else "side", "side_use": use} if side else {})}
    parts = []
    try:
        for kind, x in svc.run(ids, thinking, None, max_new, sampling, threading.Event()):
            if kind == "event" and x.kind == "content":
                parts.append(x.text or "")
            elif kind == "done" and strict and x.get("finish") == "length":
                raise digest.Truncated()
    except ValueError as exc:
        if strict and "technical session" in str(exc):             # refused as too large for the session: the digest halves the part
            raise digest.Truncated() from None
        raise
    return "".join(parts).strip()


def summarize(svc, middle: list[dict], limit: int, count) -> tuple[str, int]:
    """(summary, messages left out because even the transcript did not fit). With a technical session the text is written there, in parts
    when it is larger than the session (harness/digest.py); else, or if that fails, in one call of an ordinary slot."""
    info = svc.side_slot_info() if hasattr(svc, "side_slot_info") and side_uses.enabled("chat_summary") else None
    prompt_text = side_uses.system("chat_summary", SUMMARY_PROMPT)
    answer = int(side_uses.param("chat_summary", "answer_tokens"))
    if info:
        window, room = int(info["context"]), min(answer, int(info["context"]) // 4)

        def write(instructions: str, text: str) -> str:
            prompt = [{"role": "system", "content": instructions}, {"role": "user", "content": "The messages to compact:\n\n" + text}]
            part = digest.is_part(instructions)                 # the notes of a part are shorter than the final summary
            return _generate(svc, prompt, min(room, digest.ANSWER_TOKENS) if part else room, window, count, True, True, "chat_summary")
        try:
            with side_uses.job("chat_summary", "summary") as job:
                text = digest.digest(write, prompt_text, transcript(middle), window, room, int(side_uses.param("chat_summary", "chunk_tokens")), job)
            if text:
                return text, 0
        except digest.Stopped:
            raise ValueError("The summary was stopped.") from None
        except Exception:
            pass                                        # the session could not do it: an ordinary slot tries
    dropped, room = 0, min(answer, limit // 4)
    while True:
        prompt = [{"role": "system", "content": prompt_text},
                  {"role": "user", "content": "The messages to compact:\n\n" + transcript(middle[dropped:])}]
        n = count(prompt)
        if n + room + CTX_SLACK <= limit or dropped >= len(middle) - 1:
            break
        dropped += 1
    return _generate(svc, prompt, room, limit, count, False), dropped


ABSTRACT_PROMPT = (
    "You write the memory of one of your own earlier replies, so the conversation can go on without the reply itself. Write a short abstract "
    "in the reply's own language.\n"
    "- Say what the reply answered, decided, produced or asked of the person. Name files, functions, figures, names and errors exactly.\n"
    "- Leave out the explanations, the step-by-step instructions and the code: say what the code does and what it is called, not its lines.\n"
    "- Keep every question the reply asked and every promise it made.\n"
    "- Do not add anything that is not in the reply and do not answer it.")
ABSTRACT_MAX_SHARE = 0.5          # an abstract longer than this share of its reply is not worth the change


def abstract_reply(svc, text: str, limit: int, count) -> dict:
    """The abstract of one reply written in the technical session (it needs one): {"text": the record to send in its place,
    "mark": ...}, or {"text": ""} when the reply is short or the abstract would not be much shorter. Shown to the model in place of the
    reply; the reply itself stays in the person's conversation."""
    info = svc.side_slot_info() if hasattr(svc, "side_slot_info") and side_uses.enabled("chat_abstract") else None
    if len(text) < side_uses.param("chat_abstract", "min_chars") or not info:          # without the technical session the call would take the conversation's own slot
        return {"text": ""}
    window = int(info["context"])
    room = min(int(side_uses.param("chat_summary", "answer_tokens")), window // 4)
    body = text if digest.fits(text, window, room) else text[:digest.part_chars(window, room)]        # a reply larger than the session: its start is abstracted
    prompt = [{"role": "system", "content": side_uses.system("chat_abstract", ABSTRACT_PROMPT)}, {"role": "user", "content": "The reply:\n\n" + body}]
    written = _generate(svc, prompt, room, window, count, True, False, "chat_abstract")
    if not written or len(written) > ABSTRACT_MAX_SHARE * len(text):
        return {"text": ""}
    mark = compacted.stamp(1, 1, count([{"role": "user", "content": text}]), count([{"role": "user", "content": written}]), RECORD)
    return {"text": compacted.header(mark, rules=False) + "\n" + written, "mark": mark}


def originals_of(message: dict) -> list[dict]:
    """The messages a compacted stretch stands for, when the page sent them along with it; else the message itself. Read again, a stretch is
    summarised from its own text and not from the notes that replaced it."""
    found = message.get("originals") if isinstance(message, dict) else None
    if isinstance(found, list) and found and all(isinstance(o, dict) and o.get("role") in ("user", "assistant") for o in found):
        return found
    return [message]


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
    policy = req.get("policy", "compact")
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

    def rolling() -> dict:
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

    if policy == "rolling":
        return rolling()
    # compact: the middle turns become one summary; the newest `keep_turns` turns stay as they are
    tail = turns[-keep_turns:] if len(turns) > keep_turns else turns[-1:]
    middle_turns = turns[:len(turns) - len(tail)]
    middle = [i for t in middle_turns for i in t]
    if not middle:
        out["action"] = "over" if used > budget else "none"
        return out
    source = [[o for o in originals_of(messages[i])] for i in middle]       # a stretch compacted before is read again from its own text
    flat = [o for group in source for o in group]
    try:
        text, left_out = summarize(svc, flat, limit, count)
        if not text:
            raise ValueError("the model wrote an empty summary")
    except Exception as e:
        # the chat goes on: the oldest turns are dropped instead and the reason is reported
        out["notice"] = f"The summary could not be written ({e}); the oldest messages were left out instead."
        return rolling()
    covered = next((k for k in range(len(middle) + 1) if sum(len(g) for g in source[:k]) > left_out), len(middle))
    info = compacted.stamp(compacted.next_generation([i for i in (compacted.info_of(messages[k]) for k in middle) if i]), len(flat),
                           count(flat), count([{"role": "user", "content": text}]), RECORD)
    summary = {"role": "user", "content": compacted.header(info) + "\n" + text}
    tail_idx = [i for t in tail for i in t]
    new = [messages[i] for i in head] + [summary] + [messages[i] for i in tail_idx]
    n = count(new)
    if n > budget:
        out["notice"] = "The summary did not make the conversation fit; the oldest messages were left out instead."
        return rolling()
    out.update(action="compacted", used=n, used_before=used,
               keep=head + tail_idx, summary={"message": summary, "text": text, "replaces": middle, "mark": info,
                                               "not_summarized": middle[:covered]})
    return out
