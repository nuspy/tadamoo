"""The permission rules the person allowed for every conversation ("Always allow" in a question), kept in a small file in the user data folder.
A conversation starts with them; the person can add and take away rules from Settings."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, List

MAX_RULES = 200
RULE_MAX = 200


class AllowRules:
    def __init__(self, path: "str | Path") -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def read(self) -> List[str]:
        try:
            rules = json.loads(self.path.read_text(encoding="utf-8")).get("rules")
        except (OSError, ValueError, AttributeError):
            return []
        return [r for r in rules if isinstance(r, str)] if isinstance(rules, list) else []

    def _write(self, rules: List[str]) -> List[str]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"rules": rules}), encoding="utf-8")
        return rules

    @staticmethod
    def _clean(rule: Any) -> str:
        text = rule.strip() if isinstance(rule, str) else ""
        if not text or len(text) > RULE_MAX or "\n" in text or "\r" in text:
            raise ValueError(f"A rule is one line of text, at most {RULE_MAX} characters, such as shell_command:pytest *.")
        return text

    def add(self, rule: Any) -> List[str]:
        text = self._clean(rule)
        with self._lock:
            rules = self.read()
            if text in rules:
                return rules
            if len(rules) >= MAX_RULES:
                raise ValueError(f"At most {MAX_RULES} rules: take some away first.")
            return self._write(rules + [text])

    def remove(self, rule: Any) -> List[str]:
        text = self._clean(rule)
        with self._lock:
            return self._write([r for r in self.read() if r != text])
