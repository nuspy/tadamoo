# Plugins that bring their own program

A plugin is self-contained: what it needs travels with it when it is imported (`docs/import.md`), and TaDaMoo's own folder holds none of it. A plugin whose `plugin.json` has a `python`
section brings Python tools and a command line:

```json
"python": {
  "path": "python",
  "tools": [{"name": "make_newscast", "module": "ai_newscast.tools", "register": "register_tools", "requires": ["video", "avatar"],
             "effect": "reversible", "timeout": null, "description": "...", "parameters": {"type": "object", "properties": {}}}],
  "cli": {"": "ai_newscast.__main__:main", "investigate": "ai_newscast.investigate:main"}
}
```

`path` is the folder of the plugin that holds its packages. Package names must be the plugin's own (`ai_newscast`, never `tadamoo`).

## When it is loaded

Not when the harness starts. The tool is offered to the model from the description and the parameters written in `plugin.json`; nothing of the plugin is imported. The first call imports
`module` from the imported copy, calls its `register(registry, host)` and uses the handler registered under the tool's name. A plugin used once a year costs nothing the rest of the year.

- A plugin the person turned off (Settings > Skills turns off the skills it brought) offers nothing.
- `requires` names what TaDaMoo must be able to back now (`video` and `image` when LTX Studio can make them, `avatar` when a talking head is set up); a tool it cannot back is not offered.
- A plugin updated or removed while the harness runs: the next call reads the new copy (the modules of the old one are dropped), or answers that the plugin is not there.
- `host` gives the program what TaDaMoo owns: `sandbox` (the conversation's working folder and its guards), `media` (the media settings), `data_dir`, `url` and `key` (the model's
  endpoint), `cancelled()` and `swap`.

## The command line

`python -m tadamoo.plugin run NAME [command] [arguments]` runs the plugin's program from its copy in the user data folder, so it works from any working folder. The first word picks one of the
commands of `cli` (`module:function`, called with the list of the remaining arguments); without a match the command named `""` runs. `python -m tadamoo.plugin list` says which plugins bring a
program and what they offer. A plan or a skill of the plugin uses this form, never a path.

## Code

`tadamoo/plugin/` (`__init__.py` the runtime, `__main__.py` the command line), wired in `tadamoo/stack.py` where the media tools are offered. Tests: `tests/test_plugin_runtime.py`.
