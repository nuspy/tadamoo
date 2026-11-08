# Desktop app and the voice service

The desktop app (`tools/windows/desktop/`, `TaDaMoo.exe`) owns the voice service process. Code: `VoiceService.cs`;
it is wired into `MainForm.cs` (start in `Begin`, tray line, stop on exit).

## What the app does

- Starts the service when the app opens, before the web server and before any model. The start does not wait for
  the service to be ready, so the server and the Loader are never delayed by it.
- Runs it as `<python> -m voice.service --settings <voice.json> --host 127.0.0.1 --port <P> --token-file <F>
  --info-file <I>`, with the repository root (the folder that contains the `voice` package) as working directory.
- Puts it in a Windows job object of its own (kill on close, the handle belongs to the app). It is not in the
  server's job, so a server restart (exit code 75, a new model) does not touch it. It ends when the app ends, even
  if the app is killed, and on session end.
- Chooses a free local port at each start (never 8080, never the web server's port) and passes it with `--port`.
- Reads the first stdout line `{"ready": true, "port": P, "pid": N}` to know the service is listening. All
  stdout and stderr go to `logs\voice-service.log` (rotated at 1 MB).
- Does not start a second copy: if `/health` answers on the port in the info file, that service is left as it is
  (the app does not stop a service it did not start).
- Shows one disabled line in the tray menu: `Voice service: running`, `starting` or `stopped`.
- No dialog and no other UI. A later "Voice functions" dialog and the tray can call `Start`, `Stop` and `Restart`.

## Where it looks for Python

In this order; the first one found is used.

1. `voice\venv\Scripts\python.exe` in the repository root, then next to the executable.
2. The file named by the environment variable `TADAMOO_VOICE_PYTHON`.
3. `py -3.10` (the launcher on PATH, only if that version runs).
4. `python.exe` on PATH (the Microsoft Store stub is skipped), only if it runs and is 3.10 or newer.

If none is found the service is not started: one line goes to the log and nothing is shown.

## Files

All in the user data folder (`%TADAMOO_DATA%`, else `%APPDATA%\TaDaMoo`):

- `voice.json`: the voice settings, passed to the service; the service owns its content.
- `voice-service.token`: 32 random bytes in hex, created by the app if missing or empty. Created with an access
  list for the current user only (no inherited permissions). An existing token is kept.
- `voice-service.json`: written by the service when it is listening (`port`, `pid`). The app deletes a stale one
  before it starts a new service.

## Restart rule

If the service ends while the app did not ask for it, the app starts it again after 2 seconds, at most 3 times
within 2 minutes. At the next crash inside that window it stops trying and logs it. `Start` and `Restart` from
code clear the count. `Stop` ends the whole job (the service and its children) and does not restart.

## Test runs

With `--test-mock` the service is not started, unless `TADAMOO_VOICE_IN_TEST=1` is set.

## What was not run

The application itself was never started, and no voice service was started: this part was only compiled
(`dotnet build -c Debug`, 0 errors). The job object, the token access list, the restart rule, the tray line and the
Python search are untested at run time.
