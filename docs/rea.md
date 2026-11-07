# Reverse engineering with REA

The Agent tab has a switch, **Reverse engineering (REA)**, off by default. REA is the `rea-agents` add-on shipped with the setup (MIT, github.com/morluto/rea): a local toolkit with 229 MCP tools and one skill for taking apart native programs, Android apps, web pages, JavaScript/Electron bundles, WebAssembly, firmware and network captures. It runs on this PC with the add-on's own Node.js. Support for its components on Windows is experimental; what it does with the files you give it is your choice and your risk.

## What the switch does

On:

- adds the MCP server `rea` to TaDaMoo's own capability store (`<data>/capabilities/installed.json`): command = add-on `node` + `scripts/rea.mjs` + `mcp`, stdio. The environment carries the programs found (see below). Like anything imported from the GUI it is born approved; the tools sit behind the agent's capability search because there are more than 24.
- copies the skill `reverse-engineer-anything` (with its seven reference files) to `<data>/capabilities/skills/`. A marker file `.tadamoo-rea` records that TaDaMoo put it there.

Off removes exactly the record and the folder it added, nothing else. A capability or skill of the same name that is not TaDaMoo's is never touched, and the switch refuses to turn on over it.

The state is in `<data>/rea.json`. At server start, if it is on, the registration is made again (the add-on may have moved or been updated).

`rea setup` and `rea uninstall` are never run: they rewrite the configuration of other coding agents.

## Programs REA can use

The box lists them with what was found (path, version) or "missing". REA uses what exists and reports what does not; JavaScript and Electron analysis needs none.

| Program | Variable passed to REA | Ways to install it, in the order tried |
|---|---|---|
| Ghidra | `GHIDRA_INSTALL_DIR` | winget (needs the JDK) |
| Java JDK | `JAVA_HOME` | winget Microsoft OpenJDK 21, then Temurin 21 |
| JADX headless MCP JAR | `REA_JADX_MCP_JAR` | download of the jar REA audited, checked against the SHA-256 REA lists (needs the JDK) |
| Apktool | `REA_APKTOOL_COMMAND` | a launcher for a jar already on the PATH; else the GitHub release with a launcher (needs the JDK) |
| adb | `REA_ADB_PATH` | winget |
| Chrome-family browser | `REA_BROWSER_EXECUTABLE` | winget |
| tshark | `REA_TSHARK_COMMAND` | winget |
| WABT 1.0.42 | `REA_WABT_BIN_DIRECTORY` | GitHub release 1.0.42 (else the latest) |
| Unblob / Binwalk | `REA_BINWALK_COMMAND` | Linux only: pip there, not offered on Windows |
| pwntools | `REA_PWNTOOLS_PYTHON` | pip |
| Rizin | `REA_RIZIN_COMMAND` | winget, then the GitHub release |

Programs are found by their variable, then on the PATH (for a folder, by the program in it), then in the usual places and in `<data>/rea-tools/`. Hopper, IDA, JEB and other providers REA supports are not listed: set their variables in the system environment if you have them.

## Installers

Nothing is installed unless a button is pressed. Each missing program shows the steps that will run; **Install** does one program, **Install all** every missing program that exists for this system. Both run in the background and the page follows them:

- **Order.** What a program needs is installed first (Ghidra, JADX and Apktool need the JDK). If a program it needs cannot be installed, it is skipped and says why.
- **Ways.** Each program has one or more ways (winget, a download with a SHA-256 check, a GitHub release checked against the digest GitHub gives, pip, or a launcher for a jar already on the PATH). The first that leaves the program found wins; the next is tried when one fails.
- **Retry.** A way that fails because of the network (no route, a timeout, a server error, a rate limit) is tried again, up to three times with 5 and 20 seconds between. A failure of another kind (no such package, a hash that does not match, a build error) is not repeated.
- **Check.** After each way the PATH is read again from the system, so a program that an installer just added is found without restarting TaDaMoo, and the program must really be found for the install to count.
- **Linux only.** A program that exists only for Linux (Unblob/Binwalk) is marked "not for this system" on Windows and left out of Install all.
- **Safety.** The commands, addresses and hashes come from a fixed table on the server, never from the request. Archives that would write outside their folder are refused. Windows may ask to approve an installer. After an install the registered server gets the new path.

## API

All on the PC the server runs on only (loopback): `GET /api/rea` (status: add-on, node, MCP, skills, tools, and the installation in progress or last one), `POST /api/rea/enable`, `POST /api/rea/disable`, `POST /api/rea/install` with `{"tool": id}` or `{"all": true}` (it starts the job and answers at once with the plan). Code: `serve/rea_api.py`, `serve/rea_install.py`, `serve/web/rea.js`. Tests: `serve/tests/test_rea_api.py`, `serve/tests/test_rea_page.py`.
