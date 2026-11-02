"""BFCL v4 (Berkeley Function-Calling Leaderboard, inside the `gorilla` repository): single-turn and multi-turn function-calling cases scored by BFCL's own
AST and state checkers.

This tests the model and the way the server turns its output into tool calls (the chat template and its parser) far more than it tests the harness: BFCL
sends the case's functions to the OpenAI chat-completions endpoint of the local engine in function-calling mode and checks the calls that come back. Our agent
loop is not in the path. Read a BFCL score as a check of the served model and its template.

BFCL's own command line does the work. The adapter writes a project folder for the run (`BFCL_PROJECT_ROOT`: the `.env`, the list of case ids, `result/` and
`score/`), then runs `bfcl generate --run-ids` and `bfcl evaluate --partial-eval` for one category at a time through `bfcl_assets/launcher.py`, which adds the
served model to BFCL's registry (BFCL's OpenAI handler, address = the local engine) and starts BFCL's CLI. BFCL's generation is resumable (it skips ids that
have a result), so a category stopped halfway continues. One trial is one case id; the first trial of a category generates and scores the whole category and the
others read what it left.

Settings read: `subset` (smoke: a few cases of six categories; simple: the simple, multiple, parallel and parallel-multiple categories; category: the ones named in
`categories`; full: every single-turn and multi-turn category), `categories` (a list or a comma-separated string, with `subset` category), `limit` (the first N cases
of each category; smoke defaults to 3), `tasks` (explicit case ids), `python` (the interpreter uv uses, default 3.12: BFCL pins numpy 1.26). `trials` is ignored: BFCL
scores one answer per case. Not run: the web-search categories (they need a search-engine key, a cloud service), the memory categories and format sensitivity (a
prompt-mode test). The accuracy reported is over the cases that were run, not BFCL's weighted Overall."""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from tadamoo.bench.runner import Key, TrialContext, TrialResult

from ._common import clean_env, clip, model_key, read_lines, run_record, source_of, uv_argv, write_events, LOCAL_KEY
from .containerkit import local_endpoint
from .proc import LongRunner, long_runner

ID = "bfcl-v4"
PROJECT = "berkeley-function-call-leaderboard"
VERSION = "BFCL_v4"
REGISTRY_NAME = "basalt-local-FC"
LAUNCHER = Path(__file__).resolve().parent / "bfcl_assets" / "launcher.py"
NON_LIVE = ("simple_python", "simple_java", "simple_javascript", "multiple", "parallel", "parallel_multiple", "irrelevance")
LIVE = ("live_simple", "live_multiple", "live_parallel", "live_parallel_multiple", "live_irrelevance", "live_relevance")
MULTI_TURN = ("multi_turn_base", "multi_turn_miss_func", "multi_turn_miss_param", "multi_turn_long_context")
SIMPLE = ("simple_python", "simple_java", "simple_javascript", "multiple", "parallel", "parallel_multiple")
SMOKE = ("simple_python", "multiple", "parallel", "irrelevance", "live_simple", "multi_turn_base")
SMOKE_PER_CATEGORY = 3
SUBSET_CATEGORIES = {"smoke": SMOKE, "simple": SIMPLE, "full": NON_LIVE + LIVE + MULTI_TURN}
NOT_RUN = ("web_search_base", "web_search_no_snippet", "memory_kv", "memory_vector", "memory_rec_sum", "format_sensitivity")
GENERATE_S = 4 * 3600.0            # one category's generation; the multi-turn ones take long on one GPU
EVALUATE_S = 1800.0
ATTEMPTS = 2                       # a category whose generation or scoring failed is tried again once, by the next trial of it, and then left failed
DECODE_FAILURE = re.compile(r"decod|parse", re.I)
CONTEXT = re.compile(r"context (?:length|window|overflow|limit)|maximum context|exceeds? the context|too many tokens|prompt is too long", re.I)
NOTES = ("This tests the served model and its chat template and tool-call parser more than the harness: BFCL sends the functions to the chat-completions endpoint and "
         "checks the calls that come back; the agent loop is not used.",
         "The accuracy is over the selected cases, not BFCL's weighted Overall (which needs the agentic categories as well).",
         "The web-search categories (they need a search-engine key), the memory categories and format sensitivity were not run.")


# -- the cases ----------------------------------------------------------------------------------------------------------

def project_of(source: Path) -> Path:
    return Path(source) / PROJECT


def data_file(source: Path, category: str) -> Path:
    return project_of(source) / "bfcl_eval" / "data" / f"{VERSION}_{category}.json"


def category_ids(source: Path, category: str) -> List[str]:
    ids = [str(e["id"]) for e in read_lines(data_file(source, category)) if "id" in e]
    if not ids:
        raise FileNotFoundError(f"no cases found for BFCL category {category!r} in {data_file(source, category)}; import the benchmark first")
    return ids


def categories_of(subset: str, settings: Mapping[str, Any]) -> List[str]:
    if subset == "category":
        raw = settings.get("categories") or ["simple_python"]
        chosen = [c.strip() for c in raw.split(",")] if isinstance(raw, str) else [str(c) for c in raw]
    elif subset in SUBSET_CATEGORIES:
        chosen = list(SUBSET_CATEGORIES[subset])
    else:
        raise ValueError(f"unknown subset {subset!r}; the subsets are smoke, simple, category, full")
    allowed = set(NON_LIVE + LIVE + MULTI_TURN)
    bad = [c for c in chosen if c not in allowed]
    if bad:
        why = "is not run here (see the adapter's notes)" if all(c in NOT_RUN for c in bad) else "is not a BFCL v4 category this adapter runs"
        raise ValueError(f"{', '.join(bad)} {why}; the categories are {', '.join(sorted(allowed))}")
    return chosen


def select_ids(source: Path, subset: str, settings: Mapping[str, Any]) -> Dict[str, List[str]]:
    """category -> the case ids to run, in file order."""
    limit = int(settings.get("limit") or (SMOKE_PER_CATEGORY if subset == "smoke" else 0))
    explicit = set(settings.get("tasks") or [])
    out: Dict[str, List[str]] = {}
    for cat in categories_of(subset, settings):
        ids = category_ids(source, cat)
        if explicit:
            ids = [i for i in ids if i in explicit]
        if limit > 0:
            ids = ids[:limit]
        if ids:
            out[cat] = ids
    missing = sorted(explicit - {i for v in out.values() for i in v})
    if missing:
        raise ValueError(f"no such BFCL case in the selected categories: {', '.join(missing)}")
    return out


# -- reading what BFCL left ---------------------------------------------------------------------------------------------

def find_files(root: Path, kind: str, category: str) -> List[Path]:
    """The result or score files of a category for our model, wherever BFCL put them under result/ or score/ (it groups them in sub-folders)."""
    suffix = "result" if kind == "result" else "score"
    return sorted((Path(root) / kind / REGISTRY_NAME).rglob(f"{VERSION}_{category}_{suffix}.json"))


def read_results(root: Path, category: str) -> Dict[str, Dict[str, Any]]:
    return {str(e["id"]): e for f in find_files(root, "result", category) for e in read_lines(f) if "id" in e}


def read_scores(root: Path, category: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """(the score file's header, the failing entries by id), or (None, {}) when BFCL wrote no score file."""
    header: Optional[Dict[str, Any]] = None
    failures: Dict[str, Dict[str, Any]] = {}
    for f in find_files(root, "score", category):
        for i, e in enumerate(read_lines(f)):
            if "accuracy" in e and "id" not in e:
                header = e
            elif "id" in e:
                failures[str(e["id"])] = e
    return header, failures


def _calls_of(value: Any) -> List[Tuple[str, Any]]:
    """The (name, arguments) pairs in a model result: `[{"f": "{...}"}]` for single-turn, lists of those or of call strings for multi-turn."""
    out: List[Tuple[str, Any]] = []
    if isinstance(value, dict):
        for name, args in value.items():
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            out.append((str(name), args))
    elif isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_calls_of(item))
    elif isinstance(value, str) and re.match(r"^[A-Za-z_][\w.]*\(.*\)$", value.strip(), re.S):
        name, _, rest = value.strip().partition("(")
        out.append((name, rest[:-1]))
    return out


def events_for(entry: Optional[Mapping[str, Any]], failure: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """A case as events (analysis.py): one tool_call for each call the model made (invalid when BFCL could not decode the answer), then the way it ended."""
    events: List[Dict[str, Any]] = []
    raw = (entry or {}).get("result")
    kind = str((failure or {}).get("error_type") or "")
    invalid = bool(failure) and bool(DECODE_FAILURE.search(kind))
    for name, args in _calls_of(raw):
        events.append({"type": "tool_call", "name": name[:60], "args": args if isinstance(args, (dict, str)) else {}, "decision": "invalid" if invalid else "executed"})
        events.append({"type": "tool_result", "name": name[:60]})
    if not events:
        events.append({"type": "assistant", "text": clip(raw if isinstance(raw, str) else json.dumps(raw, default=str), 1500)})
    if invalid:
        events.append({"type": "error", "text": f"invalid call: {kind}"})
    events.append({"type": "stop", "reason": "context_overflow" if CONTEXT.search(str(raw)) else "final"})
    return events


def verifier_text(case_id: str, entry: Optional[Mapping[str, Any]], failure: Optional[Mapping[str, Any]]) -> str:
    if failure is None:
        return f"{case_id}: BFCL's checker accepted the answer"
    errors = failure.get("error")
    lines = [f"{case_id}: BFCL's checker rejected the answer", f"error type: {failure.get('error_type')}",
             "error: " + clip("; ".join(map(str, errors)) if isinstance(errors, list) else errors, 1500)]
    if failure.get("model_result_raw") is not None:
        lines.append("model answer: " + clip(json.dumps(failure["model_result_raw"], default=str), 1200))
    if failure.get("possible_answer") is not None:
        lines.append("an accepted answer: " + clip(json.dumps(failure["possible_answer"], default=str), 800))
    return "\n".join(lines)


def tokens_of(entry: Optional[Mapping[str, Any]]) -> int:
    def total(v: Any) -> int:
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, (list, tuple)):
            return sum(total(x) for x in v)
        return 0
    return total((entry or {}).get("input_token_count")) + total((entry or {}).get("output_token_count"))


def seconds_of(entry: Optional[Mapping[str, Any]]) -> float:
    def total(v: Any) -> float:
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, (list, tuple)):
            return sum(total(x) for x in v)
        return 0.0
    return total((entry or {}).get("latency"))


def case_result(case_id: str, category: str, entry: Optional[Mapping[str, Any]], header: Optional[Mapping[str, Any]], failure: Optional[Mapping[str, Any]],
                ctx: TrialContext) -> TrialResult:
    """One case as ours. Unjudged (so run again on resume) when the model server failed during generation, when BFCL produced no answer or no score file."""
    facts: Dict[str, Any] = {"category": category, "case": case_id}
    if entry is None:
        return TrialResult(error=f"BFCL produced no answer for {case_id}", facts={**facts, "class": "environment_problem"})
    raw = entry.get("result")
    if isinstance(raw, str) and raw.startswith("Error during inference") and not CONTEXT.search(raw):
        return TrialResult(error=clip(raw, 500), facts={**facts, "class": "environment_problem"}, tokens=tokens_of(entry))
    if header is None:
        return TrialResult(error=f"BFCL wrote no score file for {category}", facts={**facts, "class": "environment_problem"})
    events = events_for(entry, failure)
    traj, ver = ctx.trial_dir / "trajectory.jsonl", ctx.trial_dir / "verifier.txt"
    write_events(traj, events)
    ver.write_text(verifier_text(case_id, entry, failure), encoding="utf-8")
    passed = failure is None
    res = TrialResult(passed=passed, score=1.0 if passed else 0.0, seconds=seconds_of(entry), tokens=tokens_of(entry), trajectory_path=ctx.relative(traj),
                      verifier_output_path=ctx.relative(ver), facts=facts)
    if failure is not None:
        res.facts["error_type"] = str(failure.get("error_type") or "")
        res.error = clip(f"{failure.get('error_type')}: {'; '.join(map(str, failure['error'])) if isinstance(failure.get('error'), list) else failure.get('error')}", 600)
        if DECODE_FAILURE.search(res.facts["error_type"]):
            res.facts["class"] = "tool_format_error"
        if CONTEXT.search(str(raw)):
            res.facts["class"] = "context_overflow"
    return res


# -- the adapter --------------------------------------------------------------------------------------------------------

class BfclAdapter:
    id = ID
    SETTINGS = {"subset": ("smoke", "simple", "category", "full"), "categories": "with subset category", "limit": "cases per category", "python": "uv's interpreter"}

    def __init__(self, runner: LongRunner = long_runner, which: Callable[[str], Optional[str]] = shutil.which, environ: Optional[Mapping[str, str]] = None) -> None:
        self.runner, self.which, self.environ = runner, which, environ
        self._done: Dict[Tuple[str, str], Dict[str, Any]] = {}         # (run folder, category) -> what generation and scoring left
        self._attempts: Dict[Tuple[str, str], int] = {}

    def plan(self, settings: Mapping[str, Any], bench_dir: Path) -> List[Key]:
        subset = str(settings.get("subset") or "smoke")
        chosen = select_ids(source_of(bench_dir), subset, settings)
        return [(cat, i, 1) for cat, ids in chosen.items() for i in ids]

    # BFCL, one category at a time --------------------------------------------------------------------------------------

    def folder(self, ctx: TrialContext) -> Path:
        return ctx.run_dir / "bfcl"

    def environment(self, ctx: TrialContext, endpoint: str, served: str) -> Dict[str, str]:
        base = f"{endpoint}/v1"
        return clean_env(self.environ, {"BFCL_PROJECT_ROOT": str(self.folder(ctx)), "OPENAI_API_KEY": LOCAL_KEY, "OPENAI_BASE_URL": base, "BASALT_BFCL_REGISTRY": REGISTRY_NAME,
                                        "BASALT_BFCL_MODEL": served, "BASALT_BFCL_HOST": urlparse(endpoint).hostname or "", "UV_NO_PROGRESS": "1"})

    def command(self, source: Path, settings: Mapping[str, Any], *args: str) -> List[str]:
        return uv_argv(self.which) + ["run", "--project", str(project_of(source)), "--python", str(settings.get("python") or "3.12"), "python", str(LAUNCHER), *args]

    def write_project(self, ctx: TrialContext, category: str, ids: Sequence[str], endpoint: str) -> None:
        root = self.folder(ctx)
        root.mkdir(parents=True, exist_ok=True)
        (root / ".env").write_text(f"OPENAI_API_KEY={LOCAL_KEY}\nOPENAI_BASE_URL={endpoint}/v1\n", encoding="utf-8")
        (root / "test_case_ids_to_generate.json").write_text(json.dumps({category: list(ids)}), encoding="utf-8")

    def run_category(self, category: str, ctx: TrialContext) -> Dict[str, Any]:
        """Generates and scores one category. Returns {error, results, header, failures, seconds}."""
        source = source_of(ctx.bench_dir)
        record = run_record(ctx)
        model = dict(record.get("model") or {})
        endpoint = local_endpoint(str(model.get("endpoint") or ""), model)
        ids = select_ids(source, str(ctx.settings.get("subset") or "smoke"), ctx.settings)[category]
        self.write_project(ctx, category, ids, endpoint)
        env = self.environment(ctx, endpoint, str(model.get("name") or model_key(model)))
        log = ctx.run_dir / "logs"
        started = time.monotonic()
        gen = self.runner(self.command(source, ctx.settings, "generate", "--model", REGISTRY_NAME, "--run-ids", "--num-threads", "1"), cwd=project_of(source), env=env,
                          timeout=GENERATE_S, should_stop=ctx.should_stop, log_path=log / f"bfcl-generate-{category}.log")
        if ctx.should_stop():
            return {"error": "stopped on request before the category finished"}
        results = read_results(self.folder(ctx), category)
        if gen.returncode != 0 and not results:
            return {"error": f"bfcl generate failed (exit {gen.returncode}): {clip(((gen.stdout or '') + ' ' + (gen.stderr or ''))[-600:])}"}
        ev = self.runner(self.command(source, ctx.settings, "evaluate", "--model", REGISTRY_NAME, "--test-category", category, "--partial-eval"), cwd=project_of(source),
                         env=env, timeout=EVALUATE_S, should_stop=ctx.should_stop, log_path=log / f"bfcl-evaluate-{category}.log")
        header, failures = read_scores(self.folder(ctx), category)
        if header is None:
            return {"error": f"bfcl evaluate left no score file (exit {ev.returncode}): {clip(((ev.stdout or '') + ' ' + (ev.stderr or ''))[-600:])}"}
        return {"error": "", "results": results, "header": header, "failures": failures, "seconds": time.monotonic() - started}

    def run_trial(self, key: Key, ctx: TrialContext) -> TrialResult:
        category, case_id = str(key[0]), str(key[1])
        cache_key = (str(ctx.run_dir), category)
        if cache_key not in self._done or (self._done[cache_key].get("error") and self._attempts.get(cache_key, 0) < ATTEMPTS):
            self._attempts[cache_key] = self._attempts.get(cache_key, 0) + 1
            try:
                self._done[cache_key] = self.run_category(category, ctx)
            except (OSError, ValueError, FileNotFoundError, KeyError) as exc:
                self._done[cache_key] = {"error": f"{type(exc).__name__}: {exc}"}
        got = self._done[cache_key]
        if got.get("error"):
            return TrialResult(error=str(got["error"]), facts={"class": "environment_problem", "category": category})
        ctx.trial_dir.mkdir(parents=True, exist_ok=True)
        return case_result(case_id, category, got["results"].get(case_id), got["header"], got["failures"].get(case_id), ctx)

    # scoring ----------------------------------------------------------------------------------------------------------

    def score(self, results: Mapping[Key, TrialResult]) -> Dict[str, Any]:
        valid = [(k, r) for k, r in results.items() if r.valid]
        won = sum(1 for _k, r in valid if r.passed)
        by_cat: Dict[str, List[bool]] = {}
        for k, r in valid:
            by_cat.setdefault(str(k[0]), []).append(bool(r.passed))
        cats = {c: {"accuracy": sum(v) / len(v), "correct": sum(v), "total": len(v)} for c, v in sorted(by_cat.items())}
        errors: Dict[str, int] = {}
        for _k, r in valid:
            if not r.passed and r.facts.get("error_type"):
                errors[r.facts["error_type"]] = errors.get(r.facts["error_type"], 0) + 1
        return {"accuracy": won / len(valid) if valid else 0.0, "correct": won, "of": len(valid), "invalid": len(results) - len(valid), "categories": cats,
                "macro_accuracy": sum(c["accuracy"] for c in cats.values()) / len(cats) if cats else 0.0,
                "errors_by_type": dict(sorted(errors.items(), key=lambda kv: -kv[1])[:10]), "notes": list(NOTES)}

    def failure_facts(self, result: TrialResult) -> Dict[str, Any]:
        return {"error": result.error, **result.facts}


ADAPTER = BfclAdapter
