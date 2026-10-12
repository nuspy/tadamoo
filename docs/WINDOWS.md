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

`Avvia-Basalt.ps1` and `Ferma-Basalt.ps1` (repository root, messages in Italian) start and stop the server with
`basalt_config.json` beside them (machine-specific and not tracked; `tools/windows/basalt_config.example.json` shows
its shape for a 3-bit file on a 32 GB card). Paths are relative to the scripts' folder, so they work from any
directory and from Explorer (right click, "Run with PowerShell"), with Windows PowerShell 5.1 or PowerShell 7. Both
take their options only by name (`PositionalBinding = $false`): `--port 7232` never lands in another parameter.

* **The model** (`--model "<file .basalt>"`, `-Model`): `--model`, else the load profile's model, else the default
  server model (`python -m serve.models default`, set from the Loader or the app), else the config file's; none set
  is an error that says how to choose one (exit 1). The choice is printed with where it came from. A GGUF "Da
  convertire" is not started (the script says how to convert it; `-Converti` shows the plan, asks and converts); a
  "Non compatibile" file is refused with the reason. `--vram-reserve-mib N` overrides the profile's VRAM reserve.
  `python -m serve.loader check` exits 4 on a GPU the engine is not built for (not Blackwell): the start is refused.
* `Avvia-Basalt.ps1 [--config "<profile>"] [--port N]` (also `-Config`/`-Port`) first looks for a Basalt server of
  this folder already running, on any port (the listening `serve.server` processes of this repository and
  `basalt.pid`, confirmed by `/health` answering `"service": "basalt"`): if there is one it starts nothing, prints its
  address, settings and model, and exits 0. Otherwise it writes the load profile into `basalt_config.json` (the
  default profile chosen in the Loader when `--config` is not given; the previous file stays as `.bak`; an unknown
  name lists the profiles and exits 1) and checks memory with the Loader's own plan (`python -m serve.loader check`):
  VRAM, RAM and commit, keeping 15% of the RAM and 15% of the commit limit free and counting the VRAM the engine
  allocates in the commit, because WDDM charges it there. When something is short it says what and exits 1: nothing
  relies on the page file growing. It then clears `CL`/`_CL_`, puts the venv first on `PATH`, starts
  `bin\basalt.exe serve --config basalt_config.json [--port N]` at normal priority with its output in
  `logs\basalt-serve.log`, shows that log live, and opens the page as soon as the server answers.
  `"listen-first": true` in the config makes the server answer while the model loads (the Monitor shows Loading).
* By default the server is tied to the script's window: it shares the console (Ctrl+C reaches it and it stops the
  engine cleanly) and it runs in a job object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` whose only handle belongs to
  PowerShell (`tools/windows/BasaltLauncher.cs`, compiled by the script with `Add-Type`). However PowerShell ends -
  Ctrl+C, the window's X, Task Manager - Windows closes that handle and the whole tree goes: `basalt.exe serve`, the
  Python server (in `basalt.exe`'s own kill-on-close job) and the engine and vision sidecar (in
  `serve/util/winjob.py`'s). The process is created suspended and joins the job before it runs, so nothing escapes.
* `-Sfondo` starts the server detached in a hidden console (no job of the script's); `basalt.pid` records it (PID,
  start time, port, profile). `-SoloServer` (`--solo-server`) does the same without opening the browser and returns
  as soon as `/health` answers. `-Prova` runs every check on a copy of the config and prints the command without
  starting anything.
* `Ferma-Basalt.ps1 [--port N]` finds the server from `basalt.pid` (PID and start time) or from the port, asks it to
  unload the model (`POST /unload`: the engine gets QUIT and frees RAM and VRAM), ends the process tree, checks that
  no process of this `bin\` is left and prints the RAM available before and after. With `--port` only the process
  tree of the server on that port is ended. Processes are found by PID file, by the listening port's owner and its
  children, or by full path under this `bin\`, never by name alone.

### Load profiles, the Loader and the desktop app

* **Load profiles** (the Loader's engine fields, the port and, when saved with one, the model) and **inference
  presets** (the chat settings) are kept in `%USERPROFILE%\.cache\basalt\profiles.json` (or `%BASALT_CACHE%`), shared
  by the web UI, the desktop app and the scripts: `python -m serve.profiles list | default | apply NAME --config FILE
  [--model FILE] [--vram-reserve-mib N]`. "Valori sicuri" is built in and cannot be changed or deleted; it is
  computed from this PC (`serve/hardware.py`: cores, GPU, VRAM, RAM, commit) and the chosen model each time
  (`loader.safe()`), keeps the config file's model, and is the default until another profile is made default.
* **Models** (`serve/models.py`): the models folders and the default server model in `models.json`, the header scan's
  cache in `models-scan.json`, the conversion and verification logs in `models-jobs\`, all beside `profiles.json`.
  The Loader's model section lists every model with its status and runs Verifica / Converti in the background
  (a conversion only after its plan was shown and confirmed, with free space and the commit below 75%).
* **The Loader** (web UI, tab "Carica modello") edits the engine settings by hand or from a profile, shows the VRAM,
  RAM and commit the plan needs next to what is free now (what the running engine gives back included) and the
  predicted speed, and reloads the engine inside the running server (`POST /api/loader/load`: checked, written to the
  config file with a `.bak`, applied by restarting the engine process; the HTTP server and its port stay, a new port
  applies at the next start). Settings has the same profile picker and a Reload button. Speeds come from a
  calibration (`Calibra`, and automatically at the first load of a model) stored per model and settings in
  `%USERPROFILE%\.cache\basalt\<model>\calibration.json`; before that they are seeded estimates.
* **The desktop app** `tools\windows\desktop` (`dotnet publish -c Release -o publish`, .NET 8, WebView2) builds
  `BasaltPannello.exe` (window title "Basalt"; the name differs from the engine's `basalt.exe` on purpose). Until the
  models folders are set it asks for them (FolderBrowserDialog, folders found on this PC offered). Its start panel has
  the model (the last one used preselected, kept in `%APPDATA%\BasaltPannello\desktop.json`, with its status and
  "Usa come modello predefinito per il server"), the load profile, "VRAM da lasciare libera" (slider + GB, every change
  re-runs the memory check), the port and the check; Avvia, or Avvia solo server (`Avvia-Basalt.ps1 -SoloServer` with
  the same model and reserve). Then the web UI in its own window, or in the default browser when WebView2 is missing.
  A server it starts runs in its kill-on-close job without a console window. Closing the window hides it to the
  notification area; the icon's menu has Apri, Ricarica con altri parametri (the Loader) and Spegni (unload, end the
  job, report the RAM freed). A second start brings the first window to the front; when a Basalt server already runs
  it offers "Collegati al server in esecuzione" or "Cambia modello" (stops only that server's tree with
  `Ferma-Basalt.ps1 --port N`, then loads the chosen model). Hidden test options: `--test-mock`,
  `--test-screenshot FILE`, `--test-exit`, `--test-folder-prompt [--test-models-dir DIR]`, `--test-no-webview`.

### For agents

```powershell
# start without window, tray icon or browser; returns when /health answers (exit 1: memory short, bad profile,
# no model chosen, a GGUF still to convert, or a GPU the engine does not support)
powershell -ExecutionPolicy Bypass -File .\Avvia-Basalt.ps1 --model "D:\Models\Qwen3.8-Flash-Next-IQ3_XXS.basalt" --config "Valori sicuri" --port 7240 -SoloServer
# which model would start, and why (exit 0 ready, 5 to convert, 6 not usable, 7 none chosen)
.\.venv\Scripts\python.exe -m serve.models resolve --profile "Valori sicuri" --config .\basalt_config.json
# already running? the same command attaches and prints the port; or read it from basalt.pid
Get-Content .\basalt.pid | ConvertFrom-Json | Select-Object port, pid, profile
# ready? "loaded": true once the model is in (with listen-first the server answers while it loads)
Invoke-RestMethod http://127.0.0.1:7240/health
# stop only that server
powershell -ExecutionPolicy Bypass -File .\Ferma-Basalt.ps1 --port 7240
```

The API is OpenAI-compatible at `http://127.0.0.1:<port>/v1` (`/v1/chat/completions`, `/v1/messages`,
`/v1/responses`). `response_format` (`json_object`, `json_schema`) is validated, not constrained during generation
(the engine has no grammar sampler): a rejected answer is generated again with the error fed back, up to
`structured_retries` times (default 2), and the final chunk carries `"structured_output": {"attempts": n}`.

**ExecutionPolicy.** "Run with PowerShell" from Explorer sets the policy to Bypass for that process only. From a
terminal, the default `RemoteSigned` runs these local scripts; if the policy is `Restricted` (or the files were
downloaded and are blocked), use `powershell -ExecutionPolicy Bypass -File .\Avvia-Basalt.ps1`, or once
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (and `Unblock-File .\*.ps1` for downloaded copies). The scripts
are UTF-8 with a BOM so that Windows PowerShell 5.1 reads the accented messages correctly.

## Not supported on Windows (yet)

* `--shared-expert-arena` (refused at start).
* `conversation_transfer_test` (it needs GNU ld's `--wrap`).
* The CPU encoder's AVX-512 BF16 product (see above).
