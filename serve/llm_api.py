"""The Providers page's side of llmswitch: which provider each internal function uses, and the providers themselves.

Everything here is about the functions: TaDaMoo's own (the Chat, the agent's loop, the planner, the judges) and `external`, the other programs that call
this server through the OpenAI or Anthropic interface (serve/external_forward.py). Keys are write-only: the page can send one and is told only that there
is one. All changes come from this PC and the server's own page (the handler checks).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo import council, side_uses
from tadamoo.llm import functions
from tadamoo.llm import probe
from tadamoo.llm.store import LOCAL_EXPLICIT, PRIMARY, Ledger, SecureRegistry, is_local, runs_here
from tadamoo.llmswitch import reasoning
from tadamoo.llmswitch.providers import TYPES

TECHNICAL_KINDS = ("lmstudio", "vllm", "llamacpp")          # the servers a second small model for the technical session can come from
TECHNICAL_ID, TECHNICAL_NAME = "technical-%s", "Technical session: %s"
TECHNICAL_WINDOW = 8192                                    # the context assumed for such a model until the person states it
SHOWN_TYPES = ("basalt", "lmstudio", "vllm", "llamacpp", "openai", "claude", "gemini", "openai_compat", "anthropic_compat", "zai_coding", "zai",
               "deepseek", "kimi", "qwen", "nebius", "deepinfra", "chutes", "siliconflow", "orcarouter", "groq", "together", "fireworks",
               "cerebras", "openrouter", "novita", "hyperbolic", "nvidia", "grok", "bonsai", "jev", "claude_cli", "codex_cli")


class LlmError(ValueError):
    """What the person asked cannot be done: a message fit for them, and a code the page can translate."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class LlmApi:
    def __init__(self, registry: SecureRegistry, ledger: Optional[Ledger] = None) -> None:
        self.registry = registry
        self.ledger = ledger or Ledger()
        self.local_status: Optional[Callable[[], Dict[str, Any]]] = None       # the server says whether a model is loaded in the local engine

    # -- what the page shows -----------------------------------------------------------------------------

    def _provider_view(self, prov: Dict[str, Any], public: Dict[str, Any]) -> Dict[str, Any]:
        cap = int(prov.get("daily_token_cap") or 0)
        return {**public, "daily_token_cap": cap, "profile": prov.get("profile") or "",
                "context_window": int(prov.get("context_window") or 0), "max_output": int(prov.get("max_output") or 0),
                "this_server": bool(TYPES.get(prov.get("type") or "", {}).get("this_server")), "used_today": self.ledger.used(prov["id"]),
                "is_local": is_local(prov, self.registry.local_hosts), "runs_here": runs_here(prov, self.registry.local_hosts),
                "agent_only": TYPES.get(prov.get("type") or "", {}).get("api") == "cli",
                "judge_only": TYPES.get(prov.get("type") or "", {}).get("api") == "judge"}

    def _local(self) -> Dict[str, Any]:
        return self.local_status() if self.local_status else {"loaded": None, "model": ""}

    def _label(self, pid: Optional[str], local: Dict[str, Any]) -> str:
        """What answers, as the person reads it: "Provider: model", or the local engine's model."""
        if not pid:
            return str(local.get("model") or "") or "—"
        prov = self.registry._find(self.registry.load(), pid) or {}
        return f"{prov.get('name') or pid}: {prov['model']}" if prov.get("model") else str(prov.get("name") or pid)

    def now(self, function: str, local: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """What the function resolves to at this moment: the provider (empty for the local engine), how it got there (`via`), and the label."""
        local = local if local is not None else self._local()
        pid, via = self.registry.resolve_route(function, local.get("loaded"))
        return {"provider": pid or "", "via": via, "label": self._label(pid, local), "local_loaded": local.get("loaded")}

    def primary_view(self, local: Dict[str, Any]) -> Dict[str, Any]:
        data = self.registry.load()
        options = [{"id": p["id"], "label": self._label(p["id"], local), "type": p.get("type")} for p in data["providers"]
                   if TYPES.get(p.get("type") or "", {}).get("api") != "judge" and not TYPES.get(p.get("type") or "", {}).get("this_server")]
        return {"id": self.registry.primary(), "options": options, "default_for_new": self.registry.default_primary(), "label": self._label(self.registry.primary() or None, local)}

    def overview(self) -> Dict[str, Any]:
        data = self.registry.load()
        pub = self.registry.public()
        by_id = {p["id"]: p for p in data["providers"]}
        consent = self.registry.consent()
        local = self._local()
        rows: List[Dict[str, Any]] = []
        for fid, spec in functions.FUNCTIONS.items():
            pid = self.registry.chosen(fid)
            ok, why = self.registry.check(fid, pid or None)
            rows.append({"id": fid, "label": spec["label"], "kind": spec["kind"], "data": spec["data"], "tools": bool(spec.get("tools")),
                         "provider": pid, "ok": ok, "why": why, "consent": bool(consent.get(fid)), "needs_consent": spec["data"] == "personal",
                         "now": self.now(fid, local)})
        notices = [{"function": r["id"], "label": r["label"], "to": r["now"]["label"]} for r in rows if r["now"]["via"] == "fallback"]
        return {"functions": rows, "council": self.council_view(), "primary": self.primary_view(local), "local": local, "fallbacks": notices,
                "providers": [self._provider_view(by_id[p["id"]], p) for p in pub["providers"]],
                "types": [t for t in pub["types"] if t["type"] in SHOWN_TYPES],
                "reasoning_levels": reasoning.choices("en"), "imported": self.registry.imported(),
                "shared": [{"path": str(p), "providers": n} for p, n in self.shared_candidates()]}

    def answering(self) -> Dict[str, Any]:
        """For the status line of the page: what the Chat resolves to now, and the functions that fell back because no model is loaded."""
        local = self._local()
        chat = self.now("chat", local)
        return {"chat": chat, "local": local, "fallbacks": [{"function": f, "label": functions.FUNCTIONS[f]["label"], "to": n["label"]}
                                                           for f in functions.FUNCTIONS for n in [self.now(f, local)] if n["via"] == "fallback"]}

    def _role_view(self, role: Dict[str, Any], assigned: Dict[str, Any], consent: Dict[str, bool]) -> Dict[str, Any]:
        fid = functions.council_function(role["id"])
        pid = assigned.get(fid) or ""
        pid = "" if pid == LOCAL_EXPLICIT else pid
        ok, why = self.registry.check(fid, pid or None)
        return {**role, "route": pid, "consent": bool(consent.get(fid)), "ok": ok, "why": why}

    def council_view(self) -> Dict[str, Any]:
        """The council as saved (each role with its route and its consent for personal data), the defaults, the limits, and what a debate costs
        at most and at least after the cap on calls."""
        settings = council.load_settings(self.registry)
        away = council.outside(self.registry, settings)
        assigned, consent = dict(self.registry.load().get("assignments") or {}), self.registry.consent()        # as chosen, not as resolved
        shown = {**settings, "roles": [self._role_view(r, assigned, consent) for r in settings["roles"]], "moderator": self._role_view(settings["moderator"], assigned, consent)}
        defaults = council.clean_settings(None)
        return {"settings": shown, "defaults": {**defaults, "roles": [{**r, "route": "", "consent": False} for r in defaults["roles"]],
                                                "moderator": {**defaults["moderator"], "route": "", "consent": False}},
                "estimate": council.estimate(settings=settings, away=away), "outside": away, "ready": council.ready(settings),
                "limits": {"rounds": council.ROUNDS_MAX, "name": council.NAME_MAX, "job": council.JOB_MAX, "roles": council.ROLES_MAX, "calls_min": council.CALLS_MIN}}

    def set_council(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Saves the council: rounds, cap, `council_auto`, the advisers and the moderator, and for each role its route (a provider id, empty for the
        model loaded here) and its consent for personal data. What the body leaves out stays as it is. Nothing is saved if one route is not allowed."""
        keys = ("rounds", "max_calls", "council_auto", "roles", "moderator")
        try:
            settings = council.clean_settings({**council.load_settings(self.registry), **{k: body[k] for k in keys if k in body}})
        except ValueError as exc:
            raise LlmError(str(exc), "bad_council") from None
        sent = body.get("roles") if isinstance(body.get("roles"), list) else None
        entries = list(zip(sent, settings["roles"])) if sent is not None else [(None, r) for r in settings["roles"]]
        entries.append((body.get("moderator"), settings["moderator"]))
        with self.registry._io:
            data = self.registry.load()
            assigned = dict(data.get("assignments") or {})
            before = {k: bool(v) for k, v in (data.get("consent") or {}).items()}
            consent = dict(before)
            keep = {functions.council_function(r["id"]) for _, r in entries}
            for fid in [f for f in {*assigned, *consent} if functions.is_council(f) and f not in keep]:
                assigned.pop(fid, None)                                   # a role that was removed takes its route and its yes with it
                consent.pop(fid, None)
            moved = set()

            def off_it(fid: str) -> None:
                if data.get("default_primary"):
                    assigned[fid] = LOCAL_EXPLICIT
                else:
                    assigned.pop(fid, None)
            for raw, role in entries:
                fid = functions.council_function(role["id"])
                if not isinstance(raw, dict):
                    continue
                if "consent" in raw:
                    if not isinstance(raw["consent"], bool):
                        raise LlmError("Say yes or no for personal data going to a provider outside this PC.", "bad_request")
                    consent[fid] = raw["consent"]
                if "route" in raw:
                    pid = raw["route"] or ""
                    if pid and pid != PRIMARY and self.registry._find(data, pid) is None:
                        raise LlmError("That provider is not there any more.", "gone")
                    moved.add(fid)
                    if pid:
                        assigned[fid] = pid
                    else:
                        off_it(fid)
            for _, role in entries:
                fid = functions.council_function(role["id"])
                ok, why = self.registry.check(fid, assigned.get(fid) or None, consent=consent)
                if not ok and (fid not in moved or (before.get(fid) and not consent.get(fid))):
                    off_it(fid)                                           # taking the yes back takes the role off the outside provider
                    ok = True
                if not ok:
                    raise LlmError(f"{role['name']}: {why}", "not_allowed")
            data.update(council=settings, assignments=assigned, consent=consent)
            self.registry.save(data)
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

    def order_providers(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Sets the order of the providers: the order they are tried in when the chosen route does not answer."""
        try:
            self.registry.reorder(body.get("ids"))
        except ValueError as exc:
            raise LlmError(str(exc), "bad_order") from None
        return {"order": [p["id"] for p in self.registry.load().get("providers", [])]}

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

    def set_primary(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """The Primary model: a provider id, or empty for the local engine; it applies to the next call of every function that follows it.
        `default_for_new` (a yes or no) says whether a function with no choice of its own follows it."""
        pid, new = body.get("primary", self.registry.primary()) or "", body.get("default_for_new")
        if new is not None and not isinstance(new, bool):
            raise LlmError("Say yes or no.", "bad_request")
        if not isinstance(pid, str):
            raise LlmError("Choose a provider from the list.", "bad_request")
        try:
            self.registry.set_primary(pid, new)
        except KeyError:
            raise LlmError("That provider is not there any more.", "gone") from None
        return self.overview()

    def _form_provider(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """The provider as the form has it: its kind, address, model and key, the key from the saved provider `id` when the form leaves it empty."""
        kind = body.get("type")
        saved = self.registry._find(self.registry.load(), str(body.get("id") or "")) if body.get("id") else None
        if saved is not None and not kind:
            kind = saved.get("type")
        if kind not in TYPES or kind not in SHOWN_TYPES:
            raise LlmError("Choose one of the kinds of provider in the list.", "bad_type")
        prov = {"type": kind, "base_url": str(body.get("base_url") or "").strip(), "model": str(body.get("model") or "").strip(),
                "api_key": str(body.get("api_key") or "").strip()}
        if saved is not None and saved.get("type") == kind:
            prov["api_key"] = prov["api_key"] or str(saved.get("api_key") or "").strip()
            prov["base_url"] = prov["base_url"] or str(saved.get("base_url") or "").strip()
            prov["oauth"] = saved.get("oauth") or {}
        return prov

    def models(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """The models the provider lists, asked with the address and key of the form (or the saved key): nothing is saved."""
        prov = self._form_provider(body)
        try:
            return {"models": probe.list_models(prov)}
        except RuntimeError as exc:
            raise LlmError(str(exc), "models_failed") from None

    def try_form(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """One minimal request to the provider with the values of the form, without saving: {ok, ms} or {ok: false, error}."""
        return probe.try_request(self._form_provider(body))

    def consent(self, function: Any, allowed: Any) -> Dict[str, Any]:
        if function not in functions.FUNCTIONS or not isinstance(allowed, bool):
            raise LlmError("Say which function, and yes or no.", "bad_request")
        if not allowed:                                               # taking the yes back also takes the function off an outside provider
            pid = self.registry.chosen(function)
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
