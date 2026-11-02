"""A copy of a git folder for one task: its own folder and branch, so tasks on one repository do not step on each other, and what a
task changed waits there until the person applies it to their own folder.

The copy starts from the folder's last commit plus the changes not yet committed to tracked files. Applying a task's changes puts them in
the person's folder as staged changes (never a commit, never over a file they have changed in a way that does not fit); the copy goes on
from there. Throwing it away removes the folder and the branch. Nothing here touches a folder that is not a git repository.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

IDENTITY = ["-c", "user.name=Basalt", "-c", "user.email=basalt@localhost", "-c", "commit.gpgsign=false"]
TIMEOUT_S = 120
BRANCH = "basalt/"
HIDDEN = (".basalt/",)                    # what the agent keeps for itself is not a change of the task


class WorktreeError(Exception):
    def __init__(self, message: str, code: str = "worktree") -> None:
        super().__init__(message)
        self.code = code


def _run(args: Sequence[str], cwd: Path, input: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    try:
        done = subprocess.run(["git", *args], cwd=str(cwd), input=input, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=TIMEOUT_S, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        raise WorktreeError("git is not installed on this PC", "no_git") from None
    except subprocess.TimeoutExpired:
        raise WorktreeError("git did not answer in time", "timeout") from None
    if check and done.returncode != 0:
        raise WorktreeError((done.stderr or done.stdout).strip()[:600] or "git failed", "git")
    return done


def _run_bytes(args: Sequence[str], cwd: Path, input: Optional[bytes] = None) -> subprocess.CompletedProcess:
    """For patches: bytes in and out, so no line ending is changed on the way."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), input=input, capture_output=True, timeout=TIMEOUT_S, env=env,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        raise WorktreeError("git is not installed on this PC", "no_git") from None
    except subprocess.TimeoutExpired:
        raise WorktreeError("git did not answer in time", "timeout") from None


def repo_root(path: Path) -> Optional[Path]:
    """The top folder of the repository that holds `path`, or None."""
    try:
        done = _run(["rev-parse", "--show-toplevel"], Path(path), check=False)
    except WorktreeError:
        return None
    return Path(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None


class Worktrees:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)

    # -- making and finding ---------------------------------------------------------------------------------

    def create(self, task_id: str, workspace: str | Path) -> Dict[str, Any]:
        """A copy for `task_id`: {"repo", "dir", "workdir", "branch", "base"} (`workdir` is `dir` or the same sub folder of it)."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", task_id or ""):
            raise WorktreeError("a task id is letters, digits, - and _", "bad_id")
        workspace = Path(workspace).resolve()
        root = repo_root(workspace)
        if root is None:
            raise WorktreeError("this folder is not a git repository, so it cannot be copied for a task", "not_git")
        root = root.resolve()
        slug = f"{re.sub(r'[^A-Za-z0-9]+', '-', root.name)[:20]}-{hashlib.sha1(str(root).encode('utf-8')).hexdigest()[:6]}"
        target = self.base_dir / slug / task_id
        if target.exists():
            raise WorktreeError("this task already has a copy", "exists")
        head = _run(["rev-parse", "HEAD"], root, check=False)
        if head.returncode != 0:
            raise WorktreeError("this repository has no commit yet", "no_commit")
        base = head.stdout.strip()
        pending = _run([*IDENTITY, "stash", "create"], root, check=False).stdout.strip()
        if pending:
            base = pending                                           # the tracked files as they are now, not as last committed
        target.parent.mkdir(parents=True, exist_ok=True)
        branch = BRANCH + task_id
        _run(["worktree", "add", "-b", branch, str(target), base], root)
        exclude = Path(_run(["rev-parse", "--git-path", "info/exclude"], target).stdout.strip())
        if not exclude.is_absolute():
            exclude = target / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, "a", encoding="utf-8") as f:
            f.write("\n".join(HIDDEN) + "\n")
        try:
            rel = workspace.relative_to(root)
        except ValueError:
            rel = Path(".")
        return {"repo": str(root), "dir": str(target), "workdir": str(target / rel), "branch": branch, "base": base}

    def ensure(self, info: Dict[str, Any]) -> Dict[str, Any]:
        """The copy is there again after its folder was lost (its branch keeps what was committed)."""
        target = Path(info["dir"])
        if target.is_dir():
            return info
        root = Path(info["repo"])
        _run(["worktree", "prune"], root, check=False)
        _run(["worktree", "add", str(target), info["branch"]], root)
        return info

    # -- looking ----------------------------------------------------------------------------------------------

    def changes(self, info: Dict[str, Any]) -> List[Dict[str, str]]:
        """What the task changed since it started: [{"path", "status"}], status added, modified, deleted or renamed."""
        target = Path(info["dir"])
        _run(["add", "-A"], target)
        rows = _run(["diff", "--cached", "--name-status", "-z", "--no-renames", info["base"]], target).stdout.split("\0")
        out: List[Dict[str, str]] = []
        i = 0
        while i + 1 < len(rows):
            code, path = rows[i], rows[i + 1]
            i += 2
            if any(path.startswith(h) for h in HIDDEN):
                continue
            out.append({"path": path, "status": {"A": "added", "M": "modified", "D": "deleted"}.get(code[:1], "modified")})
        return out

    def diff(self, info: Dict[str, Any], path: str, limit: int = 60_000) -> str:
        target = Path(info["dir"])
        _run(["add", "-A"], target)
        text = _run(["diff", "--cached", "--no-color", "--no-ext-diff", info["base"], "--", path], target).stdout
        return text if len(text) <= limit else text[:limit] + "\n[... the rest of the diff is not shown]"

    # -- applying and throwing away ---------------------------------------------------------------------------

    def apply(self, info: Dict[str, Any], paths: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """Put the task's changes (all, or those of `paths`) in the person's folder as staged changes. A file they changed in a way that
        does not fit stops the whole apply and is named; nothing is half applied."""
        target, root = Path(info["dir"]), Path(info["repo"])
        found = [c["path"] for c in self.changes(info)]
        chosen = [p for p in found if paths is None or p in set(paths)]
        if not chosen:
            return {"applied": [], "base": info["base"]}
        tree = _run(["write-tree"], target).stdout.strip()
        patch = _run_bytes(["diff", "--binary", "--no-color", "--no-ext-diff", info["base"], tree, "--", *chosen], root).stdout
        if not patch.strip():
            return {"applied": [], "base": info["base"]}
        checked = _run_bytes(["apply", "--index", "--check", "--whitespace=nowarn", "-"], root, patch)
        if checked.returncode != 0:
            raise WorktreeError("These changes do not fit your folder as it is now (a file was changed there too): "
                                + (checked.stderr or checked.stdout).decode("utf-8", "replace").strip()[:400], "conflict")
        done = _run_bytes(["apply", "--index", "--whitespace=nowarn", "-"], root, patch)
        if done.returncode != 0:
            raise WorktreeError(done.stderr.decode("utf-8", "replace").strip()[:400] or "git apply failed", "git")
        if paths is None or len(chosen) == len(found):
            commit = _run([*IDENTITY, "commit-tree", tree, "-p", info["base"], "-m", "basalt: applied"], target).stdout.strip()
            info["base"] = commit                                    # from here on only what is new counts as a change
        else:
            info["base"] = self._rebase(info, tree, found, chosen)
        return {"applied": chosen, "base": info["base"]}

    def _rebase(self, info: Dict[str, Any], tree: str, found: List[str], chosen: List[str]) -> str:
        """After a partial apply the base is the old base with only the chosen files moved to the task's version."""
        target = Path(info["dir"])
        scratch = target.parent / (target.name + ".index")
        env = {**os.environ, "GIT_INDEX_FILE": str(scratch)}
        try:
            subprocess.run(["git", "read-tree", info["base"]], cwd=str(target), env=env, check=True, capture_output=True)
            for path in chosen:
                listing = _run(["ls-tree", tree, "--", path], target).stdout.strip()
                if listing:
                    meta, name = listing.split("\t", 1)
                    mode, _kind, sha = meta.split()
                    subprocess.run(["git", "update-index", "--add", "--cacheinfo", f"{mode},{sha},{name}"], cwd=str(target), env=env, check=True, capture_output=True)
                else:
                    subprocess.run(["git", "update-index", "--force-remove", "--", path], cwd=str(target), env=env, check=True, capture_output=True)
            new_tree = subprocess.run(["git", "write-tree"], cwd=str(target), env=env, check=True, capture_output=True, text=True).stdout.strip()
        finally:
            try:
                scratch.unlink()
            except OSError:
                pass
        return _run([*IDENTITY, "commit-tree", new_tree, "-p", info["base"], "-m", "basalt: applied some"], target).stdout.strip()

    def discard(self, info: Dict[str, Any]) -> None:
        """Remove the copy and its branch. Whatever was not applied is gone."""
        root, target = Path(info["repo"]), Path(info["dir"])
        if root.is_dir():
            _run(["worktree", "remove", "--force", str(target)], root, check=False)
            _run(["worktree", "prune"], root, check=False)
            _run(["branch", "-D", info["branch"]], root, check=False)
        shutil.rmtree(target, ignore_errors=True)
