"""Basalt's own registry of providers (llmswitch), with the keys kept apart, the consent for the cloud and the caps.

* The file is **dedicated to Basalt** (in the user data folder) and can **once** take what another program's registry holds
  (`import_shared`: a copy, never a reference).
* The keys and tokens are in the credential manager, never in the JSON: the file holds only a reference.
* A function that handles personal data cannot be pointed at a provider outside this PC without the person's yes for that
  function (`consent`); a fallback may only go to a route that is as local or more.
* A provider can have a cap on tokens a day (`daily_token_cap`); a request over it is refused with the reason.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from harness.llmswitch import providers as _providers
from harness.llmswitch.providers import TYPES, Registry
from harness.llm import functions
from harness.llm.credentials import CredentialStore, default_store

# Two types Basalt adds to llmswitch's own: the model loaded in this server, and Jev, which judges but does not chat.
TYPES.setdefault("basalt", {
    "label": "Basalt (this server)", "auth": "none", "api": "openai", "local": True, "this_server": True, "base_url": "",
    "note": "The model loaded in this Basalt. It is used as it is: nothing is loaded or unloaded for it from here.",
})
TYPES.setdefault("jev", {
    "label": "Jev (TypeSafe AI)", "auth": "api_key", "api": "judge", "local": False, "base_url": "https://api.typesafe.ai/v1",
    "key_env": "TYPESAFE_API_KEY", "console": "https://console.typesafe.ai/home",
    "note": "Answers typed questions (a choice, a score). It does not chat. Only short excerpts go to it, never whole documents.",
})
SECRET_FIELDS = ("api_key",)
OAUTH_FIELDS = ("access_token", "refresh_token", "id_token")
CHAT_APIS = {"openai", "lmstudio", "vllm", "gemini", "anthropic"}


def data_dir() -> Path:
    try:
        from serve.userdata import user_data_dir
        return user_data_dir()
    except Exception:
        return Path.home() / ".basalt"


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def is_local(prov: Optional[Dict[str, Any]], local_hosts: tuple = LOOPBACK) -> bool:
    """Whether the provider runs on this PC (or is this server): what may be sent to it stays here."""
    if not prov:
        return True
    spec = TYPES.get(prov.get("type") or "", {})
    if spec.get("api") == "cli":
        return False                                # a command-line agent runs here, but what it is given goes to its maker
    if spec.get("this_server") or spec.get("local"):
        return True
    host = urlparse((prov.get("base_url") or spec.get("base_url") or "")).hostname or ""
    return host in local_hosts


class SecureRegistry(Registry):
    """llmswitch's registry with the keys moved to a credential store, and the extra data Basalt keeps next to it."""

    def __init__(self, path: str | Path | None = None, credentials: Optional[CredentialStore] = None, **kw: Any) -> None:
        path = Path(path) if path else data_dir() / "llm_providers.json"
        kw.setdefault("consumers", functions.names())
        super().__init__(path, app_name="basalt", **kw)
        self.credentials = credentials or default_store(self.path.parent)
        self._io = threading.RLock()
        self.local_hosts = LOOPBACK                   # the addresses that are this PC (tests can say there are none)

    # -- the keys are not in the file --------------------------------------------------------------------

    def _ref(self, pid: str, field: str) -> str:
        return f"{pid}/{field}"

    def load(self) -> Dict[str, Any]:
        with self._io:
            data = super().load()
            for prov in data.get("providers", []):
                pid = prov.get("id")
                if not pid:
                    continue
                for field in SECRET_FIELDS:
                    if prov.get("key_ref") and not prov.get(field):
                        prov[field] = self.credentials.read(prov["key_ref"])
                oauth = prov.get("oauth") if isinstance(prov.get("oauth"), dict) else None
                if oauth is not None:
                    for field in OAUTH_FIELDS:
                        if oauth.get(field) == "@cred":
                            oauth[field] = self.credentials.read(self._ref(pid, "oauth." + field))
            return data

    def save(self, data: Dict[str, Any]) -> None:
        with self._io:
            clean = copy.deepcopy(data)
            for prov in clean.get("providers", []):
                pid = prov.get("id")
                if not pid:
                    continue
                for field in SECRET_FIELDS:
                    value = (prov.get(field) or "").strip()
                    if value:
                        ref = prov.get("key_ref") or self._ref(pid, field)
                        self.credentials.write(ref, value)
                        prov["key_ref"] = ref
                    prov[field] = ""
                oauth = prov.get("oauth") if isinstance(prov.get("oauth"), dict) else None
                if oauth is not None:
                    for field in OAUTH_FIELDS:
                        if oauth.get(field) and oauth[field] != "@cred":
                            self.credentials.write(self._ref(pid, "oauth." + field), oauth[field])
                            oauth[field] = "@cred"
            super().save(clean)

    def remove(self, pid: str) -> None:
        prov = self._find(self.load(), pid)
        super().remove(pid)
        if prov:
            for ref in (prov.get("key_ref"), *(self._ref(pid, "oauth." + f) for f in OAUTH_FIELDS)):
                if ref:
                    self.credentials.delete(ref)

    def set_profile(self, pid: str, profile: str) -> Dict[str, Any]:
        """The Loader's load profile a route to this server uses when the model has to be loaded (empty: the default one)."""
        with self._lock:
            data = self.load()
            prov = self._find(data, pid)
            if prov is None:
                raise KeyError(pid)
            if profile:
                prov["profile"] = str(profile)[:60]
            else:
                prov.pop("profile", None)
            self.save(data)
            return prov

    def set_cap(self, pid: str, tokens: int) -> Dict[str, Any]:
        """The most tokens a day that this provider may be asked for (0: no cap)."""
        with self._lock:
            data = self.load()
            prov = self._find(data, pid)
            if prov is None:
                raise KeyError(pid)
            if int(tokens) > 0:
                prov["daily_token_cap"] = int(tokens)
            else:
                prov.pop("daily_token_cap", None)
            self.save(data)
            return prov

    # -- what Basalt keeps next to the providers ---------------------------------------------------------

    def _extra(self, key: str, default: Any) -> Any:
        return self.load().get(key, default)

    def _set_extra(self, key: str, value: Any) -> None:
        with self._io:
            data = self.load()
            data[key] = value
            self.save(data)

    def consent(self) -> Dict[str, bool]:
        """The functions the person said yes to for providers outside this PC."""
        return {k: bool(v) for k, v in (self._extra("consent", {}) or {}).items()}

    def set_consent(self, function: str, allowed: bool) -> Dict[str, bool]:
        functions.spec(function)
        value = self.consent()
        value[function] = bool(allowed)
        self._set_extra("consent", value)
        return value

    def imported(self) -> List[str]:
        return list(self._extra("imported", []) or [])

    # -- what a route may be ----------------------------------------------------------------------------

    def check(self, function: str, pid: Optional[str]) -> Tuple[bool, str]:
        """Whether the function may use this provider, and if not, why. No provider is always fine: it is the loaded model."""
        spec = functions.spec(function)
        if not pid:
            return True, ""
        prov = self._find(self.load(), pid)
        if prov is None:
            return False, f"there is no provider {pid!r}"
        api = TYPES.get(prov.get("type") or "", {}).get("api", "")
        if spec["kind"] == "chat":
            if api not in CHAT_APIS and not (api == "cli" and not spec.get("tools")):
                return False, f"{prov.get('name') or pid} cannot hold a conversation" + (" with tools" if api == "cli" else "")
        elif api not in CHAT_APIS and api != "judge" and api != "cli":
            return False, f"{prov.get('name') or pid} cannot judge"
        if spec.get("data") == "personal" and not is_local(prov, self.local_hosts) and not self.consent().get(function):
            return False, (f"{spec['label']} handles your personal data and {prov.get('name') or pid} is outside this PC: "
                           "say yes for this function first")
        return True, ""

    def assign_checked(self, function: str, pid: Optional[str]) -> Dict[str, Any]:
        ok, why = self.check(function, pid)
        if not ok:
            raise ValueError(why)
        return self.assign(function, pid or None)

    def fallback_chain(self, function: str) -> List[Optional[str]]:
        """The providers to try in order: the chosen one and then only routes that are as local or more (the loaded model last)."""
        data = self.load()
        first = (data.get("assignments") or {}).get(function) or None
        chain: List[Optional[str]] = [first]
        if first and not is_local(self._find(data, first), self.local_hosts):
            chain.append(None)                                      # the loaded model is always at least as local
        return chain

    def import_shared(self, source: str | Path) -> Dict[str, Any]:
        """Copies the providers and the choices of another registry once. Nothing is linked: the two files go their own way."""
        source = Path(source)
        marker = str(source.resolve())
        if marker in self.imported():
            return {"imported": 0, "already": True}
        try:
            other = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"that registry cannot be read: {exc}") from None
        data = self.load()
        have = {p["id"] for p in data["providers"]}
        added, remap = 0, {}
        for prov in other.get("providers", []):
            if not isinstance(prov, dict) or prov.get("type") not in TYPES:
                continue
            new = copy.deepcopy(prov)
            new.pop("key_ref", None)
            pid = new.get("id") or prov["type"]
            while pid in have:
                pid += "-2"
            remap[prov.get("id")] = new["id"] = pid
            have.add(pid)
            data["providers"].append(new)
            added += 1
        for consumer, old in (other.get("assignments") or {}).items():
            if consumer in functions.FUNCTIONS and old in remap and not data["assignments"].get(consumer):
                data["assignments"][consumer] = remap[old]
        data["imported"] = [*self.imported(), marker]
        self.save(data)
        return {"imported": added, "already": False}


class Ledger:
    """Tokens each provider used today, for the daily cap."""

    def __init__(self, path: str | Path | None = None, clock=time.time) -> None:
        self.path = Path(path) if path else data_dir() / "llm_usage.json"
        self.clock = clock
        self._lock = threading.Lock()

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self.clock()))

    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def used(self, pid: str) -> int:
        return int((self._load().get(self._today()) or {}).get(pid, 0))

    def record(self, pid: str, tokens: int) -> None:
        if not pid or tokens <= 0:
            return
        with self._lock:
            data = self._load()
            day = data.setdefault(self._today(), {})
            day[pid] = int(day.get(pid, 0)) + int(tokens)
            data = {k: v for k, v in data.items() if k >= time.strftime("%Y-%m-%d", time.localtime(self.clock() - 14 * 86400))}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)

    def over_cap(self, prov: Dict[str, Any]) -> Optional[str]:
        cap = int(prov.get("daily_token_cap") or 0)
        if cap and self.used(prov["id"]) >= cap:
            return f"{prov.get('name') or prov['id']} has used its {cap} tokens for today"
        return None
