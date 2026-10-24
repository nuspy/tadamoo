"""What the harness uses to talk to language models through llmswitch: one client or judge for each function.

    from harness.llm import client_for, judge_for
    client = client_for("executor", BasaltClient(...))      # the provider the person chose for it, or the loaded model
    judge = judge_for("router", BasaltClient(...))

The registry is read again at every call, so a choice made on the Providers page applies to the next request.
"""
from __future__ import annotations

from typing import Any, Optional

from harness.llm.functions import FUNCTIONS, LOCAL, names, spec
from harness.llm.store import Ledger, SecureRegistry, data_dir, is_local

_registry: Optional[SecureRegistry] = None


def registry(path: Optional[str] = None) -> SecureRegistry:
    """Basalt's registry of providers (one per process unless a path is given: tests and the server)."""
    global _registry
    if path is not None:
        return SecureRegistry(path)
    if _registry is None:
        _registry = SecureRegistry()
    return _registry


def client_for(function: str, local: Any, reg: Optional[SecureRegistry] = None, level: str = "inherit", load_if_needed: bool = True):
    from harness.llm.client import RoutedClient
    from harness.llm.qwen_loader import QwenLoader
    base = getattr(local, "base_url", "")
    loader = QwenLoader(base, getattr(local, "api_key", None)) if base and load_if_needed else None
    return RoutedClient(function, reg or registry(), local, Ledger(), level=level, loader=loader)


def judge_for(function: str, local: Any, reg: Optional[SecureRegistry] = None):
    from harness.llm.judge import Judge
    return Judge(function, reg or registry(), local, Ledger())


__all__ = ["FUNCTIONS", "LOCAL", "names", "spec", "registry", "client_for", "judge_for", "SecureRegistry", "Ledger", "data_dir", "is_local"]
