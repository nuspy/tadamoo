# Computer control

The agent can look at the screen of this PC and work its mouse and keyboard (`tadamoo/computer/`). It is for what has no API or command: a program with only a window, a setting that exists
only in a dialog. It is **slower and less sure than a script**: it works through what is on the screen, the screen can change between a picture and the next click, and an interface it does
not recognise can mislead it. The tool descriptions say this to the model, and tell it to say so, and look again, instead of guessing.

It is **off until the person turns it on**: Wallet > Computer control (or `POST /api/computer`, from this PC only). Nothing else changes for a conversation that has it off.

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

## The setting

- `computer.json` in the user's data folder: `{"enabled": false, "monitor": 0}`.
- `GET /api/computer` (any client with the key): the setting, `supported`, the `reason` when not, the `monitors`. `POST /api/computer {enabled, monitor}`: **from this PC only** (it lets an agent act on the
  machine), what is left out stays.
- A conversation started with it on gets `--computer --computer-prefs PATH` (`serve/agent_sessions.py`); a question-only conversation gets nothing. The system prompt's list of what the agent does not
  have (`awareness.py`) drops "no computer control" when it is on.

## Tests

`tests/test_computer.py` runs the tools, the session and the stops against a backend that only records what it is asked (two screens, windows, a tree): no input is sent and no screen is read. It also
checks the Windows structure sizes and key tables, that `uia_tree.ps1` parses, and the X11 commands through a fake runner. `serve/tests/test_api_computer.py` is the API;
`serve/tests/test_computer_page.py` is the Wallet section in a headless browser at 360x640, 390x844 and 1280x800 in both languages. The real thing (Notepad, typing, a key, a picture, the tree, a
second screen, the corner) is in `tests/QUEUE_daytime.md`.
