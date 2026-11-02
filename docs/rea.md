# Reverse engineering with REA

The Agent tab has a switch, **Reverse engineering (REA)**, off by default. REA is the `rea-agents` add-on shipped with the setup (MIT, github.com/morluto/rea): a local toolkit with 229 MCP tools and one skill for taking apart native programs, Android apps, web pages, JavaScript/Electron bundles, WebAssembly, firmware and network captures. It runs on this PC with the add-on's own Node.js. Support for its components on Windows is experimental; what it does with the files you give it is your choice and your risk.

## What the switch does

On:

- adds the MCP server `rea` to Basalt's own capability store (`<data>/capabilities/installed.json`): command = add-on `node` + `scripts/rea.mjs` + `mcp`, stdio. The environment carries the programs found (see below). Like anything imported from the GUI it is born approved; the tools sit behind the agent's capability search because there are more than 24.
- copies the skill `reverse-engineer-anything` (with its seven reference files) to `<data>/capabilities/skills/`. A marker file `.basalt-rea` records that Basalt put it there.

Off removes exactly the record and the folder it added, nothing else. A capability or skill of the same name that is not Basalt's is never touched, and the switch refuses to turn on over it.

The state is in `<data>/rea.json`. At server start, if it is on, the registration is made again (the add-on may have moved or been updated).

`rea setup` and `rea uninstall` are never run: they rewrite the configuration of other coding agents.

## Programs REA can use

The box lists them with what was found (path, version) or "missing". REA uses what exists and reports what does not; JavaScript and Electron analysis needs none.

| Program | Variable passed to REA | Install button |
|---|---|---|
| Ghidra | `GHIDRA_INSTALL_DIR` | winget |
| Java JDK | `JAVA_HOME` | winget |
| JADX headless MCP JAR | `REA_JADX_MCP_JAR` | by hand |
| Apktool | `REA_APKTOOL_COMMAND` | by hand |
| adb | `REA_ADB_PATH` | winget |
| Chrome-family browser | `REA_BROWSER_EXECUTABLE` | winget |
| tshark | `REA_TSHARK_COMMAND` | winget |
| WABT 1.0.42 | `REA_WABT_BIN_DIRECTORY` | by hand |
| Unblob / Binwalk | `REA_BINWALK_COMMAND` | pip |
| pwntools | `REA_PWNTOOLS_PYTHON` | pip |
| Rizin | `REA_RIZIN_COMMAND` | by hand |

Programs "by hand" can be put on the PATH or in `<data>/rea-tools/`. Hopper, IDA, JEB and other providers REA supports are not listed: set their variables in the system environment if you have them.

## Installers

Nothing is installed unless a button is pressed. Each missing program shows the exact command (a `winget install --id ...` or `python -m pip install --user ...` line); **Install** runs that command with no console window and shows its output under the program, **Install all missing** runs the same commands one after the other. The commands come from a fixed table on the server, never from the request. Windows may ask to approve an installer. After an install the registered server gets the new path.

## API

All on the PC the server runs on only (loopback): `GET /api/rea` (status: add-on, node, MCP, skills, tools), `POST /api/rea/enable`, `POST /api/rea/disable`, `POST /api/rea/install` with `{"tool": id}` or `{"all": true}`. Code: `serve/rea_api.py`, `serve/web/rea.js`. Tests: `serve/tests/test_rea_api.py`, `serve/tests/test_rea_page.py`.
