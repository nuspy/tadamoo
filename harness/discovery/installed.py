"""The capabilities Basalt installed on the person's choice: one record each in installed.json, and the keys they ask for.

A server's keys stay in the vault. When the server is started, the one variable it asks for is read from the vault entry the
record names, handed to the new process and forgotten: it is never written to a file and never goes to the model.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

FILE = "installed.json"
VAULT_FIELD = "value"
GITHUB_ENTRY = "github"
_lock = threading.RLock()


class MissingKey(ValueError):
    """A server needs a key that is not in the vault; the message names the entry to create and never a value."""


def vault_entry_for(variable: str) -> str:
    """The vault entry a variable is read from: the variable's own name (letters, digits, `_` and `-`)."""
    entry = re.sub(r"[^A-Za-z0-9_\-]", "_", variable)[:40]
    return entry if re.match(r"^[A-Za-z0-9]", entry) else "k" + entry[:39]


def env_spec(variables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """What a record keeps about the variables a server reads: no value of a secret, only the vault entry to read it from."""
    return [{"name": str(v["name"]), "required": bool(v.get("required")), "secret": bool(v.get("secret")), "entry": vault_entry_for(str(v["name"])),
             "value": "" if v.get("secret") else str(v.get("value") or "")} for v in variables]


class InstalledStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / FILE

    def load(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        items = data.get("items") if isinstance(data, dict) else None
        return {k: v for k, v in items.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(items, dict) else {}

    def _write(self, items: Dict[str, Dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": items}, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        return self.load().get(entry_id)

    def put(self, record: Dict[str, Any]) -> None:
        with _lock:
            items = self.load()
            items[record["id"]] = record
            self._write(items)

    def delete(self, entry_id: str) -> None:
        with _lock:
            items = self.load()
            if items.pop(entry_id, None) is not None:
                self._write(items)

    def listing(self) -> List[Dict[str, Any]]:
        """What the Settings page shows: what it is, where from, when, and the keys it asks for. No path and no command."""
        out = []
        for rec in sorted(self.load().values(), key=lambda r: str(r.get("installed_at") or ""), reverse=True):
            out.append({"id": rec.get("id"), "name": rec.get("name"), "kind": rec.get("kind"), "version": rec.get("version", ""), "source": rec.get("source", ""),
                        "installed_at": rec.get("installed_at", ""), "score": rec.get("score"), "remote": bool(rec.get("url")),
                        "keys": [{"name": e["name"], "entry": e["entry"]} for e in rec.get("env", []) if isinstance(e, dict) and e.get("required") and not e.get("value")]})
        return out


def launch_env(record: Dict[str, Any], vault_dir: Any) -> Dict[str, str]:
    """The variables to start a server with: the fixed ones, and each key read from the vault now.

    A required key that is not there raises MissingKey; an optional one is left out."""
    env: Dict[str, str] = {}
    vault = None
    for spec in record.get("env", []):
        if not isinstance(spec, dict) or not spec.get("name"):
            continue
        name = str(spec["name"])
        if spec.get("value") and not spec.get("secret"):
            env[name] = str(spec["value"])
            continue
        value = ""
        if vault_dir:
            try:
                if vault is None:
                    from harness.vault import Vault
                    vault = Vault(vault_dir)
                value = vault.reveal(str(spec.get("entry") or vault_entry_for(name)), VAULT_FIELD)
            except Exception:                                    # no entry, no vault, or no way to open it: the same answer
                value = ""
        if value:
            env[name] = value
        elif spec.get("required"):
            raise MissingKey(f"The server needs {name}: put it in the vault as a secret entry named {spec.get('entry') or vault_entry_for(name)} "
                             f"(field {VAULT_FIELD}), then call it again.")
    return env


def github_token(vault_dir: Any) -> str:
    """The optional GitHub token, from the vault entry named github; empty when there is none. It raises the limit of requests and nothing else."""
    if not vault_dir:
        return ""
    try:
        from harness.vault import Vault
        return str(Vault(vault_dir).reveal(GITHUB_ENTRY, VAULT_FIELD)).strip()
    except Exception:
        return ""
