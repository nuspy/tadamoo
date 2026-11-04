"""The page's view of file checkpoints: the position of the setting, and the list, restore and delete for the folder a conversation works in."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from serve.agent_runs import RunError

BUSY = ("busy", "starting", "asking")
TRIM_AFTER = 8


def mode_view() -> Dict[str, Any]:
    from tadamoo.checkpoints import MODES, read_mode
    return {"mode": read_mode(), "modes": list(MODES)}


def set_mode(mode: Any) -> Dict[str, Any]:
    from tadamoo.checkpoints import write_mode
    try:
        write_mode(mode if isinstance(mode, str) else "")
    except ValueError as exc:
        raise RunError(str(exc), "bad_mode") from None
    return mode_view()


def _where(rec: Dict[str, Any]) -> tuple:
    path = Path(rec.get("workdir") or rec["workspace"])
    if not path.is_dir():
        raise RunError("The folder of that conversation is not there any more.", "folder_not_found")
    from tadamoo.stack import data_dir_for                # imported here: the pages do not need the agent to be installed
    return path, data_dir_for(path)


def listing(rec: Dict[str, Any]) -> Dict[str, Any]:
    from tadamoo.rewind import checkpoint_report
    path, data = _where(rec)
    rows = [{**cp, "after": cp["after"][-TRIM_AFTER:]} for cp in checkpoint_report(data, path)]
    return {"checkpoints": rows, "bytes": sum(r["bytes"] for r in rows), **mode_view()}


def _idle(rec: Dict[str, Any]) -> None:
    if rec.get("state") in BUSY:
        raise RunError("Wait for the agent to finish, or stop it, before changing its checkpoints.", "busy")


def delete(rec: Dict[str, Any], checkpoint_id: Any, everything: bool = False) -> Dict[str, Any]:
    """Deletes the checkpoint named, or all of them when `everything` is true; with neither it deletes nothing."""
    from tadamoo.rewind import delete_checkpoints
    _idle(rec)
    path, data = _where(rec)
    if everything:
        return {"deleted": delete_checkpoints(data, path)}
    try:
        if not isinstance(checkpoint_id, str):
            raise ValueError("Name the checkpoint to delete.")
        return {"deleted": delete_checkpoints(data, path, checkpoint_id)}
    except ValueError as exc:
        raise RunError(str(exc), "no_checkpoint") from None


def restore(rec: Dict[str, Any], checkpoint_id: Any) -> Dict[str, Any]:
    """Puts the files back to a checkpoint; the actions that stay done are returned as `not_undone`."""
    from tadamoo.rewind import checkpoint_report, rewind
    path, data = _where(rec)
    target = next((c for c in checkpoint_report(data, path) if c["id"] == checkpoint_id), None)
    if target is None:
        raise RunError("That checkpoint is not there any more.", "no_checkpoint")
    try:
        return {**rewind(data, path, checkpoint_id), "not_undone": target["not_undone"]}
    except (OSError, ValueError) as exc:
        raise RunError(f"Could not go back: {exc}", "rewind_failed") from None
