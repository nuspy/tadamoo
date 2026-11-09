"""The consent rule of the meeting bot: the words that object or agree, what the bot announces, and the gate that decides whether it may stay.

The rule, in short. Nothing is listened to or kept before the announcement is out. After it, the gate waits `window_s` seconds (`explicit`
mode: up to twice that) for the meeting's speech and chat. An objection from anyone, the host removing the bot, or the person pressing Leave
ends the session at once. With no objection the bot stays only if the mode allows: `window` stays when the time is up, `explicit` needs a
yes. After consent, a plain stop word still ends the session. The word lists below are the only place that says what counts."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

MODES = ("window", "explicit")
WINDOW_MIN_S, WINDOW_MAX_S, WINDOW_DEFAULT_S = 20, 300, 45
EXPLICIT_FACTOR = 2

# An objection at any time: said or typed by anyone in the meeting.
STOP_PHRASES = (
    "stop", "stop recording", "esci", "leave", "remove", "go away", "vattene",
    "non acconsento", "non consento", "do not consent", "don't consent", "no consent",
    "don't record", "do not record", "non registrare", "non trascrivere", "non voglio essere registrato",
    "i object", "mi oppongo",
)
# An objection only while consent is still being asked for (a bare "no" in the middle of a meeting is not one).
WINDOW_PHRASES = ("no", "nope", "non voglio", "non sono d'accordo")
# A yes, which `explicit` mode needs.
CONSENT_PHRASES = ("ok", "okay", "yes", "sì", "va bene", "consent", "consento", "acconsento", "d'accordo", "certo", "sure")
# Phrases that contain an objection word and mean the opposite.
HARMLESS_PHRASES = ("no problem", "no worries", "nessun problema", "non c'è problema", "non ci sono problemi")

_BOUNDARY = r"(?<![\w'])(?:{})(?![\w'])"


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return text.replace("’", "'").replace("‘", "'").replace("`", "'")


def _pattern(phrases) -> "re.Pattern[str]":
    alts = "|".join(r"\s+".join(re.escape(w) for w in p.split()) for p in sorted(phrases, key=len, reverse=True))
    return re.compile(_BOUNDARY.format(alts))


_STOP = _pattern(STOP_PHRASES)
_WINDOW = _pattern(WINDOW_PHRASES)
_CONSENT = _pattern(CONSENT_PHRASES)
_HARMLESS = _pattern(HARMLESS_PHRASES)


def objection_in(text: str, asking: bool) -> str:
    """The word that makes `text` an objection, or "" when it is none. `asking`: consent has not been given yet."""
    plain = _HARMLESS.sub(" ", _normalize(text))
    found = _STOP.search(plain) or (_WINDOW.search(plain) if asking else None)
    return " ".join(found.group(0).split()) if found else ""


def consent_in(text: str) -> bool:
    return bool(_CONSENT.search(_normalize(text)))


def window_of(value) -> int:
    """The consent window as a whole number of seconds, or the default when it is not one in range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not WINDOW_MIN_S <= value <= WINDOW_MAX_S:
        return WINDOW_DEFAULT_S
    return int(value)


# ---------------------------------------------------------------------------------------- what the bot says
def _it(lang: str) -> bool:
    return (lang or "").lower().startswith("it")


def announcement(bot: str, who: str, lang: str, mode: str = "window") -> str:
    """The announcement in one language: who the bot is, for whom it is here, what it does, how to object."""
    if _it(lang):
        text = (f"Buongiorno, sono {bot}, un assistente basato su intelligenza artificiale, presente in questa riunione per conto di {who}. "
                "Trascrivo ciò che si dice per scrivere il verbale; l'audio non viene registrato. "
                "Se qualcuno non è d'accordo, dica o scriva «stop»: esco e cancello quanto ho sentito.")
        return text + (" Per farmi restare, dite o scrivete «ok»." if mode == "explicit" else "")
    text = (f"Hello, I am {bot}, an AI assistant attending this meeting on behalf of {who}. "
            "I transcribe what is said to write the minutes; no audio is recorded. "
            "If anyone objects, say or type \"stop\" and I will leave and delete what I heard.")
    return text + (" To let me stay, say or type \"ok\"." if mode == "explicit" else "")


def chat_announcement(bot: str, who: str, mode: str = "window") -> str:
    """The announcement for the meeting chat: both languages, so that nobody has to guess."""
    return announcement(bot, who, "en", mode) + "\n" + announcement(bot, who, "it", mode)


def reannouncement(bot: str, who: str, lang: str) -> str:
    if _it(lang):
        return f"È entrato qualcuno: sono {bot}, assistente IA di {who}, e trascrivo per il verbale. Per farmi uscire dite o scrivete «stop»."
    return f"Someone joined: I am {bot}, an AI assistant of {who}, transcribing for the minutes. Say or type \"stop\" and I will leave."


def goodbye(lang: str) -> str:
    return "Ricevuto, esco e cancello quanto ho sentito." if _it(lang) else "Understood, I am leaving and deleting what I heard."


def _words(text: str) -> list[str]:
    return re.findall(r"[\w']+", _normalize(text))


def _trigrams(words: list[str]) -> set:
    return {tuple(words[i:i + 3]) for i in range(len(words) - 2)}


def is_echo(heard: str, said: list[str], minimum: int = 4) -> bool:
    """Whether `heard` is the bot's own words coming back (a loopback that carries its voice, a chat that shows its message): most of its
    three-word runs are in what the bot said. Short sentences never count, so a lone "stop" is always taken as one."""
    words = _words(heard)
    if len(words) < minimum:
        return False
    mine = set()
    for line in said:
        mine |= _trigrams(_words(line))
    runs = _trigrams(words)
    return sum(1 for t in runs if t in mine) / len(runs) >= 0.6


# ---------------------------------------------------------------------------------------- the gate
WAITING, GRANTED, ENDED = "waiting", "granted", "ended"
OUTCOMES = ("granted_window", "granted_explicit", "objection", "removed", "left", "no_consent")


@dataclass
class ConsentGate:
    """Decides, from what happens after the announcement, whether the bot may stay. Pure: the clock is passed in."""
    mode: str = "window"
    window_s: int = WINDOW_DEFAULT_S
    state: str = "idle"                 # idle (nothing announced yet) | waiting | granted | ended
    outcome: str = ""
    objections: list = field(default_factory=list)       # [{"source", "reason"}], the matched word only
    ever_granted: bool = False
    t0: float = 0.0

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            self.mode = "window"
        self.window_s = window_of(self.window_s)

    @property
    def deadline_s(self) -> float:
        return self.window_s * (EXPLICIT_FACTOR if self.mode == "explicit" else 1)

    def announced(self, now: float) -> None:
        if self.state == "idle":
            self.state, self.t0 = WAITING, now

    def hear(self, text: str, source: str) -> str:
        """One thing said (`source` "voice") or typed ("chat") by someone else. Returns "objection" when it ends the session, else "". Ignored
        before the announcement and after the end."""
        if self.state not in (WAITING, GRANTED):
            return ""
        word = objection_in(text, asking=self.state == WAITING)
        if word:
            self.objections.append({"source": source, "reason": word})
            self._end("objection")
            return "objection"
        if self.state == WAITING and self.mode == "explicit" and consent_in(text):
            self._grant("granted_explicit")
        return ""

    def removed(self) -> None:
        """The host took the bot out (or the call dropped while consent was still asked for)."""
        self._end("removed")

    def left(self) -> None:
        """The person pressed Leave."""
        self._end("left")

    def poll(self, now: float) -> str:
        """The state once time has been taken into account."""
        if self.state == WAITING:
            if self.mode == "window" and now - self.t0 >= self.window_s:
                self._grant("granted_window")
            elif self.mode == "explicit" and now - self.t0 >= self.deadline_s:
                self._end("no_consent")
        return self.state

    def _grant(self, outcome: str) -> None:
        self.state, self.outcome, self.ever_granted = GRANTED, outcome, True

    def _end(self, outcome: str) -> None:
        if self.state != ENDED:
            self.state, self.outcome = ENDED, outcome

    @property
    def refused(self) -> bool:
        """The session ended without consent or against an objection: whatever was heard is to be deleted."""
        return self.state == ENDED and (self.outcome in ("objection", "removed", "no_consent") or (self.outcome == "left" and not self.ever_granted))
