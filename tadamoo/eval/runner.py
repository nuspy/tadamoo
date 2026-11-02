"""Evaluation runner: every trial is a fresh agent run on a fresh workspace, checked on the result.

A trial that crashes, times out or hits a server error is a failed trial with its reason, never an
exception that ends the evaluation. Repetitions use a different seed each, so pass^k (all k trials pass)
means something; with temperature 0 they would repeat the same run.
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo.agent_loop import AgentLoop, answer_text
from tadamoo.client import BasaltClient
from tadamoo.prompt_blocks import instruction_blocks
from tadamoo.stack import AgentConfig, build_agent
from tadamoo.tools.registry import EffectClass, ToolRegistry

from .resume import Journal, make_fingerprint
from .serverlog import ServerTimings, summarize_requests
from .tasks import Task, load_task_set, make_workspace, run_checks
from .traces import observe

ClientFactory = Callable[[Task, int], Any]


@dataclass
class Trial:
    repetition: int
    passed: bool
    failures: List[str] = field(default_factory=list)
    stop_reason: str = ""
    error: str = ""
    turns: int = 0
    tool_calls: int = 0
    invalid_calls: int = 0           # calls the harness could not run: unknown tool, bad arguments, unreadable
    denied_calls: int = 0            # calls refused by permissions or hooks
    repaired_calls: int = 0          # calls the harness mended (name, JSON, value types) before they ran
    length_stops: int = 0            # replies the output limit ended
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    seconds: float = 0.0
    answer: str = ""
    server_requests: int = 0             # requests the server's own log shows in this trial's time window (0: not read)
    server_ttft_s: Optional[float] = None       # their median time to the first token
    server_prefill_s: Optional[float] = None    # their median prefill time
    server_prompt_tokens: int = 0
    server_cached_tokens: int = 0
    trace: List[Dict[str, str]] = field(default_factory=list)      # the tool calls in order (tadamoo/eval/traces.py)
    components: Dict[str, int] = field(default_factory=dict)       # counts of the components the run used


@dataclass
class EvalResult:
    task_id: str
    category: str
    k_trials: int
    pass_at_k: float
    pass_pow_k: bool
    trials: List[Trial] = field(default_factory=list)

    @property
    def runs(self) -> List[Trial]:        # alias used by the report
        return self.trials


def trial_is_valid(trial: Trial) -> bool:
    """False for a trial the server or the harness broke (an error stop, a crash before any turn): a resumed run does it again."""
    return not (trial.stop_reason == "error" or (trial.error and not trial.turns))


def dummy_registry(task: Task) -> ToolRegistry:
    registry = ToolRegistry()
    for spec in task.tools:
        registry.register(spec["name"], spec.get("description", ""), spec.get("parameters", {"type": "object", "properties": {}}),
                          EffectClass.PURE, lambda _r=str(spec.get("returns", "ok")), **kw: _r, untrusted=bool(spec.get("untrusted", True)))
    return registry


class EvalRunner:
    def __init__(
        self,
        client: Optional[BasaltClient] = None,
        client_factory: Optional[ClientFactory] = None,
        temperature: Optional[float] = 0.6,
        seed_base: int = 1000,
        keep_workspaces: Optional[Path] = None,
        max_total_tokens: Optional[int] = 60_000,
        max_seconds: Optional[float] = 600.0,
        council: bool = False,
        timings: Optional[ServerTimings] = None,
        instruction_blocks: bool = False,
        journal: Optional[Journal] = None,
    ) -> None:
        self.client = client
        self.client_factory = client_factory
        self.temperature = temperature
        self.seed_base = seed_base
        self.keep = keep_workspaces
        self.max_total_tokens = max_total_tokens
        self.max_seconds = max_seconds
        self.council = council
        self.timings = timings                      # reads the server's own figures for each trial's requests
        self.instruction_blocks = instruction_blocks    # the working rules of tadamoo/prompt_blocks.py in the system prompt
        self.journal = journal                          # finished trials are written to it, and valid ones it holds are reused (tadamoo/eval/resume.py)

    def load_task_set(self, path: str | Path) -> List[Task]:
        return load_task_set(path)[1]

    def _client_for(self, task: Task, rep: int) -> Any:
        if self.client_factory is not None:
            return self.client_factory(task, rep)
        if self.client is None:
            raise RuntimeError("an evaluation needs a client or a client_factory")
        return self.client

    def run_trial(self, task: Task, rep: int) -> Trial:
        t0 = time.monotonic()
        name = re.sub(r"[^A-Za-z0-9_.@-]", "_", task.id)                 # a task id may hold a slash (bfcl/simple_0): it names folders here
        work = Path(tempfile.mkdtemp(prefix=f"basalt-eval-{name}-"))
        data = Path(tempfile.mkdtemp(prefix="basalt-eval-data-"))
        trial = Trial(repetition=rep, passed=False)
        agent = None
        built = None
        mark = self.timings.mark() if self.timings is not None else None
        try:
            workspace = make_workspace(task, work / "ws")
            client = self._client_for(task, rep)
            sampling = {"seed": self.seed_base + rep, **({"temperature": self.temperature} if self.temperature is not None else {})}
            if task.mode == "tools":
                registry = dummy_registry(task)
                system = task.system or "You are a helpful assistant with tools. Use a tool only when it is needed."
                if self.instruction_blocks:
                    system += "\n\n" + instruction_blocks(registry.names())
                agent = AgentLoop(client=client, tool_registry=registry, max_turns=task.max_turns,
                                  max_total_tokens=self.max_total_tokens, max_seconds=self.max_seconds, sampling=sampling, system_prompt=system,
                                  budget_grace=False)
            else:
                cfg = AgentConfig(workspace=str(workspace), max_turns=task.max_turns, permission_mode="default", allow=task.allow,
                                  use_memory=False, use_checkpoints=False, data_dir=str(data), max_total_tokens=self.max_total_tokens,
                                  max_seconds=self.max_seconds, reference_docs=task.knowledge if task.strategy != "none" else {},
                                  reference_strategy=task.strategy, council=self.council, instruction_blocks=self.instruction_blocks,
                                  budget_grace=False)
                built = build_agent(cfg, client=client)
                agent = built.agent
                agent.sampling = sampling
            turns = agent.run(task.prompt)
            if self.timings is not None:
                seen = summarize_requests(self.timings.requests_since(mark))
                trial.server_requests, trial.server_ttft_s, trial.server_prefill_s = seen["requests"], seen["ttft_median_s"], seen["prefill_median_s"]
                trial.server_prompt_tokens, trial.server_cached_tokens = seen["prompt_tokens"], seen["cached_tokens"]
            calls = [{"name": c.tool_name, "arguments": c.written if c.written is not None else c.arguments, "executed": c.decision == "executed"}      # as written: a mended value does not pass a check on the model's own call
                     for t in turns for c in t.tool_calls]
            trial.turns = len(turns)
            trial.tool_calls = len(calls)
            trial.invalid_calls = sum(1 for t in turns for c in t.tool_calls if c.decision == "invalid")
            trial.denied_calls = sum(1 for t in turns for c in t.tool_calls if c.decision in ("denied", "blocked"))
            trial.repaired_calls = sum(1 for t in turns for c in t.tool_calls if c.repaired)
            trial.length_stops = agent.length_stops
            trial.trace, trial.components = observe(turns, agent, getattr(agent, "audit", None), client)
            trial.prompt_tokens = sum(t.prompt_tokens for t in turns)
            trial.completion_tokens = sum(t.completion_tokens for t in turns)
            trial.cached_tokens = sum(t.cached_tokens for t in turns)
            trial.stop_reason = agent.stop_reason
            trial.error = agent.error
            trial.answer = answer_text(turns)
            if agent.stop_reason == "error":
                trial.failures = [f"the model call failed: {agent.error}"]
            else:
                trial.failures = run_checks(task, workspace, trial.answer, calls)
                if agent.stop_reason != "final":
                    trial.failures.insert(0, f"the run ended as {agent.stop_reason}, not with an answer")
            trial.passed = not trial.failures
        except Exception as exc:                          # a broken trial is a failed trial
            trial.error = f"{type(exc).__name__}: {exc}"
            trial.failures = [f"the trial crashed: {trial.error}"]
        finally:
            trial.seconds = round(time.monotonic() - t0, 3)
            if agent is not None or built is not None:
                try:
                    (built or agent).close()
                except Exception:
                    pass
            if self.keep:
                try:
                    shutil.copytree(work, self.keep / f"{name}-{rep}", dirs_exist_ok=True)
                except OSError:
                    pass
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(data, ignore_errors=True)
        return trial

    def fingerprint(self, tasks: List[Task], k: int, environment: Optional[Dict[str, Any]] = None, model_file: Optional[str] = None) -> Dict[str, Any]:
        """What a resumed run must match: the code, the model file, the settings of this runner, the tasks and k."""
        env = {n: environment.get(n) for n in ("model", "gpus", "gpu_power_limit_w")} if environment else {}
        sampling = {"temperature": self.temperature, "seed_base": self.seed_base, "max_total_tokens": self.max_total_tokens, "max_seconds": self.max_seconds,
                    "council": self.council, "instruction_blocks": self.instruction_blocks}
        return make_fingerprint(experiment="eval", k=k, arms=[], tasks=[t.id for t in tasks], sampling=sampling, environment=env, model_file=model_file)

    def journaled_trial(self, task: Task, rep: int) -> Trial:
        key = (task.id, rep)
        kept = self.journal.get(key) if self.journal else None
        if kept is not None:
            return Trial(**kept)
        trial = self.run_trial(task, rep)
        if self.journal:
            self.journal.add(key, asdict(trial), trial_is_valid(trial))
        return trial

    def evaluate_task(self, task: Task | Dict[str, Any], k: int = 5, mock_output_fn: Optional[Callable[..., Any]] = None) -> EvalResult:
        if isinstance(task, dict):                         # a plain dict is accepted too
            from .tasks import _validate
            _validate(task)
            checks = list(task.get("checks", []))
            if task.get("expected_keyword"):
                checks.append({"type": "answer_contains", "text": task["expected_keyword"]})
            task = Task(id=task["id"], category=task.get("category", "general"), prompt=task["prompt"], checks=checks,
                        files=dict(task.get("files", {})), tools=list(task.get("tools", [])), mode="tools" if task.get("tools") else "agent")
        if mock_output_fn is not None:
            def factory(t: Task, rep: int):
                text, reasoning, _tokens = mock_output_fn({"id": t.id}, rep - 1)
                from types import SimpleNamespace
                return SimpleNamespace(chat_completion=lambda **kw: {"choices": [{"message": {"role": "assistant", "content": text}}]})
            runner = EvalRunner(client_factory=factory, temperature=self.temperature, seed_base=self.seed_base, keep_workspaces=self.keep,
                                max_total_tokens=self.max_total_tokens, max_seconds=self.max_seconds, council=self.council,
                                instruction_blocks=self.instruction_blocks)
            return runner.evaluate_task(task, k)
        trials = [self.journaled_trial(task, i + 1) for i in range(k)]
        passed = sum(t.passed for t in trials)
        return EvalResult(task.id, task.category, k, 1.0 if passed else 0.0, passed == k, trials)

    def run_set(self, tasks: List[Task], k: int = 5, on_result: Optional[Callable[[EvalResult], None]] = None) -> List[EvalResult]:
        out = []
        for task in tasks:
            try:
                result = self.evaluate_task(task, k)
            except Exception as exc:                       # a task that cannot even start is a failed task, not the end of the run
                reason = f"{type(exc).__name__}: {exc}"
                result = EvalResult(task.id, task.category, k, 0.0, False,
                                    [Trial(repetition=i + 1, passed=False, error=reason, failures=[f"the task could not be run: {reason}"]) for i in range(k)])
            out.append(result)
            if on_result:
                on_result(result)
        return out


def collect_environment(client: Optional[BasaltClient] = None, endpoint: str = "", repo_root: Optional[Path] = None,
                        gpu_power_limit_w: Optional[float] = None) -> Dict[str, Any]:
    """What a result must say about where it was produced: harness version, model, hardware, power limit."""
    root = repo_root or Path(__file__).resolve().parents[2]
    env: Dict[str, Any] = {"python": platform.python_version(), "platform": platform.platform(), "endpoint": endpoint or "(injected client)",
                           "harness_version": "unknown", "harness_commit": "unknown", "harness_dirty": None, "model": "unknown", "gpus": [], "gpu_power_limit_w": gpu_power_limit_w}
    try:
        import json
        release = json.loads((root / "VERSION").read_text(encoding="utf-8-sig"))
        env["harness_version"] = str(release.get("release") or release.get("version") or "unknown")
    except (OSError, ValueError, AttributeError):
        pass
    try:
        env["harness_commit"] = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
        env["harness_dirty"] = bool(subprocess.run(["git", "status", "--porcelain", "--", "harness", "serve"], cwd=root, capture_output=True, text=True, timeout=10).stdout.strip())
    except Exception:
        pass
    if client is not None and hasattr(client, "metrics"):
        try:
            metrics = client.metrics()
            gpus = ((metrics.get("static") or {}).get("gpus")) or []
            env["gpus"] = [{k: g.get(k) for k in ("name", "power_limit_w", "power_limit", "vram_total_mib", "memory_total_mib") if k in g} for g in gpus if isinstance(g, dict)]
            if gpu_power_limit_w is None:
                limits = [g.get("power_limit_w") or g.get("power_limit") for g in gpus if isinstance(g, dict)]
                env["gpu_power_limit_w"] = next((l for l in limits if l), None)
        except Exception:
            pass
    if client is not None and hasattr(client, "base_url"):
        try:
            import json
            import urllib.request
            req = urllib.request.Request(client.base_url + "/v1/models", headers=client._headers())
            with urllib.request.urlopen(req, timeout=5) as r:
                models = json.loads(r.read().decode("utf-8")).get("data", [])
            env["model"] = models[0]["id"] if models else "unknown"
        except Exception:
            pass
    return env
