"""Diagrams of a repository worked out from its files, without a model: which module imports which, and how the folders are laid out.

The output is Mermaid text, the same on every run for the same files (everything is sorted; nothing depends on time or on the order the disk lists
files). Imports are resolved for Python, JavaScript and TypeScript, and C and C++ (an import of something outside the folder is not drawn). The
model can paste the block into its answer and the page draws it.
"""
from __future__ import annotations

import ast
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from harness.repomap import LANGUAGES, MAX_FILE_BYTES, MAX_FILES, SKIP_DIRS

JS_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
JS_IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)["']([^"']+)["']""")
C_INCLUDE = re.compile(r'^\s*#\s*include\s+"([^"]+)"', re.M)
MAX_NODES = 60
DEFAULT_DEPTH = 3


def _label(text: str) -> str:
    return re.sub(r"[^\w ./()+@-]", "_", text)


def _source_files(root: Path) -> Tuple[List[str], int]:
    """Relative paths of the source files, sorted; and how many were left out (too many, too big)."""
    found: List[str] = []
    left = 0
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and not os.path.islink(os.path.join(folder, d)))
        for name in sorted(names):
            if os.path.splitext(name)[1].lower() not in LANGUAGES:
                continue
            full = Path(folder) / name
            try:
                if full.is_symlink() or full.stat().st_size > MAX_FILE_BYTES or len(found) >= MAX_FILES:
                    left += 1
                    continue
            except OSError:
                left += 1
                continue
            found.append(full.relative_to(root).as_posix())
    return sorted(found), left


def _python_modules(files: List[str]) -> Dict[str, str]:
    """Dotted module name to the file that is it."""
    out: Dict[str, str] = {}
    for rel in files:
        if not rel.endswith((".py", ".pyi")):
            continue
        parts = rel.rsplit(".", 1)[0].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            out.setdefault(".".join(parts), rel)
    return out


def _python_imports(rel: str, source: str, modules: Dict[str, str]) -> Set[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return set()
    here = rel.rsplit(".", 1)[0].split("/")[:-1]
    found: Set[str] = set()

    def add(name: str) -> bool:
        target = modules.get(name)
        if target and target != rel:
            found.add(target)
        return bool(target)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = here[:len(here) - (node.level - 1)] if node.level - 1 <= len(here) else None
                if base is None:
                    continue
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            for alias in node.names:
                if not add(f"{prefix}.{alias.name}" if prefix else alias.name):
                    add(prefix)                                      # `from pkg import name` where name is not a module of its own
    return found


def _js_imports(rel: str, source: str, known: Set[str]) -> Set[str]:
    found: Set[str] = set()
    folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    for spec in JS_IMPORT.findall(source):
        if not spec.startswith("."):
            continue
        base = os.path.normpath(os.path.join(folder, spec)).replace("\\", "/")
        if base.startswith(".."):
            continue
        for cand in [base, *(base + e for e in JS_EXTENSIONS), *(f"{base}/index{e}" for e in JS_EXTENSIONS)]:
            if cand in known and cand != rel:
                found.add(cand)
                break
    return found


def _c_imports(rel: str, source: str, known: Set[str]) -> Set[str]:
    found: Set[str] = set()
    folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    for spec in C_INCLUDE.findall(source):
        for base in (os.path.normpath(os.path.join(folder, spec)).replace("\\", "/"), os.path.normpath(spec).replace("\\", "/")):
            if base in known and base != rel:
                found.add(base)
                break
    return found


def import_edges(root: Path) -> Tuple[List[str], Set[Tuple[str, str]], int]:
    """(files, {(importer, imported)}, files left out)."""
    files, left = _source_files(root)
    known = set(files)
    modules = _python_modules(files)
    edges: Set[Tuple[str, str]] = set()
    for rel in files:
        language = LANGUAGES.get(os.path.splitext(rel)[1].lower())
        if language not in ("python", "js", "c"):
            continue
        try:
            source = (root / rel).read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        found = _python_imports(rel, source, modules) if language == "python" else _js_imports(rel, source, known) if language == "js" else _c_imports(rel, source, known)
        edges.update((rel, t) for t in found)
    return files, edges, left


def _group(rel: str, depth: int) -> str:
    """The folder `rel` is in, cut to `depth` levels; files at the top are grouped as '.'."""
    parts = rel.split("/")[:-1][:depth]
    return "/".join(parts) or "."


def _flowchart(direction: str, nodes: List[str], edges: Dict[Tuple[str, str], int], labels: Optional[Dict[str, str]] = None, weighted: bool = False) -> str:
    ids = {n: f"n{i}" for i, n in enumerate(nodes)}
    lines = [f"flowchart {direction}"]
    for n in nodes:
        lines.append(f'  {ids[n]}["{_label((labels or {}).get(n, n))}"]')
    for (a, b), count in sorted(edges.items()):
        lines.append(f"  {ids[a]} -->|{count}| {ids[b]}" if weighted else f"  {ids[a]} --> {ids[b]}")
    return "\n".join(lines)


def imports_diagram(root: str | Path, level: str = "auto", depth: int = DEFAULT_DEPTH, max_nodes: int = MAX_NODES) -> str:
    """Which module imports which: one node per file, or per folder when there are too many files (an arrow's number is how many imports it stands for)."""
    root = Path(root)
    max_nodes = max(5, min(int(max_nodes), 200))
    files, edges, left = import_edges(root)
    linked = sorted({n for e in edges for n in e})
    note = f"; {left} files not read (too many or too large)" if left else ""
    if not edges:
        return f"No imports between the files of this folder were found ({len(files)} source files{note})."
    group = None
    if not (level == "file" or (level == "auto" and len(linked) <= max_nodes)):
        top = max(1, min(int(depth), 8))
        for d in range(top, 0, -1):                                 # the deepest cut that fits
            group = d
            if len({_group(n, d) for n in linked}) <= max_nodes:
                break
    weights: Counter = Counter()
    for a, b in edges:
        ga, gb = (a, b) if group is None else (_group(a, group), _group(b, group))
        if ga != gb:
            weights[(ga, gb)] += 1
    degree: Counter = Counter()
    for (a, b), n in weights.items():
        degree[a] += n
        degree[b] += n
    nodes = sorted(degree, key=lambda n: (-degree[n], n))
    cut = len(nodes) - max_nodes
    nodes = sorted(nodes[:max_nodes])
    shown = {k: v for k, v in weights.items() if k[0] in nodes and k[1] in nodes}
    what = "files" if group is None else f"folders (cut at {group} level{'s' if group > 1 else ''})"
    head = f"Imports between {what}: {len(nodes)} nodes, {len(shown)} arrows from {len(edges)} imports in {len(files)} source files{note}"
    head += f"; {cut} least-connected nodes left out." if cut > 0 else "."
    return head + "\n```mermaid\n" + _flowchart("LR", nodes, shown, weighted=group is not None) + "\n```"


def folders_diagram(root: str | Path, depth: int = DEFAULT_DEPTH, max_nodes: int = MAX_NODES) -> str:
    """The folders that hold source files, as a tree, each with how many source files it holds itself."""
    root = Path(root)
    depth = max(1, min(int(depth), 8))
    max_nodes = max(5, min(int(max_nodes), 200))
    files, left = _source_files(root)
    if not files:
        return "No source files found in this folder."
    direct: Counter = Counter(_group(f, 99) for f in files)
    folders: Set[str] = {"."}
    for d in direct:
        parts = [] if d == "." else d.split("/")
        for i in range(1, min(len(parts), depth) + 1):
            folders.add("/".join(parts[:i]))

    def total(folder: str) -> int:
        return sum(n for d, n in direct.items() if folder == "." or d == folder or d.startswith(folder + "/"))
    ordered = sorted(folders, key=lambda f: (f.count("/") if f != "." else -1, -total(f), f))
    kept = sorted(ordered[:max_nodes], key=lambda f: (f != ".", f))
    labels = {f: ((root.name or ".") if f == "." else f.rsplit("/", 1)[-1]) + f" ({total(f)})" for f in kept}
    edges = {((f.rsplit("/", 1)[0] if "/" in f else "."), f): 1 for f in kept if f != "." and ((f.rsplit("/", 1)[0] if "/" in f else ".") in kept)}
    cut = len(folders) - len(kept)
    note = f"; {left} files not read" if left else ""
    head = f"Folders with source files, down to {depth} levels (the number is the source files inside, subfolders included): {len(kept)} folders, {len(files)} files{note}"
    head += f"; {cut} folders left out." if cut > 0 else "."
    return head + "\n```mermaid\n" + _flowchart("TD", kept, edges, labels) + "\n```"


def register_repo_diagram(registry: Any, resolve: Callable[[str], Path]) -> None:
    """Adds `repo_diagram`; `resolve` turns a path the model gave into one inside the workspace."""
    from harness.tools.registry import EffectClass

    def repo_diagram(path: str = ".", kind: str = "imports", level: str = "auto", depth: int = DEFAULT_DEPTH, max_nodes: int = MAX_NODES) -> str:
        folder = resolve(path)
        if not folder.is_dir():
            raise NotADirectoryError(f"Directory not found: {path}")
        if kind == "imports":
            if level not in ("auto", "file", "folder"):
                raise ValueError("level is auto, file or folder")
            return imports_diagram(folder, level=level, depth=depth, max_nodes=max_nodes)
        if kind == "folders":
            return folders_diagram(folder, depth=depth, max_nodes=max_nodes)
        raise ValueError("kind is imports or folders")

    registry.register(
        name="repo_diagram",
        description="Draws a diagram of a folder worked out from its files, with no guessing: kind 'imports' shows which module imports which (Python, "
                    "JavaScript/TypeScript, C/C++), kind 'folders' shows how the folders are laid out. The result has a mermaid block: put it in your "
                    "answer as it is and the page draws it.",
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "Folder to draw, relative to the workspace", "default": "."},
            "kind": {"type": "string", "enum": ["imports", "folders"], "default": "imports"},
            "level": {"type": "string", "enum": ["auto", "file", "folder"], "default": "auto", "description": "imports only: one node per file or per folder"},
            "depth": {"type": "integer", "description": "How many folder levels", "default": DEFAULT_DEPTH},
            "max_nodes": {"type": "integer", "description": "Most nodes drawn", "default": MAX_NODES}}},
        effect=EffectClass.PURE, handler=repo_diagram)
