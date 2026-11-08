"""The Rule of Two, kept as three facts about one conversation.

  U  the agent read content from outside the person's control (a page, a file it did not write, a tool's answer, an attachment, a note
     the planner made from an event, a recalled memory);
  P  it touched private data or a secret (a stored credential used, a file that holds keys, an attachment; the wallet and the vault hold the private data, the person's memories do not);
  E  the action it is about to take reaches the outside (a web address or query, a form, a request to a server).

When all three are true together the outward action asks the person first, and the question says which facts made it ask. The person
chooses how strict that is (`LEVELS`). A value that is a known secret inside an address, a query or a form field asks first too, unless the level is off.
"""
from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, unquote_plus

from tadamoo.secrets_broker import MIN_SECRET_LENGTH, shapes, url_hosts

LEVELS = ("off", "ask", "strict")
DEFAULT_LEVEL = "ask"
SHORT_SECRET = 8                      # a value shorter than this must stand alone in the text, not inside a longer word
MAX_LEARNED = 60
MAX_SOURCES = 6

# tools whose answer is text from the machine or from a process the agent started: it taints, and may hold keys
CONTENT_TOOLS = frozenset(("read_file", "list_directory", "shell_command", "job_log", "run_code", "repo_map", "search_code", "tree"))
# tools whose answer is the person's own data
PRIVATE_TOOLS = frozenset(("read_attachment", "search_attachment", "list_attachments", "transcribe_attachment", "recall_about_user", "request_payment"))

_PRIVATE_PATH = re.compile(
    r"(?:^|[\\/\s\"'=:])(?:"
    r"\.env(?:\.[\w.\-]+)?|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|\.npmrc|\.netrc|\.pypirc|\.pgpass|\.my\.cnf|\.git-credentials|\.dockercfg"
    r"|credentials(?:\.[\w]+)?|secrets?\.(?:json|ya?ml|toml|txt|env)|tokens?\.(?:json|txt)|wallet\.dat|login data|cookies|local state"
    r"|[\w.\-]+\.(?:pem|key|p12|pfx|ppk|kdbx|keystore|jks|asc|gpg)"
    r")(?=$|[\\/\s\"'|;&)<>])"
    r"|(?:^|[\\/])\.(?:ssh|aws|gnupg|kube|azure|config[\\/]gcloud)[\\/]", re.I)
_SECRET_KEY = r"(?:pass(?:word|wd)?|pwd|secret|token|api[_\-]?key|apikey|access[_\-]?key|private[_\-]?key|credential|auth|bearer|client[_\-]?secret)"
_ASSIGNMENT = re.compile(rf"(?im)[\w.\-]*{_SECRET_KEY}[\w.\-]*[\"']?\s*[:=]\s*[\"']?([^\s\"',;#<>]{{6,300}})")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(.+?)-----END [A-Z ]*PRIVATE KEY-----", re.S)
_TOKEN_SHAPES = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_\-]{20,}|xox[abprs]-[A-Za-z0-9\-]{10,}"
    r"|AIza[0-9A-Za-z_\-]{30,}|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}|apikey_[0-9a-f]{20,})\b")


def looks_private_path(text: str) -> bool:
    """Whether a path, or a command or program that names one, points at a file that holds keys or credentials."""
    return bool(_PRIVATE_PATH.search(text))


def secret_values(text: str, assignments: bool = False) -> List[str]:
    """The values in a text that are secrets by their shape (a private key, a known token form) and, when `assignments` (the text is a file
    made to hold credentials), by the name they are assigned to (KEY=value)."""
    out: List[str] = []
    if assignments:
        out += [m.group(1).rstrip("\"'") for m in _ASSIGNMENT.finditer(text)]
    for m in _PEM.finditer(text):
        out += [ln.strip() for ln in m.group(1).splitlines() if len(ln.strip()) >= 20]
    out += _TOKEN_SHAPES.findall(text)
    if assignments and not out and len(text.split()) == 1 and MIN_SECRET_LENGTH <= len(text.strip()) <= 300:
        out.append(text.strip())                     # a file that is one token
    seen: List[str] = []
    for v in out:
        if len(v) >= MIN_SECRET_LENGTH and v not in seen:
            seen.append(v)
    return seen


def _strings(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def _present(secret: str, haystacks: List[str]) -> bool:
    if len(secret) < SHORT_SECRET:
        pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(secret) + r"(?![A-Za-z0-9])")
        return any(pattern.search(h) for h in haystacks)
    return any(secret in h for h in haystacks)


class RuleOfTwo:
    def __init__(self, level: str = DEFAULT_LEVEL) -> None:
        self.level = DEFAULT_LEVEL
        self.set_level(level)
        self.untrusted: List[str] = []         # what the agent read from outside, first sources only
        self.private: List[str] = []           # what private data or secrets it touched
        self._learned: Dict[str, str] = {}     # label -> a secret value read in this conversation (from a file or an answer)

    def set_level(self, level: str) -> None:
        if level not in LEVELS:
            raise ValueError(f"the Rule of Two level must be one of {', '.join(LEVELS)}")
        self.level = level

    # -- the facts -----------------------------------------------------------------------------------

    @staticmethod
    def _add(sources: List[str], source: str) -> None:
        source = source or "unnamed"
        if source not in sources and len(sources) < MAX_SOURCES:
            sources.append(source)
        elif not sources:
            sources.append(source)

    def note_untrusted(self, source: str = "") -> None:
        self._add(self.untrusted, source)

    def note_private(self, source: str = "") -> None:
        self._add(self.private, source)

    def learn(self, label: str, text: str, assignments: bool = False) -> None:
        """Remembers the secret values in a text the agent was shown, so none of them can leave inside an address or a query."""
        for i, value in enumerate(secret_values(text, assignments)):
            if len(self._learned) >= MAX_LEARNED:
                break
            self._learned[f"{label}#{i + 1}"] = value

    def observe(self, tool: str, arguments: Any, output: Any, *, untrusted: bool, secrets: Any = None) -> None:
        """What a finished tool call tells about the conversation: where its text came from and whether it touched private data."""
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
        args = _strings(arguments)
        if untrusted or tool in CONTENT_TOOLS:
            self.note_untrusted(tool)
        if tool in PRIVATE_TOOLS:
            self.note_private(tool)
        if secrets is not None and any(secrets.has_references(a) for a in args):
            self.note_private("stored credential")
        if tool in CONTENT_TOOLS and any(looks_private_path(a) for a in args):
            self.note_private(f"{tool} of a file with keys")
            self.learn(tool, text, assignments=True)
        elif tool in CONTENT_TOOLS or untrusted:
            found = secret_values(text)
            if found:
                self.note_private(f"{tool} showed a key")
                self.learn(tool, text)

    # -- the decision ------------------------------------------------------------------------------

    @property
    def broken(self) -> bool:
        """Untrusted content and private data in one conversation (the third fact is the action asked about)."""
        return self.level != "off" and bool(self.untrusted) and bool(self.private)

    def question(self, tool: str) -> str:
        """The text that asks about an outward action, or '' when no question is due at this level."""
        if self.level == "off" or not self.untrusted:
            return ""
        read = f"(1) read content from outside your control ({', '.join(self.untrusted)})"
        if self.level == "ask":
            if not self.private:
                return ""
            return (f"Rule of Two: this conversation has {read}, (2) touched private data or secrets ({', '.join(self.private)}), "
                    f"and (3) {tool} reaches the outside. All three together are how instructions hidden in a page take data out.")
        return (f"Rule of Two, strict level: this conversation has {read}, and {tool} reaches the outside. "
                "Text it read could be steering this call.")

    def leak(self, arguments: Any, secrets: Any = None) -> str:
        """The name of a known secret whose value is inside the arguments of an outward call (any encoding the broker knows), or ''."""
        texts: List[str] = []
        for s in _strings(arguments):
            texts += [s, unquote(s), unquote_plus(s), unescape(s)]
        texts = list(dict.fromkeys(texts))
        if secrets is not None:
            hit = secrets.find_secret(texts, _present)
            if hit:
                return hit
        for label, value in self._learned.items():
            if any(_present(form, texts) for form in shapes(value)):
                return label
        return ""


    def hide(self, value: Any, secrets: Any = None) -> Any:
        """The arguments with every known secret value replaced, to show them to the person without showing the secret."""
        if isinstance(value, str):
            out = secrets.mask_secrets(value) if secrets is not None else value
            for known in self._learned.values():
                for form in shapes(known):
                    out = out.replace(form, "[hidden]")
            return out
        if isinstance(value, dict):
            return {k: self.hide(v, secrets) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.hide(v, secrets) for v in value]
        return value


class RuleOfTwoPrefs:
    """The person's choice of level, kept in a small file in the user data folder; the conversations started afterwards use it."""

    def __init__(self, path: "str | Path") -> None:
        self.path = Path(path)

    def read(self) -> str:
        try:
            level = json.loads(self.path.read_text(encoding="utf-8")).get("level")
        except (OSError, ValueError, AttributeError):
            return DEFAULT_LEVEL
        return level if level in LEVELS else DEFAULT_LEVEL

    def write(self, level: Any) -> str:
        if level not in LEVELS:
            raise ValueError(f"the level must be one of {', '.join(LEVELS)}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"level": level}), encoding="utf-8")
        return self.read()


def leak_question(tool: str, name: str, arguments: Any) -> str:
    hosts = sorted({h for text in _strings(arguments) for h in url_hosts(text)})
    where = ", ".join(hosts) if hosts else "a place outside this PC"
    return (f"{tool} would send the value of the secret '{name}' to {where}, inside an address, a query or a form field "
            "(the value is not shown here). A page the agent read could have asked for this.")
