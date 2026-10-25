"""The tools added to the standard registry: run_code, spawn_subagent, delegate_to_acp_agent, ask_a2a_agent and find_online_capability.

`register_extra_tools` is the one place `harness.stack` calls. `run_code` and `spawn_subagent` are always offered unless the
configuration lists the tools it wants (`extra_tools`). The other two exist only for agents the person wrote down in a file
in the user data folder, so the model can name an agent but never an address or a command:

* `acp_agents.json`: {"agents": {"name": {"command": ["program", "arg"], "env": ["VARIABLE_TO_PASS"], "description": "..."}}}
* `a2a_agents.json`: {"agents": {"name": {"url": "https://...", "token_env": "VARIABLE_WITH_A_BEARER_TOKEN", "description": "..."}}}

`find_online_capability` exists when the capability index does (harness/discovery); the conversation puts its question to the person
through `registry.capability_ask`.

An ACP agent starts with a clean environment plus the variables listed in `env`; its permission requests are put to the
person through the same permission manager as the harness's own tools.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.tools.registry import EffectClass

ALL = ("run_code", "spawn_subagent", "delegate_to_acp_agent", "ask_a2a_agent", "find_online_capability")
TASK_MAX = 8000


def _get(config: Any, name: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        return config.get(name, default)
    return getattr(config, name, default)


def agents_dir(config: Any = None) -> Path:
    given = _get(config, "external_agents_dir")
    if given:
        return Path(given)
    try:
        from serve.userdata import user_data_dir
        return user_data_dir()
    except Exception:                      # the harness also runs without the server package
        return Path.home() / ".basalt"


def read_agents(directory: Path, filename: str, required: str) -> Dict[str, Dict[str, Any]]:
    """The agents listed in a file; an entry without its `required` field is left out. A missing or damaged file is no agents."""
    try:
        data = json.loads((directory / filename).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    agents = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, dict):
        return {}
    return {n: a for n, a in agents.items() if isinstance(n, str) and isinstance(a, dict) and a.get(required)}


def _describe(agents: Dict[str, Dict[str, Any]]) -> str:
    return "; ".join(f"{n}: {a['description']}" if a.get("description") else n for n, a in agents.items())


def _task_error(task: Any) -> Optional[str]:
    if not isinstance(task, str) or not task.strip():
        return "Give the task for the agent."
    if len(task) > TASK_MAX:
        return f"The task is too long ({len(task)} characters; at most {TASK_MAX})."
    return None


def register_extra_tools(registry: Any, config: Any = None, *, workspace: Any = None, sandbox: Any = None, client: Any = None,
                         permissions: Any = None, hooks: Any = None, audit: Any = None, checkpoints: Any = None,
                         secrets: Any = None, output_filter: Any = None, cancel: Optional[threading.Event] = None, capabilities: Any = None) -> List[str]:
    """Adds the extra tools to `registry` and returns their names."""
    wanted = _get(config, "extra_tools")
    chosen = set(ALL) if wanted is None else set(wanted) & set(ALL)
    root = workspace if workspace is not None else _get(config, "workspace", ".")
    added: List[str] = []

    if "run_code" in chosen:
        from harness.tools.execute import register_run_code
        register_run_code(registry, root, sandbox, python=_get(config, "code_python"), cancel=cancel)
        added.append("run_code")

    if "spawn_subagent" in chosen and client is not None:
        from harness.swarm.subagent import SubagentRunner, register_subagent_tool
        runner = SubagentRunner(registry, client, permissions=permissions, hooks=hooks, audit=audit, checkpoints=checkpoints,
                                secrets=secrets, output_filter=output_filter, cancel=cancel)
        register_subagent_tool(registry, runner)
        added.append("spawn_subagent")

    directory = agents_dir(config)
    if "delegate_to_acp_agent" in chosen:
        acp = read_agents(directory, "acp_agents.json", "command")
        if acp and _register_acp(registry, acp, root, sandbox, permissions, cancel):
            added.append("delegate_to_acp_agent")
    if "ask_a2a_agent" in chosen:
        remote = read_agents(directory, "a2a_agents.json", "url")
        if remote and _register_a2a(registry, remote, cancel):
            added.append("ask_a2a_agent")
    if "find_online_capability" in chosen and capabilities is not None:       # with the capability index: the search online for a missing MCP server or skill
        from harness.discovery.tool import register_discovery_tools
        registry.capability_ask = register_discovery_tools(registry, capabilities, vault_dir=_get(config, "vault_dir") or None)
        added.append("find_online_capability")
    return added


def _register_acp(registry: Any, agents: Dict[str, Dict[str, Any]], workspace: Any, sandbox: Any, permissions: Any,
                  cancel: Optional[threading.Event]) -> bool:
    from harness.acp_client import AcpError, run_task

    def delegate(agent: str, task: str, timeout: float = 900.0) -> Any:
        if agent not in agents:
            return f"No ACP agent called '{agent}'. Available: {', '.join(agents)}."
        problem = _task_error(task)
        if problem:
            return problem
        entry = agents[agent]
        command = entry["command"]
        if not isinstance(command, list):
            return f"The entry for '{agent}' has no command."
        env = {v: os.environ[v] for v in entry.get("env") or [] if isinstance(v, str) and v in os.environ}

        def ask(title: str, details: Dict[str, Any]) -> bool:
            if permissions is None:
                return False
            return permissions.evaluate(f"acp_permission:{agent}:{title}", EffectClass.IRREVERSIBLE, details)[0]

        def go() -> Any:
            try:
                return run_task(command, Path(workspace).resolve(), task, env=env, ask=ask, cancel=cancel, name=agent,
                                timeout=min(max(float(timeout), 10.0), 3600.0))
            except AcpError as exc:
                return f"The ACP agent '{agent}' failed: {exc}"
        if sandbox is None:
            return go()
        with sandbox.watching():                       # what the agent changed in the folder is reviewed like the file tools' edits
            return go()

    registry.register(
        "delegate_to_acp_agent",
        "Hands a task to another coding agent (Agent Client Protocol) that works in this workspace and returns what it said. "
        "It may ask for permissions; they are put to the user. Agents: " + _describe(agents) + ".",
        {"type": "object",
         "properties": {"agent": {"type": "string", "enum": sorted(agents)}, "task": {"type": "string", "description": "What it should do; it does not see this conversation"},
                        "timeout": {"type": "number", "description": "Seconds to wait (default 900)", "default": 900.0}},
         "required": ["agent", "task"]},
        EffectClass.IRREVERSIBLE, delegate, untrusted=True)
    return True


def _register_a2a(registry: Any, agents: Dict[str, Dict[str, Any]], cancel: Optional[threading.Event]) -> bool:
    from harness.a2a import A2AError, ask_agent

    def ask(agent: str, message: str, timeout: float = 600.0) -> Any:
        if agent not in agents:
            return f"No A2A agent called '{agent}'. Available: {', '.join(agents)}."
        problem = _task_error(message)
        if problem:
            return problem
        entry = agents[agent]
        token = os.environ.get(str(entry.get("token_env") or ""), "")
        try:
            return ask_agent(str(entry["url"]), message, token=token, cancel=cancel, wait_s=min(max(float(timeout), 5.0), 3600.0))
        except A2AError as exc:
            return f"The A2A agent '{agent}' failed: {exc}"

    registry.register(
        "ask_a2a_agent",
        "Sends a message to a remote agent (Agent2Agent protocol) and returns its answer. The message leaves this PC. Agents: "
        + _describe(agents) + ".",
        {"type": "object",
         "properties": {"agent": {"type": "string", "enum": sorted(agents)}, "message": {"type": "string", "description": "What to ask or ask it to do; it does not see this conversation"},
                        "timeout": {"type": "number", "description": "Seconds to wait (default 600)", "default": 600.0}},
         "required": ["agent", "message"]},
        EffectClass.IRREVERSIBLE, ask, untrusted=True)
    return True
