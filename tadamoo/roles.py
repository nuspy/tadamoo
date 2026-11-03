"""Roles: what a conversation's agent is for, and so which of its tools it may use.

A role is a block of instructions, a set of allowed tools and what it may write. The tool list sent to the model is the same in every role
(it is the cached start of each request), so a role never adds or hides a definition: a call to a tool outside the role is refused by name,
with the tools the role has, and a short note at the end of each request (never stored in the conversation) tells the model its role. The
`general` role has no note and no limit, which is how a conversation behaves without roles.

The role that builds code has one name, `CODING_ROLE`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

GENERAL = "general"
CODING_ROLE = "build"
ASK, PLAN, DEBUG, ORCHESTRATE = "ask", "plan", "debug", "orchestrate"
PLANS_GLOB = ".tadamoo/plans/*.md"
MAX_NOTE_TOKENS = 600
CHARS_PER_TOKEN = 3.5
SHOWN_CHECKS = 6

READ_TOOLS = ("read_file", "list_directory", "tree", "search_code", "repo_map", "repo_diagram", "diagnostics", "go_to_definition",
              "find_references", "recall", "load_skill")
ALWAYS_TOOLS = ("ask_user", "todo_write", "present_plan")          # plan mode and the checklist work in every role
EDIT_TOOLS = ("edit_file", "write_file")
FILE_WRITERS = frozenset(EDIT_TOOLS)
BUILD_TOOLS = READ_TOOLS + ALWAYS_TOOLS + EDIT_TOOLS + ("shell_command", "remember", "forget")


@dataclass(frozen=True)
class Role:
    id: str
    text: str                                   # what the model is told (empty: nothing)
    tools: Optional[FrozenSet[str]]             # None: every tool
    writes: Optional[Tuple[str, ...]] = None    # globs under the workspace the file-writing tools may touch; None: anywhere in it
    shell: bool = True                          # the role runs commands (the short read-only list needs no question)


_TEXT: Dict[str, str] = {
    CODING_ROLE: (
        "Role: build. You change the code in this folder to do what the person asks, then check it. Look before you change: tree, search_code "
        "and read_file. Change with edit_file (several edits in one call when they belong together), keep each change small, and run the "
        "project's own checks with shell_command after it. If a check fails, fix the cause and run it again. End by saying what you changed and "
        "what the checks showed."),
    DEBUG: (
        "Role: debug. You find the cause of a fault before you fix it. 1) Run the failing command once and read its real output. 2) List the "
        "possible causes, at most four. 3) Narrow them to one or two by reading the code and adding temporary logging. 4) Confirm the cause "
        "with evidence from a run. 5) Fix that cause, remove the temporary logging and run the failing command again to show it passes. "
        "Do not change the checks to make them pass."),
    PLAN: (
        "Role: plan. You plan and you do not change the project. Read what you need. Ask the person at most three questions with ask_user, "
        "and only for decisions that are theirs. Write the plan as a Markdown file under .tadamoo/plans/ (the steps, the files each one "
        "touches, how each one is checked), keep a checklist with todo_write, and end with a short summary of the plan. The person decides "
        "when building starts."),
    ASK: (
        "Role: ask. You answer questions about this project. Read and search; you cannot change files or run commands in this role. Name the "
        "file and line behind each claim. If the person wants a change made, say so: they can switch to another role."),
    ORCHESTRATE: (
        "Role: orchestrate. You split the work and hand each part to a subagent with spawn_subagent: one clear task each, naming the files "
        "and the check that proves it. Read what comes back and verify it with the read tools. You do not edit files or run commands yourself."),
}

ROLES: Dict[str, Role] = {
    GENERAL: Role(GENERAL, "", None),
    CODING_ROLE: Role(CODING_ROLE, _TEXT[CODING_ROLE], frozenset(BUILD_TOOLS)),
    DEBUG: Role(DEBUG, _TEXT[DEBUG], frozenset(BUILD_TOOLS)),
    PLAN: Role(PLAN, _TEXT[PLAN], frozenset(READ_TOOLS + ALWAYS_TOOLS + EDIT_TOOLS), writes=(PLANS_GLOB,), shell=False),
    ASK: Role(ASK, _TEXT[ASK], frozenset(READ_TOOLS + ALWAYS_TOOLS), shell=False),
    ORCHESTRATE: Role(ORCHESTRATE, _TEXT[ORCHESTRATE], frozenset(READ_TOOLS + ALWAYS_TOOLS + ("spawn_subagent",)), shell=False),
}
ROLE_IDS: Tuple[str, ...] = (CODING_ROLE, PLAN, ASK, DEBUG, ORCHESTRATE, GENERAL)       # the order the selector shows them in


def valid(role: Any) -> bool:
    return isinstance(role, str) and role in ROLES


def _glob_regex(glob: str) -> "re.Pattern[str]":
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return re.compile(out + r"\Z", re.IGNORECASE)


def relative_path(workspace: Any, path: str) -> str:
    """The path as the workspace sees it, with forward slashes; '' when it is not inside the workspace."""
    from pathlib import Path
    root = Path(workspace).resolve()
    try:
        target = (root / path).resolve()
        return target.relative_to(root).as_posix()
    except (ValueError, OSError):
        return ""


class RolePolicy:
    """The role of one conversation: which calls it allows, the note it adds to each request, and the commands it lets run without a question."""

    def __init__(self, workspace: Any, names: Callable[[], List[str]], role: str = GENERAL,
                 on_change: Optional[Callable[["Role"], None]] = None, checks: Optional[Callable[[], List[str]]] = None) -> None:
        self.workspace = workspace
        self._names = names                    # the tools the conversation has, in the order of its tool list
        self._on_change = on_change
        self._checks = checks                  # the commands the person approved to run without asking
        self.role = ROLES[GENERAL]
        self.set(role)

    @property
    def id(self) -> str:
        return self.role.id

    def set(self, role: str) -> Role:
        if not valid(role):
            raise ValueError(f"role must be one of {list(ROLES)}")
        self.role = ROLES[role]
        if self._on_change is not None:
            self._on_change(self.role)
        return self.role

    def allowed(self) -> List[str]:
        names = self._names()
        return names if self.role.tools is None else [n for n in names if n in self.role.tools]

    def check(self, name: str, arguments: Any) -> str:
        """Why this call is not for this role, in words the model can act on; '' when it is."""
        role = self.role
        if role.tools is None:
            return ""
        if name not in role.tools:
            return f"The tool '{name}' is not available in the {role.id} role. Tools you can use: {', '.join(self.allowed())}."
        if role.writes is not None and name in FILE_WRITERS and isinstance(arguments, dict) and isinstance(arguments.get("path"), str):
            rel = relative_path(self.workspace, arguments["path"])
            if not rel or not any(_glob_regex(g).match(rel) for g in role.writes):
                return f"In the {role.id} role {name} may change only {', '.join(role.writes)}; '{arguments['path']}' is outside that."
        return ""

    def note(self) -> str:
        """The text added at the end of each request in this role; the same text for as long as the role, the tools and the approved checks are."""
        role = self.role
        if not role.text:
            return ""
        lines = [f"[harness] {role.text}"]
        if role.tools is not None:
            lines.append(f"Tools you can use in this role: {', '.join(self.allowed())}. Others are refused.")
        if role.writes is not None:
            lines.append(f"You may write only {', '.join(role.writes)}.")
        checks = self._checks() if (self._checks is not None and role.shell) else []
        if checks:
            shown = ", ".join(f"`{c}`" for c in checks[:SHOWN_CHECKS]) + (f" and {len(checks) - SHOWN_CHECKS} more" if len(checks) > SHOWN_CHECKS else "")
            lines.append(f"The person approved these commands to run without asking: {shown}. Use them as written.")
        return "\n".join(lines)

    def view(self) -> Dict[str, Any]:
        return {"role": self.id, "tools": self.allowed(), "writes": list(self.role.writes or []), "shell": self.role.shell and "shell_command" in self.allowed()}


def note_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1

