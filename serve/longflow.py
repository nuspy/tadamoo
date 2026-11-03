"""The proactive long-answer flow (docs/longform.md): the harness decides, not the model.

1. A short call estimates how many words the answer needs; an estimate over the threshold goes on.
2. A short call plans the text in parts of about 2000 words and hands each part its own task (`instruction`).
3. The parts are written one at a time by `LongAnswer` (protected content, per-part checks, cut continuation), each with its own task.
4. A short call checks the assembled text in pieces and lists fixes, each placed by a quote (`anchor`). Plain code finds the quote;
   a fix whose quote is not found, or is found twice, is reported and never guessed. The passage is written again by a focused call,
   its protected content and length are checked, and then it is put in. At most two rounds.

The short calls (1, 2, 4) carry the side session's request fields (`serve.longform._side`); the parts and the rewritten passages
are written by the conversation's own session. Everything else here is a pure function.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

from serve import longform as lf
from serve.longform import Segment

log = logging.getLogger("tadamoo.longflow")

ESTIMATE_RANGE = lf.TARGET_WORDS_RANGE
ASKED_BAND = (0.8, 1.25)            # a length the person stated bounds the estimate to this share of it
HINT_BAND = (0.25, 4.0)             # without one, the estimate stays within this share of the rule-based words
ESTIMATE_TOKENS = 200
PLAN_ATTEMPTS = 2
HEADING_CHARS, GOAL_CHARS, POINT_CHARS, POINTS_MAX, INSTRUCTION_CHARS, TITLE_CHARS = 120, 400, 200, 8, 1500, 160
ROUNDS = 2                          # verification rounds at most
PIECE_WORDS = 3000                  # the text a verification call sees at once
VERIFY_TOKENS = 1500
FIXES_PER_CALL, FIXES_PER_ROUND = 6, 10
ANCHOR_MIN_WORDS = 4
ANCHOR_CHARS, FIX_INSTRUCTION_CHARS = 300, 600
REPLACE_BAND = (0.4, 3.0)           # a rewritten passage keeps this share of the words of the one it replaces
REPLACE_SLACK = (5, 60)             # ... and a passage this short may be this many words at least and at most
INSERT_WORDS = (15, 450)
CHUNK_FLOOR = 0.5                   # a part never falls under this share of its planned words after a fix
CONTEXT_WORDS = 70                  # the text before and after a passage, shown to the writer of the fix

S = {"type": "string"}
ESTIMATE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["words", "reason"],
                   "properties": {"words": {"type": "integer"}, "reason": S}}
PLAN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["title", "chunks"], "properties": {
    "title": S,
    "chunks": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "heading", "goal", "target_words", "key_points", "instruction"],
        "properties": {"id": {"type": "integer"}, "heading": S, "goal": S, "target_words": {"type": "integer"},
                       "key_points": {"type": "array", "items": S}, "instruction": S}}}}}
VERIFY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["fixes"], "properties": {
    "fixes": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["op", "chunk", "anchor", "instruction"],
        "properties": {"op": {"type": "string", "enum": ["replace", "insert_after", "delete"]}, "chunk": {"type": "integer"},
                       "anchor": S, "instruction": S}}}}}


def _format(name: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}


ESTIMATE_FORMAT, PLAN_FORMAT, VERIFY_FORMAT = _format("answer_length", ESTIMATE_SCHEMA), _format("answer_plan", PLAN_SCHEMA), _format("answer_review", VERIFY_SCHEMA)

ESTIMATE_PROMPT = ("You estimate the length of an answer. Do not answer the request. Fill the form: words = how many words a complete, good "
                   "answer to the request has; reason = one short sentence, in the language of the request.")
PLAN_PROMPT = (
    "You plan a long text that will be written in separate parts, each by a writer who sees only the plan, the end of the previous part and "
    "the task you give for that part. Do not write the text. Fill the form. The parts follow each other without overlap and together do "
    "the whole request. For each part: id (1, 2, ...), heading, goal (what the part achieves), target_words (700 to 2500), key_points, and "
    "instruction = the exact task for the writer of that part: what to write, the points to cover, what the previous part already said and "
    "must not be repeated, what the next part takes over. Write free text in the language of the request.")
VERIFY_PROMPT = (
    "You check a long text that was written in parts against the request and the plan. Is it correct and complete? Look for: facts or "
    "claims that are wrong or contradict each other, points of the plan that are missing, a part that repeats another, text that "
    "is not part of the answer (remarks about the task). Report only real problems; an empty list is a good answer. Each fix gives op "
    "(replace = write again the passage that holds the anchor, insert_after = add a passage after the one that holds the anchor, delete = "
    "remove the passage that holds the anchor), chunk (the part number), anchor (6 to 12 words copied exactly from the text, found once in "
    "that part, inside the passage), instruction (what to write, or why to remove). At most six fixes. Write instructions in the language "
    "of the text.")
FIX_RULES = ("You edit one passage of a longer text. Write only the passage asked, in the language of the text, with no preface and no remark "
             "about the task. Copy every marker of the form ⟦NAME12⟧ unchanged, once each, in the same place. Keep the style and the Markdown "
             "structure of the text around it.")


class NotLong(lf.LongformError):
    """The estimate says the answer fits one turn: the harness leaves the request to the model."""

    def __init__(self, words: int) -> None:
        super().__init__(f"about {words} words fit one reply")
        self.words = words


# -- the estimate ---------------------------------------------------------------------------------------------------------

def json_object(text: str) -> Optional[Dict[str, Any]]:
    """The first JSON object in a reply, or None."""
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text or ""):
        try:
            obj, _ = decoder.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _line(value: Any, cap: int) -> str:
    return " ".join(str(value or "").split())[:cap]


def _block(value: Any, cap: int) -> str:
    lines = [" ".join(ln.split()) for ln in str(value or "").strip().split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:cap]


def estimate_messages(request: str) -> List[Dict[str, Any]]:
    return [{"role": "system", "content": ESTIMATE_PROMPT}, {"role": "user", "content": "Request:\n" + request[:4000]}]


def parse_estimate(content: str, *, asked: int = 0, hint: int = 0) -> Optional[Dict[str, Any]]:
    """{words, model_words, bounded, reason} from an estimate reply, or None when it holds no usable number. The words are clamped to the
    range of a text; a length the person stated, or else the rule-based words, bound the estimate (a model that answers 200 for a book
    is not believed)."""
    obj = json_object(content)
    value = obj.get("words") if obj else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    raw = int(value)
    low, high = ESTIMATE_RANGE
    if asked:
        low, high = max(low, int(asked * ASKED_BAND[0])), min(high, int(math.ceil(asked * ASKED_BAND[1])))
    elif hint:
        low, high = max(low, int(hint * HINT_BAND[0])), min(high, int(hint * HINT_BAND[1]))
    words = min(max(raw, low), max(low, high))
    return {"words": words, "model_words": raw, "bounded": words != raw, "reason": _line(obj.get("reason"), 200)}


# -- the plan -------------------------------------------------------------------------------------------------------------

def chunk_bounds(words: int) -> Tuple[int, int]:
    """The fewest and the most parts a text of `words` words may be planned in (parts of 700 to 2500 words)."""
    low = max(1, math.ceil(words / lf.CHUNK_MAX))
    return low, min(lf.MAX_SEGMENTS, max(low, words // lf.CHUNK_WORDS[0]))


def plan_messages(request: str, words: int, parts: int, language: str = "", style: str = "") -> List[Dict[str, Any]]:
    lines = ["Request:\n" + request[:6000], f"\nPlan this text of about {words} words as exactly {parts} parts of about {round(words / parts)} words each."]
    if language:
        lines.append(f"Language of the text: {lf.LANGUAGES.get(language, language)}.")
    if style:
        lines.append(f"Style: {style}")
    return [{"role": "system", "content": PLAN_PROMPT}, {"role": "user", "content": "\n".join(lines)}]


def parse_plan(content: str, words: int) -> Optional[Tuple[str, List[Segment]]]:
    """(title, parts) from a plan reply, or None when it cannot be used. Caps are applied here: the number of parts is kept to what
    parts of 700 to 2500 words allow, the words of the parts are scaled to the total and each kept within 700 to 2500, every text field
    is cut to its limit, and the parts are numbered in the order they come (the model's own ids are not trusted)."""
    obj = json_object(content)
    rows = obj.get("chunks") if obj else None
    low, high = chunk_bounds(words)
    if not isinstance(rows, list) or not low <= len(rows) <= high or not all(isinstance(r, dict) for r in rows):
        return None
    known = [r.get("target_words") for r in rows]
    known = [int(w) if isinstance(w, (int, float)) and not isinstance(w, bool) and math.isfinite(w) and w > 0 else 0 for w in known]
    total = sum(known)
    parts: List[Segment] = []
    for i, (row, w) in enumerate(zip(rows, known)):
        heading, goal = _line(row.get("heading"), HEADING_CHARS), _line(row.get("goal"), GOAL_CHARS)
        instruction = _block(row.get("instruction"), INSTRUCTION_CHARS) or goal
        if not instruction:
            return None
        size = w * words / total if total and w else words / len(rows)
        points = row.get("key_points") if isinstance(row.get("key_points"), list) else []
        parts.append(Segment(i, heading=heading, goal=goal, points=[_line(p, POINT_CHARS) for p in points[:POINTS_MAX] if _line(p, POINT_CHARS)],
                             words=int(round(min(max(size, lf.CHUNK_WORDS[0]), lf.CHUNK_WORDS[1]))), rows=[(i + 1, heading, True)],
                             instruction=instruction))
    return _line(obj.get("title"), TITLE_CHARS), parts


# -- finding a passage ----------------------------------------------------------------------------------------------------

VARIANTS = {c: family for family in ("'‘’", '"“”', "-‐‑–—") for c in family}     # marks that read the same


def _tolerant(anchor: str) -> "re.Pattern[str]":
    """The anchor as a pattern that ignores the kind of whitespace, of quote mark and of dash."""
    def word(w: str) -> str:
        return "".join(f"[{re.escape(VARIANTS[c])}]" if c in VARIANTS else re.escape(c) for c in w)
    return re.compile(r"\s+".join(word(w) for w in anchor.split()))


def locate(text: str, anchor: str) -> Tuple[Optional[Tuple[int, int]], str]:
    """((start, end) of the only place `anchor` is in `text`, ""), or (None, why): too_short, not_found or ambiguous. Exact first, then
    ignoring whitespace, quote and dash variants. A quote found twice is never guessed."""
    anchor = anchor.strip()
    if len(anchor.split()) < ANCHOR_MIN_WORDS:
        return None, "too_short"
    n = text.count(anchor)
    if n == 1:
        i = text.index(anchor)
        return (i, i + len(anchor)), ""
    if n > 1:
        return None, "ambiguous"
    found = list(_tolerant(anchor).finditer(text))
    if len(found) == 1:
        return (found[0].start(), found[0].end()), ""
    return None, "ambiguous" if found else "not_found"


@dataclass
class Place:
    start: int
    end: int
    kind: str           # text, list, heading, table or protected (code, math, HTML)


def blocks(text: str) -> List[Place]:
    """The blocks of a text: runs of lines between blank lines, a fenced block whole. Each is classed by what it is."""
    out: List[Place] = []
    pos, start, fence, last = 0, -1, "", 0
    for line in text.split("\n"):
        m = lf.FENCE_RE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line.strip()[len(m.group(1)):].strip():
                fence = ""
        elif m:
            fence = m.group(1)
        if line.strip() or fence:
            if start < 0:
                start = pos
            last = pos + len(line)
        elif start >= 0:
            out.append(Place(start, last, ""))
            start = -1
        pos += len(line) + 1
    if start >= 0:
        out.append(Place(start, last, ""))
    for b in out:
        body = text[b.start:b.end]
        first, lines = body.split("\n", 1)[0], body.split("\n")
        b.kind = ("protected" if lf.FENCE_RE.match(first) or first.strip().startswith("$$") or lf.HTML_BLOCK_RE.match(first)
                  else "heading" if lf.HEADING_RE.match(first) and len(lines) == 1
                  else "table" if all(lf.TABLE_RE.match(ln) for ln in lines)
                  else "list" if lf.LIST_RE.match(first) else "text")
    return out


def place_of(text: str, anchor: str) -> Tuple[Optional[Place], str]:
    """The block that holds the anchor, or (None, why): too_short, not_found, ambiguous or spans_blocks."""
    span, why = locate(text, anchor)
    if span is None:
        return None, why
    for b in blocks(text):
        if b.start <= span[0] and span[1] <= b.end:
            return b, ""
    return None, "spans_blocks"


def cut_block(text: str, place: Place) -> str:
    head, tail = text[:place.start].rstrip("\n"), text[place.end:].lstrip("\n")
    return head + ("\n\n" if head and tail else "") + tail


@dataclass
class Fix:
    op: str
    chunk: int
    anchor: str
    instruction: str


def parse_fixes(content: str, parts: List[int]) -> Optional[Tuple[List[Fix], List[Dict[str, Any]]]]:
    """(the usable fixes, the entries that cannot be used with the reason) from a verification reply, or None when it is not readable.
    `parts` are the numbers of the parts the call saw."""
    obj = json_object(content)
    rows = obj.get("fixes") if obj else None
    if not isinstance(rows, list):
        return None
    good: List[Fix] = []
    bad: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        fix = Fix(str(row.get("op") or ""), row.get("chunk") if isinstance(row.get("chunk"), int) and not isinstance(row.get("chunk"), bool) else 0,
                  _line(row.get("anchor"), ANCHOR_CHARS), _line(row.get("instruction"), FIX_INSTRUCTION_CHARS))
        why = ("op" if fix.op not in ("replace", "insert_after", "delete") else "chunk" if fix.chunk not in parts else
               "instruction" if fix.op != "delete" and not fix.instruction else "")
        if why:
            bad.append({"chunk": fix.chunk, "op": fix.op, "anchor": fix.anchor, "instruction": fix.instruction, "reason": "invalid_" + why})
        elif len(good) >= FIXES_PER_CALL:
            bad.append({"chunk": fix.chunk, "op": fix.op, "anchor": fix.anchor, "instruction": fix.instruction, "reason": "limit"})
        else:
            good.append(fix)
    return good, bad


def pieces(sizes: List[int], limit: int = PIECE_WORDS) -> List[List[int]]:
    """Consecutive parts grouped so that each group has at most `limit` words (a part over it stands alone)."""
    out: List[List[int]] = []
    cur: List[int] = []
    words = 0
    for i, w in enumerate(sizes):
        if cur and words + w > limit:
            out.append(cur)
            cur, words = [], 0
        cur.append(i)
        words += w
    if cur:
        out.append(cur)
    return out


# -- the flow -------------------------------------------------------------------------------------------------------------

class LongFlow(lf.LongAnswer):
    """`LongAnswer` for a new text, with the estimate and the plan before it and the check after it. Events: `estimate`, then those of
    `LongAnswer`, then `review` and `fix` for each round, `revised` (the whole text, when a fix was put in) and `done`, which carries
    `applied`, `rounds` and `unapplied` (every fix that was found and not put in, with the reason)."""

    def __init__(self, client: Any, spec: lf.Spec, *, hint_words: int = 0, stated: bool = False, **kw: Any) -> None:
        super().__init__(client, spec, **kw)
        self.hint_words = hint_words or spec.target_words       # the words the rules found
        self.hinted = bool(hint_words)                          # without them the estimate is bound by nothing but the range of a text
        self.stated = stated                                    # the person chose the length: no estimate is asked
        self.applied = 0
        self.rounds = 0
        self.unapplied: List[Dict[str, Any]] = []
        self.plan_parts: List[Segment] = []
        self._language = lf.language_code(spec.language) or lf.request_language(spec.task)

    # -- short calls

    def _short(self, messages: List[Dict[str, Any]], tokens: int, fmt: Dict[str, Any]) -> Iterator[Any]:
        """One short call; returns its text, or None when the call failed (the caller has a fallback). A stop is passed on."""
        try:
            text, _finish = yield from self._call(messages, tokens, side=True, response_format=fmt)
        except lf.Cancelled:
            raise
        except Exception as exc:
            log.info("long answer: a short call failed: %s", type(exc).__name__)
            return None
        return text

    def _estimate(self) -> Iterator[Any]:
        spec = self.spec
        asked = lf.asked_words(spec.task)
        if self.stated:
            found: Optional[Dict[str, Any]] = {"words": spec.target_words, "reason": "", "bounded": False}
            source = "asked"
        else:
            reply = yield from self._short(estimate_messages(spec.task), ESTIMATE_TOKENS, ESTIMATE_FORMAT)
            found = parse_estimate(reply or "", asked=asked, hint=self.hint_words if self.hinted else 0)
            source = "model"
            if found is None:
                found, source = {"words": self.hint_words, "reason": "", "bounded": False}, "rule"
        words = max(ESTIMATE_RANGE[0], min(ESTIMATE_RANGE[1], int(found["words"])))
        long = lf.tokens_for_words(words, self._language) > lf.LONG_MIN_TOKENS
        yield {"event": "estimate", "words": words, "source": source, "reason": found["reason"], "bounded": found["bounded"], "long": long}
        if not long:
            raise NotLong(words)
        return words

    def _plan(self) -> Iterator[Any]:
        spec = self.spec
        if spec.outline:                                        # the request gave the plan: it is followed, and neither the length nor the parts are asked
            plan, fallback = yield from super()._plan()
            self.plan_parts = plan
            return plan, fallback
        words = yield from self._estimate()
        low, high = chunk_bounds(words)
        parts = lf.chunk_count(words)
        messages = plan_messages(spec.task, words, parts, self._language, spec.style)
        found: Optional[Tuple[str, List[Segment]]] = None
        for _ in range(PLAN_ATTEMPTS):
            reply = yield from self._short(messages, min(12000, max(1500, 450 * parts + 300)), PLAN_FORMAT)
            if reply is None:
                break
            found = parse_plan(reply, words)
            if found:
                break
            messages = [*messages, {"role": "assistant", "content": reply},
                        {"role": "user", "content": f"That plan was not usable. It needs between {low} and {high} parts, each with an instruction. "
                                                    "Reply with only the form."}]
        if found:
            self.title, plan = found
            fallback = False
        else:
            spec.target_words = words
            plan, fallback = lf.even_outline(spec, parts), True
        if len(plan) > spec.max_segments:
            raise lf.LongformError(f"the plan needs {len(plan)} parts, over the limit of {spec.max_segments}; raise max_segments")
        total = sum(s.words for s in plan)
        spec.target_words = total
        spec.outline = [{"heading": s.heading, "goal": s.goal, "target_words": s.words, "key_points": s.points} for s in plan] if not fallback else []
        if spec.headings is None:
            spec.headings = len(plan) >= 3
        self.plan_parts = plan
        return plan, fallback

    # -- the check

    def run(self) -> Iterator[Optional[Dict[str, Any]]]:
        done: Optional[Dict[str, Any]] = None
        for ev in super().run():
            if ev is not None and ev["event"] == "done":
                done = ev
            else:
                yield ev
        if done is None:
            return
        if not done["stopped"] and len(self.parts) == len(self.plan_parts):
            try:
                yield from self._review()
            except lf.Cancelled:
                done["stopped"] = "cancelled"
        yield {**done, "words": sum(lf.count_words(p) for p in self.parts), "warnings": self.warnings, "applied": self.applied,
               "rounds": self.rounds, "unapplied": self.unapplied,
               "finish": "length" if done["stopped"] == "cancelled" else done["finish"],
               "usage": {"prompt_tokens": self.totals.prompt_tokens, "completion_tokens": self.totals.completion_tokens,
                         "cached_tokens": self.totals.cached_tokens, "calls": self.totals.calls}}

    def _review(self) -> Iterator[Any]:
        for rnd in range(1, ROUNDS + 1):
            self.rounds = rnd
            yield {"event": "review", "round": rnd, "of": ROUNDS}
            found = yield from self._find_fixes()
            if found is None:
                break
            fixes, bad = found
            for entry in bad:
                self._unapplied(rnd, entry)
            if not fixes:
                break
            before = self.applied
            for n, fix in enumerate(fixes, 1):
                result = yield from self._apply(fix)
                status = "applied" if result == "" else "rejected" if result.startswith("rejected") else "unlocated"
                if status != "applied":
                    self._unapplied(rnd, {"chunk": fix.chunk, "op": fix.op, "anchor": fix.anchor, "instruction": fix.instruction, "reason": result})
                yield {"event": "fix", "round": rnd, "n": n, "of": len(fixes), "op": fix.op, "chunk": fix.chunk, "status": status, "reason": result}
            if self.applied == before:
                break
        if self.applied:
            yield {"event": "revised", "text": self.text()}

    def _unapplied(self, rnd: int, entry: Dict[str, Any]) -> None:
        if any(u["chunk"] == entry["chunk"] and u["op"] == entry["op"] and u["anchor"] == entry["anchor"] for u in self.unapplied):
            return                                           # the same fix found again in the next round
        self.unapplied.append({"round": rnd, **entry})
        self.warnings.append({"code": "fix_not_applied", "segment": entry.get("chunk") or 0,
                              "detail": f"{entry.get('op')}: {entry.get('reason')}: {str(entry.get('instruction') or entry.get('anchor'))[:80]}"})

    def _find_fixes(self) -> Iterator[Any]:
        """The verifier's fixes for the whole text, one call for each piece of it; None when the check cannot go on."""
        fixes: List[Fix] = []
        bad: List[Dict[str, Any]] = []
        plan_lines = "\n".join(f"{s.index + 1}. {s.heading + ': ' if s.heading else ''}{s.goal}".rstrip()
                               + (" (" + "; ".join(s.points[:4]) + ")" if s.points else "") for s in self.plan_parts)
        for group in pieces([lf.count_words(p) for p in self.parts]):
            body = "\n\n".join(f"=== part {i + 1} ===\n{self.parts[i]}" for i in group)
            user = (f"Request:\n{self.spec.task[:3000]}\n\nPlan ({len(self.plan_parts)} parts):\n{plan_lines}\n\n"
                    f"Text of part{'s' if len(group) > 1 else ''} {group[0] + 1}{'-' + str(group[-1] + 1) if len(group) > 1 else ''}:\n{body}")
            reply = yield from self._short([{"role": "system", "content": VERIFY_PROMPT}, {"role": "user", "content": user}], VERIFY_TOKENS, VERIFY_FORMAT)
            got = parse_fixes(reply, [i + 1 for i in group]) if reply is not None else None
            if got is None:
                self.warnings.append({"code": "review_failed", "segment": group[0] + 1, "detail": "the check of the text could not be read"})
                return None
            fixes += got[0]
            bad += got[1]
        for extra in fixes[FIXES_PER_ROUND:]:
            bad.append({"chunk": extra.chunk, "op": extra.op, "anchor": extra.anchor, "instruction": extra.instruction, "reason": "limit"})
        return fixes[:FIXES_PER_ROUND], bad

    # -- putting a fix in

    def _apply(self, fix: Fix) -> Iterator[Any]:
        """Applies one fix to the part it names; returns "" when it was put in, else the reason it was not."""
        i = fix.chunk - 1
        text = self.parts[i]
        place, why = place_of(text, fix.anchor)
        if place is None:
            return why
        if fix.op != "insert_after" and place.kind in ("protected", "heading", "table"):
            return place.kind
        seg = self.plan_parts[i]
        old = text[place.start:place.end]
        if fix.op == "delete":
            new_text = cut_block(text, place)
            if lf.count_words(new_text) < CHUNK_FLOOR * seg.words:
                return "rejected_length"
        else:
            written, why = yield from self._write_passage(fix, seg, text, place, old)
            if why:
                return why
            if fix.op == "replace":
                new_text = text[:place.start] + written + text[place.end:]
            else:
                new_text = text[:place.end] + "\n\n" + written + text[place.end:]
            if lf.count_words(new_text) < CHUNK_FLOOR * seg.words or lf.count_words(new_text) > 1.6 * lf.CHUNK_MAX:
                return "rejected_length"
        self.parts[i] = new_text
        self.applied += 1
        return ""

    def _write_passage(self, fix: Fix, seg: Segment, text: str, place: Place, old: str) -> Iterator[Any]:
        """The new passage for a replace or an insert, written by the conversation's own session and checked: (text, "") or ("", "rejected_<why>")."""
        replace = fix.op == "replace"
        masked, prot = lf.protect(old) if replace else (old, lf.Protected())
        old_words = lf.count_words(old)
        if replace:
            low = max(REPLACE_SLACK[0], int(REPLACE_BAND[0] * old_words))
            high = max(REPLACE_SLACK[1], int(REPLACE_BAND[1] * old_words))
        else:
            low, high = INSERT_WORDS
        ask = old_words if replace else (low + high) // 4
        before = " ".join(text[:place.start if replace else place.end].split()[-CONTEXT_WORDS:])
        after = " ".join(text[place.end:].split()[:CONTEXT_WORDS])
        lines = [f"The whole text is for this request: {self.spec.task[:600]}",
                 f"This passage is in part {seg.index + 1} of {len(self.plan_parts)}" + (f", {seg.heading}" if seg.heading else "") + (f": {seg.goal}" if seg.goal else "") + "."]
        if before:
            lines.append("The text just before it:\n-----\n" + before + "\n-----")
        if after:
            lines.append("The text just after it:\n-----\n" + after + "\n-----")
        if replace:
            lines.append("The passage to write again:\n-----\n" + masked + "\n-----\nWhat to change: " + fix.instruction +
                         f"\nKeep what is right in it. About {ask} words.")
        else:
            lines.append("Write a new passage that goes between the text before and the text after it.\nWhat to write: " + fix.instruction +
                         f"\nAbout {ask} words.")
        user = "\n".join(lines)
        marks = lf.marks_in(masked) if replace else []
        cap = max(256, int(high * lf.TOKENS_PER_WORD) + 64)
        feedback = ""
        for attempt in range(2):
            raw, finish, _conts = yield from self._complete([{"role": "system", "content": FIX_RULES},
                                                             {"role": "user", "content": user + feedback}], cap)
            raw = lf.normalize_marks(raw).strip()
            issues = [i for i in lf.check_segment(raw, expected_marks=marks, finish=finish, language=self._language, mode="write") if i.retry]
            if not issues:
                break
            feedback = "\n\n" + lf.correction(issues)
        else:
            return "", "rejected_" + issues[0].code
        if not low <= lf.count_words(raw) <= high:
            return "", "rejected_length"
        return (prot.restore(raw) if replace else raw), ""
