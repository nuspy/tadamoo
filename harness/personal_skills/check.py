"""A personal skill's script, looked at before it is saved or run.

A skill is written in Python and does its work through four calls of the harness (`from basalt_skill import tool, ai, ask, say`), so that
every action goes through the permissions, the hooks and the audit record like the agent's own. In the restricted mode (the usual one) this
check refuses what would let a script do something without them: files, programs, the network, `eval`, and reaching into the interpreter.
It is a fence for a script that was written by mistake, not a wall against a person who means harm; a script that needs more is saved in
the free mode, which the person is asked to confirm with the risk label."""
from __future__ import annotations

import ast
from typing import Any, Dict, List

ALLOWED_IMPORTS = frozenset({
    "basalt_skill", "re", "json", "math", "datetime", "calendar", "itertools", "functools", "collections", "textwrap", "string", "statistics",
    "csv", "html", "difflib", "decimal", "fractions", "base64", "hashlib", "unicodedata", "operator", "typing", "dataclasses", "enum",
    "copy", "pprint", "zoneinfo", "random", "uuid", "urllib.parse", "__future__",
})
FORBIDDEN_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "memoryview", "exit", "quit", "help", "__builtins__", "__loader__", "__spec__",
})
FORBIDDEN_ATTRS = frozenset({
    "f_globals", "f_locals", "f_back", "f_builtins", "gi_frame", "gi_code", "cr_frame", "ag_frame", "tb_frame", "mro", "system", "popen",
    "write_text", "write_bytes", "read_text", "read_bytes", "unlink", "rmdir", "rmtree", "open",
})
MAX_SOURCE = 200_000


def check(source: str, mode: str = "restricted") -> List[Dict[str, Any]]:
    """What is wrong with the script: [{"line", "message"}], empty when it can be saved. The free mode only needs a `main(params)`."""
    if len(source) > MAX_SOURCE:
        return [{"line": 1, "message": f"the script is too long ({len(source)} characters; at most {MAX_SOURCE})"}]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [{"line": exc.lineno or 1, "message": f"syntax error: {exc.msg}"}]
    problems: List[Dict[str, Any]] = []
    mains = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    if not mains:
        problems.append({"line": 1, "message": "the script needs a function main(params)"})
    elif len(mains[0].args.args) != 1:
        problems.append({"line": mains[0].lineno, "message": "main takes one argument: the parameters, main(params)"})
    if mode == "free":
        return problems
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 1)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _allowed(alias.name):
                    problems.append({"line": line, "message": f"import {alias.name}: not allowed in a restricted skill (it reaches files, programs or the network)"})
        elif isinstance(node, ast.ImportFrom):
            if node.level or not _allowed(node.module or ""):
                problems.append({"line": line, "message": f"from {'.' * node.level}{node.module or ''} import: not allowed in a restricted skill"})
            elif any(a.name == "*" for a in node.names):
                problems.append({"line": line, "message": "import * is not allowed"})
        elif isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or (node.id.startswith("__") and node.id not in ("__name__", "__future__"))):
            problems.append({"line": line, "message": f"{node.id}: not allowed in a restricted skill"})
        elif isinstance(node, ast.Attribute) and (node.attr in FORBIDDEN_ATTRS or (node.attr.startswith("__") and node.attr.endswith("__"))):
            problems.append({"line": line, "message": f".{node.attr}: not allowed in a restricted skill"})
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append({"line": line, "message": "global and nonlocal are not allowed"})
    return problems


def _allowed(module: str) -> bool:
    return module in ALLOWED_IMPORTS


def tools_used(source: str) -> List[str]:
    """The names of the tools a script calls with a literal name, `tool("name", ...)`, for the list the person sees before they allow it."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tool" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value not in names:
                names.append(first.value)
    return names
