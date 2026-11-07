"""Where a user's own TaDaMoo settings live, apart from the caches.

    user_data_dir()   $TADAMOO_DATA, else %APPDATA%\\TaDaMoo (Windows), ~/Library/Application Support/TaDaMoo
                      (macOS), $XDG_DATA_HOME/tadamoo (elsewhere; ~/.local/share/tadamoo without it)

It holds what the user chose and must not be lost when a cache is cleared: the load profiles and inference presets
(profiles.json), the models folders and the default server model (models.json), this PC's own settings from the
configuration wizard (machine.json) and each model's speed calibration (calibration/<model>.json). The desktop app
keeps its window state there too (desktop.json). What can be rebuilt stays in modelfile.cache_root(): the models scan,
the job logs, the tokenizer and vision files unpacked from a model, the engine's log.

Earlier versions kept the user's files in the cache folder. The first time this folder is used they are COPIED from
there (never moved or deleted), once: a marker file records that the copy was made."""
from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
import threading
from pathlib import Path

APP_DIR = "TaDaMoo"                       # the desktop app's folder: one place for everything of this user
MARKER = "migrated-from-cache.json"
SESSIONS_BASE_FILE = "sessions-base.json"
COPIED = ("profiles.json", "models.json")       # the user's files that earlier versions kept in the cache folder
_lock = threading.Lock()
_checked: set = set()


def user_data_dir() -> Path:
    return Path(default_dir(os.name, sys.platform, os.environ, str(Path.home())))


def default_dir(osname: str, platform: str, env, home: str) -> str:
    """The folder as a string for an operating system, its environment and the home folder."""
    if env.get("TADAMOO_DATA"):
        return env["TADAMOO_DATA"]
    if osname == "nt":
        return os.path.join(env.get("APPDATA") or os.path.join(home, "AppData", "Roaming"), APP_DIR)
    if platform == "darwin":
        return f"{home}/Library/Application Support/TaDaMoo"
    return f"{env.get('XDG_DATA_HOME') or home + '/.local/share'}/tadamoo"


def calibration_file(meta_hash: str) -> Path:
    """A model's speed calibrations, by the hash of its meta section (the cache folder's name for that model)."""
    return path("calibration") / f"{meta_hash}.json"


def path(name: str) -> Path:
    """A file or folder in the user data folder, after the one-off copy from the cache folder."""
    migrate()
    return user_data_dir() / name


def sessions_base() -> Path:
    """The folder whose `sessions/` holds every conversation's files: the one chosen in Settings, else `journal/` of the data folder."""
    chosen = chosen_sessions_base()
    return chosen if chosen is not None else path("journal")


def chosen_sessions_base() -> Path | None:
    """The folder chosen in Settings (kept in the default data folder, which is known before anything else is opened), or None."""
    try:
        value = json.loads((user_data_dir() / SESSIONS_BASE_FILE).read_text(encoding="utf-8")).get("path")
    except (OSError, ValueError, AttributeError):
        return None
    return Path(value) if isinstance(value, str) and value.strip() else None


def set_sessions_base(folder: Path | None) -> None:
    """Keep the chosen folder, or forget it with None; the file is replaced whole."""
    target = user_data_dir() / SESSIONS_BASE_FILE
    if folder is None:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"path": str(folder)}, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def migrate() -> list[str]:
    """Copy the user's files from the cache folder of earlier versions, once per data folder: profiles.json,
    models.json and every <model>/calibration.json; a file already in the data folder is never replaced. The names
    copied (empty when there was nothing to copy or the copy was made before)."""
    from serve.engine.modelfile import cache_root
    dest, src = user_data_dir(), cache_root()
    key = (str(dest), str(src))
    if key in _checked:
        return []
    with _lock:
        if key in _checked:
            return []
        copied = []
        try:
            if not (dest / MARKER).exists():
                copied = _copy(src, dest)
                dest.mkdir(parents=True, exist_ok=True)
                (dest / MARKER).write_text(json.dumps({
                    "from": str(src), "copied": copied,
                    "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}, indent=2) + "\n",
                    encoding="utf-8")
            _checked.add(key)
        except OSError as e:                         # read-only or missing: the old files stay where they are
            print(f"[server] warning: cannot copy the settings from {src} to {dest}: {e}", file=sys.stderr, flush=True)
        return copied


def _copy(src: Path, dest: Path) -> list[str]:
    if not src.is_dir() or os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dest)):
        return []
    copied = []
    for name in COPIED:
        if (src / name).is_file() and not (dest / name).exists():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / name, dest / name)
            copied.append(name)
    for sub in sorted(src.iterdir()):
        cal = sub / "calibration.json"
        if sub.is_dir() and cal.is_file():
            out = dest / "calibration" / f"{sub.name}.json"
            if not out.exists():
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cal, out)
                copied.append(f"calibration/{sub.name}.json")
    return copied
