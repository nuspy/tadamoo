"""Evaluation runner: every trial is a fresh agent run on a fresh workspace, checked on the result.

A trial that crashes, times out or hits a server error is a failed trial with its reason, never an
exception that ends the evaluation. Repetitions use a different seed each, so pass^k (all k trials pass)
means something; with temperature 0 they would repeat the same run.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from harness.agent_loop import AgentLoop
from harness.client import BasaltClient
from harness.stack import AgentConfig, build_agent
from harness.tools.registry import EffectClass, ToolRegistry

from .tasks import Task, load_task_set, make_workspace, run_checks

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
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    seconds: float = 0.0
    answer: str = ""


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
        temperature: float = 0.6,
        seed_base: int = 1000,
        keep_workspaces: Optional[Path] = None,
        max_total_tokens: Optional[int] = 60_000,
        max_seconds: Optional[float] = 600.0,
    ) -> None:
        self.client = client
        self.client_factory = client_factory
        self.temperature = temperature
        self.seed_base = seed_base
        self.keep = keep_workspaces
        self.max_total_tokens = max_total_tokens
        self.max_seconds = max_seconds

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
        work = Path(tempfile.mkdtemp(prefix=f"basalt-eval-{task.id}-"))
        data = Path(tempfile.mkdtemp(prefix="basalt-eval-data-"))
        trial = Trial(repetition=rep, passed=False)
        agent = None
        built = None
        try:
            workspace = make_workspace(task, work / "ws")
            client = self._client_for(task, rep)
            sampling = {"temperature": self.temperature, "seed": self.seed_base + rep}
            if task.mode == "tools":
                agent = AgentLoop(client=client, tool_registry=dummy_registry(task), max_turns=task.max_turns,
                                  max_total_tokens=self.max_total_tokens, max_seconds=self.max_seconds, sampling=sampling,
                                  system_prompt=task.system or "You are a helpful assistant with tools. Use a tool only when it is needed.")
            else:
                cfg = AgentConfig(workspace=str(workspace), max_turns=task.max_turns, permission_mode="default", allow=task.allow,
                                  use_memory=False, use_checkpoints=False, data_dir=str(data), max_total_tokens=self.max_total_tokens,
                                  max_seconds=self.max_seconds, reference_docs=task.knowledge if task.strategy != "none" else {},
                                  reference_strategy=task.strategy)
                built = build_agent(cfg, client=client)
                agent = built.agent
                agent.sampling = sampling
            turns = agent.run(task.prompt)
            calls = [{"name": c.tool_name, "arguments": c.arguments, "executed": c.decision == "executed"}
                     for t in turns for c in t.tool_calls]
            trial.turns = len(turns)
            trial.tool_calls = len(calls)
            trial.invalid_calls = sum(1 for t in turns for c in t.tool_calls if c.decision == "invalid")
            trial.denied_calls = sum(1 for t in turns for c in t.tool_calls if c.decision in ("denied", "blocked"))
            trial.prompt_tokens = sum(t.prompt_tokens for t in turns)
            trial.completion_tokens = sum(t.completion_tokens for t in turns)
            trial.cached_tokens = sum(t.cached_tokens for t in turns)
            trial.stop_reason = agent.stop_reason
            trial.error = agent.error
            trial.answer = next((t.assistant_content for t in reversed(turns) if not t.tool_calls), "")
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
                    shutil.copytree(work, self.keep / f"{task.id}-{rep}", dirs_exist_ok=True)
                except OSError:
                    pass
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(data, ignore_errors=True)
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
            runner = EvalRunner(client_factory=factory, temperature=self.temperature, seed_base=self.seed_base)
            return runner.evaluate_task(task, k)
        trials = [self.run_trial(task, i + 1) for i in range(k)]
        passed = sum(t.passed for t in trials)
        return EvalResult(task.id, task.category, k, 1.0 if passed else 0.0, passed == k, trials)

    def run_set(self, tasks: List[Task], k: int = 5, on_result: Optional[Callable[[EvalResult], None]] = None) -> List[EvalResult]:
        out = []
        for task in tasks:
            result = self.evaluate_task(task, k)
            out.append(result)
            if on_result:
                on_result(result)
        return out


def collect_environment(client: Optional[BasaltClient] = None, endpoint: str = "", repo_root: Optional[Path] = None,
                        gpu_power_limit_w: Optional[float] = None) -> Dict[str, Any]:
    """What a result must say about where it was produced: harness version, model, hardware, power limit."""
    root = repo_root or Path(__file__).resolve().parents[2]
    env: Dict[str, Any] = {"python": platform.python_version(), "platform": platform.platform(), "endpoint": endpoint or "(injected client)",
                           "harness_commit": "unknown", "harness_dirty": None, "model": "unknown", "gpus": [], "gpu_power_limit_w": gpu_power_limit_w}
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
