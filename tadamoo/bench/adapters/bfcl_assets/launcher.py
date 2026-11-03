"""Starts BFCL's own command line (`bfcl generate`, `bfcl evaluate`) with the served local model added to its model registry.

It runs inside BFCL's environment (`uv run --project <berkeley-function-call-leaderboard> python launcher.py generate ...`), so BFCL's code is used as it is: only
one registry entry is added, at start, in memory. The entry uses BFCL's OpenAI chat-completions handler in function-calling mode; that handler talks to the address
in OPENAI_BASE_URL, which the adapter sets to the local engine. The registry name has no underscore, because BFCL turns every underscore of a result folder's
name back into a slash when it evaluates."""
from __future__ import annotations

import os
import sys
from urllib.parse import urlparse


def register(mapping, config_class, handler_class, registry_name: str, served_name: str) -> None:
    mapping[registry_name] = config_class(
        model_name=served_name, display_name=f"{served_name} (served by Basalt, FC)", url="", org="local", license="n/a", model_handler=handler_class,
        input_price=None, output_price=None, is_fc_model=True, underscore_to_dot=True)


def check_local(base_url: str, allowed_host: str) -> None:
    host = urlparse(base_url).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1") and host != allowed_host:
        raise SystemExit(f"OPENAI_BASE_URL points to {host or base_url!r}, not to the local engine; benchmarks run only with the served model")


def main() -> None:
    registry_name, served = os.environ["TADAMOO_BFCL_REGISTRY"], os.environ["TADAMOO_BFCL_MODEL"]
    check_local(os.environ.get("OPENAI_BASE_URL", ""), os.environ.get("TADAMOO_BFCL_HOST", ""))
    from bfcl_eval.constants import model_config
    from bfcl_eval.model_handler.api_inference.openai_completion import OpenAICompletionsHandler
    register(model_config.MODEL_CONFIG_MAPPING, model_config.ModelConfig, OpenAICompletionsHandler, registry_name, served)
    from bfcl_eval.__main__ import cli
    cli()


if __name__ == "__main__":
    sys.exit(main())
