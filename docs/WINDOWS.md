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

`vsenv.ps1` clears `CL` and `_CL_` for the session: they add options to every `cl` call, and a user-wide
`_CL_=/std:c++20` breaks the C sources (ggml) with "`/std:c11` and `/std:c++20` are incompatible".

On a machine that runs other heavy work, build at low priority and low parallelism, e.g.
`(Get-Process -Id $PID).PriorityClass = 'BelowNormal'` before `cmake --build ... --parallel 4`; nvcc uses 1-3 GB of
memory per job.

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

## Not supported on Windows (yet)

* `--shared-expert-arena` (refused at start).
* `conversation_transfer_test` (it needs GNU ld's `--wrap`).
* The CPU encoder's AVX-512 BF16 product (see above).
