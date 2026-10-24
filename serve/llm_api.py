"""The Providers page's side of llmswitch: which provider each internal function uses, and the providers themselves.

Everything here is about Basalt's own functions (the Chat, the agent's loop, the planner, the judges). What other programs see
of this server does not change: it is the loaded model. Keys are write-only: the page can send one and is told only that there
is one. All changes come from this PC and the server's own page (the handler checks).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.llm import functions
from harness.llm.store import Ledger, SecureRegistry, is_local
from harness.llmswitch import reasoning
from harness.llmswitch.providers import TYPES

SHOWN_TYPES = ("basalt", "lmstudio", "vllm", "llamacpp", "openai", "claude", "gemini", "openai_compat", "zai_coding", "zai", "deepseek",
               "kimi", "qwen", "nebius", "nvidia", "grok", "bonsai", "jev", "claude_cli", "codex_cli")


class LlmError(ValueError):
    """What the person asked cannot be done: a message fit for them, and a code the page can translate."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class LlmApi:
    def __init__(self, registry: SecureRegistry, ledger: Optional[Ledger] = None) -> None:
        self.registry = registry
        self.ledger = ledger or Ledger()

    # -- what the page shows -----------------------------------------------------------------------------

    def _provider_view(self, prov: Dict[str, Any], public: Dict[str, Any]) -> Dict[str, Any]:
        cap = int(prov.get("daily_token_cap") or 0)
        return {**public, "daily_token_cap": cap, "profile": prov.get("profile") or "",
                "this_server": bool(TYPES.get(prov.get("type") or "", {}).get("this_server")), "used_today": self.ledger.used(prov["id"]),
                "is_local": is_local(prov, self.registry.local_hosts),
                "agent_only": TYPES.get(prov.get("type") or "", {}).get("api") == "cli",
                "judge_only": TYPES.get(prov.get("type") or "", {}).get("api") == "judge"}

    def overview(self) -> Dict[str, Any]:
        data = self.registry.load()
        pub = self.registry.public()
        by_id = {p["id"]: p for p in data["providers"]}
        consent = self.registry.consent()
        rows: List[Dict[str, Any]] = []
        for fid, spec in functions.FUNCTIONS.items():
            pid = data["assignments"].get(fid) or ""
            ok, why = self.registry.check(fid, pid or None)
            rows.append({"id": fid, "label": spec["label"], "kind": spec["kind"], "data": spec["data"], "tools": bool(spec.get("tools")),
                         "provider": pid, "ok": ok, "why": why, "consent": bool(consent.get(fid)), "needs_consent": spec["data"] == "personal"})
        return {"functions": rows,
                "providers": [self._provider_view(by_id[p["id"]], p) for p in pub["providers"]],
                "types": [t for t in pub["types"] if t["type"] in SHOWN_TYPES],
                "reasoning_levels": reasoning.choices("en"), "imported": self.registry.imported(),
                "shared": [{"path": str(p), "providers": n} for p, n in self.shared_candidates()]}

    def shared_candidates(self) -> List[tuple]:
        """The registries of other programs on this PC that could be copied once (named, not read from any other path)."""
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".config")
        out = []
        try:
            for p in sorted(base.glob("*/llm_providers.json")):
                if p.resolve() == self.registry.path.resolve():
                    continue
                try:
                    import json
                    n = len(json.loads(p.read_text(encoding="utf-8")).get("providers", []))
                except (OSError, ValueError):
                    continue
                out.append((p, n))
        except OSError:
            pass
        return out

    # -- changes -----------------------------------------------------------------------------------------

    def _public(self, pid: str) -> Dict[str, Any]:
        prov = self.registry._find(self.registry.load(), pid)
        pub = next(p for p in self.registry.public()["providers"] if p["id"] == pid)
        return self._provider_view(prov, pub)

    def add_provider(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if body.get("type") not in TYPES or body["type"] not in SHOWN_TYPES:
            raise LlmError("Choose one of the kinds of provider in the list.", "bad_type")
        try:
            prov = self.registry.add({k: body.get(k) for k in ("type", "name", "base_url", "model", "embed_model", "api_key", "reasoning_level")
                                      if body.get(k) is not None})
        except ValueError as exc:
            raise LlmError(str(exc), "bad_provider") from None
        if body.get("daily_token_cap"):
            self.registry.set_cap(prov["id"], self._cap(body["daily_token_cap"]))
        if body.get("profile") and TYPES[body["type"]].get("this_server"):
            self.registry.set_profile(prov["id"], str(body["profile"]))
        return self._public(prov["id"])

    @staticmethod
    def _cap(value: Any) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise LlmError("The daily cap is a number of tokens (0 for no cap).", "bad_cap") from None
        if not 0 <= n <= 10_000_000_000:
            raise LlmError("The daily cap is a number of tokens (0 for no cap).", "bad_cap")
        return n

    def update_provider(self, pid: str, body: Dict[str, Any]) -> Dict[str, Any]:
        changes = {k: body[k] for k in ("name", "base_url", "model", "embed_model", "reasoning_level") if k in body}
        if isinstance(body.get("api_key"), str) and body["api_key"].strip():
            changes["api_key"] = body["api_key"]                     # an empty key field means "leave the key as it is"
        try:
            self.registry.update(pid, changes)
        except KeyError:
            raise LlmError("That provider is not there any more.", "gone") from None
        if "daily_token_cap" in body:
            self.registry.set_cap(pid, self._cap(body["daily_token_cap"]))
        if "profile" in body:
            self.registry.set_profile(pid, str(body["profile"] or ""))
        return self._public(pid)

    def remove_provider(self, pid: str) -> Dict[str, Any]:
        if self.registry._find(self.registry.load(), pid) is None:
            raise LlmError("That provider is not there any more.", "gone")
        self.registry.remove(pid)
        return {"removed": pid}

    def assign(self, function: Any, provider: Any) -> Dict[str, Any]:
        if function not in functions.FUNCTIONS:
            raise LlmError("That function does not exist.", "bad_function")
        try:
            self.registry.assign_checked(function, provider or None)
        except ValueError as exc:
            raise LlmError(str(exc), "not_allowed") from None
        except KeyError:
            raise LlmError("That provider is not there any more.", "gone") from None
        return self.overview()

    def consent(self, function: Any, allowed: Any) -> Dict[str, Any]:
        if function not in functions.FUNCTIONS or not isinstance(allowed, bool):
            raise LlmError("Say which function, and yes or no.", "bad_request")
        if not allowed:                                               # taking the yes back also takes the function off an outside provider
            pid = self.registry.load()["assignments"].get(function)
            self.registry.set_consent(function, False)
            ok, _ = self.registry.check(function, pid or None)
            if pid and not ok:
                self.registry.assign(function, None)
        else:
            self.registry.set_consent(function, True)
        return self.overview()

    def test(self, pid: str) -> Dict[str, Any]:
        prov = self.registry._find(self.registry.load(), pid)
        if prov is None:
            raise LlmError("That provider is not there any more.", "gone")
        spec = TYPES.get(prov.get("type") or "", {})
        if spec.get("this_server"):
            return {"ok": True, "note": "this server"}
        if spec.get("api") == "judge":
            return {"ok": bool((prov.get("api_key") or "").strip()), "note": "the key is there" if prov.get("api_key") else "no key yet"}
        return self.registry.test(pid)

    def import_shared(self, path: Any) -> Dict[str, Any]:
        known = {str(p.resolve()): p for p, _ in self.shared_candidates()}
        chosen = known.get(str(Path(str(path)).resolve())) if path else None
        if chosen is None:
            raise LlmError("Choose one of the registries in the list.", "bad_path")
        try:
            result = self.registry.import_shared(chosen)
        except ValueError as exc:
            raise LlmError(str(exc), "bad_registry") from None
        return {**result, **self.overview()}
