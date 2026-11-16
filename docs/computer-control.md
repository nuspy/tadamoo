# Computer control

The agent can look at the screen of this PC and work its mouse and keyboard (`tadamoo/computer/`). It is for what has no API or command: a program with only a window, a setting that exists
only in a dialog. It is **slower and less sure than a script**: it works through what is on the screen, the screen can change between a picture and the next click, and an interface it does
not recognise can mislead it. The tool descriptions say this to the model, and tell it to say so, and look again, instead of guessing.

It is **on by default** (the permission levels decide what runs, as for every tool) and is turned off in Wallet > Computer control (or `POST /api/computer`, from this PC only); a conversation with it off has none of the tools.

## The tools

| Tool | Effect class | What it does |
|---|---|---|
| `computer_screenshot` | reversible | a picture of the screen the agent works on, or of one window (`window`), or one screen (`monitor`, only when the agent has all screens). Scaled to at most 1568 px on the long side, kept as a file of the conversation, the card and path returned (the model looks at it with `analyze_image`, as for the browser's pictures) |
| `computer_list_windows` | pure | number, title, program, size and place of each window on the agent's screen; the one in front is marked |
| `computer_focus_window` | irreversible | brings a window to the front (restores it if minimized) |
| `computer_ui_tree` | pure | the accessibility tree of a window (the one in front by default): numbered elements with role, name, rectangle, value. For a model with no vision (the local engine) and often more exact than a picture |
| `computer_move`, `computer_click`, `computer_drag`, `computer_scroll` | irreversible | mouse: by `x`,`y` of the last picture or by `element` number; left, right, middle, double; drag by points or elements; wheel notches in four directions |
| `computer_type_text` | irreversible | types text at the keyboard focus (or after a click on the place given); `{{vault:ENTRY.FIELD}}` and `{{secret:NAME}}` work as in the browser forms |
| `computer_key` | irreversible | a key or combination: `Enter`, `Ctrl+C`, `Alt+Tab`, `Win+D`, `F5`...; `times` repeats |
| `computer_wait` | pure | waits up to 30 seconds |

Every action takes `screenshot: true` to also keep a picture of the screen it led to (when the conversation can keep files).

**Coordinates.** The model gives positions in pixels of the *last picture*; the session turns them into real pixels (`Computer.view`). A click needs a picture, or the numbers of a `computer_ui_tree`, first.
An element number stands for the centre of its rectangle and **ends** after a click, key, typing, scroll or focus (the screen has changed; read the tree again). A picture of a window gives
coordinates relative to that window.

## Which screen

`monitor` in `computer.json` (0: all screens, 1, 2... one screen, numbered from the left) says which screen the agent works on, so it can work on a second screen while the person works on the
first. Pictures, window lists, trees, and every click, drag and scroll stay inside it; with a screen chosen the agent cannot ask for another one. The file is read at **each call**, so a change
applies at once, also in a conversation already running. Wallet > Computer control shows the screens of this PC; a screen that is not there is refused.

## Stopping it

- **Stop of the turn.** The person's Stop ends a running call at its next step (a long scroll, a typed text, a drag, a wait) and the agent loop skips the calls queued behind it, as for a shell command
  (`tadamoo.sandbox.cancelled`). A drag lets go of the button, a key combination lets go of its keys, however it ends.
- **The mouse in a corner.** Before every step the session looks where the pointer is. If it is in a corner of the screens (within 4 px; only corners the pointer can really be pushed into, not the
  seam between two screens), every `computer_*` call refuses for 20 seconds after the pointer was last seen there, with a message that tells the model to stop, tell the person and wait. The agent
  itself never aims at a corner: a target there is refused. The stop is recorded (`corner_stop`).

## Permissions, secrets, record

No new rules: the tools use the ones the browser tools use.

- **Levels.** The effect classes above put each tool where the permission levels already put it (Manual asks before an irreversible one; Auto does not). `group:computer` covers the family in allow and deny
  rules, and so do patterns such as `computer_click` (`GROUPS` in `tadamoo/permissions.py`). The history knows their effects (`KNOWN_EFFECTS` in `tadamoo/rewind.py`).
- **Untrusted.** Every tool's result is marked untrusted (a window can show anything, a window title is text from outside); the Rule of Two looks at the calls that act (`outward`: click, drag, type, key).
- **The vault.** `computer_type_text` resolves the placeholders like the browser's `type`, but a program has no site: an entry that names sites is refused ("may only be sent to ..."); an entry that names
  none is typed, and never shown to the model or recorded. After a value from the vault was typed, a picture of the screen could show it: at the Auto level it is taken, otherwise the person is asked
  (or an allow rule `computer_screenshot` covers it), as for the browser's screenshot. Window titles and element names are masked like any result.
- **The record.** What the tools do goes in the audit ledger under the actor `computer`: `screenshot`, `ui_tree`, `focus_window`, `click`, `drag`, `scroll`, `type_text` (the number of characters
  and whether it came from the vault, never the text), `key`, `corner_stop`, `error`, each with the program in front.

## Systems

| System | Input | Pictures | Windows | Accessibility tree |
|---|---|---|---|---|
| **Windows** (`tadamoo/computer/windows.py`) | `SendInput` through `ctypes` (absolute moves on the virtual screen, scan codes, Unicode text) | GDI scaled by the system, PNG by `png.py` | `user32` and DWM (cloaked and tool windows left out) | UI Automation through PowerShell and the .NET assemblies Windows ships (`uia_tree.ps1`) |
| **Linux with X11** (`x11.py`) | `xdotool` | ImageMagick `import`, or `scrot` with Pillow | `xdotool` | none: pictures only |
| anything else, Wayland, no display | not supported: every call answers with the reason | | | |

**Nothing is installed or required**: Windows uses only the standard library and PowerShell; on Linux each program is looked for when used and the error names what to install (`apt install xdotool
imagemagick`). `requirements.txt` and the Linux installer are untouched. A program running as administrator ignores input from one that is not; the error says so. The pictures and tree of
a window that draws itself (a game, a canvas) can be empty: then the model works from a picture.

## Backend: the system's own or agent-desktop

`backend` in `computer.json` (and Wallet > Computer control) is `auto` (default), `agent-desktop` or `native`. `auto` takes agent-desktop when it is found: in `<data>/tools/agent-desktop/` (a release the person took), shipped with TaDaMoo (`tools/vendored/agent-desktop/`, built from `third_party/`: [vendored-tools.md](vendored-tools.md)) or on `PATH`,
and the system's own backend (above) otherwise; `agent-desktop` asked for by name and not installed says so instead of falling back. It applies to conversations started from then on.

**agent-desktop** ([vercel-labs/agent-desktop](https://github.com/vercel-labs/agent-desktop), Apache-2.0) is a separate program run one command at a time (`tadamoo/computer/agent_desktop.py`); every command
prints one JSON envelope (`ok`, `data`, or `error` with `code` and `disposition.retry`), read here as such. It reads the accessibility tree (UI Automation on Windows, AT-SPI on Linux, AX on macOS) and acts on an
element by its ref **without moving the mouse or taking the focus**. The `computer_*` tools, their rules (screen, Stop, corner, vault, record) and the element numbers are unchanged: the session maps each number
to the ref (`@snapshot:eN`), and `computer_ui_tree` lists elements with a rectangle on the agent's screen.

| | native | agent-desktop |
|---|---|---|
| Click, set a value, scroll an element | the real pointer, on the rectangle's centre | by ref, no pointer, no focus, a window behind another works |
| Right and double click, drag, typing (Windows) | the real pointer and keyboard | the same (`--headed`, only where the program says it is needed); elsewhere tried without first |
| Position clicks, keys | real input | real input through the program (`mouse-click --xy`, `press`) |
| Tree | Windows only, through PowerShell | Windows, Linux (AT-SPI, Ubuntu 24.04+), macOS; refs; says when a tree is partial |
| Pictures, screens, pointer position (corner stop) | the system's own | the system's own backend when it can (a picture of any area, scaled), else the program's (a whole screen, full size; no corner stop) |
| Clipboard tools | none | `computer_clipboard_get`, `computer_clipboard_set` (never a vault value) |
| Install | nothing | a download of about 2 MB (a click) |

**When to use which.** agent-desktop for programs that expose their elements (Windows apps, Explorer, Office, settings dialogs): more exact, quieter, and it does not disturb the person. The native backend where
there is no accessibility tree to read (a game, a canvas, a remote desktop window, a web page whose browser does not expose it) or when nothing may be installed: the model then works from pictures and positions.
The tools tell the model when a tree has no elements to act on.

**How it is used.** Text is sent to the program on its standard input (`--stdin`), never in its arguments, so a vault value is not in the list of processes. Real-input commands are not cut short by Stop (a button or
key left pressed would be worse); a read is. A dangerous shortcut (Alt+F4, Win+L, Alt+Tab, Win+R...) is refused by the program and is never forced (`--force` is not used). An error says what to do and whether
repeating is safe (`disposition.retry`): nothing was sent, or it may already have happened, so look at the screen first. The program's own state (snapshots, which can hold private screen text) is in
`<data>/tools/agent-desktop/state/`. `launch` (starting programs) is not offered: it would start any program outside the shell's deny list.

**Updating.** agent-desktop is shipped with the installers; a newer release is taken with Wallet > Computer control > Update agent-desktop, or `python -m tadamoo.computer install` (`status` and `path` too; `--dir`, `--tag`), whose copy is used before the shipped one. Only on that click or command: it reads the latest release from
`api.github.com`, downloads the archive for this system from `github.com/vercel-labs/agent-desktop/releases/download` (anything else is refused), checks its SHA-256 against the release's digest or `checksums.txt`
(a release with no checksum is installed with that said), unpacks plain files only, replaces the old files and keeps `state/`. The page shows the version and `agent-desktop status` (accessibility access).
`gh attestation verify <archive> --repo vercel-labs/agent-desktop` checks where an archive came from; the program does not do it. On Windows the program is unsigned: SmartScreen or an antivirus may warn, and Smart
App Control can block it (then the native backend is the way). Licence line: `THIRD_PARTY_LICENSES`.

**What was checked on a real PC** (Windows 11, one 3840x2160 screen at 150%, agent-desktop 1.3.1, read-only: version, status, list-displays, list-apps, list-windows and snapshots of windows already open; no click, no
text, no picture, no program started):

- The download (1.8 MB archive, one 4 MB program, no helper on Windows) matched the release's digest and `checksums.txt`; `status` reported accessibility granted and the surfaces `window, focused, sheet, menu,
  start-menu, taskbar, system-tray, system-tray-overflow, action-center`.
- **Scaling.** `list-displays` says `3840x2160, scale 1.5`: all bounds (displays, windows, elements) are **physical pixels**, the same as this system's own backend, so no conversion is needed. A window's root
  rectangle equals the one in `list-windows`. Elements of a long list can lie outside the window (virtualised rows): they are marked `offscreen` and left out.
- **Explorer:** 294 refs in 0.4 s, complete. **A Chromium browser** (Opera, full screen): 1268 refs in 1.9 s, complete. **Chrome in this state:** 149 refs, only the browser's own controls; its six page `document`
  nodes held no children, the same in three fresh snapshots and with a 15 s budget (the program's notes say a first read is thin and a covered window can keep it so; Opera was on top of Chrome here, which is not
  established as the cause). So the tree of a Chromium page is not always there: the tool says when no element offers an action.
- **Taskbar surface:** four unnamed groups and no ref: not useful here.
- **A gotcha found.** The program refuses a state folder it did not create ("private file DACL grants an untrusted principal"): TaDaMoo gives it the path and lets it make the folder.
- **Not tried:** every action (click, type, press, drag, scroll, clipboard, focus): in `tests/QUEUE_daytime.md`.

## The setting

- `computer.json` in the user's data folder: `{"enabled": false, "monitor": 0, "backend": "auto"}`.
- `GET /api/computer` (any client with the key): the setting, `supported`, the `reason` when not, the `monitors`. `POST /api/computer {enabled, monitor, backend}` and `POST /api/computer/install`: **from this PC only** (it lets an agent act on the
  machine), what is left out stays.
- A conversation started with it on gets `--computer --computer-prefs PATH` (`serve/agent_sessions.py`); a question-only conversation gets nothing. The system prompt's list of what the agent does not
  have (`awareness.py`) drops "no computer control" when it is on.

## Tests

`tests/test_computer.py` runs the tools, the session and the stops against a backend that only records what it is asked (two screens, windows, a tree): no input is sent and no screen is read. It also
checks the Windows structure sizes and key tables, that `uia_tree.ps1` parses, and the X11 commands through a fake runner. `serve/tests/test_api_computer.py` is the API;
`serve/tests/test_computer_page.py` is the Wallet section in a headless browser at 360x640, 390x844 and 1280x800 in both languages. The real thing (Notepad, typing, a key, a picture, the tree, a
second screen, the corner) is in `tests/QUEUE_daytime.md`.
