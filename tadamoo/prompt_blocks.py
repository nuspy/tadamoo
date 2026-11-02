"""Short working rules for the system prompt (`--instruction-blocks`): act on a request, vary a search that found nothing, check what can
change, back a claim with evidence, never invent a result and never end a turn on a promise. A rule that names tools is worded with the
ones the conversation has and left out when it has none of them.
"""
from __future__ import annotations

from typing import Iterable, List, Tuple

# (rule, tools it can name): "{tools}" is replaced by the registered ones joined with " or "; with none registered the rule is dropped
RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("When a request is something you can do, do it now with your tools instead of describing what you would do or asking first.", ()),
    ("When a search, a read or a command brings back nothing useful, change one thing (the query, the path, the command or the source) "
     "and try again before you conclude; if you still find nothing, say what you tried.", ()),
    ("A fact that can change (news, prices, versions, what a file or folder holds now) is checked now with {tools}, not answered from memory.",
     ("search_web", "browse_url", "read_file", "list_directory", "shell_command")),
    ("Say that something is done, fixed or true only when a tool result in this conversation shows it; otherwise name what blocked you.", ()),
    ("Never invent a result, a file, a page or the output of a command; if you could not get it, say so.", ()),
    ("Do not end a turn with a promise of work for later: do the work in this turn, or say plainly that you did not do it.", ()),
)


def instruction_blocks(tool_names: Iterable[str]) -> str:
    """The rules as lines of text for the tools the conversation has."""
    have = set(tool_names)
    lines: List[str] = []
    for rule, tools in RULES:
        if not tools:
            lines.append(rule)
            continue
        named = [t for t in tools if t in have]
        if named:
            lines.append(rule.format(tools=" or ".join(f"`{t}`" for t in named)))
    return "\n".join(f"- {ln}" for ln in lines)
