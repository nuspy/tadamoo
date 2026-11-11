"""The knowledge archives for the functions of the voice service: the copilot, the meeting bot, calls and the avatar's direct brain.

One `Knowledge` belongs to one session (a meeting, a call, one copilot window, one avatar conversation); it is never kept in the shared brain objects. It asks the
server (`/api/voice/rag/passages`, the only door: the service has no access to `/api/rag/*`) for the passages that match the words that were just said, inside a time
budget, and hands back a data block for the TAIL of the prompt. Whatever the other people said is untrusted: only the server builds the query from it (cleaned, cut,
used as search text and nowhere else).

Nothing here waits for a person. When the answer is late or fails, the result says so in `marker` and has no block: the reply goes on without the archives, never silently.

The consent of other people's ears: an archive's `others_ok` says whether its content may be said, written or shown to other people. When a passage of an archive that
asks would reach them, the server leaves it out and names the archive in `pending`; this module opens the question (a dialog on the person's screen, and for
`ask_voice` also aloud over a channel the others cannot hear), remembers the answer for the session, and the passage goes in the next time. The private copilot window
needs no answer: `confirm_output` asks only when a suggestion is really said, written or shown to the others."""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field

from .tadamoo_api import ApiError, TadamooApi

log = logging.getLogger("callbridge.knowledge")

PASSAGES, ASK, ASK_ANSWER, WARM = "/api/voice/rag/passages", "/api/voice/rag/ask", "/api/voice/rag/ask/{id}/answer", "/api/voice/rag/warm"
GRACE_S = 1.5                  # how long past the budget the service waits for the server before it gives up
POLL_S = 1.0
RETRY_AFTER_S = 120.0          # a question nobody answered is not put again at once
OUTPUT_WAIT_S = 180.0          # a person who clicked to say a reply is waited for this long, as long as the server keeps the question
BUDGET_MS = {"copilot": 1200, "meeting": 1200, "call": 900, "avatar": 900, "minutes": 20000}
ANSWERS = ("once", "meeting", "always", "no")
MIN_WORDS = 3                  # a shorter utterance ("ok", "yes, go on") is not worth a search and its delay; the minutes are searched whatever the length


@dataclass
class Passages:
    """What came back: `block` goes at the tail of the prompt ("" when there is none), `sources` are the archives its passages came from (`others_ok` each), `marker` is
    "" or why the search was not complete (words only, too late, failed), `pending` the archives that wait for the person's answer."""
    block: str = ""
    sources: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    marker: str = ""
    profile: str = ""
    elapsed_ms: int = 0
    timed_out: bool = False
    blocked: list = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [s["archive"] for s in self.sources]


def _plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (text or "").lower()) if unicodedata.category(c) != "Mn")


_NO = r"\b(no|not|non|niente|nope|nein|never|mai|stop|dont|don t)\b"
_ALWAYS = r"\b(always|sempre|per sempre|every time|ogni volta)\b"
_MEETING = r"\b(meeting|riunione|chiamata|call|conversazione|conversation|session|sessione)\b"
_ONCE = r"\b(yes|yeah|ok|okay|si|sure|certo|va bene|volta|once|time|ora|adesso|now|just this)\b"


def parse_choice(text: str) -> str | None:
    """The answer in the person's words, in English or Italian: once, meeting, always, no; None when it is not one of them. A bare yes is "once": the narrowest yes, and any word of refusal makes it no."""
    low = _plain(text)
    if not low.strip():
        return None
    if re.search(_NO, low):                                   # any refusal wins: a wrong no only leaves something unsaid
        return "no"
    if re.search(_ALWAYS, low):
        return "always"
    if re.search(_MEETING, low):
        return "meeting"
    if re.search(_ONCE, low):
        return "once"
    return None


def question_text(archive: str, preview: str, how: str, lang: str = "en") -> str:
    """The question for the person's ears or eyes: the archive named, and what would be output."""
    verb = {"spoken": ("said", "detto"), "written": ("written", "scritto"), "shown": ("shown", "mostrato")}.get(how, ("said", "detto"))
    if (lang or "").lower().startswith("it"):
        return (f"L'archivio {archive} contiene qualcosa che potrei dire agli altri, ad esempio: {preview[:120]}. Posso? Rispondi: sì, questa volta; per questa riunione; "
                f"sempre per questo archivio; oppure no.")
    return (f"The archive {archive} has something that could be {verb[0]} to the other people, for example: {preview[:120]}. May I? Answer: yes, this time; "
            f"for this meeting; always for this archive; or no.")


class VoiceAsker:
    """Asks a question aloud over a channel the others cannot hear and takes the answer from the person's own voice. `say(text) -> bool` plays it on the private route
    (False: there is none); `listen(timeout_s) -> str | None` is what the person says next into their own microphone. Both are given by the service; without them this
    object is not made and the question goes to the dialog alone."""

    def __init__(self, say: Callable[[str], bool], listen: Callable[[float], str | None], wait_s: float = 25.0, lang: str = "en") -> None:
        self.say, self.listen, self.wait_s, self.lang = say, listen, wait_s, lang

    def ask(self, archive: str, preview: str, how: str) -> str | None:
        """The answer (once, meeting, always, no) or None when the question could not be played, nothing was understood, or the person did not answer in time."""
        try:
            if not self.say(question_text(archive, preview, how, self.lang)):
                return None
            for _ in range(2):                                    # one more try when the words were not an answer
                got = parse_choice(self.listen(self.wait_s) or "")
                if got:
                    return got
        except Exception as e:  # noqa: BLE001 - recognition or the device failed: the dialog is still open
            log.warning("the question aloud failed: %s", e)
        return None


class Knowledge:
    """The archives of one session. `archives` are ids; `function` is copilot, meeting, call or avatar (minutes use `Knowledge` with `function="minutes"`)."""

    def __init__(self, cfg, function: str, archives, session: str = "", api=None, asker: VoiceAsker | None = None, lang: str = "en", alone: bool = False,
                 spawn: Callable[[Callable[[], None], str], None] | None = None, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg, self.function, self.lang, self.alone = cfg, function, lang, alone
        self.archives = [a for a in dict.fromkeys(archives or []) if isinstance(a, str) and a]
        self.session = session or f"{function}-{int(time.time())}"
        self._api, self.asker = api, asker
        self._spawn = spawn or (lambda fn, kind: threading.Thread(target=fn, name="knowledge-" + kind, daemon=True).start())     # kind: aloud | watch
        self._sleep, self._clock = sleep, clock
        self.granted: set[str] = set()            # for the rest of the session
        self.once: set[str] = set()               # for the next passage only
        self.denied: set[str] = set()
        self._asking: dict[str, str] = {}         # archive id -> question id, while it waits
        self._quiet_until: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.archives)

    def _client(self, timeout: float) -> TadamooApi:
        return self._api if self._api is not None else TadamooApi(self.cfg.tadamoo_base_url, self.cfg.tadamoo_token, timeout=timeout)

    def warm(self) -> None:
        """A meeting or a call is starting: have the server load its small embedder now. Never fails the session."""
        if not self.enabled:
            return
        try:
            self._client(5.0).post(WARM, {"archives": self.archives})
        except ApiError as e:
            log.info("the archive search could not be warmed: %s", e)

    # -- the passages ------------------------------------------------------------------------------------

    def fetch(self, words: str, how: str = "speech") -> Passages:
        """The passages for `words` (what somebody just said). `how` is `speech` when the answer will be spoken, `screen` when it is read."""
        if not self.enabled or len((words or "").split()) < (1 if self.function == "minutes" else MIN_WORDS):
            return Passages()
        budget = BUDGET_MS.get(self.function, 1200)
        with self._lock:
            granted = sorted(self.granted | self.once)
            denied = sorted(self.denied)
        body = {"function": self.function, "archives": self.archives, "query": words[:2000], "how": how, "granted": granted, "denied": denied, "budget_ms": budget}
        if self.alone:
            body["audience"] = "me"
        try:
            got = self._client(budget / 1000 + GRACE_S).post(PASSAGES, body)
        except ApiError as e:
            log.warning("the archive search failed: %s", e)
            return Passages(marker=f"no passages from the archives: the search did not answer ({e})", timed_out=True)
        out = Passages(str(got.get("block") or ""), list(got.get("sources") or []), list(got.get("pending") or []), str(got.get("marker") or ""), str(got.get("profile") or ""),
                       int(got.get("elapsed_ms") or 0), bool(got.get("timed_out")), list(got.get("blocked") or []))
        if out.block:
            with self._lock:
                self.once -= {s["archive_id"] for s in out.sources}      # a "this time" is used up by the passage that used it
        for p in out.pending:
            self._ask(p, "spoken" if how == "speech" else "shown")
        return out

    # -- the consent of the other people's ears ----------------------------------------------------------

    def _ask(self, pending: dict, how: str, force: bool = False) -> str | None:
        """Opens the question about an archive (once at a time) and watches it in the background; returns its id."""
        aid = pending["archive_id"]
        with self._lock:
            if aid in self._asking or (not force and (self._clock() < self._quiet_until.get(aid, 0.0) or aid in self.denied)) or aid in self.granted:
                return self._asking.get(aid)
            self._asking[aid] = ""
        try:
            voice = pending.get("ask") == "voice" and self.asker is not None
            q = self._client(10.0).post(ASK, {"session": self.session, "function": self.function, "archive_id": aid, "preview": pending.get("preview", ""), "how": how,
                                              "voice_ok": voice})["question"]
        except (ApiError, KeyError) as e:
            log.warning("the question about an archive could not be opened: %s", e)
            with self._lock:
                self._asking.pop(aid, None)
            return None
        with self._lock:
            self._asking[aid] = q["id"]
        if voice:
            self._spawn(lambda: self._ask_aloud(q["id"], pending, how), "aloud")
        self._spawn(lambda: self._watch(aid, q["id"]), "watch")
        return q["id"]

    def _ask_aloud(self, qid: str, pending: dict, how: str) -> None:
        answer = self.asker.ask(pending["archive"], pending.get("preview", ""), how)
        if answer in ANSWERS:
            try:
                self._client(10.0).post(ASK_ANSWER.format(id=qid), {"answer": answer})
            except ApiError as e:
                log.warning("the spoken answer could not be sent: %s", e)

    def _apply(self, aid: str, row: dict) -> None:
        answer, by = row.get("answer"), row.get("by")
        with self._lock:
            self._asking.pop(aid, None)
            if answer == "once":
                self.once.add(aid)
            elif answer in ("meeting", "always"):
                self.granted.add(aid)
            elif by == "timeout":
                self._quiet_until[aid] = self._clock() + RETRY_AFTER_S
            else:
                self.denied.add(aid)

    def _watch(self, aid: str, qid: str, limit_s: float = OUTPUT_WAIT_S) -> dict | None:
        """Polls the question until it is answered (the server answers no by itself after a while); the answer is kept for the session."""
        end = self._clock() + limit_s
        while self._clock() < end:
            try:
                row = self._client(10.0).get(ASK + "/" + qid)["question"]
            except (ApiError, KeyError):
                row = None
            if row is not None and row.get("state") == "answered":
                self._apply(aid, row)
                return row
            self._sleep(POLL_S)
        with self._lock:
            self._asking.pop(aid, None)
        return None

    def confirm_output(self, sources, how: str = "spoken", wait_s: float = OUTPUT_WAIT_S) -> tuple[bool, str]:
        """For a suggestion of the private copilot that the person chose to say, write or show to the others: (True, "") when every archive its passages came from may
        reach them, else (False, why). It waits for the person's answer, since the person asked for the output. Blocking: call it from the thread that outputs."""
        for s in sources or []:
            aid, setting, name = s["archive_id"], s.get("others_ok", "ask_dialog"), s.get("archive", "")
            if setting == "allow":
                continue
            if setting == "deny":
                return False, f"the archive {name} is not to be said to other people"
            with self._lock:
                if aid in self.granted:
                    continue
                if aid in self.once:
                    self.once.discard(aid)
                    continue
                if aid in self.denied:
                    return False, f"you answered no for the archive {name} in this session"
            qid = self._ask({"archive_id": aid, "archive": name, "ask": "voice" if setting == "ask_voice" else "dialog", "preview": s.get("preview", "")}, how, force=True)
            if qid is None:
                return False, f"the question about the archive {name} could not be opened"
            row = self._watch(aid, qid, wait_s)
            if row is None or row.get("answer") == "no":
                return False, f"not said: no answer yes about the archive {name}"
            with self._lock:
                self.once.discard(aid)
        return True, ""
