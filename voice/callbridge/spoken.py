"""What the voice may say about a conversation: plain sentences, never code, and one sentence about what the agent did.

`CodeStripper` drops fenced code and inline code from a stream of text. `plain_speech` makes a whole reply speakable. `Facts` collects what
an agent session did (files changed, test runs, commands, the end) from the session's events, and `what_was_done` turns the facts into one
sentence in Italian or English: counts and outcomes only, never a file's text, a command or a tool's output. `parse_yes_no` reads the answer to a
spoken question."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .text_sanitize import sanitize_for_tts

_FENCE = "`"


class CodeStripper:
    """Removes code spans from streamed text: a run of backticks opens a span that the next run of the same length closes. Text inside is dropped.

    Trailing backticks are held until the next chunk says how long the run is. `flush()` ends the stream: an unclosed span stays dropped."""

    def __init__(self) -> None:
        self._open = 0          # the length of the run that opened the current span, 0 outside one
        self._held = ""         # backticks at the end of the last chunk, not yet counted

    def feed(self, chunk: str) -> str:
        text = self._held + chunk
        self._held = ""
        out: list[str] = []
        i, n = 0, len(text)
        while i < n:
            if text[i] != _FENCE:
                j = text.find(_FENCE, i)
                j = n if j < 0 else j
                if not self._open:
                    out.append(text[i:j])
                i = j
                continue
            j = i
            while j < n and text[j] == _FENCE:
                j += 1
            if j == n:                              # the run may go on in the next chunk
                self._held = text[i:]
                break
            run = j - i
            if not self._open:
                self._open = run
            elif run == self._open:
                self._open = 0
            i = j
        return "".join(out)

    def flush(self) -> str:
        self._held = ""
        self._open = 0
        return ""


def strip_code(text: str) -> str:
    stripper = CodeStripper()
    return stripper.feed(text) + stripper.flush()


_CODE_LINE = re.compile(r"^(?: {4}|\t)\S|^\s*(?:\$ |>>> |diff --git|@@ |[+-]{3} |Traceback \(most recent)")


def plain_speech(text: str) -> str:
    """A reply made speakable: code (fenced, inline, indented, command lines), reasoning and markup are gone."""
    kept = [ln for ln in strip_code(text or "").split("\n") if not _CODE_LINE.match(ln)]
    return sanitize_for_tts("\n".join(kept)).strip()


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def first_sentences(text: str, count: int = 2, limit: int = 240) -> str:
    """The first `count` sentences of already plain text, cut at `limit` characters on a word."""
    parts = [p for p in _SENTENCE_END.split(text.strip()) if p]
    out = " ".join(parts[:count])
    if len(out) > limit:
        out = out[:limit].rsplit(" ", 1)[0].rstrip(",;:") if " " in out[:limit] else out[:limit]
    return out


def language_of(name: str) -> str:
    """`it` or `en` from a language name or code (the voice session's reply language); English for anything else."""
    low = (name or "").strip().lower()
    return "it" if low.startswith("it") else "en"


# ------------------------------------------------------------------------------------------------ what the agent did
#: tools that change a file, and the tool that runs a command
FILE_TOOLS = ("write_file", "edit_file")
COMMAND_TOOL = "shell_command"
_TEST_COMMAND = re.compile(r"^(?:(?:python[\d.]*|py)\s+-m\s+|uv\s+run\s+|poetry\s+run\s+|npx\s+)?"
                           r"(?:pytest|unittest|tox|nosetests|jest|vitest|mocha|phpunit|rspec|ctest"
                           r"|(?:npm|yarn|pnpm)\s+(?:run\s+)?test|cargo\s+test|go\s+test|dotnet\s+test|make\s+test)\b", re.IGNORECASE)
_STATEMENTS = re.compile(r"&&|\|\||;")


def is_test_command(command: str) -> bool:
    """Whether one of the statements of a shell command starts a test runner."""
    return any(_TEST_COMMAND.match(part.strip()) for part in _STATEMENTS.split(command or ""))


@dataclass
class Facts:
    """What one agent turn did, counted from the session's events. Nothing here holds a file's text, a command or a tool's output."""
    files: set = field(default_factory=set)       # paths (as the tool gave them) of files written or edited successfully
    file_failures: int = 0
    test_runs: int = 0
    test_failures: int = 0
    commands: int = 0                             # commands that are not test runs
    command_failures: int = 0
    other_failures: int = 0                       # any other tool that failed
    stop: str = ""                                # the turn's stop reason
    error: bool = False
    asked: bool = False                           # the turn ended waiting for the person's answer
    reply: str = ""                               # the agent's answer text, for the screen and, when nothing was done, for the voice
    _starts: dict = field(default_factory=dict, repr=False)

    @property
    def acted(self) -> bool:
        return bool(self.files or self.file_failures or self.test_runs or self.commands or self.other_failures)

    def tool_start(self, call_id: str, name: str, arguments: dict) -> None:
        self._starts[call_id] = (name, arguments if isinstance(arguments, dict) else {})

    def tool_end(self, call_id: str, name: str, status: str) -> None:
        started = self._starts.pop(call_id, (name, {}))
        name, arguments = started
        ok = status == "completed"
        if name in FILE_TOOLS:
            if ok:
                self.files.add(str(arguments.get("path") or call_id))
            else:
                self.file_failures += 1
        elif name == COMMAND_TOOL:
            if is_test_command(str(arguments.get("command") or "")):
                self.test_runs += 1
                self.test_failures += 0 if ok else 1
            else:
                self.commands += 1
                self.command_failures += 0 if ok else 1
        elif not ok and name not in ("ask_user",):
            self.other_failures += 1


_TEXT = {
    "en": {
        "done": "Done: {parts}.", "problems": "Finished with problems: {parts}.",
        "files": ("{n} file changed", "{n} files changed"), "tests_ok": "tests passed", "tests_bad": "tests failed",
        "commands": ("{n} command ran", "{n} commands ran"), "cmd_failed": ("{n} failed", "{n} failed"),
        "file_failed": ("a file change failed", "{n} file changes failed"), "other": ("a step failed", "{n} steps failed"),
        "error": "It stopped with an error. The details are on the screen.", "cancelled": "Stopped.",
        "asked": "The agent has a question. It is on the screen.", "screen": "Done. The answer is on the screen.",
        "nothing": "Nothing was changed. The answer is on the screen.",
    },
    "it": {
        "done": "Fatto: {parts}.", "problems": "Finito con dei problemi: {parts}.",
        "files": ("{n} file modificato", "{n} file modificati"), "tests_ok": "test superati", "tests_bad": "test falliti",
        "commands": ("{n} comando eseguito", "{n} comandi eseguiti"), "cmd_failed": ("{n} fallito", "{n} falliti"),
        "file_failed": ("una modifica a un file è fallita", "{n} modifiche ai file sono fallite"), "other": ("un passaggio è fallito", "{n} passaggi falliti"),
        "error": "Mi sono fermato con un errore. I dettagli sono sullo schermo.", "cancelled": "Fermato.",
        "asked": "L'agente ha una domanda. È sullo schermo.", "screen": "Fatto. La risposta è sullo schermo.",
        "nothing": "Non è cambiato niente. La risposta è sullo schermo.",
    },
}


def _plural(forms: tuple, n: int) -> str:
    return forms[0 if n == 1 else 1].format(n=n)


def what_was_done(facts: Facts, lang: str = "en") -> str:
    """One sentence about what the turn did, from the counts. When nothing was done and the agent only answered, its answer made
    speakable (code and markup removed, at most two sentences) is what is said; when that leaves nothing, a line that sends the person
    to the screen."""
    t = _TEXT[language_of(lang)]
    if facts.stop == "cancelled":
        return t["cancelled"]
    if facts.error or facts.stop == "error":
        return t["error"]
    if facts.asked:
        return t["asked"]
    if facts.acted:
        parts = []
        if facts.files:
            parts.append(_plural(t["files"], len(facts.files)))
        if facts.file_failures:
            parts.append(_plural(t["file_failed"], facts.file_failures))
        if facts.test_runs:
            parts.append(t["tests_bad"] if facts.test_failures else t["tests_ok"])
        if facts.commands:
            parts.append(_plural(t["commands"], facts.commands) + (": " + _plural(t["cmd_failed"], facts.command_failures) if facts.command_failures else ""))
        if facts.other_failures:
            parts.append(_plural(t["other"], facts.other_failures))
        bad = facts.file_failures or facts.test_failures or facts.command_failures or facts.other_failures
        return t["problems" if bad else "done"].format(parts=", ".join(parts))
    speakable = first_sentences(plain_speech(facts.reply))
    if speakable:
        return speakable
    return t["screen"] if facts.reply.strip() else t["nothing"]


# ------------------------------------------------------------------------------------------------ yes or no
_YES = re.compile(r"^\W*(?:yes|yeah|yep|sure|ok(?:ay)?|go ahead|do it|approve[d]?|allow|proceed|s[ìi]|certo|va bene|ok|procedi|approvo|approva|autorizzo|fallo)\b", re.IGNORECASE)
_NO = re.compile(r"^\W*(?:no|nope|stop|cancel|deny|don'?t|do not|negative|annulla|nega|non farlo|fermo|ferma|rifiuto)\b", re.IGNORECASE)


def parse_yes_no(text: str) -> bool | None:
    """True for an approval, False for a refusal, None when the words are neither. Only a short utterance counts."""
    t = (text or "").strip()
    if not t or len(re.findall(r"\w+", t)) > 6:
        return None
    if _NO.match(t):
        return False
    if _YES.match(t):
        return True
    return None


# ------------------------------------------------------------------------------------------------ the brain switch and approval lines
SAYINGS = {
    "en": {"harness": "Okay, the agent answers now.", "direct": "Okay, direct conversation now.",
           "ask_file": "The agent wants to change a file. Say yes or no.", "ask_file_named": "The agent wants to change the file {name}. Say yes or no.", "ask_again": "Say yes or no.",
           "screen": "An approval is waiting on the screen.", "allowed": "Okay, allowed.", "denied": "Okay, not allowed.",
           "busy": "It is still working.", "down": "I cannot reach the assistant.", "refused": "The assistant refused the request.",
           "not_mic": "Only the person at the microphone can approve that."},
    "it": {"harness": "Va bene, ora risponde l'agente.", "direct": "Va bene, ora conversazione diretta.",
           "ask_file": "L'agente vuole modificare un file. Dimmi sì o no.", "ask_file_named": "L'agente vuole modificare il file {name}. Dimmi sì o no.", "ask_again": "Dimmi sì o no.",
           "screen": "Un'approvazione ti aspetta sullo schermo.", "allowed": "Va bene, consentito.", "denied": "Va bene, non consentito.",
           "busy": "Sta ancora lavorando.", "down": "Non riesco a raggiungere l'assistente.", "refused": "L'assistente ha rifiutato la richiesta.",
           "not_mic": "Solo chi è al microfono può approvare."},
}


def saying(key: str, lang: str = "en") -> str:
    return SAYINGS[language_of(lang)][key]
