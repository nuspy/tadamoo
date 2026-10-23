"""Headless CLI runner for Basalt Harness (basalt-agent -p)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness.client import BasaltClient
from harness.tools import create_standard_tool_registry
from harness.agent_loop import AgentLoop


def parse_args(args=None):
    parser = argparse.ArgumentParser(description="Basalt Autonomous Agent Headless CLI")
    parser.add_argument("-p", "--prompt", required=True, help="User goal/prompt to execute")
    parser.add_argument("--workspace", default=".", help="Workspace working directory")
    parser.add_argument("--endpoint", default="http://127.0.0.1:7232", help="Basalt HTTP endpoint")
    parser.add_argument("--max-turns", type=int, default=10, help="Maximum execution turns")
    parser.add_argument("--json", action="store_true", help="Output execution results as JSON")
    return parser.parse_args(args)


def run_headless(prompt: str, workspace: str = ".", endpoint: str = "http://127.0.0.1:7232", max_turns: int = 10) -> dict:
    client = BasaltClient(base_url=endpoint)
    registry, sandbox = create_standard_tool_registry(workspace)
    agent = AgentLoop(client=client, tool_registry=registry, max_turns=max_turns)
    turns = agent.run(prompt)

    final_content = turns[-1].assistant_content if turns else ""
    return {
        "status": "success",
        "turns_count": len(turns),
        "final_output": final_content,
        "changes": sandbox.changes,
    }


def main():
    args = parse_args()
    try:
        result = run_headless(
            prompt=args.prompt,
            workspace=args.workspace,
            endpoint=args.endpoint,
            max_turns=args.max_turns,
        )
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(result["final_output"])
        sys.exit(0)
    except Exception as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
