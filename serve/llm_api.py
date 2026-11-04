"""The Providers page's side of llmswitch: which provider each internal function uses, and the providers themselves.

Everything here is about TaDaMoo's own functions (the Chat, the agent's loop, the planner, the judges). What other programs see
of this server does not change: it is the loaded model. Keys are write-only: the page can send one and is told only that there
is one. All changes come from this PC and the server's own page (the handler checks).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from tadamoo import council, side_uses
from tadamoo.llm import functions
from tadamoo.llm.store import Ledger, SecureRegistry, is_local, runs_here
from tadamoo.llmswitch import reasoning
from tadamoo.llmswitch.providers import TYPES

TECHNICAL_KINDS = ("lmstudio", "vllm", "llamacpp")          # the servers a second small model for the technical session can come from
TECHNICAL_ID, TECHNICAL_NAME = "technical-%s", "Technical session: %s"
TECHNICAL_WINDOW = 8192                                    # the context assumed for such a model until the person states it
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
                "context_window": int(prov.get("context_window") or 0), "max_output": int(prov.get("max_output") or 0),
                "this_server": bool(TYPES.get(prov.get("type") or "", {}).get("this_server")), "used_today": self.ledger.used(prov["id"]),
                "is_local": is_local(prov, self.registry.local_hosts), "runs_here": runs_here(prov, self.registry.local_hosts),
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
        return {"functions": rows, "council": self.council_view(),
                "providers": [self._provider_view(by_id[p["id"]], p) for p in pub["providers"]],
                "types": [t for t in pub["types"] if t["type"] in SHOWN_TYPES],
                "reasoning_levels": reasoning.choices("en"), "imported": self.registry.imported(),
                "shared": [{"path": str(p), "providers": n} for p, n in self.shared_candidates()]}

    def council_view(self) -> Dict[str, Any]:
        """The council's roles and rounds as saved, the defaults, the limits, and what a debate costs at most and at least."""
        settings = council.load_settings(self.registry)
        away = council.outside(self.registry)
        return {"settings": settings, "defaults": council.clean_settings(None), "estimate": council.estimate(settings=settings, away=away), "outside": away,
                "limits": {"rounds": council.ROUNDS_MAX, "name": council.NAME_MAX, "job": council.JOB_MAX}, "configured": council.configured(self.registry)}

    def set_council(self, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            settings = council.clean_settings({k: body[k] for k in ("rounds", "roles", "moderator") if k in body})
        except ValueError as exc:
            raise LlmError(str(exc), "bad_council") from None
        self.registry._set_extra("council", settings)
        return self.overview()

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
        if body.get("context_window") or body.get("max_output"):
            self.registry.set_limits(prov["id"], self._tokens(body.get("context_window"), "context_window"), self._tokens(body.get("max_output"), "max_output"))
        return self._public(prov["id"])

    @staticmethod
    def _tokens(value: Any, field: str) -> int:
        """A context window or a most-output figure: a whole number of tokens, 0 or empty for "not stated"."""
        if value in (None, ""):
            return 0
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise LlmError("This is a number of tokens (0 or empty if you do not know).", "bad_" + field) from None
        if isinstance(value, bool) or not 0 <= n <= 100_000_000:
            raise LlmError("This is a number of tokens (0 or empty if you do not know).", "bad_" + field)
        return n

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
        if "context_window" in body or "max_output" in body:
            prov = self.registry._find(self.registry.load(), pid) or {}
            self.registry.set_limits(pid, self._tokens(body["context_window"], "context_window") if "context_window" in body else int(prov.get("context_window") or 0),
                                     self._tokens(body["max_output"], "max_output") if "max_output" in body else int(prov.get("max_output") or 0))
        return self._public(pid)

    def remove_provider(self, pid: str) -> Dict[str, Any]:
        if self.registry._find(self.registry.load(), pid) is None:
            raise LlmError("That provider is not there any more.", "gone")
        self.registry.remove(pid)
        return {"removed": pid}

    def setup_technical(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Point the technical session at a second small model served by LM Studio, vLLM or llama-server, in one step: the provider is made (or
        updated, it keeps its id) and the function `technical` is assigned to it. All of it is kept in the registry, so it survives a restart.
        On this PC nothing leaves it, so no consent is asked; an address outside this PC needs the person's yes and the master switch as any
        provider does. `type` is one of TECHNICAL_KINDS; a llama-server started from the Loader supplies the address, the model and the window."""
        kind = body.get("type")
        if kind not in TECHNICAL_KINDS:
            raise LlmError("Choose LM Studio, vLLM or llama-server.", "bad_type")
        base_url, model = str(body.get("base_url") or "").strip(), str(body.get("model") or "").strip()
        window = self._tokens(body.get("context_window"), "context_window")
        if kind == "llamacpp" and not (base_url and model):
            from serve import llamacpp
            running = llamacpp.get().status()
            if running["state"] in ("ready", "starting"):
                base_url, model = base_url or running["base_url"], model or running["model_name"]
                window = window or int(running.get("context_per_slot") or 0)
        if not model:
            raise LlmError("Name the model that should do the quick jobs.", "bad_model")
        window = min(window or TECHNICAL_WINDOW, side_uses.EXTERNAL_WINDOW)
        pid = TECHNICAL_ID % kind
        existing = self.registry._find(self.registry.load(), pid)
        try:
            if existing is None:
                self.registry.add({"id": pid, "type": kind, "name": TECHNICAL_NAME % TYPES[kind]["label"], "base_url": base_url, "model": model})
            else:
                self.registry.update(pid, {"base_url": base_url, "model": model})
            self.registry.set_limits(pid, window, int((existing or {}).get("max_output") or 0))
            self.registry.assign_checked("technical", pid)
        except ValueError as exc:
            raise LlmError(str(exc), "not_allowed") from None
        return {**self.overview(), "provider": self._public(pid)}

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
