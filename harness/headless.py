"""Headless runner: `python -m harness -p "task"`.

Exit codes: 0 finished, 1 error (server, setup or crash), 2 bad usage, 3 turn limit, 4 token or time
budget, 5 stuck in a loop, 130 cancelled.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness import agent_loop as al
from harness.stack import AgentConfig, build_agent

EXIT_CODES = {al.FINAL: 0, al.ERROR: 1, al.MAX_TURNS: 3, al.BUDGET: 4, al.LOOP: 5, al.CANCELLED: 130}


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m harness", description="Basalt autonomous agent, headless")
    parser.add_argument("-p", "--prompt", required=True, help="goal for the agent")
    parser.add_argument("--workspace", default=".", help="folder the agent works in")
    parser.add_argument("--endpoint", default="http://127.0.0.1:7232", help="Basalt server address")
    parser.add_argument("--api-key", default=os.environ.get("BASALT_API_KEY"), help="server API key (or BASALT_API_KEY)")
    parser.add_argument("--max-turns", type=int, default=10, help="most model turns")
    parser.add_argument("--max-tokens-budget", type=int, default=None, help="stop after this many prompt+completion tokens")
    parser.add_argument("--timeout-seconds", type=float, default=None, help="stop after this much time")
    parser.add_argument("--permission-mode", choices=["plan", "read_only", "default", "auto"], default="default")
    parser.add_argument("--allow", action="append", default=[], metavar="PATTERN", help='allow rule, e.g. "shell_command:pytest*"')
    parser.add_argument("--deny", action="append", default=[], metavar="PATTERN", help="deny rule")
    parser.add_argument("--secret-env", action="append", default=[], metavar="NAME",
                        help="environment variable the agent may use as {{secret:NAME}}; NAME=host1,host2 limits "
                             "where it can be sent (without hosts it is for local commands only)")
    parser.add_argument("--skills", action="append", default=[], metavar="DIR", help="folder with SKILL.md skills")
    parser.add_argument("--interactive", action="store_true", help="ask on the terminal before risky actions")
    parser.add_argument("--phone", action="store_true",
                        help="offer the phone tools (callAPIcall; CALLAPICALL_URL and CALLAPICALL_TOKEN); every call asks you")
    parser.add_argument("--on-behalf-of", default="", help="name the phone assistant gives when it calls")
    parser.add_argument("--reference", default=None, metavar="DIR", help="folder of .md and .txt reference documents")
    parser.add_argument("--reference-strategy", choices=["auto", "cag", "rag", "none"], default="auto",
                        help="cag: a fixed block at the start of the context; rag: searched passages; auto: by size")
    parser.add_argument("--wiki", action="store_true", help="offer the wikibridge tools (BASALT_WIKIBRIDGE_URL, BASALT_WIKIBRIDGE_KEY)")
    parser.add_argument("--wiki-project", default="", help="wikibridge project slug used when a call names none")
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument("--no-checkpoints", action="store_true")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser.parse_args(args)


def _ask_on_terminal(tool: str, arguments: Dict[str, Any]) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        return False
    shown = json.dumps(arguments, ensure_ascii=False)[:300]
    print(f"\nAllow {tool} {shown}? [y/N] ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def split_secret_specs(specs: List[str]) -> tuple[List[str], Dict[str, List[str]]]:
    names, hosts = [], {}
    for spec in specs:
        name, _, rest = spec.partition("=")
        names.append(name)
        if rest:
            hosts[name] = [h.strip() for h in rest.split(",") if h.strip()]
    return names, hosts


def read_reference(folder: Optional[str]) -> Dict[str, str]:
    """The .md and .txt files of a folder, by relative path; a missing folder is an error, not an empty set."""
    if not folder:
        return {}
    root = Path(folder)
    if not root.is_dir():
        raise FileNotFoundError(f"reference folder not found: {root}")
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in (".md", ".txt")}


def config_from_args(args: argparse.Namespace) -> AgentConfig:
    secret_names, secret_hosts = split_secret_specs(args.secret_env)
    return AgentConfig(
        workspace=args.workspace, endpoint=args.endpoint, api_key=args.api_key, max_turns=args.max_turns,
        permission_mode=args.permission_mode, allow=args.allow, deny=args.deny, secret_names=secret_names,
        secret_hosts=secret_hosts,
        max_total_tokens=args.max_tokens_budget, max_seconds=args.timeout_seconds, use_memory=not args.no_memory,
        use_checkpoints=not args.no_checkpoints, skill_paths=args.skills,
        phone=args.phone, phone_on_behalf_of=args.on_behalf_of, reference_docs=read_reference(args.reference),
        reference_strategy=args.reference_strategy, wiki=args.wiki, wiki_project=args.wiki_project,
        confirm=_ask_on_terminal if args.interactive else None,
    )


def run_headless(cfg: AgentConfig, prompt: str, client: Any = None) -> Dict[str, Any]:
    built = build_agent(cfg, client=client)
    try:
        turns = built.agent.run(prompt, retrieved=built.retrieved_message(prompt))
        agent = built.agent
        final = next((t.assistant_content for t in reversed(turns) if not t.tool_calls), "")
        return {
            "status": "success" if agent.stop_reason == al.FINAL else agent.stop_reason,
            "stop_reason": agent.stop_reason,
            "error": agent.error,
            "turns_count": len(turns),
            "final_output": final,
            "changes": built.sandbox.changes,
            "tokens": {
                "prompt": sum(t.prompt_tokens for t in turns),
                "completion": sum(t.completion_tokens for t in turns),
                "cached": sum(t.cached_tokens for t in turns),
            },
            "audit_log": str(built.audit.path),
        }
    finally:
        built.close()


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        result = run_headless(config_from_args(args), args.prompt)
    except Exception as exc:
        if args.json:
            print(json.dumps({"status": "error", "stop_reason": al.ERROR, "error": str(exc)}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result["final_output"] or result["error"] or f"[{result['stop_reason']}]")
    return EXIT_CODES.get(result["stop_reason"], 1)


if __name__ == "__main__":
    sys.exit(main())
