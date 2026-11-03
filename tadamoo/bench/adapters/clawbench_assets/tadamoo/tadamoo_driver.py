#!/usr/bin/env python3
"""Runs the Basalt harness on one ClawBench task inside the benchmark's container.

ClawBench starts a browser in the container and hands the task over in the environment: INSTRUCTION, TIME_LIMIT_S, BASE_URL (the local model server),
CLAWBENCH_BROWSER_CDP_URL (the browser's debugging address). The harness drives that browser through `--browser-cdp-url` and works from the page, the personal
files in /my-info and the files it writes. The shell and every tool that reaches the web outside the browser are refused, because the benchmark intercepts
requests in the browser only.

Files left in /data for ClawBench: agent-messages.jsonl (the trajectory, one event per line in the format of tadamoo/bench/analysis.py), usage.jsonl (tokens)
and basalt-result.json (the harness's own result)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

HOME = Path(os.environ.get("TADAMOO_HOME", "/opt/basalt"))
DATA = Path(os.environ.get("TADAMOO_DATA", "/data"))
WORKSPACE = Path(os.environ.get("TADAMOO_WORKSPACE", "/root/workspace"))
DENIED = ("group:shell", "group:web", "group:agents", "group:mcp", "group:phone")
STOP_NAMES = {"time_limit_exceeded": "timeout", "agent_idle": "gave_up", "eval_matched": "final"}     # the run script's reasons, in the words of analysis.py
FINISH_NOTE = ("\n\nCOMPLETION NOTE:\nWhen you have clicked Send, Submit, Place order or the final button the task names, give a brief final answer and stop. "
               "Do not keep browsing, and do not open other sites or other mail providers.")


def endpoint_of(base_url: str) -> str:
    """The server address the harness wants: the model's base address without its trailing /v1."""
    text = base_url.strip().rstrip("/")
    return text[:-3] if text.endswith("/v1") else text


def credentials_note(my_info: Path) -> str:
    """The disposable webmail login written out in full, so the agent types it exactly (a file tool may shorten a secret-looking value)."""
    try:
        creds = json.loads((my_info / "email_credentials.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    email, password = creds.get("email", ""), creds.get("password", "")
    if not (email and password):
        return ""
    return ("\n\nWEBMAIL CREDENTIALS:\n"
            f"- login_url: {creds.get('login_url', 'https://purelymail.com/user/login')}\n- email: {email}\n- password: {password}\n"
            "Use these exactly when a site or the webmail asks for them.")


def build_argv(env: Mapping[str, str], prompt: str) -> List[str]:
    limit = int(float(env.get("TIME_LIMIT_S") or 1800))
    argv = ["-p", prompt, "--endpoint", endpoint_of(env.get("BASE_URL", "")), "--workspace", str(WORKSPACE), "--permission-mode", "auto",
            "--browser", "--browser-cdp-url", env.get("CLAWBENCH_BROWSER_CDP_URL") or "http://127.0.0.1:9222", "--browser-permissions", "grant-safe",
            "--rule-of-two", "off", "--no-report", "--no-memory", "--no-checkpoints", "--timeout-seconds", str(max(60, limit - 45))]
    for pattern in DENIED:
        argv += ["--deny", pattern]
    key = env.get("API_KEY") or ""
    if key:
        argv += ["--api-key", key]
    return argv


def write_usage(path: Path, tokens: Mapping[str, Any], model: str, turns: int) -> None:
    prompt, completion = int(tokens.get("prompt") or 0), int(tokens.get("completion") or 0)
    rows = []
    if prompt or completion:
        rows.append({"type": "usage", "call_id": "basalt-total", "model": model, "input_tokens": prompt, "output_tokens": completion,
                     "cache_read_tokens": int(tokens.get("cached") or 0), "turns": turns})
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    env = os.environ
    sys.path.insert(0, str(HOME))
    from tadamoo import agent_loop as al
    from tadamoo.bench.adapters.containerkit import TrajectoryRecorder, stop_name
    from tadamoo.headless import config_from_args, parse_args, run_headless

    DATA.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    my_info = Path("/my-info")
    if my_info.is_dir() and not (WORKSPACE / "my-info").exists():
        import shutil
        shutil.copytree(my_info, WORKSPACE / "my-info")
    prompt = (env.get("INSTRUCTION") or "") + credentials_note(WORKSPACE / "my-info") + FINISH_NOTE
    args = parse_args(build_argv(env, prompt) if argv is None else argv)
    recorder = TrajectoryRecorder(DATA / "agent-messages.jsonl")
    model = env.get("MODEL_NAME", "")
    started = time.monotonic()
    result: Dict[str, Any] = {}
    try:
        result = run_headless(config_from_args(args), prompt, on_event=recorder.on_event)
        stop_reason = result.get("stop_reason") or al.FINAL
        recorder.stop(stop_name(str(stop_reason), str(result.get("error") or "")), str(result.get("error") or ""))
        code = 0 if stop_reason == al.FINAL else 1
    except KeyboardInterrupt:                            # the run script ends the agent when the interceptor matched, the agent idled or time ran out
        try:
            why = (DATA / ".stop-reason").read_text(encoding="utf-8").strip()
        except OSError:
            why = "interrupted"
        recorder.stop(STOP_NAMES.get(why, why))
        result = {"status": "stopped", "stop_reason": why}
        code = 1
    except BaseException as exc:                         # the trajectory must end with a reason even when the harness fails
        recorder.stop("error", f"{type(exc).__name__}: {exc}")
        result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        code = 1
    finally:
        recorder.close()
    result["seconds"] = round(time.monotonic() - started, 1)
    (DATA / "basalt-result.json").write_text(json.dumps({k: v for k, v in result.items() if k != "changes"}, indent=2, default=str), encoding="utf-8")
    write_usage(DATA / "usage.jsonl", result.get("tokens") or {}, model, int(result.get("turns_count") or 0))
    return code


if __name__ == "__main__":
    sys.exit(main())
