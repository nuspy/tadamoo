"""Plugins that bring their own program: Python tools and a command line, read from the plugin's imported copy and only when they are used.

A plugin is self-contained: its program lives in its own folder, never in TaDaMoo's. The `plugin.json` of one that has a program has a `python` section:

    "python": {"path": "python",
               "tools": [{"name", "module", "register", "requires", "description", "parameters", "effect", "timeout"}],
               "cli": {"": "package.__main__:main", "investigate": "package.investigate:main"}}

`path` is the folder (inside the plugin) that holds the packages. Each tool is registered with the harness from the description and parameters written here, so nothing of
the plugin is imported when the harness starts; the first call imports `module` from the imported copy and calls its `register(registry, host)`, which registers the real
handler (the handler of the tool of this name is then used). A copy that was updated since is read again. `requires` names what the host must have now (`video`, `avatar`...);
a tool the host cannot back is not offered. The command line is `python -m tadamoo.plugin run NAME [command] ...`: the plugin's program runs from its copy, whatever the
working folder is. A plugin that is turned off offers nothing."""
from __future__ import annotations

import importlib
import json
import logging
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from tadamoo.importer.common import Manifest
from tadamoo.skillmanager.state import off_ids
from tadamoo.tools.registry import EffectClass

log = logging.getLogger("tadamoo.plugin")

MANIFESTS = ("plugin.json", ".claude-plugin/plugin.json")
EFFECTS = {e.value for e in EffectClass}
_LOCK = threading.RLock()
_LOADED: Dict[str, Tuple[str, Dict[str, Callable[..., Any]]]] = {}          # plugin name -> (hash of the copy it was read from, handlers made so far)


@dataclass
class Host:
    """What TaDaMoo gives a plugin's program: the sandbox of the conversation, the media settings, where data is kept, the model's endpoint, whether to stop, and what it can back now."""
    sandbox: Any = None
    media: Any = None
    data_dir: Optional[Path] = None
    url: str = ""
    key: Optional[str] = None
    cancelled: Optional[Callable[[], bool]] = None
    swap: str = "own"
    can: Set[str] = field(default_factory=set)


@dataclass
class Plugin:
    name: str
    folder: Path
    copy_hash: str
    manifest: Dict[str, Any]
    parts: List[str]

    @property
    def python(self) -> Dict[str, Any]:
        value = self.manifest.get("python")
        return value if isinstance(value, dict) else {}

    @property
    def path(self) -> Path:
        return self.folder / str(self.python.get("path") or "python")


def _manifest(folder: Path) -> Dict[str, Any]:
    for rel in MANIFESTS:
        try:
            data = json.loads((folder / rel).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def installed(cap_dir: Path) -> List[Plugin]:
    """The imported plugins that bring a program."""
    root = Path(cap_dir) / "imported"
    out = []
    for key, entry in sorted(Manifest(root / "manifest.json").load().items()):
        if entry.get("kind") != "plugin" or entry.get("parent"):
            continue
        name = str(entry.get("name") or "")
        folder = root / "plugins" / name
        data = _manifest(folder)
        if name and isinstance(data.get("python"), dict):
            out.append(Plugin(name, folder, str(entry.get("copy_hash") or ""), data, [p for p in entry.get("parts", []) if isinstance(p, str)]))
    return out


def find(cap_dir: Path, name: str) -> Optional[Plugin]:
    return next((p for p in installed(cap_dir) if p.name == name), None)


def enabled(cap_dir: Path, plugin: Plugin) -> bool:
    """On unless the person turned off one of the skills it brought (turning a plugin off turns those off)."""
    off = off_ids(cap_dir)
    return not any(part in off for part in plugin.parts if part.startswith("skill:"))


# -- loading, when it is used -------------------------------------------------------------------------------------

def _add_path(plugin: Plugin) -> None:
    path = str(plugin.path)
    if path not in sys.path:
        sys.path.insert(0, path)


def _purge(plugin: Plugin) -> None:
    """Forgets the modules read from a copy that has been replaced, so that the next call reads the new one."""
    root = plugin.path.resolve()
    for name, module in list(sys.modules.items()):
        where = getattr(module, "__file__", None)
        if where:
            try:
                if Path(where).resolve().is_relative_to(root):
                    del sys.modules[name]
            except (OSError, ValueError):
                continue
    importlib.invalidate_caches()


class _Recorder:
    """The registry a plugin's `register` is given: it keeps the handlers, the descriptions are the ones in plugin.json."""

    def __init__(self) -> None:
        self.handlers: Dict[str, Callable[..., Any]] = {}

    def register(self, name: str, description: str = "", parameters: Any = None, effect: Any = None, handler: Optional[Callable[..., Any]] = None, **_: Any) -> None:
        if handler is not None:
            self.handlers[name] = handler


def _resolve(cap_dir: Path, name: str, spec: Dict[str, Any], host: Host) -> Callable[..., Any]:
    plugin = find(cap_dir, name)
    if plugin is None or not enabled(cap_dir, plugin):
        raise RuntimeError(f"the plugin {name} is not installed or is turned off")
    with _LOCK:
        have = _LOADED.get(name)
        if have is None or have[0] != plugin.copy_hash:
            if have is not None:
                _purge(plugin)
            have = _LOADED[name] = (plugin.copy_hash, {})
        handlers = have[1]
        if spec["name"] not in handlers:
            _add_path(plugin)
            module = importlib.import_module(spec["module"])
            recorder = _Recorder()
            getattr(module, spec.get("register") or "register_tools")(recorder, host)
            handlers.update(recorder.handlers)
        if spec["name"] not in handlers:
            raise RuntimeError(f"the plugin {name} did not register the tool {spec['name']}")
        return handlers[spec["name"]]


def _lazy(cap_dir: Path, name: str, spec: Dict[str, Any], host: Host) -> Callable[..., Any]:
    def call(**arguments: Any) -> Any:
        try:
            handler = _resolve(cap_dir, name, spec, host)
        except (RuntimeError, ImportError, AttributeError) as exc:
            return {"status": "unavailable", "note": f"{type(exc).__name__}: {exc}"}
        return handler(**arguments)
    call.__name__ = spec["name"]
    return call


def _usable(spec: Any) -> bool:
    return (isinstance(spec, dict) and all(isinstance(spec.get(k), str) and spec[k] for k in ("name", "module", "description")) and isinstance(spec.get("parameters"), dict)
            and spec.get("effect", "reversible") in EFFECTS)


def register_tools(registry: Any, cap_dir: Path, host: Host) -> List[str]:
    """Offers the tools of the enabled plugins that the host can back, without importing anything. Returns their names."""
    done: List[str] = []
    for plugin in installed(cap_dir):
        if not enabled(cap_dir, plugin):
            continue
        for spec in plugin.python.get("tools", []):
            if not _usable(spec):
                log.warning("plugin %s: a tool in plugin.json is not usable (name, module, description, parameters and a known effect are needed)", plugin.name)
                continue
            if any(r not in host.can for r in (spec.get("requires") or [])) or registry.get(spec["name"]) is not None:
                continue
            extra = {"timeout": spec["timeout"]} if "timeout" in spec else {}
            registry.register(name=spec["name"], description=spec["description"], parameters=spec["parameters"], effect=EffectClass(spec.get("effect", "reversible")),
                              handler=_lazy(Path(cap_dir), plugin.name, spec, host), **extra)
            done.append(spec["name"])
    return done


# -- the command line ---------------------------------------------------------------------------------------------

def run_cli(cap_dir: Path, name: str, argv: List[str]) -> int:
    """Runs the plugin's command line from its copy: the first word picks one of the commands named in `cli` (else the one named ""), the rest are its arguments."""
    plugin = find(cap_dir, name)
    if plugin is None:
        print(f"the plugin {name} is not installed, or brings no program", file=sys.stderr)
        return 2
    cli = plugin.python.get("cli") if isinstance(plugin.python.get("cli"), dict) else {}
    command = argv[0] if argv and argv[0] in cli and argv[0] else ""
    target = cli.get(command)
    if not isinstance(target, str) or ":" not in target:
        print(f"the plugin {name} has no command line" + (f" for '{command}'" if command else ""), file=sys.stderr)
        return 2
    module, _, function = target.partition(":")
    _add_path(plugin)
    sys.argv[0] = " ".join(x for x in (f"{name}", command) if x)
    return int(getattr(importlib.import_module(module), function)(argv[1:] if command else argv) or 0)
