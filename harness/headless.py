"""Headless runner: `python -m harness -p "task"`.

Exit codes: 0 finished, 1 error (server, setup or crash), 2 bad usage, 3 turn limit, 4 token or time
budget, 5 stuck in a loop, 6 ended on a question for the person, 7 put aside before it was done,
8 the project's checks still failed (or could not be run), 130 cancelled.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from harness import agent_loop as al
from harness.plan_mode import gate_from_confirm
from harness.stack import AgentConfig, add_agent_arguments, agent_settings, build_agent, split_secret_specs     # noqa: F401 (the last is used by `harness session`)
from harness.tools.registry import clock_paused

EXIT_CODES = {al.FINAL: 0, al.ERROR: 1, al.MAX_TURNS: 3, al.BUDGET: 4, al.LOOP: 5, al.ASKED: 6, al.SUSPENDED: 7, al.VERIFY_FAILED: 8, al.CANCELLED: 130}


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m harness", description="Basalt autonomous agent, headless")
    parser.add_argument("-p", "--prompt", required=True, help="goal for the agent")
    add_agent_arguments(parser, permission_mode="")
    parser.add_argument("--max-tokens-budget", type=int, default=None, help="stop after this many prompt+completion tokens")
    parser.add_argument("--timeout-seconds", type=float, default=None, help="stop after this much time")
    parser.add_argument("--interactive", action="store_true", help="ask on the terminal before risky actions (and start in the default permission mode, not auto)")
    parser.add_argument("--progress", action="store_true", help="print one line for each tool the agent uses and how it went")
    parser.add_argument("--result-file", default=None, metavar="PATH", help="write the result as JSON here, without the edited texts")
    parser.add_argument("--pid-file", default=None, metavar="PATH", help="write this process's id here, so the server knows which process is the agent")
    parser.add_argument("--ask-server", action="store_true", help="ask for approvals on the server's pages instead of the terminal")
    parser.add_argument("--run-id", default="", help="the name of this run in the server's Agent tab")
    parser.add_argument("--no-report", action="store_true", help="do not show the run and its edits in the server's Agent tab")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parsed = parser.parse_args(args)
    if not parsed.permission_mode:                        # nobody is there to confirm unless the person asked to be asked
        parsed.permission_mode = "default" if parsed.interactive else "auto"
    return parsed


SHOWN_ARGS = ("command", "path", "query", "url", "number", "name")


def _progress_line(event: Dict[str, Any]) -> Optional[str]:
    """A line for the person to read about a tool call starting or ending, or None for events that need none."""
    kind = event.get("type")
    if kind == "tool_start":
        args = event.get("arguments") or {}
        key = next((k for k in SHOWN_ARGS if k in args), None)
        detail = " ".join(str(args[key]).split())[:100] if key else ""
        return f"> {event.get('name')} {detail}".rstrip()
    if kind == "tool_end":
        if event.get("status") == "completed":
            return "  ok"
        first = next((l.strip() for l in str(event.get("output", "")).splitlines() if l.strip()), "")
        return f"  failed ({event.get('decision')}): {first[:140]}"
    return None


def _on_event(cfg: AgentConfig, reporter: Any):
    def handle(event: Dict[str, Any]) -> None:
        if cfg.progress:
            line = _progress_line(event)
            if line:
                print(line, flush=True)
        if reporter is not None:
            reporter.on_event(event)
    return handle


def _confirmer(args: argparse.Namespace):
    if args.ask_server:
        from harness import outward_calls
        from harness.approval_client import ServerApprover
        outward_calls.report_to(args.endpoint, args.api_key)
        return ServerApprover(args.endpoint, args.api_key, args.run_id or "cli")
    if not args.interactive:
        return None
    if _has_terminal():
        return _ask_on_terminal
    from harness.approval_client import ServerApprover
    board = ServerApprover(args.endpoint, args.api_key, args.run_id or "cli")
    return board if board.available() else None      # no terminal and no board: nobody can confirm, and the permission layer says so to the model


def _has_terminal() -> bool:
    return bool(sys.stdin) and sys.stdin.isatty()


def _ask_on_terminal(tool: str, arguments: Dict[str, Any]) -> bool:
    if tool == "present_plan":
        print(f"\nThe agent presents this plan:\n\n{arguments.get('plan', '')}\n", file=sys.stderr)
        print(f"Approve it and go on in '{arguments.get('mode')}' mode? [y/N] ", end="", file=sys.stderr, flush=True)
    else:
        shown = json.dumps(arguments, ensure_ascii=False)[:300]
        print(f"\nAllow {tool} {shown}? [y/N] ", end="", file=sys.stderr, flush=True)
    with clock_paused():                                  # the person's time is not the tool's
        return sys.stdin.readline().strip().lower() in ("y", "yes")


def config_from_args(args: argparse.Namespace) -> AgentConfig:
    return AgentConfig(
        **agent_settings(args),
        max_total_tokens=args.max_tokens_budget, max_seconds=args.timeout_seconds,
        report=not args.no_report, unattended=not args.interactive,
        confirm=_confirmer(args), run_id=args.run_id, progress=args.progress,
    )


def run_headless(cfg: AgentConfig, prompt: str, client: Any = None, on_event: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    built = build_agent(cfg, client=client)
    try:
        if cfg.confirm is not None:                       # a payment and a plan are put to the person the way any risky action is
            if built.payment_confirm is not None:
                built.payment_confirm.ask = lambda request: bool(cfg.confirm("request_payment", request))
            if built.plan_gate is not None:
                built.plan_gate.ask = gate_from_confirm(cfg.confirm)
        if cfg.report:
            from harness.reporting import PanelReporter
            built.reporter = PanelReporter(cfg.endpoint, cfg.api_key, built.sandbox, prompt, session=cfg.run_id or None)
            built.reporter.start()
        shown = _on_event(cfg, built.reporter)
        built.agent.on_event = shown if on_event is None else (lambda event: (shown(event), on_event(event)))     # `on_event` also sees every event (a benchmark's trajectory)
        try:
            turns = built.agent.run(prompt, retrieved=built.retrieved_message(prompt))
        except BaseException as exc:
            if built.reporter:
                built.reporter.finish("error", error=f"{type(exc).__name__}: {exc}")
            raise
        agent = built.agent
        final = al.answer_text(turns)
        if built.reporter:
            built.reporter.finish(agent.stop_reason, final, agent.error)
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
            "permission_denials": {"total": built.permissions.denied, "by_tool": dict(built.permissions.denied_by_tool)},
            "audit_log": str(built.audit.path),
        }
    finally:
        built.close()


def _write_result(path: str, result: Dict[str, Any]) -> None:
    slim = {**result, "changes": [{k: v for k, v in c.items() if k not in ("old_text", "new_text", "diff")} for c in result.get("changes", [])]}
    Path(path).write_text(json.dumps(slim, indent=2, ensure_ascii=False), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.pid_file:
        Path(args.pid_file).write_text(str(os.getpid()), encoding="ascii")
    try:
        result = run_headless(config_from_args(args), args.prompt)
        if args.result_file:
            _write_result(args.result_file, result)
    except Exception as exc:
        if args.result_file:
            _write_result(args.result_file, {"status": "error", "stop_reason": al.ERROR, "error": str(exc)})
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
