"""Plugins that bring their own program: tools registered from plugin.json without importing anything, the program read from the imported copy on the first call, a copy that was
updated read again, a plugin that is off or missing offers nothing, and the command line from any working folder."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tadamoo import plugin
from tadamoo.importer import Importer, Spaces
from tadamoo.planner import PlanStore
from tadamoo.skillmanager.state import set_off
from tadamoo.tools.registry import ToolRegistry

TOOL = {"name": "say_hello", "module": "hello_pkg.tools", "register": "register_tools", "requires": ["video"], "effect": "pure",
        "description": "Says hello to someone.", "parameters": {"type": "object", "properties": {"who": {"type": "string"}}}}


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_plugin(base: Path, greeting: str = "hello", **python) -> Path:
    folder = base / "hello"
    spec = {"name": "hello", "version": "1.0.0", "description": "Says hello.", "skills": ["skills/hello"],
            "python": {"path": "python", "tools": [TOOL], "cli": {"": "hello_pkg.cli:main", "shout": "hello_pkg.cli:shout"}, **python}}
    write(folder / "plugin.json", json.dumps(spec))
    write(folder / "skills" / "hello" / "SKILL.md", "---\nname: hello\ndescription: Says hello.\n---\n\nSay hello.\n")
    write(folder / "python" / "hello_pkg" / "__init__.py", "")
    write(folder / "python" / "hello_pkg" / "tools.py",
          f"LOADS = []\n\n\ndef register_tools(registry, host):\n    LOADS.append(1)\n    def say_hello(who='world'):\n        return {{'said': '{greeting} ' + who, 'url': host.url, 'loads': len(LOADS)}}\n"
          "    registry.register(name='say_hello', description='ignored: plugin.json has it', parameters={}, effect=None, handler=say_hello)\n")
    write(folder / "python" / "hello_pkg" / "cli.py", "def main(argv):\n    print('main ' + ' '.join(argv))\n    return 0\n\n\ndef shout(argv):\n    print('SHOUT ' + ' '.join(argv).upper())\n    return 3\n")
    return folder


@pytest.fixture
def world(tmp_path):
    spaces = Spaces.from_data(tmp_path / "data")
    imp = Importer(spaces, PlanStore(spaces.plans_dir))
    src = tmp_path / "src"
    folder = make_plugin(src)
    imp.import_("plugin", folder)
    yield imp, folder, spaces.cap_dir
    for name in [n for n in sys.modules if n.split(".")[0] == "hello_pkg"]:
        del sys.modules[name]
    plugin._LOADED.clear()
    sys.path[:] = [p for p in sys.path if "hello" not in p or "data" not in p]


def test_the_tool_is_offered_from_plugin_json_and_nothing_is_imported_until_it_is_called(world):
    imp, folder, cap = world
    registry = ToolRegistry()
    done = plugin.register_tools(registry, cap, plugin.Host(url="http://x", can={"video"}))
    assert done == ["say_hello"] and registry.get("say_hello").description == "Says hello to someone."
    assert "hello_pkg" not in sys.modules                                                     # the harness has started, the plugin is not loaded
    out = registry.get("say_hello").handler(who="Ada")
    assert out == {"said": "hello Ada", "url": "http://x", "loads": 1} and "hello_pkg.tools" in sys.modules
    assert registry.get("say_hello").handler(who="Bo")["loads"] == 1                          # read once


def test_a_tool_the_host_cannot_back_a_missing_plugin_and_a_plugin_that_is_off_offer_nothing(world):
    imp, folder, cap = world
    assert plugin.register_tools(ToolRegistry(), cap, plugin.Host(can=set())) == []            # it asks for video
    assert plugin.register_tools(ToolRegistry(), cap, plugin.Host(can={"video"})) == ["say_hello"]
    set_off(cap, ["skill:hello"], True)
    assert plugin.register_tools(ToolRegistry(), cap, plugin.Host(can={"video"})) == []
    set_off(cap, ["skill:hello"], False)
    registry = ToolRegistry()
    plugin.register_tools(registry, cap, plugin.Host(can={"video"}))
    imp.delete("plugin", "hello")
    assert registry.get("say_hello").handler(who="x")["status"] == "unavailable"             # removed after it was offered: told in words, not a crash


def test_an_updated_copy_is_read_again_on_the_next_call(world, tmp_path):
    imp, folder, cap = world
    registry = ToolRegistry()
    plugin.register_tools(registry, cap, plugin.Host(can={"video"}))
    assert registry.get("say_hello").handler(who="Ada")["said"] == "hello Ada"
    make_plugin(tmp_path / "src", greeting="ciao")
    imp.reimport("plugin", "hello", confirm=True, overwrite_local=True)
    assert registry.get("say_hello").handler(who="Ada") == {"said": "ciao Ada", "url": "", "loads": 1}


def test_a_tool_that_is_not_usable_is_skipped_and_the_others_are_offered(world, tmp_path):
    imp, folder, cap = world
    manifest = json.loads((cap / "imported" / "plugins" / "hello" / "plugin.json").read_text())
    manifest["python"]["tools"] = [{"name": "broken"}, {**TOOL, "effect": "nonsense", "name": "bad_effect"}, TOOL]
    (cap / "imported" / "plugins" / "hello" / "plugin.json").write_text(json.dumps(manifest))
    assert plugin.register_tools(ToolRegistry(), cap, plugin.Host(can={"video"})) == ["say_hello"]


def run_cli(cap, *args, cwd):
    return subprocess.run([sys.executable, "-m", "tadamoo.plugin", "--data-dir", str(cap.parent), *args], cwd=cwd, capture_output=True, text=True,
                          env={**__import__("os").environ, "PYTHONPATH": str(Path(plugin.__file__).resolve().parents[2])})


def test_the_command_line_runs_from_the_copy_whatever_the_working_folder_is(world, tmp_path):
    imp, folder, cap = world
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    ok = run_cli(cap, "run", "hello", "a", "b", cwd=elsewhere)
    assert ok.returncode == 0 and ok.stdout.strip() == "main a b", ok.stderr
    shout = run_cli(cap, "run", "hello", "shout", "x", cwd=elsewhere)
    assert shout.returncode == 3 and shout.stdout.strip() == "SHOUT X"
    listed = json.loads(run_cli(cap, "list", cwd=elsewhere).stdout)
    assert listed == [{"name": "hello", "on": True, "folder": str(cap / "imported" / "plugins" / "hello"), "tools": ["say_hello"], "commands": ["", "shout"]}]
    missing = run_cli(cap, "run", "nobody", cwd=elsewhere)
    assert missing.returncode == 2 and "not installed" in missing.stderr
