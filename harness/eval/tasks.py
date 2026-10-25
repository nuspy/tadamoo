"""Task sets for the evaluation: what a task is, how its workspace is made and how its result is checked.

A task is JSON. Agent tasks give the model a workspace (`files`) and a prompt; tool tasks give it a few
dummy tools. After the run, `checks` look at what actually happened (files, commands, the final answer,
the tools called), never at how the model got there.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from harness.sandbox import ProcessSandbox

SETS_DIR = Path(__file__).with_name("sets")
DEFAULT_ALLOW = ["shell_command:python *"]
CHECK_TYPES = ("file_contains", "file_not_contains", "file_equals", "file_regex", "file_unchanged", "no_file", "command",
               "answer_contains", "answer_not_contains", "answer_regex", "tool_called", "tool_not_called", "tool_args")


@dataclass
class Task:
    id: str
    category: str
    prompt: str
    mode: str = "agent"                       # "agent": a workspace and the standard tools; "tools": dummy tools only
    files: Dict[str, str] = field(default_factory=dict)
    checks: List[Dict[str, Any]] = field(default_factory=list)
    tools: List[Dict[str, Any]] = field(default_factory=list)
    allow: List[str] = field(default_factory=lambda: list(DEFAULT_ALLOW))
    max_turns: int = 12
    system: Optional[str] = None
    knowledge: Dict[str, str] = field(default_factory=dict)   # reference documents the task may use
    strategy: str = "none"                                    # how they are offered: none, rag or cag


NEEDS = {"file_contains": ("path", "text"), "file_not_contains": ("path", "text"), "file_equals": ("path", "content"), "file_regex": ("path", "pattern"),
         "file_unchanged": ("path",), "no_file": ("path",), "command": ("cmd",), "answer_regex": ("pattern",), "tool_called": ("name",),
         "tool_not_called": ("name",), "tool_args": ("name", "args")}


def _validate_check(task_id: str, check: Dict[str, Any]) -> None:
    kind = check.get("type")
    missing = [k for k in NEEDS.get(kind, ()) if k not in check]
    if kind in ("answer_contains", "answer_not_contains") and "text" not in check and not check.get("any_of"):
        missing = ["text (or any_of)"]
    if missing:
        raise ValueError(f"task {task_id}: a {kind} check needs {', '.join(missing)}")
    if kind in ("answer_regex", "file_regex"):
        try:
            re.compile(check["pattern"])
        except re.error as exc:
            raise ValueError(f"task {task_id}: the pattern of a {kind} check is not valid: {exc}") from None


_SPACES = dict.fromkeys(map(ord, "    ​"), " ")
_QUOTES = {ord("‘"): "'", ord("’"): "'", ord("“"): '"', ord("”"): '"', ord("–"): "-", ord("—"): "-", ord("−"): "-"}


def normalize_text(text: str) -> str:
    """Text as compared by the answer checks: Unicode-normalized, lower case, curly quotes and dashes plain, Markdown emphasis and code
    marks removed, runs of spaces made one."""
    text = unicodedata.normalize("NFKC", text).translate(_SPACES).translate(_QUOTES).replace("*", "").replace("`", "")
    return " ".join(text.casefold().split())


def answer_has(answer: str, check: Dict[str, Any]) -> bool:
    """Whether the answer holds the check's `text` (or any of `any_of`); with `word`, only as a whole word or number."""
    haystack = normalize_text(answer)
    for needle in check.get("any_of") or [check["text"]]:
        n = normalize_text(str(needle))
        if not n:
            continue
        if check.get("word"):
            if re.search((r"(?<!\w)" if n[0].isalnum() else "") + re.escape(n) + (r"(?!\w)" if n[-1].isalnum() else ""), haystack):
                return True
        elif n in haystack:
            return True
    return False


def _validate(task: Dict[str, Any]) -> None:
    for key in ("id", "prompt"):
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise ValueError(f"every task needs a '{key}'")
    for check in task.get("checks", []):
        if check.get("type") not in CHECK_TYPES:
            raise ValueError(f"task {task['id']}: unknown check type {check.get('type')!r}; expected one of {CHECK_TYPES}")
        _validate_check(task["id"], check)
    for rel in task.get("files", {}):
        parts = Path(rel).parts
        if Path(rel).is_absolute() or rel.startswith(("/", "\\")) or ".." in parts:
            raise ValueError(f"task {task['id']}: file path '{rel}' must stay inside the workspace")


def load_task_set(path_or_name: str | Path) -> Tuple[Dict[str, Any], List[Task]]:
    """A set by file path or by the name of one in sets/ (core, toolcalls). Returns (metadata, tasks)."""
    path = Path(path_or_name)
    if not path.is_file():
        path = SETS_DIR / f"{path_or_name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    tasks: List[Task] = []
    seen = set()
    shared_docs = {k: str(v) for k, v in (data.get("knowledge") or {}).get("docs", {}).items()}
    for raw in data.get("tasks", []):
        _validate(raw)
        if raw["id"] in seen:
            raise ValueError(f"duplicate task id {raw['id']}")
        seen.add(raw["id"])
        checks = list(raw.get("checks", []))
        if raw.get("expected_keyword"):                     # shorthand for one answer check
            checks.append({"type": "answer_contains", "text": raw["expected_keyword"]})
        mode = raw.get("mode") or ("agent" if raw.get("files") or checks or not raw.get("tools") else "tools")
        tasks.append(Task(
            id=raw["id"], category=raw.get("category", "general"), prompt=raw["prompt"], mode="tools" if raw.get("tools") else mode,
            files={k: str(v) for k, v in raw.get("files", {}).items()}, checks=checks, tools=list(raw.get("tools", [])),
            allow=list(raw["allow"]) if "allow" in raw else list(DEFAULT_ALLOW), max_turns=int(raw.get("max_turns", 12)),
            system=raw.get("system"), knowledge=dict(shared_docs) if raw.get("uses_knowledge") else {},
        ))
    meta = {"name": data.get("name", Path(path).stem), "version": data.get("version", ""), "description": data.get("description", "")}
    return meta, tasks


def make_workspace(task: Task, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in task.files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    return root


def _same(a: Any, b: Any, ignore_case: bool) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower() if ignore_case else a.strip() == b.strip()
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y, ignore_case) for x, y in zip(a, b))
    return a == b


def run_checks(task: Task, workspace: Path, answer: str, calls: Sequence[Dict[str, Any]]) -> List[str]:
    """What is wrong with the result, as plain sentences; an empty list means the task passed.

    `calls` are the tool calls that ran: {"name", "arguments", "executed"} in order."""
    failures: List[str] = []
    ran = [c for c in calls if c.get("executed")]
    sandbox = ProcessSandbox(workspace, timeout=30)

    def read(rel: str) -> Optional[str]:
        p = workspace / rel
        return p.read_bytes().decode("utf-8", errors="replace") if p.is_file() else None

    for check in task.checks:
        kind = check["type"]
        path = check.get("path", "")
        if kind in ("file_contains", "file_not_contains", "file_equals", "file_regex", "file_unchanged"):
            text = read(path)
            if text is None:
                failures.append(f"{path} does not exist")
            elif kind == "file_contains" and check["text"] not in text:
                failures.append(f"{path} does not contain {check['text']!r}")
            elif kind == "file_not_contains" and check["text"] in text:
                failures.append(f"{path} still contains {check['text']!r}")
            elif kind == "file_equals" and text.replace("\r\n", "\n") != check["content"]:
                failures.append(f"{path} is not what was asked")
            elif kind == "file_regex" and not re.search(check["pattern"], text, re.MULTILINE):
                failures.append(f"{path} does not match {check['pattern']!r}")
            elif kind == "file_unchanged" and text.replace("\r\n", "\n") != task.files.get(path, "").replace("\r\n", "\n"):
                failures.append(f"{path} was changed")
        elif kind == "no_file":
            if (workspace / path).exists():
                failures.append(f"{path} exists but must not")
        elif kind == "command":
            cmd = check["cmd"].replace("{python}", f'"{sys.executable}"')
            res = sandbox.run(cmd, timeout=float(check.get("timeout", 30)))
            if res["exit_code"] != check.get("expect_exit", 0):
                failures.append(f"`{check['cmd'][:60]}` exited {res['exit_code']}: {(res['stderr'] or res['stdout'])[-160:].strip()}")
            elif check.get("stdout_contains") and check["stdout_contains"] not in res["stdout"]:
                failures.append(f"`{check['cmd'][:60]}` did not print {check['stdout_contains']!r}")
        elif kind == "answer_contains":
            if not answer_has(answer, check):
                failures.append(f"the answer does not contain {(check.get('any_of') or [check['text']])[0]!r}" + (" (or an alternative)" if check.get("any_of") else ""))
        elif kind == "answer_not_contains":
            if answer_has(answer, check):
                failures.append(f"the answer contains {(check.get('any_of') or [check['text']])[0]!r}")
        elif kind == "answer_regex":
            if not re.search(check["pattern"], answer, re.IGNORECASE | re.MULTILINE):
                failures.append(f"the answer does not match {check['pattern']!r}")
        elif kind == "tool_called":
            n = sum(1 for c in ran if c["name"] == check["name"])
            if n < check.get("min", 1):
                failures.append(f"{check['name']} was called {n} time(s)")
        elif kind == "tool_not_called":
            if any(c["name"] == check["name"] for c in ran):
                failures.append(f"{check['name']} was called but must not be")
        elif kind == "tool_args":
            ic = bool(check.get("ignore_case", True))
            wanted = check["args"]
            hit = any(c["name"] == check["name"] and isinstance(c["arguments"], dict)
                      and all(k in c["arguments"] and _same(c["arguments"][k], v, ic) for k, v in wanted.items()) for c in ran)
            if not hit:
                failures.append(f"no call of {check['name']} with {json.dumps(wanted, ensure_ascii=False)}")
    return failures


def expand_strategies(tasks: List[Task], strategies: Sequence[str]) -> List[Task]:
    """Each task that uses reference documents, once per strategy; the others unchanged.

    The copies are told apart by id (`id@cag`) and category (`knowledge/cag`), so a report compares them."""
    import dataclasses
    out: List[Task] = []
    for t in tasks:
        if not t.knowledge:
            out.append(t)
            continue
        for strategy in strategies:
            if strategy not in ("none", "rag", "cag"):
                raise ValueError(f"unknown strategy {strategy!r}; expected none, rag or cag")
            out.append(dataclasses.replace(t, id=f"{t.id}@{strategy}", category=f"knowledge/{strategy}", strategy=strategy))
    return out
