"""A personal skill running in a conversation: what answers its script's calls.

`tool()` goes through the agent's own gate (permissions, hooks, the audit record, and the question to the person when one is needed) and
is kept in the conversation like the model's calls; `ai()` is one model call at temperature 0 with its own short instruction, and the
answer is kept for the same prompt and inputs, so a skill that runs again reads the same text without asking again; `ask()` puts a
question with buttons to the person; `say()` tells them how it is going."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from tadamoo.client import TadamooError, extract_reasoning_and_content
from tadamoo.personal_skills.runner import Handlers, RunResult, run

AI_SYSTEM = ("You are one step of a program. Do exactly what the text asks and answer with the result only: no greeting, no explanation, "
             "no markdown fence unless the text asks for one. If the text gives data, work only on that data.")
CACHE_FILE, CACHE_KEPT = "ai.json", 400


def fill(prompt: str, inputs: Dict[str, Any]) -> str:
    """The prompt with each {name} replaced by its input (text as it is, anything else as JSON); a {word} that is not an input stays."""
    def value(v: Any) -> str:
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return re.sub(r"\{(\w+)\}", lambda m: value(inputs[m.group(1)]) if m.group(1) in inputs else m.group(0), prompt)


class SkillRun:
    def __init__(self, session: Any, task: Any, skill: Dict[str, Any]) -> None:
        self.session, self.task, self.skill = session, task, skill
        self.agent = task.agent
        self.checkpointed = [False]

    # -- what answers the script ---------------------------------------------------------------------------

    def _tool(self, name: str, arguments: Dict[str, Any]) -> "tuple[bool, str]":
        return self.agent.call_tool(name, arguments, self.checkpointed)

    def _cache(self) -> Path | None:
        folder = self.skill.get("cache_dir")
        return Path(folder) / CACHE_FILE if folder else None

    def _ai(self, prompt: str, inputs: Dict[str, Any], cache: bool) -> str:
        text = fill(prompt, inputs)
        key = hashlib.sha256(json.dumps([AI_SYSTEM, text], ensure_ascii=False).encode("utf-8")).hexdigest()[:32]
        path = self._cache() if cache else None
        kept: Dict[str, str] = {}
        if path is not None:
            try:
                kept = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                kept = {}
            if isinstance(kept.get(key), str):
                return kept[key]
        answer, attempt = "", 0
        while True:
            if self.agent.cancel.is_set():
                raise RuntimeError("stopped")
            try:
                resp = self.agent.client.chat_completion(messages=[{"role": "system", "content": AI_SYSTEM}, {"role": "user", "content": text}],
                                                         temperature=0.0, extra={"reasoning_effort": "none"}, timeout=600.0)
                answer = extract_reasoning_and_content((resp.get("choices") or [{}])[0].get("message") or {})[1].strip()
                self.agent.total_tokens += int((resp.get("usage") or {}).get("total_tokens") or 0)
                break
            except TadamooError:                                # waited for like the conversation's own calls: the last wait repeats until the person stops it
                waits = self.session.waits
                wait = waits[min(attempt, len(waits) - 1)]
                attempt += 1
                if not self.session._wait(wait, self.agent.cancel):
                    raise RuntimeError("stopped") from None
        if path is not None:
            kept[key] = answer
            for old in list(kept)[:-CACHE_KEPT]:
                kept.pop(old, None)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return answer

    def _ask(self, question: str, options: List[str]) -> str:
        choices = options or ["Yes", "No"]
        answer = self.session._ask({"mode": "confirm", "kind": "skill_question", "question": question[:500], "facts": {"skill": self.skill.get("name")},
                                    "options": [{"label": str(o)[:60], "value": str(o)[:60]} for o in choices[:8]]})
        if answer == "":
            raise RuntimeError("stopped")
        return answer

    def _say(self, text: str) -> None:
        self.session.emit({"event": "skill_say", "turn": self.task.turn, "text": str(text)[:500]})

    # -- running ---------------------------------------------------------------------------------------------

    def execute(self) -> RunResult:
        folder = Path(tempfile.mkdtemp(prefix="pskill-"))
        try:
            script = folder / "skill.py"
            script.write_text(str(self.skill.get("script", "")), encoding="utf-8")
            self.session.emit({"event": "skill", "turn": self.task.turn, "phase": "start", "name": self.skill.get("name")})
            handlers = Handlers(self._tool, self._ai, self._ask, self._say)
            workspace = Path(self.session.built.sandbox.root)
            result = run(script, self.skill.get("values") or {}, handlers, self.agent.cancel,
                         float(self.skill.get("timeout") or 0) or None, workspace if workspace.is_dir() else None)
            self.session.emit({"event": "skill", "turn": self.task.turn, "phase": "end", "name": self.skill.get("name"), "ok": result.ok,
                               "seconds": result.seconds, "calls": len(result.calls)})
            return result
        finally:
            shutil.rmtree(folder, ignore_errors=True)


def result_text(name: str, result: RunResult) -> str:
    """What the conversation says the skill did: its result, or why it stopped (with what it had printed)."""
    printed = f"\n\nOutput:\n{result.printed}" if result.printed and (not result.ok or not result.output) else ""
    if result.ok:
        return (result.output or "The skill finished with no result.") + printed
    if result.error == "stopped":
        return f"The skill “{name}” was stopped." + printed
    return f"The skill “{name}” stopped: {result.error}" + printed
