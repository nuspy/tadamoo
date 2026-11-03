# llama-server (llama.cpp) in the Loader

llama.cpp's server runs beside Basalt's own engine, mostly to hold a small model that does the quick jobs of the technical session (`docs/harness.md`, *Where the quick jobs run*). It is another engine: it takes the GPU memory its model needs, so it is started by the person, never by itself unless *Start it with Basalt* is on.

Code: `serve/llamacpp.py` (the manager), `serve/llamacpp_api.py` (the routes), `serve/web/llamacpp.js` (Models > llama.cpp). Everything that touches the machine goes through `llamacpp.Host`, which tests replace.

## The program

Looked for, in this order: the path saved in the page, `TADAMOO_LLAMA_SERVER`, the folder an update installed into, `bin/llama-server` and `bin/llama.cpp/` of the install, `PATH`, a few usual folders. The first found is used; the others are listed. The version is the line `llama-server --version` prints, asked once per program.

## Running it

`POST /api/llamacpp/start` takes `model` (a `.gguf` that exists), `port` (1024 to 65535, default 8080), `ctx` (tokens per slot), `ngl` (layers on the GPU), `parallel` (slots), `alias` and `autostart`. It runs

    llama-server -m <model> --host 127.0.0.1 --port <port> -c <ctx x parallel> -ngl <ngl> -np <parallel> --alias <alias> --jinja

Nothing else can be added to the command line. It listens on loopback only, in the server's job object, so it ends with Basalt. The settings are kept in `machine.json` (`llamacpp`).

State comes from the process and `GET /health`: `starting` (503, loading), `ready` (200), `error` (the process exited: the end of its log is shown), `stopped`, `external` (something that was not started here answers on the port; it is shown but not stopped). A port in use is refused before anything starts.

`autostart` is off unless switched on; when on, `serve.server` starts the program with the saved settings at start-up, and a missing file or program is silent.

## Using it for the quick jobs

*Use for the quick jobs* (or `POST /api/llm/technical {"type": "llamacpp"}`) points the function `technical` at the running server with its alias and the context per slot. Nothing leaves the PC, so neither the consent nor the master switch for outside providers is asked.

## The update

`POST /api/llamacpp/update/check` reads the latest release list of `ggml-org/llama.cpp` from GitHub (the one request the module makes by itself, and only when the person presses the button) and answers a plan: release, files with their sizes, total, destination (`<user data>/llama.cpp/<release>`), what is installed, a `token`. Files are chosen for the system: on Windows the CUDA 12 build and its runtime, on Linux the Ubuntu build, on macOS the build for the CPU; every address must be on `github.com` or `githubusercontent.com` over https.

Nothing is downloaded by the check. `POST /api/llamacpp/update/apply {"token", "confirm": true}` downloads exactly that plan, and is refused when the confirmation is not `true`, when the token is not the current plan's, or while llama-server runs. Each file's size and, when the release gives one, its SHA-256 are checked; the archives are unpacked with a path check into a staging folder that replaces the destination only when all of it worked. A failure installs nothing. `current.json` in the update folder records the installed program, which becomes a candidate of the search; a saved path inside that folder moves to the new one, a path the person gave elsewhere stays.

The downloader is a parameter of `LlamaCpp.update_apply`, so tests download from a fake.
