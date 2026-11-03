"""Starts tau2-bench's own command line (`tau2 run ...`) with every model call sent to the local engine.

It runs inside tau2-bench's environment (`uv run --project <tau2-bench> python launcher.py run ...`). All text model calls of tau2-bench (the agent, the user
simulator, the communication judge, the natural-language-assertion judge) go through one function, `tau2.utils.llm_utils.completion` (litellm's). This launcher
wraps it so that, whatever model name a setting or a default names, the request goes to the local engine's address under the served model's name and with a
placeholder key; nothing can reach a cloud provider. The judge-model constant of the assertion judge is set to the same name so the saved run says what was used.

With repair on (the default), a tool call the model wrote slightly wrong gets the repairs of our harness (tadamoo/tools/repair.py): arguments that are almost
JSON are read, and a tool name one slip away from a real one is corrected. Each repair is written as a line to the file in TADAMOO_TAU_REPAIRS_LOG; TADAMOO_REPO is the repository the repair file is read from."""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlparse

DROPPED = ("reasoning_effort",)           # settings meant for a cloud model; the local engine is called plainly


def check_local(base_url: str, allowed_host: str) -> None:
    host = urlparse(base_url).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1") and host != allowed_host:
        raise SystemExit(f"TADAMOO_TAU_BASE_URL points to {host or base_url!r}, not to the local engine; benchmarks run only with the served model")


def load_repair(repo: str):
    """tadamoo/tools/repair.py (standard library only) loaded from its file, so none of the harness package has to be installed here; None when it is not found."""
    import importlib.util
    path = os.path.join(repo, "harness", "tools", "repair.py") if repo else ""
    if not path or not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("basalt_tool_repair", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def repair_tool_calls(response, tools, repair, log_path: str = "") -> int:
    """Repairs the tool calls of a litellm response in place. Returns how many calls were changed."""
    names = [str(((t or {}).get("function") or {}).get("name") or "") for t in (tools or [])]
    changed = 0
    try:
        calls = response.choices[0].message.tool_calls or []
    except (AttributeError, IndexError, TypeError):
        return 0
    for call in calls:
        what = []
        raw = call.function.arguments
        try:
            json.loads(raw)
        except (TypeError, ValueError):
            fixed = repair.repair_json(raw if isinstance(raw, str) else "")
            if isinstance(fixed, dict):
                call.function.arguments = json.dumps(fixed)
                what.append("arguments")
        name = call.function.name
        if names and name not in names:
            resolved, _near = repair.resolve_tool_name(name, names)
            if resolved:
                call.function.name = resolved
                what.append("name")
        if what:
            changed += 1
            if log_path:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"repaired": what, "name": call.function.name}) + "\n")
    return changed


def local_completion(real, base_url: str, served: str, repair=None, log_path: str = ""):
    def completion(*args, **kwargs):
        kwargs["model"] = "openai/" + served
        kwargs["api_base"] = base_url
        kwargs["api_key"] = "local"
        for key in DROPPED:
            kwargs.pop(key, None)
        response = real(*args, **kwargs)
        if repair is not None:
            repair_tool_calls(response, kwargs.get("tools"), repair, log_path)
        return response
    return completion


def main() -> None:
    base_url, served = os.environ["TADAMOO_TAU_BASE_URL"], os.environ["TADAMOO_TAU_MODEL"]
    check_local(base_url, os.environ.get("TADAMOO_TAU_HOST", ""))
    repair = load_repair(os.environ.get("TADAMOO_REPO", "")) if os.environ.get("TADAMOO_TAU_REPAIR", "1") != "0" else None
    from tau2.utils import llm_utils
    llm_utils.completion = local_completion(llm_utils.completion, base_url, served, repair, os.environ.get("TADAMOO_TAU_REPAIRS_LOG", ""))
    from tau2.evaluator import evaluator_nl_assertions
    evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS = "openai/" + served
    evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS_ARGS = {}
    from tau2.cli import main as tau_main
    tau_main()


if __name__ == "__main__":
    sys.exit(main())
