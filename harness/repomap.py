"""A map of a repository for the model: per file, the classes, functions and methods in it, the most relevant files first.

Python is read with `ast`; JavaScript, TypeScript, C, C++, CUDA, Go, Rust and Java with cautious regular expressions that only look at
the start of a line, so a miss is a missing line and not an invented one. Files are ranked by how recently they changed (modified
time) and by how many other files mention the names they define, with a boost for the files a `focus` points at. The map stops at a
token budget (estimated from characters), so it can be asked for at the start of a task and again, focused, later.
"""
from __future__ import annotations

import ast
import math
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

SKIP_DIRS = frozenset({".git", ".hg", ".svn", ".basalt", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
                       ".idea", ".vscode", "build", "dist", "target", "vendor", "out", ".next", ".cache", "coverage", "site-packages"})
LANGUAGES = {".py": "python", ".pyi": "python", ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js",
             ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".cxx": "c", ".hpp": "c", ".hh": "c", ".cu": "c", ".cuh": "c",
             ".go": "go", ".rs": "rust", ".java": "java"}
MAX_FILES = 5000
MAX_FILE_BYTES = 300_000
MAX_TOTAL_BYTES = 60_000_000
MAX_SYMBOLS_PER_FILE = 40
MAX_SIGNATURE = 110
CHARS_PER_TOKEN = 3.5
GENERIC = frozenset({"main", "init", "get", "set", "run", "new", "test", "self", "cls", "name", "value", "data", "type", "args", "kwargs",
                     "__init__", "__str__", "__repr__", "__enter__", "__exit__", "close", "open", "read", "write", "start", "stop", "update"})
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

Symbol = Tuple[int, str, str]       # (depth, text shown, name defined)


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_SIGNATURE else text[:MAX_SIGNATURE - 3] + "..."


# -- symbols per language --------------------------------------------------------------------------------------


def _python_symbols(source: str) -> List[Symbol]:
    tree = ast.parse(source)
    out: List[Symbol] = []

    def function(node: Any, depth: int) -> None:
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
        out.append((depth, _clip(f"{prefix} {node.name}({ast.unparse(node.args)}){ret}"), node.name))

    def visit(body: Sequence[ast.stmt], depth: int) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                bases = ", ".join(ast.unparse(b) for b in node.bases)
                out.append((depth, _clip(f"class {node.name}" + (f"({bases})" if bases else "")), node.name))
                if depth == 0:
                    visit(node.body, 1)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function(node, depth)
            elif depth == 0 and isinstance(node, (ast.If, ast.Try)):
                visit(node.body, depth)                        # definitions guarded by a version or import check
    visit(tree.body, 0)
    return out


JS_FUNCTION = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*(<[^>(]*>)?\s*\(([^)]*)\)")
JS_ARROW = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*(?::\s*[^=]+)?=>")
JS_CLASS = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)(?:\s*<[^>]*>)?(?:\s+extends\s+([\w.]+))?")
JS_TYPE = re.compile(r"^(?:export\s+)?(?:declare\s+)?(interface|type|enum)\s+(\w+)")
JS_METHOD = re.compile(r"^\s{1,8}(?:(?:public|private|protected|static|async|readonly|override)\s+)*(\w+)\s*(?:<[^>(]*>)?\(([^)]*)\)\s*(?::\s*[^{;=]+)?\{\s*$")
JS_NOT_METHODS = frozenset({"if", "for", "while", "switch", "catch", "function", "return", "constructor_", "with", "else", "do"})


def _js_symbols(source: str) -> List[Symbol]:
    out: List[Symbol] = []
    in_class = False
    for line in source.splitlines():
        if not line.strip():
            continue
        if line[0] not in " \t":
            in_class = False
            if m := JS_CLASS.match(line):
                out.append((0, _clip(f"class {m.group(1)}" + (f" extends {m.group(2)}" if m.group(2) else "")), m.group(1)))
                in_class = True
            elif m := JS_FUNCTION.match(line):
                out.append((0, _clip(f"function {m.group(1)}({m.group(3)})"), m.group(1)))
            elif m := JS_ARROW.match(line):
                out.append((0, f"const {m.group(1)} = (...) =>", m.group(1)))
            elif m := JS_TYPE.match(line):
                out.append((0, f"{m.group(1)} {m.group(2)}", m.group(2)))
        elif in_class and (m := JS_METHOD.match(line)) and m.group(1) not in JS_NOT_METHODS:
            out.append((1, _clip(f"{m.group(1)}({m.group(2)})"), m.group(1)))
    return out


C_KEYWORDS = frozenset({"if", "for", "while", "switch", "return", "else", "sizeof", "catch", "defined", "do", "case", "typedef", "using",
                        "delete", "new", "throw", "static_assert", "alignas", "__attribute__", "operator_"})
C_TYPE = re.compile(r"^(?:template\s*<[^>]*>\s*)?(class|struct|enum(?:\s+class)?|union|namespace)\s+(?:\[\[[^\]]*\]\]\s*)?(\w+)\s*(?::[^{;]*)?\{?\s*$")
C_FUNCTION = re.compile(r"^(?:[A-Za-z_][\w:<>,~*&\s]*?[\s*&])?(~?[A-Za-z_][\w:]*)\s*\(([^;{}]*)\)\s*(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?(?:->\s*[\w:<>*&\s]+)?(\{)?\s*$")


def _c_symbols(source: str) -> List[Symbol]:
    out: List[Symbol] = []
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if not line or line[0] in " \t#/*{})" or line.rstrip().endswith(";"):
            continue
        if m := C_TYPE.match(line):
            out.append((0, f"{m.group(1)} {m.group(2)}", m.group(2)))
            continue
        m = C_FUNCTION.match(line.rstrip())
        if not m or m.group(1) in C_KEYWORDS or line.startswith(("else", "return", "case ", "typedef", "using")):
            continue
        if not m.group(3):                                         # the brace must open on the next non-empty line
            nxt = next((ln for ln in lines[i + 1:i + 4] if ln.strip()), "")
            if not nxt.startswith("{"):
                continue
        out.append((0, _clip(line.rstrip().rstrip("{").rstrip()), m.group(1).split("::")[-1]))
    return out


GO_FUNC = re.compile(r"^func\s+(?:\(([^)]*)\)\s*)?(\w+)\s*(?:\[[^\]]*\])?\(([^)]*)\)")
GO_TYPE = re.compile(r"^type\s+(\w+)\s+(struct|interface)\b")


def _go_symbols(source: str) -> List[Symbol]:
    out: List[Symbol] = []
    for line in source.splitlines():
        if m := GO_FUNC.match(line):
            recv = f"({m.group(1)}) " if m.group(1) else ""
            out.append((0, _clip(f"func {recv}{m.group(2)}({m.group(3)})"), m.group(2)))
        elif m := GO_TYPE.match(line):
            out.append((0, f"type {m.group(1)} {m.group(2)}", m.group(1)))
    return out


RUST_ITEM = re.compile(r"^(\s*)(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|unsafe|const|extern(?:\s+\"[^\"]*\")?)\s+)*(fn|struct|enum|trait|mod|type)\s+(\w+)(?:<[^>]*>)?(\([^)]*\))?")
RUST_IMPL = re.compile(r"^(\s*)(?:unsafe\s+)?impl\b\s*(?:<[^>]*>\s*)?([^{]+?)\s*(?:where\b.*)?\{?\s*$")


def _rust_symbols(source: str) -> List[Symbol]:
    out: List[Symbol] = []
    for line in source.splitlines():
        if line.lstrip().startswith("//"):
            continue
        if (m := RUST_ITEM.match(line)) and len(m.group(1)) <= 4:
            out.append((1 if m.group(1) else 0, _clip(f"{m.group(2)} {m.group(3)}{m.group(4) or ''}"), m.group(3)))
        elif (m := RUST_IMPL.match(line)) and len(m.group(1)) == 0:
            target = re.sub(r"<[^<>]*>", "", m.group(2))
            out.append((0, _clip(f"impl {target}"), (re.findall(r"\w+", target) or [""])[-1]))
    return out


JAVA_TYPE = re.compile(r"^\s*(?:(?:public|protected|private|static|final|abstract|sealed)\s+)*(class|interface|enum|record)\s+(\w+)")
JAVA_METHOD = re.compile(r"^\s{2,8}(?:(?:public|protected|private|static|final|abstract|synchronized|default)\s+)+(?:<[^>]+>\s+)?[\w<>\[\], ?.]+\s+(\w+)\s*\(([^)]*)\)\s*(?:throws [\w., ]+)?\s*[{;]")


def _java_symbols(source: str) -> List[Symbol]:
    out: List[Symbol] = []
    for line in source.splitlines():
        if m := JAVA_TYPE.match(line):
            out.append((0 if not line[:1].isspace() else 1, f"{m.group(1)} {m.group(2)}", m.group(2)))
        elif m := JAVA_METHOD.match(line):
            out.append((1, _clip(f"{m.group(1)}({m.group(2)})"), m.group(1)))
    return out


EXTRACTORS: Dict[str, Callable[[str], List[Symbol]]] = {"python": _python_symbols, "js": _js_symbols, "c": _c_symbols, "go": _go_symbols,
                                                        "rust": _rust_symbols, "java": _java_symbols}
PY_DEFINITION = re.compile(r"^(\s*)(async\s+def|def|class)\s+(\w+)([^:\n]*)")


def _python_fallback(source: str) -> List[Symbol]:
    """For a file that does not parse (half edited): the definitions that can be read from the start of a line."""
    out: List[Symbol] = []
    for line in source.splitlines():
        if (m := PY_DEFINITION.match(line)) and len(m.group(1)) <= 4:
            out.append((1 if m.group(1) else 0, _clip(f"{m.group(2)} {m.group(3)}{m.group(4)}"), m.group(3)))
    return out


def symbols_of(language: str, source: str) -> List[Symbol]:
    """The definitions in a source file, in order; a Python file that does not parse is read with the plain pattern, not skipped."""
    try:
        return EXTRACTORS[language](source)
    except (SyntaxError, ValueError, RecursionError):
        return _python_fallback(source) if language == "python" else []


def names_of(symbols: Iterable[Symbol]) -> Set[str]:
    """The identifiers a file defines, for counting how many other files mention them (short and generic names say nothing)."""
    return {name for _, _, name in symbols if len(name) >= 4 and name not in GENERIC and not name.startswith("__")}


# -- ranking and output ----------------------------------------------------------------------------------------


class RepoFile:
    def __init__(self, rel: str, language: str, mtime: float, symbols: List[Symbol], words: Set[str]) -> None:
        self.rel, self.language, self.mtime, self.symbols, self.words = rel, language, mtime, symbols, words
        self.names = names_of(symbols)
        self.score = 0.0


def scan(root: Path) -> Tuple[List[RepoFile], int]:
    """The source files under `root` with their symbols and the words they use; and how many files were left unread (too many, too big)."""
    files: List[RepoFile] = []
    left = 0
    total = 0
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and not os.path.islink(os.path.join(folder, d)))
        for name in sorted(names):
            language = LANGUAGES.get(os.path.splitext(name)[1].lower())
            if language is None:
                continue
            full = Path(folder) / name
            try:
                st = full.stat()
                if full.is_symlink() or st.st_size > MAX_FILE_BYTES or len(files) >= MAX_FILES or total + st.st_size > MAX_TOTAL_BYTES:
                    left += 1
                    continue
                source = full.read_bytes().decode("utf-8", errors="replace")
            except OSError:
                left += 1
                continue
            total += st.st_size
            files.append(RepoFile(full.relative_to(root).as_posix(), language, st.st_mtime, symbols_of(language, source), set(WORD.findall(source))))
    return files, left


def rank(files: List[RepoFile], focus: str = "", now: Optional[float] = None) -> List[RepoFile]:
    """Most relevant first: files changed recently, files whose names other files use, files the focus points at."""
    now = time.time() if now is None else now
    mentioned: Counter = Counter()
    for f in files:
        mentioned.update(f.words)
    terms = [t.lower() for t in re.split(r"[\s,;]+", focus) if t]
    for f in files:
        # a name counts once per other file that uses it, and a very common one counts at most 10 times
        refs = sum(min(max(mentioned[n] - 1, 0), 10) for n in f.names)
        age_days = max(0.0, (now - f.mtime) / 86400)
        recency = 1.0 / (1.0 + age_days / 3.0)
        hit = any(t in f.rel.lower() or any(t in n.lower() for n in f.names) for t in terms)
        f.score = math.log1p(refs) + 2.0 * recency + (6.0 if hit else 0.0)
    return sorted(files, key=lambda f: (-f.score, f.rel))


def render(f: RepoFile, max_lines: int = MAX_SYMBOLS_PER_FILE) -> str:
    """The file's name and its definitions; when there are more than `max_lines`, the top-level ones come first and then the nested ones."""
    keep = set(range(len(f.symbols)))
    if len(f.symbols) > max_lines:
        order = sorted(range(len(f.symbols)), key=lambda i: (f.symbols[i][0] > 0, i))
        keep = set(order[:max_lines])
    lines = [f.rel] + ["  " * (f.symbols[i][0] + 1) + f.symbols[i][1] for i in sorted(keep)]
    if len(keep) < len(f.symbols):
        lines.append(f"  ... {len(f.symbols) - len(keep)} more")
    return "\n".join(lines)


def build_repo_map(root: str | Path, max_tokens: int = 1500, focus: str = "", now: Optional[float] = None) -> str:
    """The map of the source files under `root`, within about `max_tokens` tokens."""
    root = Path(root)
    files, unread = scan(root)
    budget = max(200, int(max_tokens)) * CHARS_PER_TOKEN
    ranked = rank(files, focus, now)
    with_symbols = [f for f in ranked if f.symbols]
    out: List[str] = []
    used = 0.0
    shown = 0
    per_file = max(6, min(MAX_SYMBOLS_PER_FILE, int(budget / 60 / 8)))       # no one file takes the whole map
    for f in with_symbols:
        block = render(f, per_file)
        if used + len(block) + 1 > budget:
            if shown:
                break
            block = block[:int(budget)]
        out.append(block)
        used += len(block) + 1
        shown += 1
    header = (f"Repository map: {shown} of {len(with_symbols)} source files with definitions"
              f"{f' ({unread} not read: too many or too large)' if unread else ''}; most relevant first "
              "(recent changes, use by other files" + (", your focus" if focus else "") + ").")
    if not out:
        return header + "\nNo definitions found."
    omitted = len(with_symbols) - shown
    return header + "\n\n" + "\n".join(out) + (f"\n\n[{omitted} more files not shown; ask again with a focus or a smaller path]" if omitted > 0 else "")


def register_repo_map(registry: Any, resolve: Callable[[str], Path]) -> None:
    """Adds `repo_map`; `resolve` turns a path the model gave into one inside the workspace."""
    from harness.tools.registry import EffectClass

    def repo_map(path: str = ".", focus: str = "", max_tokens: int = 1500) -> str:
        folder = resolve(path)
        if not folder.is_dir():
            raise NotADirectoryError(f"Directory not found: {path}")
        return build_repo_map(folder, max_tokens=max(200, min(int(max_tokens), 8000)), focus=focus)

    registry.register(
        name="repo_map",
        description="Lists the source files of a folder with the classes, functions and methods in each, the most relevant first. Use it to find "
                    "where things are before reading files.",
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "Folder to map, relative to the workspace", "default": "."},
            "focus": {"type": "string", "description": "Words (a file name, a function) that should come first", "default": ""},
            "max_tokens": {"type": "integer", "description": "Size of the map in tokens", "default": 1500}}},
        effect=EffectClass.PURE, handler=repo_map)
