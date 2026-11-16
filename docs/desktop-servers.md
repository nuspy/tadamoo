# The desktop app and the servers it shows

The desktop window (`tools/windows/desktop`) shows the page of one server. That is **this PC's** server (the one the app starts, with the Loader and the engine) or a **remote** one from the list.

## The list (tray icon > Servers…)

Each entry has a name, an address and a key. The address is `http://name[:port]`, `https://name[:port]` or just `name[:port]` (taken as http; a bare name without a port is TaDaMoo's own port, 7232; a scheme
without a port is that scheme's port, so `https://hrns.tadamoo.com` is port 443). No path, no query. The list is kept per user in `servers.json` beside `desktop.json`; the keys are protected with the
user's Windows account (DPAPI). The last server shown is taken up again at the next start (`--server NAME` chooses one).

- **Prova / Test**: asks `/health` and `/api/server-key`, and says in words whether the address answers, whether it is a TaDaMoo server, whether the Host rule lets this name through (and what to do if not) and
  whether the key is accepted. It also says when the server has no engine.
- **Edit** changes the name, the address and the key (it can be shown), and tests before saving.
- **Change the key on the server** asks for the current key and the new one (twice) and posts `/api/server-key` ([deploy.md](deploy.md)); the entry keeps the new key. The page's Settings > Network does the same
  and tells the window (`serverKey` message), so the entry follows whichever way the key was changed.

A remote server opens in the window with its key already given to the page (a script that runs before the page: `sessionStorage` key and `window.__tadamooRemote`). A remote server has **no Loader tab**, and the
tray items for this PC's server (reload with other settings, the server alone, the automatic load) are off; "Turn off" closes the window and leaves the server running. If WebView2 is missing the page opens in the
browser and asks for the key there.

## The GUI alone

`--gui-only`, or a file `gui-only.flag` next to the program, starts only the window: no repository, no local server, no engine, no voice service, no administrator question. It asks at the start which server
to use (the list, "Add…" when it is empty). The Windows installer of this variant is described in [windows-gui-only.md](windows-gui-only.md).

Tests: `tests/test_desktop_servers.py` runs `tools/windows/desktop_tests` (the address rules, the list on disk with the key protected, the connection test and the change of the key against a real server with made-up
keys). The windows themselves are tried by hand (`tests/QUEUE_daytime.md`).
