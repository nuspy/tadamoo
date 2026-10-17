# Basalt on Windows

Basalt builds and runs natively on Windows 10/11 x64 with an NVIDIA Blackwell GPU (compute capability 12.0) under
the WDDM driver.  This page lists what is needed, the exact build commands, and what differs from Linux.

## What is needed

| Tool | Version used | Notes |
|---|---|---|
| Visual Studio (Community is enough) | 2026 with the **v143 (VS 2022) C++ x64 build tools** | `cl` 19.44 (toolset 14.44) |
| CUDA Toolkit | 13.1 | 13.2.0 / 13.2.1 miscompile sm_120 kernels: do not use them |
| CMake | 3.24 or newer (4.2 used) | |
| Ninja | 1.11 or newer | |
| Python | 3.10 or newer with numpy | only for the server and the i-quant test fixtures |

nvcc 13.1 accepts MSVC 19.3x/19.4x (the VS 2022 toolsets) as its host compiler.  The VS 2026 toolset (14.5x, cl
19.5x) is newer than nvcc knows and would need `-allow-unsupported-compiler`; instead the whole engine (C, C++
and the CUDA host code) is built with the v143 toolset that VS 2026 installs beside its own, so every object
comes from the same compiler.

## Build

From PowerShell in the source tree:

```powershell
# the MSVC x64 environment (cl, link, the Windows SDK) for this session, toolset 14.44
. .\tools\windows\vsenv.ps1

cmake -S . -B build-win -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_ARCHITECTURES=120 `
      -DCMAKE_C_COMPILER=cl -DCMAKE_CXX_COMPILER=cl
cmake --build build-win --parallel 4
```

The engine is `bin\basalt.exe`, the benchmark `bin\basalt-bench.exe` and the image encoder sidecar
`bin\basalt-vision.exe`.  `-DBUILD_TESTING=ON` adds the tests (see Tests below).

**`CL` and `_CL_` must not be set.**  They add options to every `cl` call behind CMake's back.  A user-wide
`_CL_=/std:c++20` breaks the C sources (ggml: "`/std:c11` and `/std:c++20` are incompatible"); in Strata the same
variable made ggml's SIMD feature probes fail, CMake cached "no AVX2", and ggml-cpu was silently built as scalar
code.  `vsenv.ps1` clears both for the session, the configure stops with an error when it sees either, and every
AVX2 / AVX-512 source checks at compile time that its `/arch` flag reached it (`#error` otherwise).  If a build
directory was ever configured with them set, configure it again from a clean shell (basalt itself runs no SIMD
feature probes: its instruction sets are the explicit per-file flags below).

On a machine that runs other heavy work, build at low priority and low parallelism, e.g.
`(Get-Process -Id $PID).PriorityClass = 'BelowNormal'` before `cmake --build ... --parallel 4`; nvcc uses 1-3 GB of
memory per job.  Check the committed memory (`Get-Counter '\Memory\% Committed Bytes In Use'`) and that no other
build runs (`Get-Process nvcc,cicc,ptxas,cl`) first.

## What the Windows build does differently

The Linux code paths are unchanged; the Windows ones sit behind `_WIN32` / `MSVC`.

* **Compiler flags** (`CMakeLists.txt`): `/utf-8 /bigobj /Zc:__cplusplus /Zc:preprocessor` (the last one is what
  CUDA 13's CCCL headers ask for), `NOMINMAX WIN32_LEAN_AND_MEAN _USE_MATH_DEFINES`, CUDA sources as C++20
  (MSVC does not accept designated initializers in C++17 as GCC does).  Warnings are `/W3` (`/WX` with
  `-DWERROR=ON`).  The CPU kernels' per-file instruction sets are `BASALT_ISA_*`: `/arch:AVX2` and `/arch:AVX512`
  plus the `__FMA__ __F16C__ __AVX512VNNI__ __AVX512VBMI__` macros MSVC does not define itself; the kernels are
  still picked at run time (`cpu_avx512_ok`, `cpu_avx2_ok`).
* **`include/basalt/platform/compat.hpp`**: CPUID/XGETBV, the PAUSE hint, 64-bit `FILE*` seeks, positional reads
  on a file descriptor (`ReadFile` with an offset), `setenv`, UTF-8 paths opened as UTF-16, binary-mode file
  descriptors, read-only file mappings (`MapViewOfFile`), rename-over-existing (`MoveFileEx`).
* **Memory** (`src/platform/memory.cpp`): `VirtualLock` after raising the working-set minimum (the Windows
  `mlock`); the available memory is the smaller of the free RAM and the commit left (RAM + page file - committed),
  because Windows charges every allocation - and the VRAM a WDDM driver backs - to the commit, and a budget from
  the RAM alone could be granted only by growing the page file.  The RAM margin left free is 15% of the RAM (5% on
  Linux).  Read-ahead hints are `PrefetchVirtualMemory`.
* **Pinned arena** (`src/core/pinned.cu`): large pages when the account holds `SeLockMemoryPrivilege` (it is
  enabled in the process token), else 4 KB pages.  Page-locked memory the GPU maps is charged to the GPU's WDDM
  shared-memory segment (about half the RAM); a sliced arena is pinned only up to 4 GiB below that budget (read from
  DXGI, else RAM/2 - 8 GiB) and the rest is locked in RAM instead, so later `cudaMalloc` calls do not fail.  The
  shared-file arena (`--shared-expert-arena`, a `/dev/shm` file on Linux) is refused with a clear error.
* **Unbuffered reads** (`src/platform/direct_file.cpp`, `--ple-io direct`): `FILE_FLAG_NO_BUFFERING` +
  `FILE_FLAG_OVERLAPPED` with an I/O completion port instead of `O_DIRECT` preads.
* **CPU pool** (`src/kernels/cpu/pool.cpp`, `pool_affinity_win.hpp`): the topology comes from
  `GetLogicalProcessorInformationEx`; on a hybrid CPU (Intel P/E cores) the `EfficiencyClass` orders the P-cores
  first.  Workers get a hard group affinity, the host thread a CPU Set (soft, restorable).  The decode loop's
  background worker takes the host core's SMT sibling, as on Linux.
* **Expert files** (`src/core/expert_source.cpp`, `gguf_reader.hpp`, `mmproj.cpp`): file mappings; the mapped
  pages a copy has used leave the working set with `VirtualUnlock` (Linux: `MADV_DONTNEED` + `POSIX_FADV_DONTNEED`).
* **Process** (`src/program/generate.cpp`): `basalt serve` starts the Python server with `CreateProcess` in a
  kill-on-close job and waits for it (there is no `exec`); stdin and stdout are binary, so the protocol is
  byte-for-byte the Linux one; no `oom_score_adj`.  `basalt-bench` drives the engine through `CreateProcess` and
  anonymous pipes and reads NVML from `nvml.dll`.
* **Vision**: the CPU encoder's AVX-512 BF16 product is not built with MSVC (GCC vector casts); the fp32 path runs.
  The fp16 rounding uses the bit-exact converter (`f16_bits.hpp`) where GCC has `_Float16`.
* **hc_fused**: MSVC cannot pass a 128-byte-aligned kernel parameter by value (C2719), and CUDA 13 declares
  `CUtensorMap` `alignas(128)`; the launch struct holds the same bytes at 64-byte alignment, which is what the TMA
  unit requires.
* **Expert arena load** (`load_experts_direct`, `experts_unbuffered`): when a timed probe says the expert file is not
  in the file cache and the RAM left could not keep it there, the arena is read with `FILE_FLAG_NO_BUFFERING`
  straight into the (pinned) arena by 16 readers; otherwise through the file cache as on Linux.
* **Positional reads** (`pread_at`): the descriptors the loaders open wrap handles opened for overlapped reads, so
  the prompt path's expert stager and the other multi-threaded readers really read in parallel (a synchronous
  Windows handle runs its reads one at a time).

## GPU behaviour under WDDM

* **Programmatic dependent launch** works under WDDM on the RTX 5090 (the parity tests that capture PDL chains pass).
  `basalt --no-pdl` launches every kernel the plain way (same results) if a driver misbehaves; it is also the A/B arm.
* **Legacy-stream copies.**  WDDM batches command submission, which makes a known CUDA hazard much more likely: a
  pageable `cudaMemcpy` (or `cudaMemset`) runs on the legacy stream and can return before it lands, and kernels on a
  `cudaStreamNonBlocking` stream do not wait for it.  Two parity tests read stale data this way on Windows (fixed with a
  device synchronization); the engine's mid-run uploads that kernels on non-blocking streams read wait for the legacy
  stream (`cudaStreamSynchronize(cudaStreamLegacy)`, as `generate.cpp`'s residency table already did).
* **TDR.**  Windows resets a GPU that does not answer a preemption request within `TdrDelay` (2 s).  The decode path's
  kernels spin on flags the CPU pool writes; with compute preemption a spinning kernel is preemptible, so this is not
  expected to trigger, but it is not verified with a model yet.  Basalt changes no Windows setting.

## Tests

```powershell
cmake -S . -B build-win -DBUILD_TESTING=ON     # in the same shell as the build
cmake --build build-win --parallel 4
cd build-win; ctest -j 1 --timeout 600 --output-on-failure
```

On an i9-14900K (AVX2, no AVX-512) + RTX 5090 (WDDM, driver with CUDA 13.1), Windows 11: **82 of 83 pass**, all
synthetic (no model).  The one failure, `expert_multi_test`, needs AVX-512 (VNNI/VBMI) for the canonical Q2_0 expert
kernel and fails the same way on a Linux machine without it; `pool_test`, `pool_stress` and `expert_parity` report
SKIPPED inside a passing test for the same reason.  The serve tests (`python -m pytest serve/tests`) pass except three
whose fixtures assume POSIX (an absolute `/x` path, `/`-joined expected paths, a shebang fake engine).

The CPU topology the pool sees on the 14900K: hybrid, 8 P-cores (16 threads) + 16 E-cores; the host takes P-core 0
and the default pool (`All`) is the other 7 P-cores, then the 16 E-cores (23 workers).  Strata measured 15 workers
best on this CPU (`--pool-workers 15`: the 7 P-cores and the first 8 E-cores); not yet measured with Basalt.

## Starting and stopping with the PowerShell scripts

`Avvia-Basalt.ps1` and `Ferma-Basalt.ps1` (repository root; messages in English, or Italian with `--lang it`,
`BASALT_LANG=it` or the language chosen in the web page's Settings, see `serve/lang.py`) start and stop Basalt Win+GUI with
`basalt_config.json` beside them (machine-specific and not tracked; `tools/windows/basalt_config.example.json` shows
its shape for a 3-bit file on a 32 GB card). Paths are relative to the scripts' folder, so they work from any
directory and from Explorer (right click, "Run with PowerShell"), with Windows PowerShell 5.1 or PowerShell 7. Both
take their options only by name (`PositionalBinding = $false`): `--port 7232` never lands in another parameter. The
option names are Strata's (`Avvia-Strata.ps1`), each with one or two dashes and in any case.

* **Three modes.** `-gui` (the default, no option) opens `tools\windows\desktop\publish\BasaltWinGUI.exe` (an older `BasaltPannello.exe` is used when it is the
  only one built) with
  `--config`, `--model`, `--port`, `--vram-libera` and `--file`, prints the address and does not open the browser;
  when the app is not built it prints `dotnet publish tools\windows\desktop -c Release -o tools\windows\desktop\publish`
  and falls back to `-browser`. `-browser` is the server tied to the script's window with the UI in the browser.
  `-server` is the server alone, detached, without app or browser, for agents: the script returns once `/health`
  says the model is loaded. `-perApp` (internal) ties the server to the caller without any UI. The old `-Sfondo` and
  `-SoloServer` still work as `-server`, with a note.
* **The model** (`--model "<file .basalt>"`): `--model`, else the load profile's model, else the default server model
  (`python -m serve.models default`, set from the Loader), else the config file's; none set is an error that says how
  to choose one (exit 1). The choice is printed with where it came from. A GGUF "Da convertire" is not started (the
  script says how to convert it; `-converti` shows the plan, asks and converts); a "Non compatibile" file is refused
  with the reason. `--vram-libera GB` (or `--vram-reserve-mib N`) overrides the profile's VRAM reserve.
  `python -m serve.loader check` exits 4 on a GPU the engine is not built for (not Blackwell): the start is refused.
* **A server already running** (the listening `serve.server` processes of this repository and `basalt.pid`, confirmed
  by `/health` answering `"service": "basalt"`, on any port): `-gui` opens the app, which asks "Connect to the running
  server" or "Change model"; `-browser` opens the browser on it. `-server` resolves the parameters asked for as for a
  new start and compares them with the running server's (`python -m serve.profiles compare --url URL [--profile P]
  [--model F] [--vram-reserve-mib N] [--port N] --config FILE`: exit 0 the same, 3 different, 2 error; the running
  values come from `/api/settings`, "Valori sicuri" from the server's own `/api/loader`). The same: it prints its
  address, settings and model and exits 0. Different: it lists the differences and asks "Reuse the running server" or
  "Restart with the new parameters" in a topmost WinForms dialog (a desktop session, not session 0), else at the
  console (R / S, or R / A in Italian), else nobody is asked. No answer within 60 s (`BASALT_ASK_TIMEOUT_S`) or nobody
  to ask means reuse: a server is never stopped without an answer. `-riusa` / `-riavvia` answer in advance; restarting
  runs `Ferma-Basalt.ps1 --port N` (that server's tree only) and starts on the same port. `BASALT_NO_DIALOG=1` skips
  the window (tests); `BASALT_SCAN_PORTS=7280-7289` (tests) limits the search to those ports, so a test never sees,
  asks or stops a real server, and leaves `basalt.pid` alone.
* Otherwise `-browser` and `-server` write the load profile into `basalt_config.json` (the default profile when
  `--config` is not given: the one made default in the Loader, else Machine Default, else Valori sicuri; the previous
  file stays as `.bak`; an unknown name lists the profiles and exits 1) and check memory with the Loader's own plan
  (`python -m serve.loader check`): VRAM, RAM and commit, keeping this PC's share of the RAM and of the commit limit
  free (15% unless the wizard set another) and counting the VRAM the engine allocates in the commit, because WDDM
  charges it there. When something is short it says what and exits 1: nothing relies on the page file growing. It then
  clears `CL`/`_CL_`, puts the venv first on `PATH`, starts `bin\basalt.exe serve --config basalt_config.json
  [--port N]` at normal priority with its output in `logs\basalt-serve.log` and shows that log live.
  `"listen-first": true` in the config makes the server answer while the model loads (the Monitor shows Loading).
* With `-browser` the server is tied to the script's window: it shares the console (Ctrl+C reaches it and it stops the
  engine cleanly) and it runs in a job object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` whose only handle belongs to
  PowerShell (`tools/windows/BasaltLauncher.cs`, compiled by the script with `Add-Type`). However PowerShell ends -
  Ctrl+C, the window's X, Task Manager - Windows closes that handle and the whole tree goes: `basalt.exe serve`, the
  Python server (in `basalt.exe`'s own kill-on-close job) and the engine and vision sidecar (in
  `serve/util/winjob.py`'s). The process is created suspended and joins the job before it runs, so nothing escapes.
  It runs with `--relaunch-on-model-change`: when the Loader loads another model the server saves it and exits with
  code 75, and the script starts it again with that model (the page reconnects by itself).
* `-server` starts the server detached in a hidden console (no job of the script's); `basalt.pid` records it (PID,
  start time, port, profile). `-prova` runs every check (on a copy of the config) and prints the command without
  starting anything.
* `Ferma-Basalt.ps1 [--port N] [--file FILE]` finds the server from `basalt.pid` (PID and start time) or from the port,
  asks it to unload the model (`POST /unload`: the engine gets QUIT and frees RAM and VRAM), ends the process tree,
  checks that no process of this `bin\` is left and prints the RAM available before and after. With `--port` only the
  process tree of the server on that port is ended. Processes are found by PID file, by the listening port's owner and
  its children, or by full path under this `bin\`, never by name alone.

### Where the settings live

The user's settings are in the user data folder (`serve/userdata.py`): `%APPDATA%\BasaltPannello\` (or
`%BASALT_DATA%`): `profiles.json`, `models.json`, `machine.json` (the wizard's answers), `calibration\<model>.json`,
`prefs.json` (the language: `python -m serve.lang set it|en|auto`, or Settings in the web page) and the app's
`desktop.json`. What can be rebuilt stays in `%USERPROFILE%\.cache\basalt\` (or `%BASALT_CACHE%`): the
header scan `models-scan.json`, the job logs `models-jobs\`, and per model the unpacked tokenizer and vision files and
`engine.log`. The first use copies (never moves) the settings that earlier versions kept in the cache folder.

### Load profiles, the Loader, the wizard and the desktop app

* **Load profiles** (the Loader's engine fields, the port and, when saved with one, the model) and **inference
  presets** (the chat settings), shared by the web UI, the desktop app and the scripts: `python -m serve.profiles list
  | default | json [--model FILE] | apply NAME --config FILE [--model FILE] [--vram-reserve-mib N]`. "Valori sicuri" is
  built in and cannot be changed or deleted; it is computed from this PC (`serve/hardware.py`: cores, GPU, VRAM, RAM,
  commit) and the chosen model each time (`loader.safe()`) and keeps the config file's model. The default profile is
  the one made default by hand, else "Machine Default", else Valori sicuri.
* **Models** (`serve/models.py`): the models folders and the default server model in `models.json`. The Loader's model
  section lists every model with its status and runs Verifica / Converti in the background (a conversion only after
  its plan was shown and confirmed, with free space and the commit below 75%).
* **The Loader** (web UI, tab "Carica modello") edits the engine settings by hand or from a profile, shows the VRAM,
  RAM and commit the plan needs next to what is free now (what the running engine gives back included) and the
  predicted speed, and loads (`POST /api/loader/load`: checked, written to the config file with a `.bak`, applied by
  restarting the engine process; another model restarts the server when its owner can, a new port applies at the next
  start). Settings has the same profile picker and a Reload button. Speeds come from a calibration (`Calibra`, and
  automatically at the first load of a model) stored per model and settings; before that they are seeded estimates.
* **The configuration wizard** ("Configurazione guidata", `serve/web/wizard.js`, `GET/POST /api/wizard/*`,
  `python -m serve.loader wizard [--model F] [--monitor primary|secondary|none] [--free-share S] [--vram-libera GB]
  [--save] [--json]`): reads this PC (RAM, commit, page file, P/E cores, the GPU through NVML and the video adapters
  through `EnumDisplayDevicesW`, which says which adapter shows the primary display; NVML's `display_active` adds a
  secondary monitor connected to the GPU), asks the user to confirm where the monitor is, what share of RAM and commit
  and how much VRAM to leave free (0.5 GB, +1.5 GB with the main monitor on the GPU, +0.4 GB with a secondary one;
  what other programs use now is shown, not added), computes the values with `loader.safe()`, optionally measures them
  on a real load (below 75% of the commit limit: real memory against the plan, calibration, the drafting floor per
  request through the engine's `spec_min_p` key, the verify windows 5, 7 and 8 with one reload each) and saves
  "Machine Default". Every real load also records the RAM and commit it really took against its plan, per model
  (`calibration\<model>.json`), and later plans add what was above.
* **The desktop app** `tools\windows\desktop` (`dotnet publish -c Release -o publish`, .NET 8, WebView2) builds
  `BasaltWinGUI.exe` (window title, tray and dialogs "Basalt Win+GUI"; the file name differs from the engine's
  `basalt.exe` on purpose; the icon `basalt.ico` is also the web page's favicon; options
  `--config`, `--model`, `--port`, `--vram-libera`, `--file`, `--lang it|en`, as the script; its texts and WebView2's
  language follow `--lang` > `BASALT_LANG` > `prefs.json` > English, never the Windows display language). It starts `bin\basalt.exe serve --config
  basalt_config.json --standby --relaunch-on-model-change` in its kill-on-close job without a console window (a new
  installation gets a config file without a model) and opens the web Loader in its window: the command line's profile,
  model and VRAM reach the page as `?profile=`, `?model=`, `?reserve=`, and `?wizard=1` offers the wizard while no
  Machine Default exists. The folder fields have "Sfoglia…": the page asks the app for the Windows folder dialog
  (`chrome.webview.postMessage`). Exit code 75 starts the server again with `--listen-first` and the new model; any
  other end shows the reason, the log's last lines and the native start panel (model, profile, VRAM, port, memory
  check, Avvia / Avvia solo server) as a fallback. Without WebView2 the page opens in the default browser. Closing the
  window hides it to the notification area; the icon's menu has Open, Reload with other settings… (the Loader), Start
  the server alone (for agents)… and Turn off (unload, end the job, report the RAM freed). A second start brings the
  first window to the front; when a Basalt server already runs it offers "Connect to the running server" or "Change
  model" (stops only that server's tree with `Ferma-Basalt.ps1 --port N`, then opens the Loader). Hidden test
  options: `--test-mock` (the mock engine on port 7288), `--test-screenshot FILE`, `--test-exit`, `--test-no-wizard`,
  `--test-fallback`, `--test-folder-prompt [--test-models-dir DIR]`, `--test-no-webview`.

### For agents

```powershell
# start without window, tray icon or browser; returns when the model is loaded (exit 1: memory short, bad profile,
# no model chosen, a GGUF still to convert, or a GPU the engine does not support)
powershell -ExecutionPolicy Bypass -File .\Avvia-Basalt.ps1 -server --model "D:\Models\Qwen3.8-Flash-Next-IQ3_XXS.basalt" --config "Machine Default" --port 7240
# the best values for this PC, without questions, saved as Machine Default
.\.venv\Scripts\python.exe -m serve.loader wizard --monitor none --save --json
# which model would start, and why (exit 0 ready, 5 to convert, 6 not usable, 7 none chosen)
.\.venv\Scripts\python.exe -m serve.models resolve --profile "Machine Default" --config .\basalt_config.json
# already running? the same parameters attach and print the port; different ones ask (add -riusa or -riavvia to
# answer in advance; with nobody to ask the running server is reused); or read the port from basalt.pid
Get-Content .\basalt.pid | ConvertFrom-Json | Select-Object port, pid, profile
# ready? "loaded": true once the model is in (with listen-first the server answers while it loads)
Invoke-RestMethod http://127.0.0.1:7240/health
# stop only that server
powershell -ExecutionPolicy Bypass -File .\Ferma-Basalt.ps1 --port 7240
```

The API is OpenAI- and Anthropic-compatible at `http://127.0.0.1:<port>/v1` (`/v1/chat/completions`, `/v1/responses`,
`/v1/messages`, `/v1/messages/count_tokens`; examples with curl, the Python SDKs and Claude Code in the
[README](../README.md#using-it-as-a-server-api)). `response_format` (`json_object`, `json_schema`) is validated, not constrained during generation
(the engine has no grammar sampler): a rejected answer is generated again with the error and the place where it went
wrong fed back (never the rejected text itself), up to `structured_retries` times (default 2); reasoning streams while
only the validated answer is held, a rejected try is announced by a `structured_output.discarded` chunk, and the final
chunk carries `"structured_output": {"attempts": n}`. Every request is also kept, in memory, in the request log behind
the LOGS tab (`GET /api/reqlog`; `--request-log off` turns it off, see the [README](../README.md#request-log---request-log)).

**ExecutionPolicy.** "Run with PowerShell" from Explorer sets the policy to Bypass for that process only. From a
terminal, the default `RemoteSigned` runs these local scripts; if the policy is `Restricted` (or the files were
downloaded and are blocked), use `powershell -ExecutionPolicy Bypass -File .\Avvia-Basalt.ps1`, or once
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (and `Unblock-File .\*.ps1` for downloaded copies). The scripts
are UTF-8 with a BOM so that Windows PowerShell 5.1 reads the accented messages correctly.

## Not supported on Windows (yet)

* `--shared-expert-arena` (refused at start).
* `conversation_transfer_test` (it needs GNU ld's `--wrap`).
* The CPU encoder's AVX-512 BF16 product (see above).
