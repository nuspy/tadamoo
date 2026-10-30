"""Long final texts in bounded segments (docs/longform.md).

A model asked for ten thousand words in one turn ends its turn early. Here the text is planned, then written one segment
per call, each call with a bounded length, the plan and the end of the text so far as context. Content that must not
change (images, link targets, code, diagrams, math, HTML, citations) is replaced by markers before any model call and put
back byte for byte at the end. Every segment is checked by plain code before it is kept, and a problem that remains after
the retries is reported, never hidden.

`LongAnswer` drives any client with the `chat_completion` of `harness.client.BasaltClient` and `harness.llm.client.RoutedClient`
(the loaded model, or a cloud route), and yields progress events. Everything else here is a pure function.
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from harness.continuation import cut_point, join_continuation

MODES = ("write", "translate", "rewrite")
SEGMENT_WORDS = 2000            # words one segment aims at
SEGMENT_WORDS_RANGE = (200, 2500)
TARGET_WORDS_RANGE = (50, 200000)
CHUNK_WORDS = (700, 2500)               # the words a part reliably holds when a plan is cut into parts
CHUNK_MAX = 2500                        # a part is never planned longer than this
MAX_SEGMENTS = 60
RETRIES_MAX = 3
LOW_RATIO, HIGH_RATIO = 0.7, 1.6        # a segment outside this part of its target is flagged
TOKENS_PER_WORD = 2.2                   # cap on a segment's output, in tokens per word of its upper bound
TRANSCRIPT_SHARE = 0.6                  # past this share of the context the earlier segments are dropped from the prompt
ANCHOR_WORDS = 120                      # the end of the text so far, repeated in a prompt that carries no earlier segments
CONTINUATIONS = 2
CHARS_PER_TOKEN = 3.0
SAMPLING_FIELDS = ("temperature", "top_p", "top_k", "min_p", "repetition_penalty", "presence_penalty", "frequency_penalty", "penalty_last_n",
                   "seed")      # the request's own sampling, passed on to every call that writes
SIDE_TIMEOUT_S = 180.0                  # a short technical call is not waited for longer than this
STOP_SHORT_SEGMENTS = 2                 # a written text stops after this many segments in a row under half their target

BLOCK_KINDS = ("FM", "CODE", "MATH", "HTML", "REF")     # markers that stand alone on a line and keep their order
MARK_RE = re.compile(r"⟦\s*([A-Z]{1,6}\d+)\s*⟧")

LANGUAGES = {"en": "English", "it": "Italian", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese"}
LANGUAGE_NAMES = {**{v.lower(): k for k, v in LANGUAGES.items()},
                  "italiano": "it", "inglese": "en", "spagnolo": "es", "francese": "fr", "tedesco": "de", "portoghese": "pt",
                  "español": "es", "français": "fr", "deutsch": "de", "português": "pt"}
# how many words a text has in the second language per word of the first (a guide for the length of a translation)
LENGTH_RATIO = {("en", "it"): 1.15, ("it", "en"): 0.87, ("en", "es"): 1.1, ("es", "en"): 0.9, ("en", "fr"): 1.15,
                ("fr", "en"): 0.87, ("en", "pt"): 1.1, ("pt", "en"): 0.9}
STOPWORDS = {
    "en": "the and of to is in that it for with as was on are this be by not you have from or at an they which".split(),
    "it": "il di che è e la per un una in con non le si del della da sono come più gli dei nel alla ma anche questo".split(),
    "es": "el de que y la en un una por con no los se las del para es como más pero su al lo este".split(),
    "fr": "le de et la les des en un une que pour dans est pas qui sur au avec ce il plus par sont".split(),
    "de": "der die und das ist nicht den von zu mit sich auf für ein eine dem des im auch als es an".split(),
    "pt": "o de que e a do da em um uma para com não os se as dos por mais como mas ao é este".split(),
}


class LongformError(ValueError):
    """A request or a source the mode cannot take; the message says what to change."""


# -- protected content ---------------------------------------------------------------------------------------------

FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
INLINE_CODE_RE = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
LINK_RE = re.compile(r"(?<!!)\[([^\]\n]+)\]\(([^)\n]+)\)")
REF_RE = re.compile(r"^\[(?!\^)[^\]]+\]:[ \t]*\S.*$")
URL_RE = re.compile(r"(?:https?://|ftp://|file://|www\.|basalt-file:)[^\s<>()\[\]⟦⟧\"']+")
CITE_RE = re.compile(r"\[S\d+(?:\s*[,;]\s*S\d+)*\]")
FOOTNOTE_RE = re.compile(r"\[\^[^\]\s]+\](?!:)")
HTML_BLOCK_RE = re.compile(r"^\s*<(?:!--|/?(?:div|p|table|details|summary|img|iframe|video|audio|figure|section|center|pre|ul|ol|"
                           r"h[1-6]|script|style|svg|picture|a|br|hr)\b)", re.I)
TRAILING = ".,;:!?"


class Protected:
    """The originals behind the markers of one text."""

    def __init__(self) -> None:
        self.items: Dict[str, str] = {}
        self._count: Dict[str, int] = {}

    def add(self, kind: str, original: str) -> str:
        self._count[kind] = n = self._count.get(kind, 0) + 1
        key = f"{kind}{n}"
        self.items[key] = original
        return f"⟦{key}⟧"

    def restore(self, text: str, drop_unknown: bool = True) -> str:
        """The text with every marker replaced by its original; an unknown marker is removed. Originals may hold markers
        of earlier items (a link around code), so this repeats until none is left."""
        for _ in range(6):
            new = MARK_RE.sub(lambda m: self.items.get(m.group(1), "" if drop_unknown else m.group(0)), text)
            if new == text:
                break
            text = new
        return text


def normalize_marks(text: str) -> str:
    """Markers a model wrote with stray spaces, as `⟦KEY⟧`."""
    return MARK_RE.sub(lambda m: f"⟦{m.group(1)}⟧", text)


def marks_in(text: str) -> List[str]:
    return MARK_RE.findall(text)


def _fenced(lines: List[str], prot: Protected) -> List[str]:
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = FENCE_RE.match(line)
        if m:
            fence = m.group(1)
            end = len(lines) - 1
            for j in range(i + 1, len(lines)):
                c = FENCE_RE.match(lines[j])
                if c and c.group(1)[0] == fence[0] and len(c.group(1)) >= len(fence) and not lines[j].strip()[len(c.group(1)):].strip():
                    end = j
                    break
            out.append(prot.add("CODE", "\n".join(lines[i:end + 1])))
            i = end + 1
            continue
        stripped = line.strip()
        if stripped.startswith("$$"):
            end = i if stripped.endswith("$$") and len(stripped) > 3 else len(lines) - 1
            if end != i:
                for j in range(i + 1, len(lines)):
                    if "$$" in lines[j]:
                        end = j
                        break
            out.append(prot.add("MATH", "\n".join(lines[i:end + 1])))
            i = end + 1
            continue
        if HTML_BLOCK_RE.match(line):
            end = i
            while end + 1 < len(lines) and lines[end + 1].strip():
                end += 1
            out.append(prot.add("HTML", "\n".join(lines[i:end + 1])))
            i = end + 1
            continue
        out.append(line)
        i += 1
    return out


def protect(text: str) -> Tuple[str, Protected]:
    """(text with markers, the originals). Line endings become \\n."""
    prot = Protected()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    head: List[str] = []
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() in ("---", "..."):
                head = [prot.add("FM", "\n".join(lines[:j + 1]))]
                lines = lines[j + 1:]
                break
    lines = head + _fenced(lines, prot)
    lines = [prot.add("REF", ln) if REF_RE.match(ln) else ln for ln in lines]
    text = "\n".join(lines)
    text = INLINE_CODE_RE.sub(lambda m: prot.add("IC", m.group(0)), text)
    text = IMAGE_RE.sub(lambda m: prot.add("IMG", m.group(0)), text)
    text = LINK_RE.sub(lambda m: f"[{m.group(1)}]({prot.add('URL', m.group(2))})", text)

    def bare(m: "re.Match[str]") -> str:
        url = m.group(0)
        tail = ""
        while url and url[-1] in TRAILING:
            url, tail = url[:-1], url[-1] + tail
        return prot.add("URL", url) + tail if url else m.group(0)
    text = URL_RE.sub(bare, text)
    text = CITE_RE.sub(lambda m: prot.add("CITE", m.group(0)), text)
    text = FOOTNOTE_RE.sub(lambda m: prot.add("FN", m.group(0)), text)
    return text, prot


# -- when a request needs parts -------------------------------------------------------------------------------------------

LONG_MIN_TOKENS = 3000              # a text asked for above this is not written reliably in one turn
TOKENS_PER_WORD_BY_LANGUAGE = {"en": 1.35, "it": 1.5}   # a language not listed (or not recognised) counts like Italian: a long request is not missed
WORDS_ASKED_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:[.,' ]\d{3})+|\d+(?:[.,]\d+)?)\s*(k\b)?\s*(?:-\s*)?(?:words?|parole|palabras|mots|wörter|palavras)\b", re.I)
PAGES_ASKED_RE = re.compile(r"(?<![\w.])(\d+)\s*(?:pages?|pagine|páginas|paginas|seiten)\b", re.I)
WORDS_PER_PAGE = 450
WRITE_VERB_RE = re.compile(r"\b(?:write|draft|compose|scrivi|scrivimi|redigi|stendi|prepara|genera|escribe|écris|schreibe)\b", re.I)
CHAPTER_RE = re.compile(r"\b(?:chapters?|capitol[oi])\b", re.I)
KIND_RE = re.compile(r"\b(?:report|relazione|rapporto|saggio|essay|ebook|libro|book|article|articolo|tesi|guida|guide|racconto|story)\b", re.I)
TRANSLATE_RE = re.compile(r"\b(?:translate|traduci|traduzione|traducir|traduire|übersetze)\b", re.I)
REWRITE_RE = re.compile(r"\b(?:rewrite|riscrivi|riscrittura|riformula|reescribe|réécris|umschreiben)\b", re.I)
ANALYSE_RE = re.compile(r"\b(?:analy[sz]e|analysis|analizza|analisi|riassumi|summari[sz]e)\b", re.I)
CHAPTER_WORDS = 2500                # a chapter asked for without a length (over LONG_MIN_TOKENS in every language)


def tokens_per_word(language: str = "") -> float:
    return TOKENS_PER_WORD_BY_LANGUAGE.get(language, TOKENS_PER_WORD_BY_LANGUAGE["it"])


def tokens_for_words(words: float, language: str = "") -> int:
    """The tokens a text of `words` words takes in `language` (the one place words and tokens are converted)."""
    return int(round(words * tokens_per_word(language)))


def words_for_tokens(tokens: float, language: str = "") -> int:
    return int(round(tokens / tokens_per_word(language)))


def request_language(text: str) -> str:
    """The language of a request, or '' when it cannot be told (a short request is judged from fewer words than a long text)."""
    return detect_language(text, min_words=4)


def asked_words(text: str) -> int:
    """The length a request asks for, in words ("10,000 words", "10.000 parole", "20 pages"); 0 when it names none."""
    best = 0
    for m in WORDS_ASKED_RE.finditer(text):
        raw = re.sub(r"[.,' ](?=\d{3}\b)", "", m.group(1))
        try:
            value = float(raw.replace(",", ".")) * (1000 if m.group(2) else 1)
        except ValueError:
            continue
        best = max(best, int(value))
    for m in PAGES_ASKED_RE.finditer(text):
        best = max(best, int(m.group(1)) * WORDS_PER_PAGE)
    return best


HINT_RE = re.compile(r"\n*\[long answer:([^\]\n]*)\][ \t]*$")


def parse_hint(text: str) -> Optional[Dict[str, Any]]:
    """The line the Agent tab's Long answer control puts at the end of a message ("[long answer: mode=translate; words=5000; language=it]"):
    the request without it, the mode, the words and the other fields, or None when the message has no such line."""
    m = HINT_RE.search(text) if isinstance(text, str) else None
    if m is None:
        return None
    fields = {k.strip(): v.strip() for k, v in (p.split("=", 1) for p in m.group(1).split(";") if "=" in p)}
    body = text[:m.start()].rstrip()
    mode = fields.get("mode") if fields.get("mode") in MODES else "write"
    words = int(fields["words"]) if fields.get("words", "").isdigit() else max(count_words(body), words_for_tokens(LONG_MIN_TOKENS, request_language(body)))
    return {"mode": mode, "words": words, "parts": chunk_count(words), "reason": "asked", "text": body, "fields": fields}


def detect_long_request(text: str, source_words: int = 0) -> Optional[Dict[str, Any]]:
    """A request that needs parts, found by plain rules, or None. `source_words` are the words of an attached text. The answer holds the
    mode, the words the text will have, the number of parts and why."""
    if not isinstance(text, str) or not text.strip():
        return None
    typed, asked = count_words(text), asked_words(text)
    size = max(typed, source_words)
    lang = request_language(text)

    def over(words: int, times: int = 1) -> bool:
        return tokens_for_words(words, lang) > times * LONG_MIN_TOKENS

    def found(mode: str, words: int, reason: str) -> Dict[str, Any]:
        return {"mode": mode, "words": words, "tokens": tokens_for_words(words, lang), "parts": chunk_count(words), "reason": reason}
    if TRANSLATE_RE.search(text) or REWRITE_RE.search(text):
        mode = "translate" if TRANSLATE_RE.search(text) else "rewrite"
        return found(mode, size, "source") if over(size) else None
    if ANALYSE_RE.search(text) and over(size, 2):
        return found("write", size // 2, "source")
    if over(asked) and (WRITE_VERB_RE.search(text) or CHAPTER_RE.search(text) or KIND_RE.search(text) or over(asked, 2)):       # twice the threshold needs no writing verb
        return found("write", asked, "length")
    if not asked and CHAPTER_RE.search(text) and WRITE_VERB_RE.search(text) and over(CHAPTER_WORDS):
        return found("write", CHAPTER_WORDS, "chapter")
    return None


TARGET_RE = re.compile(r"\b(?:into|in|to|a|en|al|ins|auf|para|vers|em)\s+(" + "|".join(sorted(map(re.escape, LANGUAGE_NAMES), key=len, reverse=True)) + r")\b", re.I)
INSTRUCTION_WORDS = 60              # a request's first paragraph is its instruction when it is this short and the source follows


def target_language(text: str) -> str:
    """The language a request names as the one to translate into ("into Italian", "in inglese"), as a code; '' when it names none."""
    m = TARGET_RE.search(text[:400])
    return language_code(m.group(1)) if m else ""


def split_request(text: str) -> Tuple[str, str]:
    """(instruction, source) of a translate or rewrite request that holds both: the first paragraph when it is short and a text follows it;
    otherwise ('', the whole request)."""
    head, sep, rest = text.strip().partition("\n\n")
    if sep and rest.strip() and 0 < len(head.split()) <= INSTRUCTION_WORDS:
        return head.strip(), rest.strip()
    return "", text.strip()


# -- splitting -----------------------------------------------------------------------------------------------------

def count_words(text: str) -> int:
    return len(MARK_RE.sub(" ", text).split())


HEADING_RE = re.compile(r"^#{1,6}\s")
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
TABLE_RE = re.compile(r"^\s*\|")


def split_units(text: str) -> List[str]:
    """Paragraphs, headings, tables, lists (with their continuation lines) and block markers, in order. A unit is never cut."""
    lines = text.split("\n")
    units: List[List[str]] = []
    cur: List[str] = []

    def close() -> None:
        nonlocal cur
        while cur and not cur[-1].strip():
            cur.pop()
        if cur:
            units.append(cur)
        cur = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            nxt = next((x for x in lines[i + 1:] if x.strip()), "")
            if cur and LIST_RE.match(cur[0]) and nxt and (LIST_RE.match(nxt) or nxt[:1] in (" ", "\t")):
                cur.append(line)           # a list goes on after a blank line
            else:
                close()
            i += 1
            continue
        if HEADING_RE.match(line):
            close()
            units.append([line])
            i += 1
            continue
        if cur and bool(TABLE_RE.match(line)) != bool(TABLE_RE.match(cur[0])):
            close()
        cur.append(line)
        i += 1
    close()
    return ["\n".join(u) for u in units]


def _split_sentences(unit: str, limit: int) -> List[str]:
    """A paragraph over `limit` words, cut at sentence ends (and at words when it has none)."""
    pieces: List[str] = []
    for sentence in re.split(r"(?<=[.!?…])\s+", unit):
        words = sentence.split()
        while len(words) > limit:
            pieces.append(" ".join(words[:limit]))
            words = words[limit:]
        if words:
            pieces.append(" ".join(words))
    out: List[str] = []
    cur: List[str] = []
    for piece in pieces:
        if cur and sum(map(count_words, cur)) + count_words(piece) > limit:
            out.append(" ".join(cur))
            cur = []
        cur.append(piece)
    if cur:
        out.append(" ".join(cur))
    return out


def pack(units: List[str], soft: int) -> List[str]:
    """Segments of about `soft` words from whole units. A heading stays with the unit after it; a table stays whole."""
    hard = int(soft * 1.4)
    glued: List[str] = []
    for u in units:
        if glued and HEADING_RE.match(glued[-1]) and "\n" not in glued[-1] and not HEADING_RE.match(u):
            glued[-1] = glued[-1] + "\n\n" + u
        else:
            glued.append(u)
    flat: List[str] = []
    for u in glued:
        if count_words(u) > hard and not TABLE_RE.match(u) and not LIST_RE.match(u):
            flat.extend(_split_sentences(u, soft))
        elif count_words(u) > hard and LIST_RE.match(u):
            items = re.split(r"\n(?=\s*(?:[-*+]|\d+[.)])\s)", u)
            cur: List[str] = []
            for it in items:
                if cur and sum(map(count_words, cur)) + count_words(it) > soft:
                    flat.append("\n".join(cur))
                    cur = []
                cur.append(it)
            flat.append("\n".join(cur))
        else:
            flat.append(u)
    segments: List[str] = []
    cur2: List[str] = []
    words = 0
    for u in flat:
        w = count_words(u)
        if cur2 and words + w > hard:
            segments.append("\n\n".join(cur2))
            cur2, words = [], 0
        cur2.append(u)
        words += w
        if words >= soft:
            segments.append("\n\n".join(cur2))
            cur2, words = [], 0
    if cur2:
        if segments and words < soft * 0.25 and count_words(segments[-1]) + words <= hard * 1.2:
            segments[-1] += "\n\n" + "\n\n".join(cur2)      # a short tail joins the segment before it
        else:
            segments.append("\n\n".join(cur2))
    return segments


# -- language and shape checks -------------------------------------------------------------------------------------

def language_code(name: Any) -> str:
    """A language as a two-letter code when it is one of the known ones, else ''."""
    if not isinstance(name, str):
        return ""
    key = name.strip().lower()
    return key if key in LANGUAGES else LANGUAGE_NAMES.get(key, "")


def detect_language(text: str, min_words: int = 30) -> str:
    """The language of a text when it is clearly one of the known ones, else ''."""
    words = [w.lower() for w in re.findall(r"[^\W\d_]+", MARK_RE.sub(" ", text))]
    if len(words) < min_words:
        return ""
    scores = sorted(((sum(1 for w in words if w in set(sw)) / len(words), code) for code, sw in STOPWORDS.items()), reverse=True)
    (best, code), (second, _) = scores[0], scores[1]
    return code if best >= 0.12 and best >= second * 1.4 else ""


def table_shapes(text: str) -> List[int]:
    """The column count of each table in order (a run of lines that start with a pipe)."""
    shapes: List[int] = []
    run: List[str] = []
    for line in text.split("\n") + [""]:
        if TABLE_RE.match(line):
            run.append(line)
        else:
            if run:
                cols = {len(re.sub(r"\\\|", "", r).strip().strip("|").split("|")) for r in run}
                shapes.append(next(iter(cols)) if len(cols) == 1 else -1)
            run = []
    return shapes


def heading_levels(text: str) -> List[int]:
    return [len(m.group(1)) for m in re.finditer(r"^(#{1,6})\s", text, re.M)]


def ends_midword(text: str) -> bool:
    """A paragraph that ends on a letter with no closing mark: the model ended its turn inside a sentence."""
    lines = [ln for ln in text.rstrip().split("\n") if ln.strip()]
    if not lines:
        return False
    last = lines[-1].strip()
    if HEADING_RE.match(last) or LIST_RE.match(last) or last[:1] in "|>`" or last.endswith("⟧") or len(last.split()) < 5:
        return False
    return last[-1].isalpha()


def ngram_repeat_ratio(text: str, n: int = 6) -> float:
    """The share of the text's n-word groups that already appeared earlier in it."""
    words = MARK_RE.sub(" ", text).lower().split()
    if len(words) < n * 4:
        return 0.0
    seen: set = set()
    repeated = 0
    total = len(words) - n + 1
    for i in range(total):
        gram = tuple(words[i:i + n])
        if gram in seen:
            repeated += 1
        seen.add(gram)
    return repeated / total


def trim_overlap(previous: str, new: str, minimum: int = 8) -> str:
    """`new` without the words at its start that repeat the end of `previous` (the model began a little early)."""
    before = previous.split()[-ANCHOR_WORDS:]
    after = new.split()
    for k in range(min(len(before), len(after), 60), minimum - 1, -1):
        if before[-k:] == after[:k]:
            rest = new.lstrip()
            for _ in range(k):
                rest = rest.split(None, 1)[1] if len(rest.split(None, 1)) > 1 else ""
            return rest
    return new


LEAK_RE = re.compile(r"^\s*(?:sure|certainly|of course|here(?:'s| is| are)|ecco\b|claro|voici|hier ist)|part \d+ of \d+|"
                     r"write only the part|source of this part|previous part", re.I)


@dataclass
class Issue:
    code: str
    detail: str = ""
    hard: bool = True
    retry: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "hard": self.hard}


def check_segment(out: str, *, source: str = "", expected_marks: Optional[List[str]] = None, finish: str = "stop",
                  lo: float = 0.0, hi: float = 0.0, language: str = "", mode: str = "write",
                  earlier: str = "", glossary: Optional[Dict[str, str]] = None) -> List[Issue]:
    """What is wrong with a segment's text (markers still in it), found by plain code. `expected_marks` are the markers of the
    source segment, in order; `lo` and `hi` bound the words; `earlier` is the text before this segment."""
    issues: List[Issue] = []
    if not out.strip():
        return [Issue("empty", "the segment has no text")]
    if finish == "length":
        issues.append(Issue("truncated", "the output limit ended the segment"))
    elif ends_midword(out):
        issues.append(Issue("cut_midword", "the segment ends in the middle of a sentence"))
    found = marks_in(out)
    expected = list(expected_marks or [])
    missing = [m for m in expected if m not in found]
    extra = sorted({m for m in found if m not in expected})
    doubled = sorted({m for m in found if found.count(m) > 1})
    if missing:
        issues.append(Issue("markers_missing", "missing " + ", ".join(f"⟦{m}⟧" for m in missing[:8])))
    if extra or doubled:
        issues.append(Issue("markers_extra", "unknown or repeated " + ", ".join(f"⟦{m}⟧" for m in (extra + doubled)[:8])))
    if not missing and not extra and not doubled:
        blocks = lambda ms: [m for m in ms if re.sub(r"\d+$", "", m) in BLOCK_KINDS]
        if blocks(found) != blocks(expected):
            issues.append(Issue("markers_order", "block markers are not in their order"))
    words = count_words(out)
    if hi and words > hi:
        issues.append(Issue("too_long", f"{words} words, over {int(hi)}", hard=False, retry=False))
    if lo and words < lo:
        issues.append(Issue("too_short", f"{words} words, under {int(lo)}", hard=False))
    if language and words >= 30:
        seen = detect_language(out)
        if seen and seen != language:
            issues.append(Issue("language", f"written in {LANGUAGES[seen]}, not {LANGUAGES[language]}"))
    if LEAK_RE.search(out[:200]) or LEAK_RE.search(MARK_RE.sub("", out)[-200:]):
        issues.append(Issue("leaked", "the text holds a remark about the task or the instructions"))
    if ngram_repeat_ratio(out) > 0.3:
        issues.append(Issue("repeated", "the text repeats itself"))
    paragraphs = [p.strip() for p in earlier.split("\n\n") if len(p.strip()) > 80]
    if paragraphs and any(p.strip() in paragraphs for p in out.split("\n\n") if len(p.strip()) > 80):
        issues.append(Issue("duplicate", "a paragraph of an earlier segment is written again"))
    if sum(1 for ln in out.split("\n") if FENCE_RE.match(ln)) % 2:
        issues.append(Issue("fence", "a code fence is not closed"))
    shapes = table_shapes(out)
    if -1 in shapes or (source and table_shapes(source) != shapes):
        issues.append(Issue("table", "a table changed shape"))
    if mode == "translate":
        if source and heading_levels(source) != heading_levels(out):
            issues.append(Issue("headings", "the headings differ from the source", hard=False, retry=False))
        lost = [n for n in dict.fromkeys(re.findall(r"\d[\d.,]*\d|\d", MARK_RE.sub(" ", source))) if n not in out]
        if lost:
            issues.append(Issue("numbers", "numbers of the source not found: " + ", ".join(lost[:5]), hard=False, retry=False))
        absent = [f"{k} -> {v}" for k, v in (glossary or {}).items() if k.lower() in source.lower() and v.lower() not in out.lower()]
        if absent:
            issues.append(Issue("glossary", "glossary terms not used: " + "; ".join(absent[:4]), hard=False, retry=False))
    return issues


# -- request ---------------------------------------------------------------------------------------------------------

@dataclass
class Spec:
    mode: str = "write"
    task: str = ""                      # write: what to write; translate and rewrite: what to do
    source: str = ""                    # translate and rewrite: the text
    target_words: int = 0
    language: str = ""                  # code when known, else as the person wrote it
    style: str = ""
    glossary: Dict[str, str] = field(default_factory=dict)
    segment_words: int = SEGMENT_WORDS
    sections: int = 0
    headings: Optional[bool] = None
    retries: int = 1
    protect: bool = True
    max_segments: int = MAX_SEGMENTS
    max_seconds: float = 0.0
    context_tokens: int = 0
    end_turn_bias: Optional[float] = None
    outline: List[Dict[str, Any]] = field(default_factory=list)     # sections the caller planned: heading, goal, target_words, key_points
    title: str = ""

    @staticmethod
    def from_request(raw: Any, task_text: str = "") -> "Spec":
        """The long-answer options of a request (`true` for the defaults). LongformError for anything the mode cannot take."""
        if raw is True:
            raw = {}
        if not isinstance(raw, dict):
            raise LongformError("long_answer: true or an object")
        known = {"mode", "task", "instruction", "source", "target_words", "target_language", "language", "style", "glossary",
                 "segment_words", "sections", "headings", "retries", "protect", "max_segments", "max_seconds", "context_tokens",
                 "end_turn_bias", "outline", "title"}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise LongformError(f"long_answer: unknown field {unknown[0]!r}")

        def number(key: str, lo: float, hi: float, default: float, whole: bool = True) -> Any:
            v = raw.get(key)
            if v is None:
                return default
            if isinstance(v, bool) or not isinstance(v, (int, float)) or (whole and int(v) != v) or not lo <= v <= hi:
                raise LongformError(f"long_answer.{key}: a {'whole ' if whole else ''}number from {lo:g} to {hi:g}")
            return int(v) if whole else float(v)
        mode = {"generate": "write"}.get(raw.get("mode") or "write", raw.get("mode") or "write")
        if mode not in MODES:
            raise LongformError(f"long_answer.mode: one of {', '.join(MODES)}")
        for key in ("task", "instruction", "source", "target_language", "language", "style"):
            if raw.get(key) is not None and not isinstance(raw[key], str):
                raise LongformError(f"long_answer.{key}: text")
        outline = clean_outline(raw.get("outline"))
        if outline and mode != "write":
            raise LongformError("long_answer.outline: only for write")
        glossary = raw.get("glossary") or {}
        if not isinstance(glossary, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in glossary.items()):
            raise LongformError("long_answer.glossary: an object of terms and their translations")
        text = (raw.get("task") or raw.get("instruction") or "").strip()
        source = (raw.get("source") or "").strip()
        target = number("target_words", *TARGET_WORDS_RANGE, 0)
        if mode == "write":
            text = text or task_text.strip()
            target = target or (sum(r["target_words"] for r in outline if r["target_words"]) if outline else 0) or 2000
            if not text:
                raise LongformError("long_answer: write needs a task (the last user message, or `task`)")
        else:
            source = source or task_text.strip()
            if not source:
                raise LongformError(f"long_answer: {mode} needs the text in `source` (or as the last user message)")
            if mode == "translate" and not (raw.get("target_language") or raw.get("language")):
                raise LongformError("long_answer: translate needs `target_language`")
            if mode == "rewrite" and not text:
                raise LongformError("long_answer: rewrite needs `instruction` (what to change)")
        lang = (raw.get("target_language") or raw.get("language") or "").strip()
        retries = number("retries", 0, RETRIES_MAX, 1)
        headings = raw.get("headings")
        if headings is not None and not isinstance(headings, bool):
            raise LongformError("long_answer.headings: true or false")
        if not isinstance(raw.get("protect", True), bool):
            raise LongformError("long_answer.protect: true or false")
        bias = raw.get("end_turn_bias")
        if bias is not None and (isinstance(bias, bool) or not isinstance(bias, (int, float)) or not -20 <= bias <= 0):
            raise LongformError("long_answer.end_turn_bias: a number from -20 to 0")
        return Spec(mode=mode, task=text, source=source, target_words=target, language=language_code(lang) or lang,
                    style=(raw.get("style") or "").strip(), glossary=dict(glossary),
                    segment_words=number("segment_words", *SEGMENT_WORDS_RANGE, SEGMENT_WORDS),
                    sections=number("sections", 0, MAX_SEGMENTS, 0), headings=headings, retries=retries,
                    protect=raw.get("protect", True), max_segments=number("max_segments", 1, 500, MAX_SEGMENTS),
                    max_seconds=number("max_seconds", 0, 86400 * 7, 0.0, whole=False), context_tokens=number("context_tokens", 0, 10 ** 8, 0),
                    end_turn_bias=float(bias) if bias is not None else None, outline=outline,
                    title=" ".join(str(raw.get("title") or "").split())[:160])


@dataclass
class Segment:
    index: int
    heading: str = ""
    goal: str = ""
    points: List[str] = field(default_factory=list)
    source: str = ""                    # masked source of this segment (translate, rewrite)
    words: int = 0                      # the words it should have
    marks: List[str] = field(default_factory=list)
    rows: List[Tuple[int, str, bool]] = field(default_factory=list)     # outline sections it covers: (number, heading, begins here)
    instruction: str = ""               # the exact task for the writer of this part, when the planner handed one over


# -- the plan ----------------------------------------------------------------------------------------------------------

def outline_request(spec: Spec, sections: int) -> str:
    return (f"Plan a text of about {spec.target_words} words in {sections} sections, to be written one section at a time.\n"
            f"Task: {spec.task}\n"
            + (f"Language: {LANGUAGES.get(spec.language, spec.language)}.\n" if spec.language else "")
            + 'Reply with only a JSON object: {"title": string, "sections": [{"heading": string, "goal": string, '
              '"target_words": integer, "key_points": [string]}]}. The sections follow each other without overlap and cover the task.')


def parse_outline(text: str, sections: int, target_words: int) -> Optional[Tuple[str, List[Segment]]]:
    """(title, segments) from a model's outline reply, or None when it holds no usable one. Words are scaled to the target."""
    decoder = json.JSONDecoder()
    for start in (m.start() for m in re.finditer(r"\{", text)):
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        rows = obj.get("sections") if isinstance(obj, dict) else None
        if not isinstance(rows, list) or not 1 <= len(rows) <= max(2 * sections, 8):
            continue
        segments: List[Segment] = []
        for i, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("heading", ""), str):
                break
            pts = row.get("key_points") if isinstance(row.get("key_points"), list) else []
            w = row.get("target_words")
            segments.append(Segment(i, heading=" ".join(str(row.get("heading") or "").split())[:120],
                                    goal=" ".join(str(row.get("goal") or "").split())[:400],
                                    points=[" ".join(str(p).split())[:200] for p in pts[:8]],
                                    words=int(w) if isinstance(w, (int, float)) and not isinstance(w, bool) and w > 0 else 0))
        else:
            total = sum(s.words for s in segments)
            for s in segments:
                s.words = int(round(s.words * target_words / total)) if total and s.words else target_words // len(segments)
            return (" ".join(str(obj.get("title") or "").split())[:160], segments)
    return None


def clean_outline(raw: Any) -> List[Dict[str, Any]]:
    """A caller's outline as rows {heading, goal, target_words, key_points}; LongformError for one that is not a list of sections."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not 1 <= len(raw) <= 200:
        raise LongformError("long_answer.outline: a list of 1 to 200 sections")
    rows = []
    for i, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            raise LongformError(f"long_answer.outline[{i}]: an object with heading, goal, target_words, key_points")
        words = row.get("target_words")
        if words is not None and (isinstance(words, bool) or not isinstance(words, (int, float)) or words < 0):
            raise LongformError(f"long_answer.outline[{i}].target_words: a positive number")
        points = row.get("key_points") if isinstance(row.get("key_points"), list) else []
        rows.append({"heading": " ".join(str(row.get("heading") or "").split())[:120], "goal": " ".join(str(row.get("goal") or "").split())[:400],
                     "target_words": int(words or 0), "key_points": [" ".join(str(p).split())[:200] for p in points[:8]]})
    return rows


def chunk_count(total_words: int, chunk_words: int = SEGMENT_WORDS) -> int:
    """How many parts a text of `total_words` is written in: parts of `chunk_words` (kept within what a part reliably holds)."""
    size = min(max(chunk_words, CHUNK_WORDS[0]), CHUNK_WORDS[1])
    n = max(1, round(total_words / size))
    while total_words / n > CHUNK_MAX:
        n += 1
    return n


def chunks_from_outline(rows: List[Dict[str, Any]], total_words: int, chunk_words: int = SEGMENT_WORDS) -> List[Segment]:
    """The parts of a planned text: the number comes from the total, and each part takes the sections that fall in its share of the words
    (a section longer than a part is written over several, a short one shares a part with its neighbours)."""
    n = chunk_count(total_words, chunk_words)
    known = sum(r["target_words"] for r in rows)
    sizes = [r["target_words"] * total_words / known if known and r["target_words"] else total_words / len(rows) for r in rows]
    scale = total_words / (sum(sizes) or 1)
    sizes = [x * scale for x in sizes]
    starts = [sum(sizes[:i]) for i in range(len(rows))]
    share = total_words / n
    chosen: List[List[int]] = [[] for _ in range(n)]
    for r, (a, size) in enumerate(zip(starts, sizes)):
        b = a + size
        lap = [max(0.0, min(b, (i + 1) * share) - max(a, i * share)) for i in range(n)]
        hit = [i for i in range(n) if lap[i] >= 0.05 * share or lap[i] >= 0.5 * size]
        for i in hit or [lap.index(max(lap))]:
            chosen[i].append(r)
    out: List[Segment] = []
    begun: set = set()
    for i, picked in enumerate(chosen):
        seg = Segment(i, words=int(round(share)))
        for r in picked:
            row = rows[r]
            first = r not in begun
            begun.add(r)
            seg.rows.append((r + 1, row["heading"], first))
            goal = row["goal"] or row["heading"]
            seg.goal = (seg.goal + " | " if seg.goal else "") + (goal if first else f"Continue: {goal}")
            seg.points += [p for p in row["key_points"] if p not in seg.points]
        seg.heading = next((h for _, h, first in seg.rows if first and h), "")
        out.append(seg)
    return out


def even_outline(spec: Spec, sections: int) -> List[Segment]:
    return [Segment(i, goal="Continue the text so that, with the other parts, it does the task.", words=spec.target_words // sections)
            for i in range(sections)]


def source_plan(spec: Spec, masked: str, prot: Protected) -> List[Segment]:
    """Translate and rewrite: the source blocks are the plan, and each block's length decides the length of its part."""
    pieces = pack(split_units(masked), spec.segment_words)
    total = sum(count_words(p) for p in pieces) or 1
    ratio = 1.0
    if spec.mode == "translate":
        ratio = LENGTH_RATIO.get((detect_language(MARK_RE.sub(" ", masked)), spec.language), 1.0)
    elif spec.target_words:
        ratio = spec.target_words / total
    return [Segment(i, source=p, words=max(1, int(round(count_words(p) * ratio))), marks=marks_in(p)) for i, p in enumerate(pieces)]


# -- prompts -----------------------------------------------------------------------------------------------------------

def brief(spec: Spec, plan: List[Segment], title: str = "") -> str:
    lang = LANGUAGES.get(spec.language, spec.language)
    rules = [
        "Write only the part asked in the current message: no preface, no notes, no remarks about the task.",
        "Start exactly where the previous part ended. Never restate earlier parts. Never summarise or conclude unless this is the last part.",
        "Copy every marker of the form ⟦NAME12⟧ unchanged, once each, in the same place. Never translate, explain or change a marker.",
        "Keep headings, lists, tables, numbering and Markdown structure.",
        "Keep to the length asked for the part.",
    ]
    if spec.glossary:
        rules.append("Use the glossary terms exactly.")
    if lang:
        rules.append(f"Write in {lang}.")
    lines = ["You are writing one long text in separate parts, one part per message. Rules:"]
    lines += [f"{i}. {r}" for i, r in enumerate(rules, 1)]
    if spec.mode == "write":
        lines.append(f"\nTask: {spec.task}")
        if title:
            lines.append(f"Title: {title}")
        if spec.outline:
            lines.append("Plan of the whole text (it is written in parts, so keep to your part):")
            lines += [f"{i}. {r['heading'] + ': ' if r['heading'] else ''}{r['goal']}".rstrip()
                      + (f" (about {r['target_words']} words)" if r["target_words"] else "") for i, r in enumerate(spec.outline, 1)]
        elif any(s.heading or s.goal for s in plan):
            lines.append("Plan:")
            lines += [f"{s.index + 1}. {s.heading + ': ' if s.heading else ''}{s.goal}".rstrip() for s in plan]
    elif spec.mode == "translate":
        lines.append(f"\nTranslate the source into {lang}, part by part, faithfully and completely: every sentence, nothing added, "
                     "nothing left out.")
    else:
        lines.append(f"\nRewrite the source part by part. {spec.task}\nKeep the facts, the order and everything not asked to change.")
    if spec.style:
        lines.append(f"Style: {spec.style}")
    if spec.glossary:
        lines.append("Glossary:")
        lines += [f"- {k} => {v}" for k, v in spec.glossary.items()]
    return "\n".join(lines)


def segment_request(spec: Spec, seg: Segment, total: int, ask: int, headings: bool, anchor: str = "", done: Optional[List[str]] = None) -> str:
    k = seg.index + 1
    out = [f"Part {k} of {total}."]
    if done:
        out.append("Already written, one line per part:\n" + "\n".join(done))
    if spec.mode == "write":
        if seg.rows:
            out.append("Sections of the plan in this part, in order: " + "; ".join(
                f"{n}. {h or 'untitled'} ({'starts here' if first else 'continues from the previous part'})" for n, h, first in seg.rows))
        if headings:
            begins = [h for _, h, first in seg.rows if first and h] if seg.rows else ([seg.heading] if seg.heading else [])
            if begins:
                out.append("Write these headings where their sections start: " + ", ".join(f"`## {h}`" for h in begins) + ".")
        if seg.instruction:
            out.append("Your task for this part:\n" + seg.instruction)
        elif seg.goal:
            out.append(f"Goal: {seg.goal}")
        if seg.points:
            out.append("Key points:\n" + "\n".join(f"- {p}" for p in seg.points))
        out.append(f"Length: about {ask} words.")
        out.append("Begin the text." if k == 1 else "Restart exactly after the last text written. Do not summarise what came before.")
        out.append("This is the last part: close the text properly." if k == total else "Do not conclude.")
    else:
        what = "translation" if spec.mode == "translate" else "rewritten text"
        out.append("Source of this part (between the lines):\n-----\n" + seg.source + "\n-----")
        out.append(f"Output only the {what} of this part, about {ask} words.")
    if anchor:
        out.append("The text so far ends with (do not repeat it):\n-----\n" + anchor + "\n-----")
    return "\n".join(out)


def done_line(seg: Segment) -> str:
    """One line for a part already written, for the prompt of a layout that no longer carries the parts themselves."""
    label = "; ".join(h for _, h, _first in seg.rows if h) or seg.heading or seg.goal[:80] or "part"
    return f"{seg.index + 1}. {label[:120]} (done)"


def correction(issues: List[Issue]) -> str:
    return ("Correction: your previous attempt was rejected: " + "; ".join(f"{i.code} ({i.detail})" if i.detail else i.code for i in issues)
            + ". Write this part again, complete and correct, with every marker exactly once.")


CONTINUE_NOTE = "Continue exactly where you stopped. Do not repeat anything already written."


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    return int(sum(len(str(m.get("content") or "")) for m in messages) / CHARS_PER_TOKEN) + 8 * len(messages)


def _side() -> Dict[str, Any]:
    """The request fields that select the short technical session for the short calls (estimate, plan, review); empty where this build has none."""
    try:
        from harness.side import side_extra
    except ImportError:
        return {}
    return dict(side_extra())


# -- the run -----------------------------------------------------------------------------------------------------------

class Cancelled(Exception):
    """Raised inside a model call that the person stopped."""


@dataclass
class Totals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    calls: int = 0

    def add(self, usage: Dict[str, Any]) -> None:
        self.calls += 1
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.cached_tokens += int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)


class LongAnswer:
    """Writes one long text. `run()` yields events as dicts with an `event` key:

    plan, segment_start, segment_progress, segment_done, delta, verify, done. Heartbeats are `None`. A model error is raised after
    the events of what was already delivered. `cancel` is checked between calls and inside them."""

    def __init__(self, client: Any, spec: Spec, *, prior: Optional[List[Dict[str, Any]]] = None, system: str = "",
                 cancel: Optional[threading.Event] = None, sampling: Optional[Dict[str, Any]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.client, self.spec = client, spec
        self.prior = list(prior or [])              # earlier conversation turns (text), kept ahead of the parts
        self.system = system.strip()
        self.cancel = cancel or threading.Event()
        self.sampling = dict(sampling or {})
        self.clock = clock
        self.totals = Totals()
        self.prot = Protected()
        self.warnings: List[Dict[str, Any]] = []
        self.parts: List[str] = []                  # accepted segments, markers restored, in order
        self.title = ""
        self._at = 0                                # the segment being written (1-based), 0 while planning

    # -- model calls

    def _call(self, messages: List[Dict[str, Any]], max_tokens: int, *, side: bool = False,
              response_format: Optional[Dict[str, Any]] = None) -> Iterator[Any]:
        """Yields None or a segment_progress event while the model writes; returns (text, finish). A `side` call is a short technical
        one: sampling off, the side session's fields added, and `response_format` constrains the reply."""
        out: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        text: List[str] = []

        def on_delta(kind: str, piece: str) -> None:
            if self.cancel.is_set():
                from harness.client import StreamCancelled
                raise StreamCancelled()
            if kind == "content":
                text.append(piece)

        def work() -> None:
            try:
                if side:
                    result = self.client.chat_completion(messages=messages, max_tokens=max_tokens, temperature=0.0, response_format=response_format,
                                                         extra={"reasoning_effort": "none", **_side()}, timeout=SIDE_TIMEOUT_S, on_delta=on_delta)
                else:
                    extra = {"reasoning_effort": "none", **self.sampling}
                    if self.spec.end_turn_bias is not None:
                        extra["end_turn_bias"] = self.spec.end_turn_bias
                    result = self.client.chat_completion(messages=messages, max_tokens=max_tokens, extra=extra, timeout=3600.0, on_delta=on_delta)
                out.put(("done", result))
            except BaseException as exc:                 # handed to the caller's thread
                out.put(("error", exc))
        thread = threading.Thread(target=work, daemon=True, name="longform-call")
        thread.start()
        last = 0.0
        while True:
            try:
                kind, value = out.get(timeout=0.5)
            except queue.Empty:
                now = self.clock()
                if now - last >= 1.0 and not side:
                    last = now
                    yield {"event": "segment_progress", "index": self._at, "words": sum(len(p.split()) for p in text)}
                else:
                    yield None
                continue
            break
        if kind == "error":
            from harness.client import StreamCancelled
            if isinstance(value, StreamCancelled) or self.cancel.is_set():
                raise Cancelled()
            raise value
        choice = value["choices"][0]
        self.totals.add(value.get("usage") or {})
        return (choice["message"].get("content") or "", choice.get("finish_reason") or "stop")

    def _complete(self, messages: List[Dict[str, Any]], cap: int) -> Iterator[Any]:
        """One part, continued when the limit or a mid-sentence end cut it. Returns (text, finish, continuations)."""
        text, finish = yield from self._call(messages, cap)
        conts = 0
        while conts < CONTINUATIONS and (finish == "length" or (finish == "stop" and ends_midword(text))) and not self.cancel.is_set():
            conts += 1
            more = [*messages, {"role": "assistant", "content": text[:cut_point(text)]}, {"role": "user", "content": CONTINUE_NOTE}]
            piece, finish = yield from self._call(more, cap)
            text = join_continuation(text, piece)
        return text, finish, conts

    # -- the plan

    def _plan(self) -> Iterator[Any]:
        spec = self.spec
        if spec.mode != "write":
            masked, self.prot = protect(spec.source) if spec.protect else (spec.source.replace("\r\n", "\n"), Protected())
            plan = source_plan(spec, masked, self.prot)
            if len(plan) > spec.max_segments:
                raise LongformError(f"the source needs {len(plan)} segments, over the limit of {spec.max_segments}; raise max_segments")
            return plan, False
        if spec.outline:
            self.title = spec.title
            plan = chunks_from_outline(spec.outline, spec.target_words, spec.segment_words)
            if len(plan) > spec.max_segments:
                raise LongformError(f"the plan needs {len(plan)} parts, over the limit of {spec.max_segments}; raise max_segments")
            return plan, False
        sections = spec.sections or max(1, round(spec.target_words / spec.segment_words))
        if sections > spec.max_segments:
            raise LongformError(f"the text needs {sections} segments, over the limit of {spec.max_segments}; raise max_segments")
        if sections == 1:
            return [Segment(0, goal="", words=spec.target_words)], False
        messages = [{"role": "user", "content": outline_request(spec, sections)}]
        for _ in range(2):
            reply, _finish = yield from self._call(messages, 2000)
            found = parse_outline(reply, sections, spec.target_words)
            if found:
                self.title, plan = found
                if len(plan) > spec.max_segments:
                    raise LongformError(f"the outline has {len(plan)} sections, over the limit of {spec.max_segments}")
                return plan, False
            messages = [*messages, {"role": "assistant", "content": reply},
                        {"role": "user", "content": "That was not usable. Reply with only the JSON object, nothing before or after it."}]
        return even_outline(spec, sections), True

    # -- the parts

    def _segment(self, seg: Segment, plan: List[Segment], history: List[Dict[str, Any]], headings: bool, ask: int, rolling: bool,
                 system: str) -> Iterator[Any]:
        spec = self.spec
        lo, hi = seg.words * LOW_RATIO, ask * HIGH_RATIO
        cap = max(256, int(max(hi, seg.words * HIGH_RATIO) * TOKENS_PER_WORD) + 64)
        language = language_code(spec.language)
        earlier = "\n\n".join(self.parts)
        anchor = " ".join(earlier.split()[-ANCHOR_WORDS:]) if (rolling or seg.instruction) and earlier else ""
        done = [done_line(p) for p in plan[:seg.index]] if rolling else None
        user = segment_request(spec, seg, len(plan), ask, headings, anchor, done)
        base = [{"role": "system", "content": system}, *self.prior, *([] if rolling else history)]
        feedback: List[Issue] = []
        raw, issues, finish, tries = "", [], "stop", 0
        while True:
            content = user + (("\n\n" + correction(feedback)) if feedback else "")
            raw, finish, conts = yield from self._complete([*base, {"role": "user", "content": content}], cap)
            raw = normalize_marks(raw).strip()
            if spec.mode == "write" and self.parts:
                raw = trim_overlap(self.parts[-1], raw)
            issues = check_segment(raw, source=seg.source, expected_marks=seg.marks, finish=finish, lo=lo, hi=hi, language=language,
                                   mode=spec.mode, earlier=earlier, glossary=spec.glossary)
            again = [i for i in issues if i.retry]
            if not again or tries >= spec.retries or self.cancel.is_set():
                break
            tries += 1
            feedback = again
        return raw, issues, finish, tries, user

    def _finish_part(self, raw: str, seg: Segment) -> Tuple[str, List[Dict[str, Any]]]:
        """The segment as it goes into the answer: markers back, a marker used twice kept once, a block marker that was lost put
        at the end of the part. Returns (text, warnings)."""
        notes: List[Dict[str, Any]] = []
        seen: set = set()

        def once(m: "re.Match[str]") -> str:
            key = m.group(1)
            if key not in seg.marks or key in seen:
                return ""
            seen.add(key)
            return m.group(0)
        text = MARK_RE.sub(once, raw) if seg.marks or marks_in(raw) else raw
        lost = [k for k in seg.marks if k not in seen]
        if lost:
            blocks = [k for k in lost if re.sub(r"\d+$", "", k) in BLOCK_KINDS or k.startswith("IMG")]
            if blocks:
                text += "\n\n" + "\n\n".join(f"⟦{k}⟧" for k in blocks)
            notes.append({"code": "markers_lost", "segment": seg.index + 1, "detail":
                          "; ".join(f"{k}: {self.prot.items.get(k, '')[:60]!r}" for k in lost[:6]), "put_back": blocks})
        return self.prot.restore(text), notes

    def run(self) -> Iterator[Optional[Dict[str, Any]]]:
        spec = self.spec
        started = self.clock()
        try:
            plan, fallback = yield from self._plan()
        except Cancelled:
            yield {"event": "done", "finish": "length", "stopped": "cancelled", "words": 0, "target_words": 0, "segments": 0, "of": 0,
                   "warnings": [], "seconds": round(self.clock() - started, 1),
                   "usage": {"prompt_tokens": self.totals.prompt_tokens, "completion_tokens": self.totals.completion_tokens,
                             "cached_tokens": self.totals.cached_tokens, "calls": self.totals.calls}}
            return
        headings = spec.headings if spec.headings is not None else (spec.mode == "write" and len(plan) >= 3)
        yield {"event": "plan", "mode": spec.mode, "title": self.title, "fallback": fallback,
               "segments": [{"index": s.index + 1, "heading": s.heading, "words": s.words} for s in plan],
               "target_words": sum(s.words for s in plan)}
        system = brief(spec, plan, self.title)
        if self.system:
            system = self.system + "\n\n" + system
        history: List[Dict[str, Any]] = []
        context = spec.context_tokens
        rolling, short_run, produced, cancelled, stopped = False, 0, 0, False, ""
        truncated = False
        for seg in plan:
            if self.cancel.is_set():
                cancelled = True
                break
            if spec.max_seconds and self.clock() - started > spec.max_seconds:
                stopped = "time"
                break
            ask = seg.words
            if spec.mode == "write" and short_run and seg.index:
                ask = min(int(seg.words * 1.5), int(seg.words * seg.words / max(1, produced)))
            probe = [{"role": "system", "content": system}, *self.prior, *history,
                     {"role": "user", "content": segment_request(spec, seg, len(plan), ask, headings)}]
            if context and not rolling and estimate_tokens(probe) > context * TRANSCRIPT_SHARE:
                rolling = True
            self._at = seg.index + 1
            yield {"event": "segment_start", "index": seg.index + 1, "of": len(plan), "heading": seg.heading, "words": seg.words,
                   "layout": "rolling" if rolling else "transcript"}
            try:
                got = yield from self._segment(seg, plan, history, headings, ask, rolling, system)
            except Cancelled:
                cancelled = True
                break
            raw, issues, finish, tries, user = got
            if self.cancel.is_set():
                cancelled = True
                break
            text, notes = self._finish_part(raw, seg)
            produced = count_words(raw)
            warns = [{"code": i.code, "segment": seg.index + 1, "detail": i.detail} for i in issues if i.code != "markers_missing"] + notes
            truncated = truncated or any(i.code in ("truncated", "cut_midword") for i in issues)
            self.warnings += warns
            history += [{"role": "user", "content": user}, {"role": "assistant", "content": raw}]
            self.parts.append(text)
            yield {"event": "segment_done", "index": seg.index + 1, "of": len(plan), "words": count_words(text), "target": seg.words,
                   "retries": tries, "warnings": warns}
            for piece in ([text] if seg.index == 0 else ["\n\n" + text]):
                yield {"event": "delta", "text": piece}
            short_run = short_run + 1 if spec.mode == "write" and produced < 0.5 * seg.words else 0
            if short_run >= STOP_SHORT_SEGMENTS and seg.index + 1 < len(plan):
                stopped = "short_segments"
                self.warnings.append({"code": "stopped_short", "segment": seg.index + 1,
                                      "detail": f"the model wrote under half of what was asked in {STOP_SHORT_SEGMENTS} segments in a row"})
                break
        words = sum(count_words(p) for p in self.parts)
        if cancelled:
            stopped = "cancelled"
        elif len(self.parts) < len(plan) and not stopped:
            stopped = "incomplete"
        total = sum(s.words for s in plan)
        if stopped == "time":
            self.warnings.append({"code": "stopped_time", "segment": len(self.parts), "detail": "the time limit was reached"})
        yield {"event": "verify", "words": words, "target_words": total, "ratio": round(words / total, 2) if total else None,
               "segments": len(self.parts), "of": len(plan), "warnings": self.warnings}
        yield {"event": "done", "finish": "length" if (truncated or stopped in ("short_segments", "time", "incomplete", "cancelled")) else "stop",
               "stopped": stopped, "words": words, "target_words": total, "segments": len(self.parts), "of": len(plan),
               "warnings": self.warnings, "seconds": round(self.clock() - started, 1),
               "usage": {"prompt_tokens": self.totals.prompt_tokens, "completion_tokens": self.totals.completion_tokens,
                         "cached_tokens": self.totals.cached_tokens, "calls": self.totals.calls}}

    def text(self) -> str:
        return "\n\n".join(self.parts)
