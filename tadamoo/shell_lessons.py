"""Shell failures that are worth a lesson, found by code from the calls of one request.

A command that failed is paired with a later command of the same intent that worked, but only when something real changed between them: another
program, other flags, other arguments, or a file edit. A plain red test run and a failed look around (grep with no match, ls of a missing folder)
are part of the work and never count, unless the command could not run at all (not found, a module missing). The same command run again with
nothing changed in between is a flaky call, not a lesson."""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

SHELL_TOOLS = frozenset(("shell_command", "run_code"))
EDIT_TOOLS = frozenset(("write_file", "edit_file", "write_output_file"))
WINDOW = 12                         # calls between the failure and the command that worked
NOT_RUN_CODES = (127, 9009)         # the shell could not find the program
_OPERATORS = frozenset(("&&", "||", ";", "|", "&", ">", ">>", "<", "2>&1"))
_PREPARE = frozenset(("cd", "set", "export", "source", ".", "pushd", "setlocal", "echo"))
_FAMILY = {"python": "python", "python3": "python", "py": "python", "pythonw": "python", "pip": "pip", "pip3": "pip", "node": "node", "nodejs": "node"}
_SUBCOMMANDS = frozenset(("install", "run", "build", "test", "add", "i", "ci", "start", "exec", "-m"))
_TEST_PROGRAMS = frozenset(("pytest", "py.test", "tox", "nosetests", "jest", "vitest", "mocha", "ctest", "phpunit", "rspec"))
_RUNNERS = frozenset(("npm", "yarn", "pnpm", "bun", "cargo", "go", "dotnet", "mvn", "gradle", "gradlew", "make", "flutter", "deno", "swift"))
_TEST_WORDS = frozenset(("test", "tests", "check"))
_PYTHON_TEST_MODULES = frozenset(("pytest", "unittest", "nose", "tox"))
_LOOK_AROUND = frozenset(("grep", "rg", "ag", "find", "ls", "dir", "cat", "type", "head", "tail", "wc", "stat", "file", "which", "where", "whereis", "pwd",
                          "test", "[", "tree", "du", "df", "findstr", "select-string", "get-content", "get-childitem", "gci", "sls", "diff", "cmp"))
_GIT_LOOK = frozenset(("status", "diff", "log", "show", "branch", "ls-files", "grep", "remote", "blame", "rev-parse", "describe"))
_NOT_RUN = re.compile(r"command not found|is not recognized as|No module named|ModuleNotFoundError|Cannot find module|non è riconosciut|not recognized as the name of a cmdlet",
                      re.IGNORECASE)
_EXEC_SUFFIX = re.compile(r"\.(?:exe|cmd|bat|ps1|com)$")


@dataclass(frozen=True)
class Parts:
    program: str
    flags: frozenset
    subject: Tuple[str, ...]
    test: bool
    look: bool

    @property
    def family(self) -> str:
        return _FAMILY.get(self.program, self.program)


def _words(command: str) -> List[str]:
    try:
        words = shlex.split(command, posix=False)
    except ValueError:
        words = command.split()
    return [w.strip("\"'") for w in words]


def _segments(command: str) -> List[List[str]]:
    out: List[List[str]] = [[]]
    for w in _words(command):
        if w in _OPERATORS:
            out.append([])
        elif w:
            out[-1].append(w)
    return [s for s in out if s]


def _program(word: str) -> str:
    return _EXEC_SUFFIX.sub("", os.path.basename(word.replace("\\", "/")).lower())


def _is_test(program: str, rest: Sequence[str]) -> bool:
    low = [w.lower() for w in rest]
    if program in _TEST_PROGRAMS:
        return True
    if _FAMILY.get(program) == "python" and "-m" in low:
        at = low.index("-m") + 1
        return at < len(low) and low[at] in _PYTHON_TEST_MODULES
    if program in _RUNNERS:
        return any(w in _TEST_WORDS for w in low[:2])
    return False


def _is_look(program: str, rest: Sequence[str]) -> bool:
    if program == "git":
        return bool(rest) and rest[0].lower() in _GIT_LOOK
    return program in _LOOK_AROUND


def parse(command: str) -> Parts:
    """What a command line is, for comparing two of them: its program, flags and arguments (of the last real step of a chain), and whether any step
    of it is a test run, and whether it only looks around."""
    segments = _segments(command)
    steps = [(_program(s[0]), s[1:]) for s in segments if _program(s[0]) not in _PREPARE] or [(_program(s[0]), s[1:]) for s in segments[-1:]]
    if not steps:
        return Parts("", frozenset(), (), False, False)
    program, rest = steps[-1]
    flags = frozenset(w for w in rest if w.startswith("-") and w != "-")
    subject = tuple(w.replace("\\", "/").lower() for w in rest if not w.startswith("-") or w == "-")
    return Parts(program, flags, subject, any(_is_test(p, r) for p, r in steps), all(_is_look(p, r) for p, r in steps))


def _keys(parts: Parts) -> frozenset:
    """The arguments of a command as they identify what it works on: a path counts by its file name as well."""
    return frozenset(parts.subject) | frozenset(os.path.basename(w) for w in parts.subject) - {""}


def same_intent(a: Parts, b: Parts) -> bool:
    """Whether two commands work on the same thing: they share an argument that is not a mere subcommand, or have none and are of the same program."""
    if not a.subject and not b.subject:
        return bool(a.program) and a.family == b.family
    return bool(_keys(a) & _keys(b) - _SUBCOMMANDS)


def _changes(a: Parts, b: Parts, edits: Sequence[Any]) -> List[str]:
    out: List[str] = []
    if a.program != b.program:
        out.append(f"{b.program or 'another command'} instead of {a.program or 'the first one'}")
    added, dropped = sorted(b.flags - a.flags), sorted(a.flags - b.flags)
    if added:
        out.append("flags added: " + " ".join(added[:4]))
    if dropped:
        out.append("flags dropped: " + " ".join(dropped[:4]))
    if a.subject != b.subject:
        out.append("other arguments: " + " ".join(b.subject[:4]))
    if edits:
        out.append("after " + ", ".join(dict.fromkeys(f"{e.name} {e.path}".strip() for e in edits[:3])))
    return out


def failed(call: Any) -> bool:
    return call.decision == "executed" and ((call.code is not None and call.code != 0) or (call.code is None and call.status == "failed"))


def worked(call: Any) -> bool:
    return call.decision == "executed" and call.status == "completed" and call.code in (0, None)


def not_run(call: Any) -> bool:
    """The command could not run at all (program or module missing): not a failed test or a failed look."""
    return call.code in NOT_RUN_CODES or bool(_NOT_RUN.search(call.fail_text))


def find_fixes(calls: Sequence[Any]) -> List[Dict[str, Any]]:
    """The failures of shell and code calls that a later call of the same intent put right after a real change. Each is a dict with the failed
    and the working call, the changes between them and the file edits made in between."""
    out: List[Dict[str, Any]] = []
    pending: List[Tuple[Any, Parts]] = []
    for c in calls:
        if c.name not in SHELL_TOOLS or not c.command:
            continue
        parts = parse(c.command)
        if failed(c):
            if not_run(c) or not (parts.test or parts.look):
                pending.append((c, parts))
            continue
        if not worked(c):
            continue
        near = [(f, p) for f, p in reversed(pending) if c.index - f.index <= WINDOW and f.name == c.name and same_intent(p, parts)]
        if not near:
            continue
        bad, bad_parts = near[0]
        pending = [(f, p) for f, p in pending if not same_intent(p, parts)]
        edits = [x for x in calls[bad.index + 1:c.index] if x.name in EDIT_TOOLS and x.status == "completed"]
        changes = _changes(bad_parts, parts, edits)
        if changes:
            out.append({"failed": bad, "fixed": c, "failed_parts": bad_parts, "fixed_parts": parts, "changes": changes, "edits": edits})
    return out
