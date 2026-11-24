# Building and testing

Clone, requirements and the build commands are in [the original Basalt README](reference/upstream-basalt.md). This page has the rest.
The build commands are in the [README](../README.md). Windows: [platforms](platforms.md).

## What is in the repository

- `src/`, `include/`: the engine (`basalt`, `basalt-bench`, `basalt-probe`, `basalt-vision`).
- `serve/`: the Python server with the OpenAI and Anthropic API and the stats page.
- `tools/`: the converter (`tools/convert/convert.py`), the `.basalt` reader (`model_file.py`), the quality gate (`ppl_gate.py`), the
  greedy-identity check and the tokenizer.
- `tests/`: parity and unit tests. `data/`: expert profiles (`data/profiles`) and benchmark fixtures (`data/bench`).

## Windows: the MSVC toolset

CUDA 13.1's `nvcc` does not work with the newest MSVC toolset (VS 2026, 14.5x): `-allow-unsupported-compiler` gets
past the version check, but `cudafe++` itself then crashes (access violation) on code generated for it. Build with
an older toolset instead, selected for just that shell with `vcvars_ver` (no change to the Visual Studio
installation or to this repository):

```
"C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" -vcvars_ver=14.44
```

14.44 (VS 2022-era) is officially supported by CUDA 13.1 and, as of this writing, installed side by side with the
newer one under the same Visual Studio instance (`VC\Tools\MSVC\`); adjust the path and the version to what `dir
"C:\Program Files\Microsoft Visual Studio\*\*\VC\Tools\MSVC"` actually lists. If the `_CL_` environment variable is
set (some setups add flags such as `/std:c++20` to every MSVC invocation through it), clear it first in the same
shell (`$env:_CL_ = $null` in PowerShell) -- it reaches the compiler-detection probe too, which fails `cmake`'s
configure step with a message naming `_CL_` specifically.

## Warnings
The build fetches nothing. The ggml and llama.cpp parts Basalt uses (quantization formats, CPU dot products, MMQ) are in `src/` and
`include/basalt/ggml/`, under the MIT licence in `THIRD_PARTY_LICENSES`.

`-DWERROR=ON` makes project warnings errors (CI sets it on Linux).

## Building only some models

`-DBASALT_MODELS=<ids>` is a semicolon list of architecture ids, or `all` (the default), which builds every model and follows the
models a later version adds. A model left out is not compiled at
all: its C++ and CUDA sources, its kernels and its tests are skipped, which is most of the build time for DeepSeek.

| Model | Id |
| --- | --- |
| Qwen3.8-Flash-Next | `qwen4exp` |
| DeepSeek-V4.1-Flash | `deepseek41` |
| Qwen3.8-27B | `qwen35` |
| Hy4-flash | `hy4flash` |
| Inkling 2 Mini | `inkling2` |

```sh
cmake -S . -B build -DBASALT_MODELS=qwen4exp                  # Flash-Next only
cmake -S . -B build -DBASALT_MODELS=deepseek41                # DeepSeek only
cmake -S . -B build -DBASALT_MODELS=qwen35                    # the 27B only
cmake -S . -B build -DBASALT_MODELS=hy4flash                  # Hy4-flash only
cmake -S . -B build -DBASALT_MODELS=inkling2                  # Inkling 2 Mini only
cmake -S . -B build "-DBASALT_MODELS=qwen4exp;deepseek41"     # two of them; quote the semicolon
```

An unknown id or an empty list stops the configure and names the valid ids. A pack of a model the build left out fails at load with
`architecture is 'deepseek41', a Basalt model this build left out (built with BASALT_MODELS=qwen4exp); rebuild with cmake
-DBASALT_MODELS="qwen4exp;deepseek41;qwen35" to load it`. The value is kept in the build folder's cache: to change it, pass the option again (`-DBASALT_MODELS=all` goes back to every
model). A folder configured by 0.4 holds that version's default list, `qwen4exp;deepseek41;qwen35`; the configure reads it as `all`.

The build needs nothing outside this repository and fetches nothing. The parts of ggml and llama.cpp that Basalt-Plus uses
(quantization formats, CPU dot products, the prompt path's MMQ kernels) are extracted into `src/` and `include/basalt/ggml/`,
with their MIT licence in `THIRD_PARTY_LICENSES`.
## Tests

Configure with `-DBUILD_TESTING=ON`, build, and run `ctest --test-dir build`.

```sh
bin/basalt-bench --community --model Qwen3.8-FN-NVFP4-DQ6_K.basalt --submitter "Your Name"
```
- Kernel parity tests need the GPU. `expert_parity` and `pool_test` use a synthetic fixture; `--file experts.bin` checks real blobs.
- Server tests: `python -m unittest discover -s serve/tests -t .` after `pip install -r requirements-test.txt`.
- Tokenizer parity: register `tokenizer_parity` with `-DBASALT_TOKENIZER_DIR=<a pack's tokenizer/ directory>`.

`basalt-bench` reads its prompts from `data/bench` (run from the repository root, or pass `--fixtures DIR`).
