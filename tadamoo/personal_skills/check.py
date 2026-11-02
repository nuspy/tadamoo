"""A personal skill's script, looked at before it is saved or run.

A skill is written in Python and does its work through four calls of the harness (`from basalt_skill import tool, ai, ask, say`), so that
every action it takes through them goes through the permissions, the hooks and the audit record like the agent's own. The script itself runs
as a program of the person: it may import any module and use files, programs and the network."""
from __future__ import annotations

import ast
from typing import Any, Dict, List


def check(source: str) -> List[Dict[str, Any]]:
    """What is wrong with the script: [{"line", "message"}], empty when it can be saved. It only needs a `main(params)`."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [{"line": exc.lineno or 1, "message": f"syntax error: {exc.msg}"}]
    mains = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    if not mains:
        return [{"line": 1, "message": "the script needs a function main(params)"}]
    if len(mains[0].args.args) != 1:
        return [{"line": mains[0].lineno, "message": "main takes one argument: the parameters, main(params)"}]
    return []


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
