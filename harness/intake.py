"""What runs between the person's text and the model's first step.

Free stages first, deterministic and without a model: the text the model reads is cleaned without losing anything (the person's own
words stay as they wrote them, shown and stored), signals are read from it, and a gate decides whether one structured triage call is
worth making. In `rules` mode (the default) there is never a call: the gate only decides the note. In `full` mode a call is made when the gate
says so: one grammar-constrained form, every quotation checked against the person's text, the ask-or-act decision made here in code,
never by the model. Whatever is asked becomes one card (harness/questions.py); whatever is assumed or listed becomes one short note after
the person's message, so the cached prefix of the conversation is untouched. A request that lists three or more deliverables is
recorded in the task registry as a parent with its parts. Nothing here ever blocks an unattended run.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from harness import control_secret
from harness import questions as qs

log = logging.getLogger("harness.intake")

MODES = ("off", "rules", "full")
DEFAULT_MODE = "off"
DEFAULT_SIDE_CALL_TOKENS = 6000          # a conversation longer than this gets no extra model call while its cache is warm
TRIAGE_INPUT_CHARS = 3000
TRIAGE_TOKENS = 600
TRIAGE_TIMEOUT_S = 30.0
MAX_MISSING, MAX_SUBTASKS, MIN_SUBTASKS = 4, 6, 3
EVIDENCE_CHARS, CONSTRAINT_CHARS, SPAN_CHARS, TITLE_CHARS, ACCEPT_CHARS = 160, 160, 200, 80, 120
ASK_LIMIT = {"chat": 3.0, "agent": 2.0, "agent_started": 6.0}     # a question is asked when its risk is above the cost of asking
AMBIGUITY = {"none": 0.05, "minor": 0.25, "material": 0.5}
COST = {"low": 1.0, "medium": 4.0, "high": 15.0}
NAMED_OWNERS = {"other_person": "people", "booking": "booking", "resource": "resource"}
NOTE_HEAD = "[harness] Notes on the request above (not the user's words):"
TASK_PREFIX = "req-"

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_INVISIBLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_FENCE = re.compile(r"(```.*?```|~~~.*?~~~)", re.S)
_ENUM = re.compile(r"^\s*(?:\d{1,2}[\.\)]|[-*•])\s+\S", re.M)
_WORDS = re.compile(r"\w+", re.U)
_URL = re.compile(r"https?://\S+")
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\./|\.\./|~/|/)[\w.\-/\\]{2,}|\b[\w\-]+\.(?:py|js|ts|cpp|h|md|json|txt|csv|yml|yaml)\b")
VERBS = ("write", "create", "make", "build", "add", "fix", "change", "update", "remove", "delete", "rename", "move", "refactor", "implement", "run", "install",
         "search", "find", "analyze", "analyse", "compare", "send", "book", "buy", "pay", "call", "check", "test", "convert", "summarize", "summarise", "translate",
         "scrivi", "crea", "costruisci", "aggiungi", "correggi", "sistema", "cambia", "aggiorna", "rimuovi", "elimina", "cancella", "rinomina", "sposta", "implementa",
         "esegui", "installa", "cerca", "trova", "analizza", "confronta", "invia", "manda", "prenota", "compra", "paga", "chiama", "controlla", "verifica", "testa",
         "converti", "riassumi", "traduci", "organizza", "prepara")
RISK_WORDS = ("delete", "remove", "send", "pay", "buy", "book", "call", "email", "publish", "deploy", "drop", "overwrite", "format",
              "elimina", "cancella", "rimuovi", "invia", "manda", "paga", "compra", "prenota", "chiama", "pubblica", "sovrascrivi", "formatta")
DEICTIC = ("this", "that", "these", "those", "it", "them", "the above", "questo", "questa", "questi", "queste", "quello", "quella", "quelli", "lo", "la", "li", "le", "sopra")
CONTINUE = re.compile(r"^\s*(?:ok|okay|yes|no|sure|go on|continue|go ahead|proceed|thanks|thank you|do it|s[iì]|vai|vai avanti|continua|procedi|grazie|fallo|va bene)\W*$", re.I)
_RELATIVE = (("dopodomani", 2), ("day after tomorrow", 2), ("domani", 1), ("tomorrow", 1), ("ieri", -1), ("yesterday", -1), ("oggi", 0), ("today", 0), ("stasera", 0), ("tonight", 0))


# -- S1: cleaning that loses nothing the person wrote -----------------------------------------------------------------------

def clean(text: str) -> str:
    """The text as the model reads it: control and zero-width characters, bidi marks and ANSI escapes out, line ends as LF, no trailing spaces,
    at most one blank line in a row, NFC. A fenced block is left exactly as it is. Nothing is spelled, translated, summarised or reordered."""
    parts = _FENCE.split(text.replace("\r\n", "\n").replace("\r", "\n"))
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(part)
            continue
        part = unicodedata.normalize("NFC", _INVISIBLE.sub("", _ANSI.sub("", part)))
        part = "\n".join(line.rstrip(" \t") for line in part.split("\n"))
        out.append(re.sub(r"\n{3,}", "\n\n", part))
    return "".join(out)


def probe(text: str) -> str:
    """A folded copy for rules and comparisons: NFKC, case-folded, spaces collapsed. Never shown, never sent as the person's words."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


# -- S2: signals --------------------------------------------------------------------------------------------------------

@dataclass
class Signals:
    words: int = 0
    enumerations: int = 0
    action: bool = False
    deictic: bool = False
    risk: bool = False
    attachments: int = 0
    urls: int = 0
    paths: int = 0
    code_share: float = 0.0
    relative: List[Tuple[str, int]] = field(default_factory=list)


def _has_word(folded: str, words: Tuple[str, ...]) -> bool:
    return any(re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", folded) for w in words)


def signals(text: str, attachments: Optional[List[str]] = None) -> Signals:
    folded = probe(_FENCE.sub(" ", text))
    fenced = sum(len(m) for m in _FENCE.findall(text))
    return Signals(words=len(_WORDS.findall(_FENCE.sub(" ", text))), enumerations=len(_ENUM.findall(text)), action=_has_word(folded, VERBS),
                   deictic=_has_word(folded, DEICTIC), risk=_has_word(folded, RISK_WORDS), attachments=len(attachments or []),
                   urls=len(_URL.findall(text)), paths=len(_PATH.findall(text)), code_share=round(fenced / max(1, len(text)), 2),
                   relative=[(w, d) for w, d in _RELATIVE if _has_word(folded, (w,))])


# -- S3: the gate -------------------------------------------------------------------------------------------------------

@dataclass
class Context:
    """What the harness knows about the request, as plain facts."""
    origin: str = "user"
    attachments: List[str] = field(default_factory=list)
    attended: bool = True
    ask_only: bool = False
    auto: bool = False
    started: bool = False            # the conversation has already used a tool
    answers_card: bool = False       # the message is the answer to a card
    history_tokens: int = 0
    warm: bool = False               # the server held most of the last request in its cache
    now: Optional[datetime] = None
    turn: str = ""


def gate(text: str, sig: Signals, ctx: Context, side_call_tokens: int = DEFAULT_SIDE_CALL_TOKENS) -> Tuple[bool, str]:
    """Whether a triage call is worth making, and the reason in a word. SKIP: not the person's own message, a command, the answer to a card, a
    continuation, a short chat without an action or a risk, a Target Loop message, a long conversation whose cache is warm (the extra
    call would cost the next turn its cache). Otherwise the bar is higher in a conversation without tools."""
    stripped = text.strip()
    if ctx.origin != "user":
        return False, "origin"
    if ctx.answers_card or stripped.startswith(qs._ASK_BACK):
        return False, "answer"
    if stripped.startswith(("/", "Target Loop ·", "[harness]")):
        return False, "command"
    if CONTINUE.match(stripped):
        return False, "continuation"
    if ctx.warm and ctx.history_tokens > side_call_tokens:
        return False, "cache"
    if sig.words < 6 and not (sig.action or sig.risk):
        return False, "short"
    if ctx.ask_only:
        wanted = sig.words >= 60 or sig.enumerations >= 3
    else:
        wanted = (sig.words >= 35 or sig.enumerations >= 2 or (sig.action and sig.deictic) or sig.risk or (sig.attachments > 0 and sig.action))
    return (True, "signals") if wanted else (False, "simple")


# -- S4/S5: the triage call and its validation --------------------------------------------------------------------------

_STR = {"type": "string"}
TRIAGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["evidence", "intent", "size", "ambiguity", "missing_info", "risks", "needs_plan", "stated_constraints", "subtasks"],
    "properties": {
        "evidence": {"type": "array", "items": _STR},
        "intent": {"type": "string", "enum": ["answer", "explain", "search", "edit_code", "create", "analyze", "operate", "plan", "chat", "command"]},
        "size": {"type": "string", "enum": ["trivial", "small", "medium", "large"]},
        "ambiguity": {"type": "string", "enum": ["none", "minor", "material"]},
        "missing_info": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["what", "why", "default", "findable", "wrong_guess_cost", "reversible", "owner"],
            "properties": {"what": _STR, "why": _STR, "default": _STR,
                           "findable": {"type": "string", "enum": ["no", "files", "web", "memory", "tools"]},
                           "wrong_guess_cost": {"type": "string", "enum": ["low", "medium", "high"]},
                           "reversible": {"type": "boolean"},
                           "owner": {"type": "string", "enum": ["user", "other_person", "booking", "resource", "none"]}}}},
        "risks": {"type": "array", "items": {"type": "string", "enum": ["destructive", "external_effect", "money", "other_people", "secrets", "long_running", "needs_login", "private_data"]}},
        "needs_plan": {"type": "boolean"},
        "stated_constraints": {"type": "array", "items": _STR},
        "subtasks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["title", "span", "depends_on", "acceptance"],
            "properties": {"title": _STR, "span": _STR, "depends_on": {"type": "array", "items": {"type": "integer"}}, "acceptance": _STR}}},
    },
}
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "intake_triage", "strict": True, "schema": TRIAGE_SCHEMA}}
TRIAGE_PROMPT = (
    "You read one message a user wrote to an assistant that can use tools. Do not answer it and do not do the task; fill the form. "
    "Copy quotes exactly, never paraphrase inside evidence, stated_constraints or span. Write free text in the user's language.\n"
    "size: trivial = one answer or one small action; small = a few steps; medium = several files or sources; large = three or more "
    "separable deliverables the user lists or clearly implies.\n"
    "ambiguity: material only if two reasonable readings give different results AND the wrong one costs real work; when in doubt, none.\n"
    "missing_info: only facts needed to start that the message does not give; not what files, search or memory can supply (mark those "
    "findable); default is the assumption you would use; wrong_guess_cost is high only when hard to undo or involving money, other people "
    "or sending things out; owner names who must decide when it is not the user.\n"
    "needs_plan: true only if the user should approve steps before anything is changed.\n"
    "subtasks: empty unless size is large; each points at the user's words in span, ordered so none depends on a later one."
)


def _fold(text: str) -> str:
    return probe(text)


def _grounded(quote: Any, folded_text: str) -> bool:
    return isinstance(quote, str) and len(quote.strip()) >= 2 and _fold(quote) in folded_text


def validate(data: Any, text: str) -> Optional[Dict[str, Any]]:
    """The triage form checked: a quotation (evidence, stated constraint, span) that is not in the person's text is dropped, and the whole
    form is thrown away when more than half of them are. Caps are applied here. None when the form is not usable."""
    if not isinstance(data, dict) or data.get("size") not in ("trivial", "small", "medium", "large") or data.get("ambiguity") not in AMBIGUITY:
        return None
    folded = _fold(text)
    total = bad = 0

    def keep(items: Any, limit: int, cap: int) -> List[str]:
        nonlocal total, bad
        out = []
        for item in (items if isinstance(items, list) else [])[:limit]:
            total += 1
            if _grounded(item, folded):
                out.append(" ".join(item.split())[:cap])
            else:
                bad += 1
        return out

    evidence = keep(data.get("evidence"), 3, EVIDENCE_CHARS)
    constraints = keep(data.get("stated_constraints"), 5, CONSTRAINT_CHARS)
    subtasks: List[Dict[str, Any]] = []
    for i, st in enumerate((data.get("subtasks") if isinstance(data.get("subtasks"), list) else [])[:MAX_SUBTASKS]):
        if not isinstance(st, dict):
            continue
        total += 1
        title, span = " ".join(str(st.get("title") or "").split())[:TITLE_CHARS], st.get("span")
        if not title or not _grounded(span, folded):
            bad += 1
            continue
        deps = sorted({d for d in st.get("depends_on") or [] if isinstance(d, int) and not isinstance(d, bool) and 0 <= d < i})
        subtasks.append({"index": i, "title": title, "span": " ".join(span.split())[:SPAN_CHARS], "depends_on": deps,
                         "acceptance": " ".join(str(st.get("acceptance") or "").split())[:ACCEPT_CHARS]})
    if total and bad * 2 > total:
        return None
    missing = []
    for m in (data.get("missing_info") if isinstance(data.get("missing_info"), list) else [])[:MAX_MISSING]:
        if not isinstance(m, dict) or not str(m.get("what") or "").strip():
            continue
        missing.append({"what": qs.clean(m.get("what"), qs.TEXT_CHARS), "why": qs.clean(m.get("why"), qs.DESCRIPTION_CHARS),
                        "default": qs.clean(m.get("default"), qs.LABEL_CHARS),
                        "findable": m.get("findable") if m.get("findable") in ("no", "files", "web", "memory", "tools") else "tools",
                        "cost": m.get("wrong_guess_cost") if m.get("wrong_guess_cost") in COST else "low",
                        "reversible": m.get("reversible") is not False,
                        "owner": m.get("owner") if m.get("owner") in ("user", "other_person", "booking", "resource", "none") else "none"})
    return {"evidence": evidence, "size": data["size"], "ambiguity": data["ambiguity"], "intent": data.get("intent"), "missing": missing,
            "needs_plan": data.get("needs_plan") is True, "constraints": constraints, "subtasks": subtasks}


# -- S6: ask or act, decided here ---------------------------------------------------------------------------------------

def risk_of(item: Dict[str, Any], ambiguity: str) -> float:
    """P(the guess is wrong) x cost of a wrong guess, doubled when it cannot be undone or is not the user's to decide."""
    return AMBIGUITY[ambiguity] * COST[item["cost"]] * (2.0 if not item["reversible"] or item["owner"] != "none" else 1.0)


def policy(form: Dict[str, Any], ctx: Context) -> Dict[str, List[Dict[str, Any]]]:
    """Sorts the missing facts into `ask` (a card), `assume` (stated in the note), `look` (find it before asking) and, for a run nobody
    watches, `park` (another person, a booking or a resource: left for the person while the rest goes on). Unattended runs and the Auto level
    ask nothing but the named owners; a conversation without tools asks at most one question (two when both are costly)."""
    out: Dict[str, List[Dict[str, Any]]] = {"ask": [], "assume": [], "look": [], "park": []}
    scored = []
    for m in form["missing"]:
        if m["findable"] != "no":
            out["look"].append(m)
        else:
            scored.append((risk_of(m, form["ambiguity"]), m))
    scored.sort(key=lambda x: -x[0])
    limit = ASK_LIMIT["chat"] if ctx.ask_only else ASK_LIMIT["agent_started"] if ctx.started else ASK_LIMIT["agent"]
    room = 2 if ctx.ask_only else 3
    for score, m in scored:
        named = m["owner"] in NAMED_OWNERS
        if not ctx.attended:
            (out["park"] if named else out["assume"]).append(m)
        elif ctx.auto and not named:
            out["assume"].append(m)
        elif score > limit and len(out["ask"]) < room and (not ctx.ask_only or not out["ask"] or (m["cost"] == "high" and out["ask"][0]["cost"] == "high")):
            out["ask"].append(m)
        else:
            out["assume"].append(m)
    return out


def card_from(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The questions for the missing facts, made by the harness from the form's own fields: the default first and recommended, the screen
    adds the person's own answer."""
    questions = []
    for n, m in enumerate(items, 1):
        q: Dict[str, Any] = {"id": f"q{n}", "text": m["what"].rstrip("?") + "?", "kind": "single" if m["default"] else "free", "options": [],
                             "allow_other": bool(m["default"]), "required": True}
        if m["default"]:
            q["options"] = [{"id": "o1", "label": m["default"], "recommended": True, **({"description": m["why"]} if m["why"] else {})}]
        if m["owner"] in NAMED_OWNERS:
            q["involves"] = [f"{NAMED_OWNERS[m['owner']]}: {m['what']}"[:qs.INVOLVES_CHARS]]
        questions.append(q)
    return {"intro": "", "questions": questions, "notes": []}


# -- S8: the note -------------------------------------------------------------------------------------------------------

def relative_dates(sig: Signals, now: Optional[datetime]) -> List[str]:
    if now is None:
        return []
    return [f'"{w}" = {(now + timedelta(days=d)):%A %Y-%m-%d}' for w, d in sig.relative]


def note_text(*, assumptions: List[str], look: List[str], constraints: List[str], dates: List[str], checklist: int, subtasks: List[Dict[str, Any]],
              settled: str = "") -> str:
    """The deterministic note: only what is assumed, what to look up before asking, what the user said they require (in their own words),
    the dates read from the clock, and the parts of a large request. Empty when there is nothing to say."""
    lines = []
    if assumptions:
        lines.append("Assumed, because the message does not say (say so in your answer if it mattered): " + "; ".join(assumptions) + ".")
    if look:
        lines.append("Find these in the files, the web or your memory before you ask the user: " + "; ".join(look) + ".")
    if constraints:
        lines.append("Requirements in the user's words: " + " | ".join(f'"{c}"' for c in constraints) + ".")
    if dates:
        lines.append("Dates from the local clock: " + "; ".join(dates) + ".")
    if checklist >= 3 and not subtasks:
        lines.append(f"The message lists {checklist} items: do every one and end by saying which are done and which are not.")
    if subtasks:
        lines.append("The request has these parts; do all of them in this order and end by saying which are done:")
        lines += [f"  {i}. {s['title']}" + (f" (after {', '.join(str(d + 1) for d in s['depends_on'])})" if s["depends_on"] else "") +
                  (f" - done when: {s['acceptance']}" if s["acceptance"] else "") for i, s in enumerate(subtasks, 1)]
    if settled:
        lines.append(settled)
    return NOTE_HEAD + "\n" + "\n".join(lines) if lines else ""


# -- the task registry --------------------------------------------------------------------------------------------------

class TaskSink:
    """Posts to the server's task registry (`/api/tasks`); a courtesy, so a failure is logged once and never raises."""

    def __init__(self, base_url: str, api_key: Optional[str] = None, timeout: float = 3.0) -> None:
        self.base_url, self.api_key, self.timeout, self.failed = (base_url or "").rstrip("/"), api_key, timeout, False

    def record(self, task_id: str, status: str, result: str = "", children: Optional[List[str]] = None, metadata: Optional[Dict[str, Any]] = None) -> bool:
        if not self.base_url or self.failed:
            return False
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}), **control_secret.header_for(self.base_url)}
        body = {"id": task_id, "status": status, "result": result[:300], **({"children": children} if children is not None else {}), **({"metadata": metadata} if metadata else {})}
        req = urllib.request.Request(self.base_url + "/api/tasks", data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
            return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.failed = True
            log.warning("intake: task registry not reachable: %s", exc)
            return False


# -- the whole step -----------------------------------------------------------------------------------------------------

@dataclass
class Outcome:
    note: str = ""
    card: Optional[Dict[str, Any]] = None
    parked: List[Dict[str, Any]] = field(default_factory=list)
    subtasks: List[Dict[str, Any]] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    triaged: bool = False
    reason: str = ""


class Intake:
    """One conversation's intake: its mode, the model that makes the triage call, and the settings that bound it."""

    def __init__(self, mode: str = DEFAULT_MODE, client: Any = None, side_call_tokens: int = DEFAULT_SIDE_CALL_TOKENS) -> None:
        if mode not in MODES:
            raise ValueError(f"intake mode must be one of {', '.join(MODES)}")
        self.mode, self.client, self.side_call_tokens = mode, client, side_call_tokens

    def triage(self, text: str, ctx: Context) -> Optional[Dict[str, Any]]:
        """The one model call: a form about the message, validated against it. None on any failure (no retry)."""
        facts = f"attended: {'yes' if ctx.attended else 'no'}\nattachments: {', '.join(ctx.attachments[:8]) or 'none'}"
        try:
            resp = self.client.chat_completion(
                messages=[{"role": "system", "content": TRIAGE_PROMPT}, {"role": "user", "content": f"FACTS\n{facts}\n\nMESSAGE\n{text[:TRIAGE_INPUT_CHARS]}"}],
                temperature=0.0, max_tokens=TRIAGE_TOKENS, timeout=TRIAGE_TIMEOUT_S, response_format=RESPONSE_FORMAT, extra={"reasoning_effort": "none"})
            content = resp["choices"][0]["message"].get("content") or ""
            return validate(json.loads(content), text)
        except Exception as exc:                                  # any failure of an extra call is a skipped call, never an error for the person
            log.info("intake: triage skipped: %s", type(exc).__name__)
            return None

    def run(self, text: str, ctx: Context) -> Outcome:
        """The note, the card and the parts for one request. The text is the cleaned message the model reads."""
        sig = signals(text, ctx.attachments)
        out = Outcome()
        dates = relative_dates(sig, ctx.now)
        form = None
        wanted, out.reason = gate(text, sig, ctx, self.side_call_tokens)
        if self.mode == "full" and wanted and self.client is not None:
            form = self.triage(text, ctx)
            out.triaged = form is not None
        assumptions: List[str] = []
        look: List[str] = []
        constraints: List[str] = []
        settled = ""
        if form is not None:
            decision = policy(form, ctx)
            assumptions = [f"{m['what']}: {m['default']}" for m in decision["assume"] if m["default"]]
            look = [m["what"] for m in decision["look"]]
            constraints = form["constraints"]
            if decision["ask"]:
                out.card = card_from(decision["ask"])
            out.parked = decision["park"]
            if len(form["subtasks"]) >= MIN_SUBTASKS and form["size"] == "large":
                out.subtasks = form["subtasks"]
            if out.parked:
                settled = "Left for the user to decide later (go on with the rest): " + "; ".join(m["what"] for m in out.parked) + "."
        out.assumptions = assumptions
        out.note = note_text(assumptions=assumptions, look=look, constraints=constraints, dates=dates, checklist=sig.enumerations, subtasks=out.subtasks, settled=settled)
        return out
