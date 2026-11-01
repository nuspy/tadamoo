"""What the person accepted for each project folder: the checks detected from its files (harness/project_profile.py) and the exact commands
they typed themselves. Kept in one small file in the user data folder, never in the project, so a cloned repository cannot approve anything.

An accepted check is stored with the digest of what it ran when it was shown; it counts only while the folder still detects the same command
with the same digest. A command the person types is theirs and always counts. A conversation's process is started with the result
(`flags`) and told again when the person changes it (`effective`)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from harness.permissions import READ_ONLY_COMMANDS
from harness.project_profile import COMMAND_CHARS, KINDS, MAX_SAFE, Check, clean_list, detect, is_empty


class CodingError(ValueError):
    """What is wrong with a value, in words for the person; `field` names it."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


def project_key(folder: Any) -> str:
    return str(Path(str(folder)).resolve()).replace("\\", "/").rstrip("/").lower()


class CodingPrefs:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _stored(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _project(self, folder: Any) -> Dict[str, Any]:
        mine = (self._stored().get("projects") or {}).get(project_key(folder))
        mine = mine if isinstance(mine, dict) else {}
        accepted = [a for a in mine.get("accepted") or [] if isinstance(a, dict) and isinstance(a.get("command"), str) and isinstance(a.get("digest"), str)]
        return {"accepted": accepted, "own": clean_list(mine.get("own")), "offered": mine.get("offered") is True, "declined": mine.get("declined") is True}

    def _write(self, folder: Any, mine: Dict[str, Any]) -> None:
        data = self._stored()
        projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
        projects[project_key(folder)] = mine
        data["projects"] = projects
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def view(self, folder: Any) -> Dict[str, Any]:
        """The checks the folder suggests with what the person did about each, the commands they typed, and the facts the card shows."""
        mine = self._project(folder)
        held = {a["command"]: a for a in mine["accepted"]}
        checks = []
        for c in detect(folder):
            a = held.get(c.command) if c.command else None
            state = "none" if a is None else "accepted" if a["digest"] == c.digest else "changed"
            checks.append({**c.view(), "state": state, "gate": bool(a and a.get("gate", True))})
        return {"folder": str(folder), "empty": is_empty(folder), "kinds": list(KINDS), "checks": checks, "own": mine["own"],
                "read_only": list(READ_ONLY_COMMANDS), "limits": {"own": MAX_SAFE, "command_chars": COMMAND_CHARS}}

    def accept(self, folder: Any, accepted: Any = None, own: Any = None) -> Dict[str, Any]:
        """Saves what the person ticked. `accepted` is the whole list of detected commands they accept, each `{command, gate}`: only commands the
        folder shows now can be accepted, and what is not in the list is no longer accepted. `own` is their typed commands, one to a line."""
        mine = self._project(folder)
        if accepted is not None:
            if not isinstance(accepted, list) or not all(isinstance(a, dict) and isinstance(a.get("command"), str) for a in accepted):
                raise CodingError("Give the accepted checks as a list of commands.", "accepted")
            shown = {c.command: c for c in detect(folder) if c.command}
            chosen = []
            for a in accepted:
                c: Check | None = shown.get(a["command"])
                if c is None:
                    raise CodingError(f"This folder does not suggest the command '{a['command'][:80]}'.", "accepted")
                if a.get("gate") is not None and not isinstance(a["gate"], bool):
                    raise CodingError("Running before the answer ends is on or off.", "accepted")
                chosen.append({"command": c.command, "kind": c.kind, "digest": c.digest, "gate": a.get("gate") is not False})
            mine["accepted"] = chosen
        if own is not None:
            lines = own.splitlines() if isinstance(own, str) else own
            if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
                raise CodingError("Give the commands as text, one to a line.", "own")
            wanted = [" ".join(x.split()) for x in lines if x.strip()]
            if any(len(x) > COMMAND_CHARS for x in wanted):
                raise CodingError(f"A command is at most {COMMAND_CHARS} characters.", "own")
            if len(set(wanted)) > MAX_SAFE:
                raise CodingError(f"At most {MAX_SAFE} commands.", "own")
            mine["own"] = clean_list(wanted)
        self._write(folder, mine)
        return self.view(folder)

    def offer_pending(self, folder: Any) -> bool:
        """Whether the card that offers this folder's detected checks has still to be shown: once per folder, and never where the person has already
        accepted or typed checks or said no."""
        mine = self._project(folder)
        if mine["offered"] or mine["declined"] or mine["accepted"] or mine["own"] or is_empty(folder):
            return False
        return any(c.command for c in detect(folder))

    def mark_offered(self, folder: Any) -> None:
        """The card was shown: it is not shown again for this folder, whatever the person does with it."""
        mine = self._project(folder)
        if not mine["offered"]:
            mine["offered"] = True
            self._write(folder, mine)

    def settle_offer(self, folder: Any, commands: Any) -> Dict[str, Any]:
        """The answer to the card: the commands ticked are accepted to run before an answer ends (only commands the folder detects now count); none
        ticked is a no, remembered for the folder."""
        mine = self._project(folder)
        shown = {c.command: c for c in detect(folder) if c.command}
        chosen = [shown[c] for c in (commands if isinstance(commands, list) else []) if isinstance(c, str) and c in shown]
        held = {a["command"] for a in mine["accepted"]}
        mine["accepted"] += [{"command": c.command, "kind": c.kind, "digest": c.digest, "gate": True} for c in chosen if c.command not in held]
        mine["offered"], mine["declined"] = True, not chosen and not mine["accepted"]
        self._write(folder, mine)
        return self.view(folder)

    def effective(self, folder: Any) -> Dict[str, List[str]]:
        """What a conversation in this folder uses now: the exact commands that run without a question, and the accepted checks the verify gate runs."""
        mine = self._project(folder)
        current = {c.command: c for c in detect(folder) if c.command}
        valid = [a for a in mine["accepted"] if a["command"] in current and current[a["command"]].digest == a["digest"]]
        return {"safe": clean_list(mine["own"] + [a["command"] for a in valid]),
                "checks": clean_list([a["command"] for a in valid if a.get("gate", True)])}

    def flags(self, folder: Any) -> List[str]:
        """The options a conversation's process is started with."""
        got = self.effective(folder)
        out: List[str] = ["--offer-checks"] if self.offer_pending(folder) else []
        for command in got["safe"]:
            out += ["--safe-command", command]
        for command in got["checks"]:
            out += ["--project-check", command]
        return out
