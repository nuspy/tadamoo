"""The questions the agent puts to the person (`ask_user`) and the harness's own (intake): one payload shape, repaired and checked here.

Pure functions, no I/O. A payload is `{"intro", "questions": [...], "notes": [...]}`; a question is `{"id", "text", "kind", "options",
"allow_other", "required", ...}` with kind single, multi, free or confirm. The model's call is repaired where the intent is clear
(`normalise`); the person's answer is checked against the stored question set, never against what the client sends back (`check_answer`);
the text the model reads is made by `format_answer`. Unattended runs pick their answers with `auto_answers`.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

KINDS = ("single", "multi", "free", "confirm")
MAX_QUESTIONS, MAX_OPTIONS = 4, 5
TEXT_CHARS, LABEL_CHARS, DESCRIPTION_CHARS, INTRO_CHARS = 200, 60, 140, 200
PLACEHOLDER_CHARS, DEFAULT_CHARS, INVOLVES_CHARS = 80, 300, 100
TOTAL_CHARS = 2000                       # all the words of one card
ANSWER_TEXT_CHARS = 500                  # one typed answer
PAYLOAD_CHARS = 4000                     # the whole answer
ANSWER_PREFIX = "ask:"                   # the person's answer as it travels: prefix + JSON
ANSWER_HEADER = "The user answered your questions:"
SKIP_TEXT = "The user skipped the questions: decide with your best judgement and say what you assumed."
CHAT_TEXT = "The user chose to answer in the chat: wait for their next message."
GO_ON = "Go on with these answers. Do not ask them again."
RECOMMENDED_TAG = "(your recommended option)"
AUTO_TAGS = {"recommended": "(chosen automatically: your recommended option)", "default": "(default)",
             "none": "(no answer: decide yourself, conservatively)", "parked": "(pending: the person will answer later; go on with other work)"}
CONFIRM = ({"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"})

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,23}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]")
_RECOMMENDED = re.compile(r"\s*[\(\[]\s*(?:recommended|consigliat[oa]|raccomandat[oa])\s*[\)\]]\s*$", re.I)
_OTHERS = {"other", "others", "altro", "altri", "none", "nessuno", "nessuna", "something else", "altro..."}
_LISTED = re.compile(r"^\s*(?:[A-Ea-e]|[1-5])[\)\.]\s+(\S.*)$")
EXAMPLE = ('{"questions":[{"text":"Which database should the service use?","kind":"single","options":'
           '[{"label":"PostgreSQL","description":"Already used by the other service","recommended":true},{"label":"SQLite"}]}]}')


class QuestionError(ValueError):
    """The call has no usable question, or the answer is not one the question allows; the message says what to change."""


def clean(text: Any, limit: int) -> str:
    """One line of text the person will read: control and invisible characters out, spaces collapsed, cut with an ellipsis."""
    out = " ".join(_CONTROL.sub("", unicodedata.normalize("NFC", str(text if text is not None else ""))).split())
    return out if len(out) <= limit else out[:max(1, limit - 1)].rstrip() + "…"


def _identifier(value: Any, taken: set, prefix: str, position: int) -> str:
    wanted = str(value or "").strip()
    if _ID.match(wanted) and wanted not in taken:
        return wanted
    n = position
    while f"{prefix}{n}" in taken:
        n += 1
    return f"{prefix}{n}"


def _listed_options(text: str) -> Tuple[str, List[str]]:
    """Options written inside the question text on lines of their own ("A) ..."): the text without them, and the options."""
    lines = text.split("\n")
    found = [m.group(1) for m in (_LISTED.match(ln) for ln in lines) if m]
    if len(found) < 2:
        return text, []
    return " ".join(ln.strip() for ln in lines if not _LISTED.match(ln) and ln.strip()), found


def _option(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, str):
        raw = {"label": raw}
    if not isinstance(raw, dict):
        return None
    label, recommended = str(raw.get("label") if raw.get("label") is not None else raw.get("text") or ""), raw.get("recommended") is True
    if _RECOMMENDED.search(label):
        label, recommended = _RECOMMENDED.sub("", label), True
    label = clean(label, LABEL_CHARS)
    if not label:
        return None
    out: Dict[str, Any] = {"label": label}
    if raw.get("description"):
        out["description"] = clean(raw["description"], DESCRIPTION_CHARS)
    if recommended:
        out["recommended"] = True
    return out


def _question(raw: Any, position: int, taken: set) -> Optional[Dict[str, Any]]:
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") if raw.get("text") is not None else raw.get("question") or "").replace("\r", "")
    raw_options = raw.get("options") if isinstance(raw.get("options"), list) else []
    if not raw_options:
        text, raw_options = _listed_options(text)
    text = clean(text, TEXT_CHARS)
    if not text:
        return None
    kind = str(raw.get("kind") or "").lower()
    if kind not in KINDS:
        kind = "single" if raw_options else "free"
    allow_other = raw.get("allow_other") is not False
    options: List[Dict[str, Any]] = []
    if kind in ("single", "multi"):
        seen = set()
        for item in raw_options:
            opt = _option(item)
            if opt is None or opt["label"].casefold() in seen:
                continue
            if allow_other and opt["label"].casefold() in _OTHERS:
                continue                                           # the screen adds its own "Other"
            seen.add(opt["label"].casefold())
            options.append(opt)
        if len(options) > MAX_OPTIONS:                             # the recommended ones stay, then the first
            keep = [o for o in options if o.get("recommended")][:MAX_OPTIONS]
            keep += [o for o in options if o not in keep][:MAX_OPTIONS - len(keep)]
            options = [o for o in options if o in keep]
        if len(options) < 2:
            kind, options = "free", []
        elif kind == "single":
            first = next((i for i, o in enumerate(options) if o.get("recommended")), None)
            for i, o in enumerate(options):
                if i != first:
                    o.pop("recommended", None)
    qid = _identifier(raw.get("id"), taken, "q", position)
    taken.add(qid)
    for i, o in enumerate(options, 1):
        o["id"] = f"o{i}"
    out: Dict[str, Any] = {"id": qid, "text": text, "kind": kind, "options": options,
                           "allow_other": kind in ("single", "multi") and allow_other, "required": raw.get("required") is not False}
    if kind == "confirm":
        out["options"] = [dict(o) for o in CONFIRM]
        if isinstance(raw.get("recommended"), bool):
            out["recommended"] = raw["recommended"]
    if kind == "free":
        if raw.get("placeholder"):
            out["placeholder"] = clean(raw["placeholder"], PLACEHOLDER_CHARS)
        if raw.get("default"):
            out["default"] = clean(raw["default"], DEFAULT_CHARS)
    involves = [clean(x, INVOLVES_CHARS) for x in raw.get("involves") or [] if isinstance(x, str) and x.strip()] if isinstance(raw.get("involves"), list) else []
    if involves:
        out["involves"] = involves[:3]
    return out


def normalise(args: Any) -> Dict[str, Any]:
    """The model's `ask_user` call as a clean payload. Repairs what is clear (see the rules in the module's tests); raises QuestionError
    when no question has any text."""
    if not isinstance(args, dict):
        raise QuestionError("ask_user takes an object with a list of questions. Example: " + EXAMPLE)
    raw = args.get("questions")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise QuestionError("ask_user needs a list of questions. Example: " + EXAMPLE)
    taken: set = set()
    questions, notes, used = [], [], len(clean(args.get("intro"), INTRO_CHARS))
    for position, item in enumerate(raw, 1):
        q = _question(item, position, taken)
        if q is None:
            continue
        size = len(q["text"]) + sum(len(o["label"]) + len(o.get("description", "")) for o in q["options"])
        if len(questions) >= MAX_QUESTIONS or (questions and used + size > TOTAL_CHARS):
            notes.append(f"Only the first {len(questions)} question(s) were asked; the others were left out.")
            break
        used += size
        questions.append(q)
    if not questions:
        raise QuestionError("None of the questions has any text. Example: " + EXAMPLE)
    return {"intro": clean(args.get("intro"), INTRO_CHARS), "questions": questions, "notes": notes}


def summary(payload: Dict[str, Any]) -> str:
    """The questions as a short plain text: for logs, the pending list and the history, never for the card."""
    return " / ".join(q["text"] for q in payload.get("questions", []))[:300]


def history_text(payload: Dict[str, Any]) -> str:
    """The card as the assistant's own words in the conversation, so the model reads later what was asked."""
    lines = [payload["intro"]] if payload.get("intro") else []
    for i, q in enumerate(payload["questions"], 1):
        opts = "; ".join(o["label"] + (" " + RECOMMENDED_TAG if o.get("recommended") else "") for o in q["options"] if q["kind"] != "confirm")
        lines.append(f"{i}. {q['text']}" + (f" ({opts})" if opts else ""))
    return "\n".join(lines)


# -- the person's answer ----------------------------------------------------------------------------------------------

def parse_answer(value: Any) -> Dict[str, Any]:
    """The `ask:` + JSON string the page sends, as a dict; QuestionError when it is not one."""
    if not isinstance(value, str) or not value.startswith(ANSWER_PREFIX) or len(value) > len(ANSWER_PREFIX) + PAYLOAD_CHARS:
        raise QuestionError("That is not an answer to these questions.")
    try:
        data = json.loads(value[len(ANSWER_PREFIX):])
    except ValueError:
        raise QuestionError("That is not an answer to these questions.") from None
    if not isinstance(data, dict) or data.get("status") not in ("submitted", "skipped", "chat"):
        raise QuestionError("That is not an answer to these questions.")
    return data


def check_answer(payload: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    """The answer checked against the stored questions: `{"status", "answers": {qid: {"picked": [ids], "text"}}}`. Unknown ids, two picks on
    a single, an empty required question, a pick on a free question and overlong text are refused with the reason."""
    status = data["status"]
    if status != "submitted":
        return {"status": status, "answers": {}}
    given = data.get("answers") if isinstance(data.get("answers"), dict) else {}
    known = {q["id"]: q for q in payload["questions"]}
    if any(k not in known for k in given):
        raise QuestionError("An answer names a question that was not asked.")
    answers: Dict[str, Dict[str, Any]] = {}
    for qid, q in known.items():
        item = given.get(qid) if isinstance(given.get(qid), dict) else {}
        picked = item.get("picked") if isinstance(item.get("picked"), list) else []
        if any(not isinstance(p, str) for p in picked) or len(set(picked)) != len(picked):
            raise QuestionError(f"Question {qid}: the choices are not valid.")
        ids = {o["id"] for o in q["options"]}
        if any(p not in ids for p in picked):
            raise QuestionError(f"Question {qid}: one of the choices was not offered.")
        raw_text = item.get("text") if isinstance(item.get("text"), str) else ""
        text = _CONTROL.sub("", raw_text).strip()
        if len(text) > ANSWER_TEXT_CHARS:
            raise QuestionError(f"Question {qid}: the typed answer is longer than {ANSWER_TEXT_CHARS} characters.")
        if q["kind"] in ("single", "confirm") and len(picked) > 1:
            raise QuestionError(f"Question {qid}: choose one.")
        if q["kind"] == "single" and picked and text:
            raise QuestionError(f"Question {qid}: choose one, or type your own.")
        if q["kind"] == "confirm" and text:
            raise QuestionError(f"Question {qid}: answer yes or no.")
        if q["kind"] == "free" and picked:
            raise QuestionError(f"Question {qid}: this question takes a typed answer.")
        if text and q["kind"] in ("single", "multi") and not q["allow_other"]:
            raise QuestionError(f"Question {qid}: typing your own is not offered here.")
        if q["required"] and not picked and not text:
            raise QuestionError(f"Question {qid}: answer it, or skip the questions.")
        answers[qid] = {"picked": picked, "text": text}
    return {"status": "submitted", "answers": answers}


def answer_label(q: Dict[str, Any], answer: Dict[str, Any]) -> str:
    by_id = {o["id"]: o["label"] for o in q["options"]}
    parts = [by_id[p] for p in answer["picked"]] + ([f'Other: "{answer["text"]}"'] if answer["text"] and q["kind"] != "free" else [])
    if q["kind"] == "free" and answer["text"]:
        parts = [answer["text"]]
    return "; ".join(parts) if parts else "(no answer)"


def format_answer(payload: Dict[str, Any], checked: Dict[str, Any], tags: Optional[Dict[str, str]] = None) -> str:
    """The message the model reads for an answer: one line per question with the labels picked (never ids), and a tag where the harness
    chose (`tags`: question id -> a key of AUTO_TAGS, or "recommended" for an option the person left as proposed)."""
    if checked["status"] == "skipped":
        return SKIP_TEXT
    if checked["status"] == "chat":
        return CHAT_TEXT
    tags = tags or {}
    lines = [ANSWER_HEADER]
    for i, q in enumerate(payload["questions"], 1):
        answer = checked["answers"].get(q["id"], {"picked": [], "text": ""})
        text = answer_label(q, answer)
        if tags.get(q["id"]):
            text += " " + AUTO_TAGS[tags[q["id"]]]
        elif q["kind"] in ("single", "multi") and any(o.get("recommended") and o["id"] in answer["picked"] for o in q["options"]):
            text += " " + RECOMMENDED_TAG
        lines.append(f"{i}. {clean(q['text'], 80)} -> {text}")
    lines.append(GO_ON)
    return "\n".join(lines)


# -- nobody there to answer -----------------------------------------------------------------------------------------

def auto_pick(q: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """What an unattended run answers for one question and where it comes from: the recommended option (single), the recommended options
    (multi), the default text (free) or the recommended side (confirm); otherwise nothing, and the source is "none". A question that
    involves other people, a booking or a resource is `parked`, not picked."""
    if q.get("involves"):
        return {"picked": [], "text": ""}, "parked"
    if q["kind"] in ("single", "multi"):
        ids = [o["id"] for o in q["options"] if o.get("recommended")]
        return {"picked": ids, "text": ""}, "recommended" if ids else "none"
    if q["kind"] == "free":
        return {"picked": [], "text": q.get("default", "")}, "default" if q.get("default") else "none"
    if isinstance(q.get("recommended"), bool):
        return {"picked": ["yes" if q["recommended"] else "no"], "text": ""}, "recommended"
    return {"picked": [], "text": ""}, "none"


def auto_answers(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """The checked answer an unattended run gives to a whole card and the source tag of each question."""
    answers, tags = {}, {}
    for q in payload["questions"]:
        answers[q["id"]], tags[q["id"]] = auto_pick(q)
    return {"status": "submitted", "answers": answers}, tags


def assumption_lines(payload: Dict[str, Any], checked: Dict[str, Any], tags: Dict[str, str]) -> List[str]:
    """What an unattended run decided by itself, one line per question the harness answered: the list the run's result carries."""
    out = []
    for q in payload["questions"]:
        if tags.get(q["id"]) in ("recommended", "default", "none"):
            out.append(f"{clean(q['text'], 120)} -> {answer_label(q, checked['answers'][q['id']])} ({tags[q['id']]})")
    return out


def parked_payload(q: Dict[str, Any]) -> Dict[str, Any]:
    """One question left for the person, in the shape of the pending list: a button per option (the option's id is the answer) and a sentence
    after `other:`."""
    options = [{"label": o["label"], "value": o["id"]} for o in q["options"]]
    return {"mode": "confirm", "kind": "parked_question", "question": q["text"], "options": options, "free_prefix": "other:",
            "facts": {"involves": q.get("involves", []), "question_kind": q["kind"]}}


# -- how often and what the model may ask ------------------------------------------------------------------------

MAX_ASKS = 2                              # ask_user calls for one request of the person (an answer to a card is not a new request)
FAILED_BEFORE_PLAIN = 2                   # unusable calls before the model's words are shown as one plain question
_ASK_BACK = (ANSWER_HEADER, SKIP_TEXT, CHAT_TEXT)

PROMPT_RULES = """Asking the user (ask_user).
- Ask only when (a) the answer changes what you will do in a real way, (b) you could not find it with your tools in a call or two (read files, search, list), and (c) guessing wrong costs more than one question. Otherwise choose the sensible default and say in your answer what you assumed.
- Never ask for permission to proceed, to confirm a plan, to run a safe tool, or about a risky action (the harness asks that itself). Never ask what the user already told you. Never ask for passwords or keys.
- One call, up to 3 questions, only questions whose answers do not depend on each other. If an answer changes the next question, ask it in a second call.
- Each question is one sentence in the user's language. Give 2 to 4 short options a person can choose as they are: complete, concrete, no placeholders, different from each other. Put the one you recommend first and set recommended. Do not write an Other option; the screen adds it. Never put the options inside the question text.
- Use kind multi only if several options can be true together; free when no list makes sense; confirm for yes or no.
- After the answer, go on with the work. Do not repeat the question or the answer back.
Good: {"questions":[{"text":"Which database should the service use?","kind":"single","options":[{"label":"PostgreSQL","description":"Already used by the other service","recommended":true},{"label":"SQLite","description":"No server, single file"}]}]}
Bad: asking "Shall I continue?\""""


def _same(text: str) -> str:
    return " ".join(re.sub(r"\W+", " ", text.casefold()).split())


def _first_words(raw: Any, depth: int = 0) -> str:
    """Any sentence found in a call that had no usable question, to show as a plain one."""
    if isinstance(raw, str):
        return clean(raw, TEXT_CHARS) if len(raw.strip()) >= 3 else ""
    items = raw if isinstance(raw, list) else list(raw.values()) if isinstance(raw, dict) else []
    for item in items if depth < 3 else []:
        found = _first_words(item, depth + 1)
        if found:
            return found
    return ""


class AskGuard:
    """What stands between the model and an ask loop, for one conversation: at most MAX_ASKS calls for one request of the person, never the
    same question twice, and after FAILED_BEFORE_PLAIN unusable calls the words it did write are shown as one plain question."""

    def __init__(self) -> None:
        self.asked, self.failures, self.seen_text = 0, 0, None
        self.questions: set = set()

    def request(self, user_text: str) -> None:
        """Told the person's latest message before each call: a new request (not the answer to a card) starts the count again."""
        if user_text == self.seen_text:
            return
        self.seen_text = user_text
        if user_text and not user_text.startswith(_ASK_BACK):
            self.asked, self.failures, self.questions = 0, 0, set()

    def check(self, args: Any, user_text: str) -> Dict[str, Any]:
        """The payload to ask, or QuestionError with the line the model is told. Counts the call when it is let through."""
        self.request(user_text)
        if self.asked >= MAX_ASKS:
            raise QuestionError("Do not ask again: decide with what you have and say what you assumed.")
        try:
            payload = normalise(args)
        except QuestionError as exc:
            self.failures += 1
            words = _first_words(args.get("questions") if isinstance(args, dict) else None) or _first_words(args)
            if self.failures < FAILED_BEFORE_PLAIN or not words:
                raise exc
            payload = normalise({"questions": [{"text": words, "kind": "free"}]})
        fresh = [q for q in payload["questions"] if _same(q["text"]) not in self.questions]
        if not fresh:
            raise QuestionError("You already asked this in this conversation and the answer is above. Do not ask it again.")
        if len(fresh) < len(payload["questions"]):
            payload = {**payload, "questions": fresh}
        self.questions.update(_same(q["text"]) for q in fresh)
        self.asked += 1
        return payload
