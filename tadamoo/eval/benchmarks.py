"""Public benchmarks read from a folder the person chose: BFCL (function calling), and subsets of Terminal-Bench and SWE-bench Pro.

Nothing is downloaded here. The person fetches a dataset once, puts it in a folder, and names the folder on the command line; each loader turns what
it finds into the evaluation's own tasks and a check that scores the result. The layouts below are the ones the public datasets are described as
having; a file that does not look like that is reported (`Loaded.skipped`) and never guessed at, so the first real folder tells at once whether a
loader needs a change.

    BFCL           <folder>/BFCL_v3_<category>.json (one JSON object per line: id, question, function) and
                   <folder>/possible_answer/BFCL_v3_<category>.json (id, ground_truth); the irrelevance categories have no answers
    Terminal-Bench <folder>/<task>/task.yaml (instruction), with the files the task starts from beside it and the tests in <task>/tests/;
                   the tests are copied in only for the check, and `/app` in the instruction and the tests is the working folder
    SWE-bench Pro  <folder>/instances.jsonl (instance_id, problem_statement, test_patch, FAIL_TO_PASS, PASS_TO_PASS) and a checkout of the
                   repository at its base commit in <folder>/repos/<instance_id>

Terminal-Bench tasks are made for a container image and SWE-bench Pro ones for a prepared environment; here they run on this machine, so a task whose
tests need more than the local Python is a failed task for a reason that is not the model's. Pick a subset (`--only`) that this machine can check.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .tasks import Task

BFCL_PREFIX = re.compile(r"^BFCL_v\d+_(?P<category>[a-z_]+)\.jsonl?$")
BFCL_SKIP = ("java", "javascript", "sql", "rest", "multi_turn", "memory", "web_search", "format_sensitivity")      # other call syntaxes, or several turns
BFCL_MAX_TURNS = 3
TYPES = {"dict": "object", "float": "number", "tuple": "array", "int": "integer", "bool": "boolean", "str": "string", "list": "array", "any": ""}
SWE_IDS_MAX = 200                # tests of PASS_TO_PASS run in one command at most
TB_HIDDEN = ["tests", "solution.sh", "solution.yaml", "run-tests.sh", "task.yaml", "Dockerfile", "docker-compose.yaml", ".git"]      # what the model must not see


@dataclass
class Loaded:
    tasks: List[Task] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)       # what was left out, and why
    meta: Dict[str, Any] = field(default_factory=dict)


def _read_json_lines(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    try:
        whole = json.loads(text)
        if isinstance(whole, list):
            return [r for r in whole if isinstance(r, dict)]
    except ValueError:
        pass
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip():
            try:
                row = json.loads(line)
            except ValueError:
                raise ValueError(f"{path.name}, line {number}: not JSON") from None
            if isinstance(row, dict):
                rows.append(row)
    return rows


# -- BFCL ------------------------------------------------------------------------------------------------------


def tool_name(name: str) -> str:
    """A function name as a tool name: BFCL names may hold dots (`math.factorial`), which a tool name cannot."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", str(name))[:64]


def tool_schema(node: Any) -> Any:
    """A BFCL parameter schema as a JSON Schema the harness validates (`dict` is an object, `float` a number, `tuple` an array)."""
    if isinstance(node, list):
        return [tool_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key == "type" and isinstance(value, str):
            mapped = TYPES.get(value, value)
            if mapped:
                out[key] = mapped
        elif key in ("properties",) and isinstance(value, dict):
            out[key] = {k: tool_schema(v) for k, v in value.items()}
        elif key in ("items", "additionalProperties"):
            out[key] = tool_schema(value)
        elif key in ("type", "default"):
            continue
        else:
            out[key] = value
    return out


def load_bfcl(folder: str | Path, categories: Optional[Sequence[str]] = None, limit: Optional[int] = None) -> Loaded:
    root = Path(folder)
    if not root.is_dir():
        raise ValueError(f"{root} is not a folder")
    loaded = Loaded(meta={"name": "bfcl", "version": "", "description": f"Berkeley Function-Calling Leaderboard, read from {root}"})
    files = sorted(p for p in root.iterdir() if p.is_file() and BFCL_PREFIX.match(p.name))
    if not files:
        raise ValueError(f"no BFCL_v<n>_<category>.json file in {root}")
    for path in files:
        if limit and len(loaded.tasks) >= limit:
            break
        category = BFCL_PREFIX.match(path.name).group("category")      # type: ignore[union-attr]
        if categories and category not in categories:
            continue
        if any(word in category for word in BFCL_SKIP):
            loaded.skipped.append(f"{path.name}: {category} is not single-turn Python function calling")
            continue
        answers: Dict[str, Any] = {}
        answer_file = root / "possible_answer" / path.name
        if answer_file.is_file():
            answers = {str(r.get("id")): r.get("ground_truth") for r in _read_json_lines(answer_file)}
        irrelevance = "irrelevance" in category
        if not irrelevance and not answers:
            loaded.skipped.append(f"{path.name}: no possible_answer/{path.name}")
            continue
        for row in _read_json_lines(path):
            rid = str(row.get("id", ""))
            turns = row.get("question")
            messages = turns[0] if isinstance(turns, list) and turns and isinstance(turns[0], list) else turns
            users = [m.get("content") for m in messages or [] if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)]
            functions = [f for f in row.get("function") or [] if isinstance(f, dict) and f.get("name")]
            if not rid or not users or not functions or (not irrelevance and rid not in answers):
                loaded.skipped.append(f"{rid or '(no id)'}: no question, no function or no answer")
                continue
            specs = [{"name": tool_name(f["name"]), "description": str(f.get("description", "")), "untrusted": False,
                      "parameters": tool_schema(f.get("parameters") or {"type": "object", "properties": {}})} for f in functions]
            check = {"type": "bfcl_calls", "irrelevance": irrelevance, "expected": answers.get(rid) or [],
                     "functions": [{"name": tool_name(f["name"]), "required": list((f.get("parameters") or {}).get("required") or []),
                                    "properties": list(((f.get("parameters") or {}).get("properties") or {}))} for f in functions]}
            loaded.tasks.append(Task(id=f"bfcl/{rid}", category=f"bfcl/{category}", prompt=users[-1], mode="tools", tools=specs, checks=[check],
                                     max_turns=BFCL_MAX_TURNS, system="You are a helpful assistant with tools. Use a tool only when it is needed and fits the request; "
                                     "if no tool fits, say so and call none."))
            if limit and len(loaded.tasks) >= limit:
                break
    if not loaded.tasks:
        raise ValueError(f"nothing could be loaded from {root}: " + ("; ".join(loaded.skipped[:5]) or "no category matches"))
    return loaded


_PUNCT = re.compile(r"[ \t\n\r,./\-_*^()\"']")


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return _PUNCT.sub("", value).lower()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in value.items()}
    return value


def _accepts(value: Any, accepted: Any) -> bool:
    """Whether a value is one of the answers a BFCL ground truth lists for a parameter."""
    options = accepted if isinstance(accepted, list) else [accepted]
    seen = _norm(value)
    return any(seen == _norm(option) for option in options if option != "")


def bfcl_failures(check: Dict[str, Any], calls: Sequence[Dict[str, Any]]) -> List[str]:
    """What is wrong with the calls the model made, against BFCL's ground truth (parameters by name, any order of calls, an accepted list per
    parameter, an empty string meaning the parameter may be left out); an empty list means the answer is right."""
    ran = [c for c in calls if c.get("executed")]
    if check.get("irrelevance"):
        return [f"{ran[0]['name']} was called but no function fits the request"] if ran else []
    expected = check.get("expected") or []
    if len(ran) != len(expected):
        return [f"{len(ran)} call(s) were made and {len(expected)} were expected"]
    schema = {f["name"]: f for f in check.get("functions") or []}
    free = list(ran)
    problems: List[str] = []
    for want in expected:
        if not isinstance(want, dict) or len(want) != 1:
            problems.append("the ground truth of this task is not in the expected shape")
            continue
        raw_name, params = next(iter(want.items()))
        name = tool_name(raw_name)
        found = [(_call_problems(call, name, params if isinstance(params, dict) else {}, schema.get(name, {})), call) for call in free]
        right = next((call for issues, call in found if not issues), None)
        if right is not None:
            free.remove(right)                              # each call answers one expected call
        else:
            problems.append(f"{raw_name}: " + (min((issues for issues, _ in found), key=len)[0] if found else "was not called"))
    return problems


def _call_problems(call: Dict[str, Any], name: str, params: Dict[str, Any], function: Dict[str, Any]) -> List[str]:
    if call["name"] != name:
        return [f"{call['name']} was called instead"]
    args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    out = [f"required argument {r} is missing" for r in function.get("required", []) if r not in args]
    out += [f"unexpected argument {k}" for k in args if k not in params and k not in ("description", "explain")]
    for key, accepted in params.items():
        if key not in args:
            if not (isinstance(accepted, list) and "" in accepted) and key in function.get("required", []):
                out.append(f"argument {key} is missing")
        elif not _accepts(args[key], accepted):
            out.append(f"argument {key} is {json.dumps(args[key], ensure_ascii=False)[:60]}, not one of the accepted values")
    return out


# -- Terminal-Bench -------------------------------------------------------------------------------------------


def read_yaml_fields(text: str) -> Dict[str, Any]:
    """The top-level keys of a task.yaml: scalars and `|` or `>` blocks. PyYAML does the work when it is installed."""
    try:
        import yaml
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except ImportError:
        pass
    out: Dict[str, Any] = {}
    key, block, lines = None, False, []
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if match and not (block and line.startswith(" ")):
            if key is not None and block:
                out[key] = "\n".join(lines).strip("\n")
            key, value = match.group(1), match.group(2).strip()
            block, lines = value in ("|", ">", "|-", ">-"), []
            if not block:
                out[key] = value.strip("'\"")
        elif block:
            lines.append(line[2:] if line.startswith("  ") else line)
    if key is not None and block:
        out[key] = "\n".join(lines).strip("\n")
    return out


def load_terminal_bench(folder: str | Path, limit: Optional[int] = None) -> Loaded:
    root = Path(folder)
    if not root.is_dir():
        raise ValueError(f"{root} is not a folder")
    loaded = Loaded(meta={"name": "terminal-bench", "version": "", "description": f"Terminal-Bench tasks read from {root}; checked here with the local Python, not in a container"})
    for task_dir in sorted(p for p in root.iterdir() if p.is_dir() and (p / "task.yaml").is_file()):
        fields = read_yaml_fields((task_dir / "task.yaml").read_text(encoding="utf-8"))
        instruction = str(fields.get("instruction") or "").strip()
        tests = task_dir / "tests"
        if not instruction or not tests.is_dir():
            loaded.skipped.append(f"{task_dir.name}: no instruction or no tests/ folder")
            continue
        docker = (task_dir / "Dockerfile").read_text(encoding="utf-8", errors="replace") if (task_dir / "Dockerfile").is_file() else ""
        if re.search(r"apt-get|apk add|yum install|dnf install", docker):
            loaded.skipped.append(f"{task_dir.name}: the image installs system packages this machine may not have (the task is still loaded; check the first result)")
        loaded.tasks.append(Task(id=f"tb/{task_dir.name}", category=f"terminal-bench/{fields.get('category') or 'general'}",
                                 prompt=instruction.replace("/app/", "").replace("/app", "."), mode="agent", allow=["shell_command:*"], max_turns=30,
                                 workspace_from=str(task_dir), workspace_ignore=TB_HIDDEN, hidden={str(task_dir): "tests"}, path_map={"/app": "{workspace}"},
                                 checks=[{"type": "command", "cmd": "{python} -m pytest tests -q -x", "timeout": float(fields.get("max_test_timeout_sec") or 180)}]))
        if limit and len(loaded.tasks) >= limit:
            break
    if not loaded.tasks:
        raise ValueError(f"no task with a task.yaml and a tests/ folder in {root}")
    return loaded


# -- SWE-bench Pro ----------------------------------------------------------------------------------------------


def _id_list(value: Any) -> List[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [value] if value.strip() else []
    return [str(v) for v in value] if isinstance(value, list) else []


def load_swebench(folder: str | Path, limit: Optional[int] = None, test_command: str = "{python} -m pytest -q") -> Loaded:
    root = Path(folder)
    source = next((root / n for n in ("instances.jsonl", "instances.json") if (root / n).is_file()), None)
    if source is None:
        raise ValueError(f"no instances.jsonl in {root}")
    loaded = Loaded(meta={"name": "swe-bench-pro", "version": "", "description": f"SWE-bench Pro instances read from {root}; scored here by running the instance's tests"})
    for row in _read_json_lines(source):
        iid = str(row.get("instance_id") or "")
        repo = root / "repos" / iid
        fail, keep = _id_list(row.get("FAIL_TO_PASS")), _id_list(row.get("PASS_TO_PASS"))
        if not iid or not row.get("problem_statement") or not row.get("test_patch") or not fail:
            loaded.skipped.append(f"{iid or '(no id)'}: no problem statement, test patch or FAIL_TO_PASS")
        elif not repo.is_dir():
            loaded.skipped.append(f"{iid}: no checkout at repos/{iid}")
        else:
            loaded.tasks.append(Task(id=f"swe/{iid}", category=f"swe-bench-pro/{row.get('repo') or 'repo'}", prompt=str(row["problem_statement"]), mode="agent",
                                     allow=["shell_command:*"], max_turns=40, workspace_from=str(repo), workspace_ignore=[".git"],
                                     checks=[{"type": "swe_resolved", "test_patch": str(row["test_patch"]), "fail_to_pass": fail, "pass_to_pass": keep[:SWE_IDS_MAX],
                                              "test_command": test_command}]))
        if limit and len(loaded.tasks) >= limit:
            break
    if not loaded.tasks:
        raise ValueError(f"no instance could be loaded from {source}: " + ("; ".join(loaded.skipped[:5]) or "the file is empty"))
    return loaded
