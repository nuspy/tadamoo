"""tau-bench (sierra-research/tau2-bench; the current release is "tau-bench 2.0", whose v1.0 was called tau3-bench): a customer-service agent talks to a simulated
user, follows a domain policy and calls domain tools; the final database state, the actions and the facts told to the user decide a 0/1 reward. Scored pass^k.

One trial is one `tau2 run` of one task with one trial (`--num-trials 1`), so the run's journal keeps the unit the person resumes by; the k trials of a task are
k such runs with different seeds. The agent is tau2-bench's own `llm_agent` (its system prompt, its tool calling, its loop) on the served local model; our harness
takes part through the tool-call repair of tadamoo/tools/repair.py, applied to every reply (malformed argument JSON, a tool name one slip off), because tau2-bench
owns the loop and executes the tools. So this measures the model, the server's template and tool-call parser, and that repair; it does not exercise our agent loop.

Every model call of tau2-bench goes to the local engine (`tau3_assets/launcher.py` wraps its one completion function): the agent, the user simulator, the
communication judge and the assertion judge. The official simulator and judges are cloud models, so the score is provisional and not comparable with the board.

Settings read: `subset` (airline, retail, telecom, banking, or smoke: the first two airline tasks), `trials` (k, default 1; pass^k needs k), `tasks` (explicit task
ids), `limit` (the first N tasks), `judge` ('local': the local model judges the communicated facts; 'none': the original substring match), `repair` (true by
default), `max_steps` (tau2-bench's own default), `sim_timeout_s` (wall-clock limit of one simulation, none by default), `trial_timeout_s` (limit of the whole process,
two hours by default). Domains as tau2-bench names them: banking is `banking_knowledge`, which needs the `knowledge` extra and is installed on first use."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from tadamoo.bench.runner import Key, TrialContext, TrialResult

from ._common import LOCAL_KEY, clean_env, clip, model_key, pass_hat_k, read_lines, run_record, source_of, uv_argv, write_events
from .containerkit import local_endpoint
from .proc import LongRunner, long_runner

ID = "tau3-bench"
DOMAINS = {"airline": "airline", "retail": "retail", "telecom": "telecom", "banking": "banking_knowledge"}
SMOKE_TASKS = 2
BASE_SEED = 300                       # tau2-bench's own default seed; trial k uses BASE_SEED + k - 1
TRIAL_TIMEOUT_S = 2 * 3600.0
LAUNCHER = Path(__file__).resolve().parent / "tau3_assets" / "launcher.py"
REPO_ROOT = Path(__file__).resolve().parents[3]
UNJUDGED = {"infrastructure_error", "user_error", "unexpected_error"}      # no verdict on the agent: the engine failed or the simulated user (the local model) did
STOP_NAMES = {"user_stop": "final", "agent_stop": "final", "max_steps": "timeout", "timeout": "timeout", "too_many_errors": "gave_up", "agent_error": "error",
              "context_window_exceeded": "context_overflow"}
NOTES = ("The user simulator, the communication judge and the assertion judge are the local model, not the cloud models of the board: provisional, not comparable with the board.",
         "tau2-bench's own agent loop runs the served model; our harness contributes the tool-call repair only, so this measures the model and the server's tool-call "
         "handling more than the harness.",
         "Trials of one task differ only by the seed of the user simulator and the server's sampling; with a fixed-sampling server they can be identical.")


# -- the tasks ----------------------------------------------------------------------------------------------------------

def domain_dir(source: Path, domain: str) -> Path:
    return Path(source) / "data" / "tau2" / "domains" / DOMAINS[domain]


def task_ids(source: Path, domain: str) -> List[str]:
    """The ids of the base task set of a domain, in file order: the split's `base` list for the domains that have a split, else every task in tasks.json."""
    folder = domain_dir(source, domain)
    try:
        split = json.loads((folder / "split_tasks.json").read_text(encoding="utf-8"))
        ids = [str(i) for i in split.get("base") or []]
    except (OSError, ValueError):
        ids = []
    if not ids:
        try:
            tasks = json.loads((folder / "tasks.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            tasks = []
        ids = [str(t["id"]) for t in tasks if isinstance(t, dict) and "id" in t]
    if not ids:
        raise FileNotFoundError(f"no tasks found for the domain {domain!r} in {folder}; import the benchmark first")
    return ids


def select_tasks(source: Path, subset: str, settings: Mapping[str, Any]) -> Tuple[str, List[str]]:
    domain = "airline" if subset == "smoke" else subset
    if domain not in DOMAINS:
        raise ValueError(f"unknown subset {subset!r}; the subsets are smoke, {', '.join(DOMAINS)}")
    ids = task_ids(source, domain)
    explicit = settings.get("tasks")
    if explicit:
        missing = [t for t in explicit if t not in ids]
        if missing:
            raise ValueError(f"no such task in {domain}: {', '.join(map(str, missing))}")
        return domain, [str(t) for t in explicit]
    limit = int(settings.get("limit") or (SMOKE_TASKS if subset == "smoke" else 0))
    return domain, ids[:limit] if limit > 0 else ids


# -- reading what tau2-bench left ---------------------------------------------------------------------------------------

def find_simulation(save_dir: Path, task: str) -> Optional[Dict[str, Any]]:
    """The simulation of the task in the results.json under the save folder (or None)."""
    for f in sorted(Path(save_dir).rglob("results.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for sim in data.get("simulations") or []:
            if str(sim.get("task_id")) == str(task):
                return sim
    return None


def _usage_tokens(messages: Sequence[Mapping[str, Any]], role: str) -> int:
    total = 0
    for m in messages:
        if m.get("role") == role and isinstance(m.get("usage"), Mapping):
            u = m["usage"]
            total += int(u.get("prompt_tokens") or u.get("input_tokens") or 0) + int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    return total


def events_from(messages: Sequence[Mapping[str, Any]], termination: str) -> List[Dict[str, Any]]:
    """The conversation as events (analysis.py): each tool call the agent made with how it went, what the agent said, and the way the run ended."""
    failed = {str(m.get("id")): clip(m.get("content") or "tool error", 600) for m in messages if m.get("role") == "tool" and m.get("error") and m.get("requestor") != "user"}
    events: List[Dict[str, Any]] = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for call in m.get("tool_calls") or []:
            bad = str(call.get("id")) in failed
            events.append({"type": "tool_call", "name": str(call.get("name"))[:60], "args": call.get("arguments") or {}, "decision": "failed" if bad else "executed"})
            events.append({"type": "tool_result", "name": str(call.get("name"))[:60], **({"error": failed[str(call.get("id"))]} if bad else {})})
        if m.get("content"):
            events.append({"type": "assistant", "text": clip(m["content"], 1500)})
    events.append({"type": "stop", "reason": STOP_NAMES.get(termination, termination or "final")})
    return events


def verifier_text(sim: Mapping[str, Any]) -> str:
    info = sim.get("reward_info") or {}
    lines = [f"reward: {info.get('reward')}  basis: {', '.join(map(str, info.get('reward_basis') or [])) or 'unknown'}  ended: {sim.get('termination_reason')}"]
    db = info.get("db_check")
    if db:
        lines.append(f"database: {'matches' if db.get('db_match') else 'DOES NOT match the expected state'}")
    for c in info.get("action_checks") or []:
        if not c.get("action_match"):
            a = c.get("action") or {}
            lines.append(f"action not matched: {a.get('name')} {clip(json.dumps(a.get('arguments') or {}, ensure_ascii=False), 200)}")
    for c in info.get("communicate_checks") or []:
        if not c.get("met"):
            lines.append(f"information not communicated: {clip(c.get('info'), 200)} ({clip(c.get('justification'), 200)})")
    for c in info.get("nl_assertions") or []:
        if not c.get("met"):
            lines.append(f"assertion not met: {clip(c.get('nl_assertion'), 200)} ({clip(c.get('justification'), 200)})")
    for c in info.get("env_assertions") or []:
        if not c.get("met"):
            lines.append("environment assertion not met")
    return "\n".join(lines)


def trial_result(sim: Optional[Mapping[str, Any]], domain: str, task: str, ctx: TrialContext, note: str = "", repairs: int = 0) -> TrialResult:
    """tau2-bench's simulation of one task as ours. The reward counts when the agent had its chance; an engine failure, or a failure of the simulated user (the local
    model), leaves the trial unjudged so it is run again on resume."""
    if sim is None:
        return TrialResult(error=f"tau2 left no simulation for the task: {note or 'no output'}", facts={"class": "environment_problem"})
    termination = str(sim.get("termination_reason") or "")
    info = sim.get("reward_info")
    messages = sim.get("messages") or []
    events = events_from(messages, termination)
    traj, ver = ctx.trial_dir / "trajectory.jsonl", ctx.trial_dir / "verifier.txt"
    write_events(traj, events)
    ver.parent.mkdir(parents=True, exist_ok=True)
    ver.write_text(verifier_text(sim), encoding="utf-8")
    res = TrialResult(seconds=float(sim.get("duration") or 0.0), tokens=_usage_tokens(messages, "assistant") + _usage_tokens(messages, "user"),
                      trajectory_path=ctx.relative(traj), verifier_output_path=ctx.relative(ver),
                      facts={"domain": domain, "task": task, "termination": termination, "agent_tokens": _usage_tokens(messages, "assistant"),
                             "user_tokens": _usage_tokens(messages, "user"), "repaired_calls": repairs, "steps": len(messages)})
    if termination in UNJUDGED or not isinstance(info, Mapping) or info.get("reward") is None:
        res.error = f"{termination or 'no reward'}: {clip(note or (sim.get('info') or ''), 300)}"
        res.facts["class"] = "environment_problem"
        return res
    reward = float(info["reward"])
    res.score = reward
    res.passed = (1 - 1e-6) <= reward <= (1 + 1e-6)             # tau2-bench's own test of a successful reward
    if not res.passed:
        res.error = f"reward {reward:g} ({termination})"
    return res


# -- the adapter --------------------------------------------------------------------------------------------------------

class Tau3Adapter:
    id = ID
    SETTINGS = {"subset": ("smoke",) + tuple(DOMAINS), "trials": "k for pass^k", "judge": ("local", "none"), "repair": "tool-call repair (true)"}

    def __init__(self, runner: LongRunner = long_runner, which: Callable[[str], Optional[str]] = shutil.which, environ: Optional[Mapping[str, str]] = None,
                 repo_root: Path = REPO_ROOT) -> None:
        self.runner, self.which, self.environ, self.repo_root = runner, which, environ, Path(repo_root)

    def plan(self, settings: Mapping[str, Any], bench_dir: Path) -> List[Key]:
        domain, ids = select_tasks(source_of(bench_dir), str(settings.get("subset") or "smoke"), settings)
        trials = int(settings.get("trials") or 1)
        return [(domain, task, rep) for task in ids for rep in range(1, trials + 1)]

    def llm_args(self, base_url: str, temperature: float = 0.0) -> Dict[str, Any]:
        return {"api_base": base_url, "api_key": LOCAL_KEY, "temperature": temperature}

    def command(self, key: Key, ctx: TrialContext, source: Path, served: str, base_url: str, save_dir: Path) -> List[str]:
        domain, task, rep = str(key[0]), str(key[1]), int(key[2])
        settings = ctx.settings
        name = "openai/" + served
        argv = uv_argv(self.which) + ["run", "--project", str(source)]
        if DOMAINS[domain] == "banking_knowledge":
            argv += ["--extra", "knowledge"]
        argv += ["python", str(LAUNCHER), "run", "--domain", DOMAINS[domain], "--task-ids", task, "--num-trials", "1", "--agent", "llm_agent", "--agent-llm", name,
                 "--agent-llm-args", json.dumps(self.llm_args(base_url)), "--user", "user_simulator", "--user-llm", name, "--user-llm-args", json.dumps(self.llm_args(base_url)),
                 "--save-to", str(save_dir), "--max-concurrency", "1", "--seed", str(BASE_SEED + rep - 1), "--log-level", "ERROR"]
        if str(settings.get("judge") or "local") == "none":
            argv += ["--communicate-scorer", "substring"]
        else:
            argv += ["--communicate-judge", json.dumps({"model": name, "model_args": {"api_base": base_url, "api_key": LOCAL_KEY}})]
        if settings.get("max_steps"):
            argv += ["--max-steps", str(int(settings["max_steps"]))]
        if settings.get("sim_timeout_s"):
            argv += ["--timeout", str(float(settings["sim_timeout_s"]))]
        return argv

    def run_trial(self, key: Key, ctx: TrialContext) -> TrialResult:
        domain, task = str(key[0]), str(key[1])
        judge = str(ctx.settings.get("judge") or "local")
        if judge not in ("local", "none"):
            return TrialResult(error=f"judge '{judge}' is not allowed; use 'local' or 'none'", facts={"class": "environment_problem"})
        source = source_of(ctx.bench_dir)
        if not domain_dir(source, domain).is_dir():
            return TrialResult(error=f"the domain folder {domain_dir(source, domain)} is missing; import the benchmark again", facts={"class": "environment_problem"})
        model = dict(run_record(ctx).get("model") or {})
        try:
            endpoint = local_endpoint(str(model.get("endpoint") or ""), model)
        except ValueError as exc:
            return TrialResult(error=str(exc), facts={"class": "environment_problem"})
        served = str(model.get("name") or model_key(model))
        base_url = endpoint + "/v1"
        ctx.trial_dir.mkdir(parents=True, exist_ok=True)
        save_dir = ctx.trial_dir / "sim"
        shutil.rmtree(save_dir, ignore_errors=True)                  # an earlier attempt of this trial must not be read as this one
        repairs_log = ctx.trial_dir / "repairs.jsonl"
        repairs_log.unlink(missing_ok=True)
        env = clean_env(self.environ, {"OPENAI_API_KEY": LOCAL_KEY, "OPENAI_API_BASE": base_url, "BASALT_TAU_BASE_URL": base_url, "BASALT_TAU_MODEL": served,
                                       "BASALT_TAU_HOST": urlparse(endpoint).hostname or "", "BASALT_TAU_REPAIR": "0" if ctx.settings.get("repair") is False else "1",
                                       "BASALT_TAU_REPAIRS_LOG": str(repairs_log), "BASALT_REPO": str(self.repo_root), "UV_NO_PROGRESS": "1"})
        done = self.runner(self.command(key, ctx, source, served, base_url, save_dir), cwd=source, env=env, timeout=float(ctx.settings.get("trial_timeout_s") or TRIAL_TIMEOUT_S),
                           should_stop=ctx.should_stop, log_path=ctx.trial_dir / "tau2.log")
        if ctx.should_stop():
            return TrialResult(error="stopped on request before the task finished", facts={"class": "environment_problem"})
        sim = find_simulation(save_dir, task)
        note = clip(((done.stdout or "") + " " + (done.stderr or ""))[-600:]) if done.returncode != 0 or sim is None else ""
        repairs = len(read_lines(repairs_log))
        return trial_result(sim, domain, task, ctx, f"exit code {done.returncode}: {note}" if note else "", repairs)

    # scoring ----------------------------------------------------------------------------------------------------------

    def score(self, results: Mapping[Key, TrialResult]) -> Dict[str, Any]:
        valid = [(k, r) for k, r in results.items() if r.valid]
        tasks: Dict[Tuple[str, str], List[bool]] = {}
        for k, r in valid:
            tasks.setdefault((str(k[0]), str(k[1])), []).append(bool(r.passed))
        kmax = min((len(v) for v in tasks.values()), default=0)

        def hat(items: Sequence[List[bool]], k: int) -> float:
            usable = [v for v in items if len(v) >= k]
            return sum(pass_hat_k(len(v), sum(v), k) for v in usable) / len(usable) if usable else 0.0

        by_k = {k: hat(list(tasks.values()), k) for k in range(1, kmax + 1)}
        domains: Dict[str, Dict[str, Any]] = {}
        for d in sorted({d for d, _t in tasks}):
            items = [v for (dd, _t), v in tasks.items() if dd == d]
            domains[d] = {"tasks": len(items), **{f"pass_hat_{k}": hat(items, k) for k in range(1, kmax + 1)}}
        won = sum(1 for _k, r in valid if r.passed)
        return {"pass_hat_k": by_k.get(kmax, 0.0), "k": kmax, "pass_hat": {str(k): v for k, v in by_k.items()}, "pass_rate": won / len(valid) if valid else 0.0,
                "passed": won, "of": len(valid), "invalid": len(results) - len(valid), "tasks": len(tasks), "by_domain": domains,
                "repaired_calls": sum(int(r.facts.get("repaired_calls") or 0) for _k, r in valid), "notes": list(NOTES)}

    def failure_facts(self, result: TrialResult) -> Dict[str, Any]:
        return {"error": result.error, **result.facts}


ADAPTER = Tau3Adapter
