"""The Harbor agent of the Terminal-Bench adapter. Harbor imports this module by its path (`-a harness.bench.adapters.terminal_bench_agent:BasaltAgent`), so it
imports `harbor` and is loaded only inside a Harbor process; the repository must be on that process's PYTHONPATH (the adapter sets it).

The agent is an external one in Harbor's terms: the loop runs on the host and acts on the task's container only through `environment.exec`. Our headless agent
runs in a worker thread (its loop is blocking) and each tool of it becomes one `exec` on Harbor's event loop. When Harbor ends the trial (the task's agent timeout
cancels `run`), the agent is told to stop and any command it still sends is refused, so the final state the tests judge is the one at the time limit."""
from __future__ import annotations

import asyncio
import functools
import json
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from harness.bench.adapters.containerkit import AgentOutcome, ExecOut, TrajectoryRecorder, is_server_fault, local_endpoint, run_harness_agent

VERSION_FILE = Path(__file__).resolve().parents[3] / "VERSION"
GRACE_S = 15.0
NOTE = ("The system is the task's Linux container. shell_command runs a bash command in it, and read_file, write_file, edit_file, list_directory and search_code work on "
        "the container's files; a relative path is relative to {cwd}. Nobody can answer questions, so do the task completely on your own. The task is judged by hidden "
        "tests that look at the state of the container when you stop: do what the instruction asks, check it yourself, and stop only when it is really done.")


class ExecGate:
    """Lets the worker thread's commands through until the trial is over."""

    def __init__(self) -> None:
        self.open = True

    def close(self) -> None:
        self.open = False


def harbor_exec(environment: Any, loop: asyncio.AbstractEventLoop, gate: ExecGate):
    """The `exec_fn` of the container tools: a blocking call from the worker thread that runs `environment.exec` on Harbor's loop."""

    def run(command: str, timeout: Optional[float]) -> ExecOut:
        if not gate.open:
            return ExecOut(-1, "", "The trial has ended; the command was not run.")
        coro = environment.exec(command=command, timeout_sec=int(timeout) if timeout else None)
        try:
            done = asyncio.run_coroutine_threadsafe(coro, loop).result()
        except RuntimeError as exc:
            if "timed out" in str(exc):
                return ExecOut(124, "", str(exc), True)
            raise
        except (asyncio.TimeoutError, TimeoutError):
            return ExecOut(124, "", f"timed out after {timeout} seconds", True)
        except asyncio.CancelledError:
            return ExecOut(-1, "", "The trial has ended; the command was not run.")
        return ExecOut(int(done.return_code), done.stdout or "", done.stderr or "")
    return run


class BasaltAgent(BaseAgent):
    """Our headless agent as a Harbor agent. Options (`--ak`): `settings_file`, a JSON file with `endpoint` (the local model server) and `overrides`
    (AgentConfig fields the run's settings changed) and `max_seconds` (the task's time for the agent, less a margin, so that the agent can tell it is running out)."""

    client_factory: Optional[Any] = None          # tests give a scripted model client here; a run builds the client from the endpoint

    @staticmethod
    def name() -> str:
        return "basalt-harness"

    def version(self) -> Optional[str]:
        try:
            return VERSION_FILE.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def __init__(self, logs_dir: Path, model_name: Optional[str] = None, *, settings_file: str = "", **kwargs: Any) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        data: Dict[str, Any] = json.loads(Path(settings_file).read_text(encoding="utf-8")) if settings_file else {}
        self.endpoint = local_endpoint(str(data.get("endpoint") or ""), data)
        self.overrides: Dict[str, Any] = dict(data.get("overrides") or {})
        self.guidance = tuple(data.get("guidance") or ())
        self.max_seconds: Optional[float] = float(data["max_seconds"]) if data.get("max_seconds") else None       # the task's own time, less a margin to write the last report
        self.outcome: Optional[AgentOutcome] = None

    async def setup(self, environment: BaseEnvironment) -> None:
        return

    def _drive(self, instruction: str, exec_fn: Any, cancel: threading.Event, trajectory: TrajectoryRecorder, finished: threading.Event) -> AgentOutcome:
        work = Path(tempfile.mkdtemp(prefix="basalt-tb-"))             # outside the mounted logs folder: the agent in the container cannot read its own audit files
        try:
            where = exec_fn("pwd", 30)
            cwd = where.stdout.strip() if where.rc == 0 and where.stdout.strip() else "the container's default folder"
            client = self.client_factory(self.endpoint) if self.client_factory else None
            return run_harness_agent(instruction, exec_fn, endpoint=self.endpoint, work_dir=work, trajectory=trajectory,
                                     environment_note=NOTE.format(cwd=cwd), client=client, cancel=cancel, overrides=self.overrides, cwd_label=cwd, max_seconds=self.max_seconds,
                                     guidance=self.guidance)
        except Exception as exc:                                    # the agent could not even be built: the trial is not the agent's to lose
            trajectory.stop("setup_failed", f"{type(exc).__name__}: {exc}")
            return AgentOutcome(stop_reason="setup_failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
            finished.set()

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        loop = asyncio.get_running_loop()
        gate, cancel, finished = ExecGate(), threading.Event(), threading.Event()
        trajectory = TrajectoryRecorder(Path(self.logs_dir) / "trajectory.jsonl")
        job = functools.partial(self._drive, instruction, harbor_exec(environment, loop, gate), cancel, trajectory, finished)
        try:
            self.outcome = await loop.run_in_executor(None, job)
        except asyncio.CancelledError:
            cancel.set()
            gate.close()
            trajectory.stop("timeout", "the time limit of the trial ended the agent")
            try:
                await loop.run_in_executor(None, finished.wait, GRACE_S)
            except BaseException:
                pass
            raise
        finally:
            gate.close()
            trajectory.close()
        self._fill(context)
        if self.outcome.stop_reason == "setup_failed" or is_server_fault(self.outcome):    # the agent had no fair try: Harbor must not take the reward for the agent's
            raise RuntimeError(f"AgentUnavailableError: {self.outcome.error}")

    def _fill(self, context: AgentContext) -> None:
        o = self.outcome or AgentOutcome()
        context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens = o.prompt_tokens, o.completion_tokens, o.cached_tokens
        context.metadata = {"stop_reason": o.stop_reason, "error": o.error, "turns": o.turns, "tool_calls": o.tool_calls, "seconds": round(o.seconds, 1)}
        try:
            (Path(self.logs_dir) / "harness-summary.json").write_text(json.dumps(o.to_dict(), indent=2), encoding="utf-8")
        except OSError:
            pass
