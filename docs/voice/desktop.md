# Voice desktop layer

Dictation, the clipboard history and the status card need things only the desktop can give: global hotkeys, the clipboard, a paste into
the focused app and a small always-on-top window. They live in `voice/callbridge/desktop/` behind four interfaces, so the dictation and
history code (`dictation.py`, `clipboard_history.py`) knows no operating system. All of it runs in the user's interactive session,
inside the voice service process.

## Interfaces (`desktop/base.py`)

| Interface | What it does |
|---|---|
| `KeyHook` | Global hotkeys with their own thread. `Binding(mods, key, on_press, on_release, exact, repeat, suppress)`; the callbacks run in a worker, in order. |
| `Clipboard` | `sequence()` change counter, `read()` / `write_text()` / `write_image()` with an exclusion marker, `send_copy()` / `send_paste()` with the keys the focused app needs, `modifiers_down()`. |
| `ForegroundApp` | `identity()` (window class, program) and `is_terminal()`. |
| `Overlay` | The status and clipboard-preview card: `set_dictation()`, `show_clipboard()`, `post()`, `mainloop()` on the main thread. |

`Desktop` bundles them: `platform()` returns the one for this process (created once); `available()` says whether it works and why not.
Hotkeys are neutral names (`alt+space`, `ctrl+alt+up`; keys `a-z`, `0-9`, `f1-f12`, `space`, `tab`, `enter`, `esc`, arrows),
parsed by `parse_hotkey`; each platform maps them to its own key codes. The hotkeys come from the voice config (`dictation_hotkey`).

## Platforms

| System | Implementation | Files |
|---|---|---|
| Windows | The original code behind the interfaces: a low-level keyboard hook that suppresses the combination, Win32 clipboard with the standard exclusion formats, `keybd_event` paste (Ctrl+V, Shift+Insert in terminals), Tk overlay with a no-focus window. | `windows.py`, `win_keyhook.py`, `win_clipboard.py` |
| Linux, X11 session | Hotkeys with `pynput`; clipboard with `xclip` (text and PNG) or `xsel` (text only); paste with `xdotool key --clearmodifiers`, else `pynput`; focused window with `xprop` (`xdotool getactivewindow` as a fallback); held modifiers from the X server key map (`python-xlib`, a `pynput` dependency); Tk overlay with plain symbols. | `x11.py`, `x11_keys.py`, `x11_clipboard.py`, `x11_tools.py` |
| Linux, Wayland | Not supported. | `unsupported.py` |
| macOS, other | Not supported. | `unsupported.py` |

The unsupported desktop answers `available()` with the reason and raises `DesktopUnsupported` from every hook, clipboard and foreground
call: "global shortcuts and paste need X11; Wayland does not allow them" (Wayland), or the macOS and the no-display messages.
Dictation and the history then stay off and say why (`status()["state"] == "unsupported"`); the overlay is the exception, a no-op card,
because the service needs its main-thread loop. Wayland is detected from `XDG_SESSION_TYPE=wayland`, or `WAYLAND_DISPLAY` without
`XDG_SESSION_TYPE=x11`; a session with no `DISPLAY` is unsupported too.

## Where X11 differs from Windows

- Keys are not suppressed (`Desktop.can_suppress` is false): `pynput` can only grab the whole keyboard. The focused app and the window
  manager still see the combination, so a hotkey the manager uses (Alt+Space often opens its window menu) must be changed in the config.
- Terminals paste with Ctrl+Shift+V and copy with Ctrl+Shift+C: Shift+Insert pastes the PRIMARY selection there, not the clipboard.
  Terminals known: GNOME Terminal, Konsole, Xfce Terminal, Terminator, Tilix, kitty, Alacritty, WezTerm, foot, st and similar. xterm
  and editors with a built-in terminal are not in the list and get Ctrl+V.
- X11 has no clipboard change counter: `sequence()` fingerprints the content, so copying the same text twice is not seen as a change.
- No standard exclusion marker can be written next to the text with these tools. The marker of password managers
  (`x-kde-passwordManagerHint`) is honoured on read; content this process wrote as excluded is remembered and read back as excluded,
  but a third-party clipboard manager still sees it.
- The history folder is `~/.local/share/callAPIcall/clipboard` (or under `XDG_DATA_HOME`); on Windows `%LOCALAPPDATA%\callAPIcall\clipboard`.
- A held key can reach `pynput` as release and press pairs; a release counts only if no press of the key follows within 60 ms.

## What is proven

- Windows: the original code is unchanged in behaviour (a Binding with `suppress` true behaves as before); the 90 imported tests pass and
  the Windows adapter, the hook procedure (called by hand, no hook installed) and the clipboard delegation are tested with fakes. A live
  hook, clipboard and paste have not been run since the move.
- X11: written, not proven on a real X11 desktop. Tests (`voice/tests/test_desktop_*.py`) fake the subprocess and `pynput` layers and check
  the command lines, the key matching, the terminal key choice, the selection by session type and the Unsupported messages.
- Not covered anywhere: dictation with a real microphone, a real hotkey press, the Tk overlay on screen.
- `ptt.py` (push-to-talk) still uses `pynput` directly and a Windows call for the Ctrl state; it is not moved behind these interfaces.
