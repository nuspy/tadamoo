# The GUI on macOS (prepared, not built)

The desktop app is a window on the page the server serves (the web interface), plus the start of a local server. On macOS only the first part is wanted: a window on a server, local or remote.
Nothing is built or released for macOS now: whoever uses a Mac compiles from this repository, and the release comes at the end.

What the shell needs, so that it can be written without touching the server:

- a list of servers (name, address, port, key if any) and a button to try one; the server's own page opens in the window;
- the page already uses relative addresses, so it works against any address the window opens;
- the server's host rule applies: a remote server is reached by an IP address, or a name listed in its `allowed-hosts`;
- a choice of shell, to settle when the work starts: Tauri (small, Rust), Avalonia (C#, like the Windows app) or Electron.
