# SageAttention 2: the wheel, the environment and how to build it again

The LTX 2.5 clips run with SageAttention 2.2.0 by default (`ltx_attention`, [settings.md](settings.md)). PyPI has no Windows build of it, so we build the wheel once and keep it. This page is what is needed on another PC.

## What is kept and where

| what | where (default) | size |
|---|---|---|
| `sageattention-2.2.0-cp312-cp312-win_amd64.whl` (sm_120 only, Python 3.12, torch 2.14 nightly cu130) | `E:/AI/Tools/tadamoo_wheels` (`ltx_wheels`, or `TADAMOO_LTX_WHEELS`) | 7.5 MB (7,528,556 bytes) |
| `triton_windows-3.9.0.post30-cp312-abi3-win_amd64.whl` (what Sage's kernels need to compile at the first call) | the same folder | 73 MB (73,158,093 bytes) |
| the environment: a copy of LTX Studio's LTX 2.5 environment (`lingbot_venv`) with the two wheels installed | `E:/AI/Tools/ltx25_sage2` (`ltx_attention_env`, or `TADAMOO_LTX_ATTENTION_ENV`) | a copy of the source, a few GB more than the wheels |

The wheels are not in the repository (binary, 80 MB). LTX Studio's own environment is never changed: the component works on the copy only.

## Install

`python -m tadamoo.media.components install video-generation` (the component `video-attention`): checks that the copy's own Python imports `sageattention` 2.2.0, and when it does not, runs `python tadamoo/media/attention_env.py install`, which copies LTX Studio's
LTX 2.5 environment to `ltx_attention_env` (when it is not there), installs the two wheels from `ltx_wheels` with `pip --no-index --no-deps`, and imports `sageattention` as the proof. When a wheel is not in the folder it says which one and sends here.
Without the copy or the package a clip is made with cuDNN attention and its answer carries a `warning` that says so; nothing fails.

The first clip after the install is slower than the next ones (Triton compiles its kernels once, about 20 s here); the copy keeps them.

The first clip after the install is slower than the next ones (Triton compiles its kernels once, about 20 s here); the copy keeps them.

A wheel built for another Python or torch than the copy's does not load: the install ends with "cannot import sageattention". Build it again with the Python and torch of the copy (below).

## Build the wheel again

Sage 2.2.0 compiles CUDA for sm_120 with nvcc and the MSVC compiler. On Windows four things stop the stock build; each was found by running it:

1. **`cudafe++` dies with 0xC0000409** (a stack overflow in the CUDA front end; CUDA 13.1 and 13.4, MSVC 14.44 and 14.51). Work on a copy of the toolkit (never the installed one): `editbin /STACK:268435456 <copy>\bin\cudafe++.exe`.
2. **`-std=c++17` after torch's `-std=c++20`**: nvcc takes the last one and the headers need c++20. In a copy of the source's `setup.py` the flags are `-std=c++20`.
3. **`-Xptxas=-v`** makes `ptxas` crash with an access violation: take it out of `NVCC_FLAGS` in `setup.py`.
4. **CUDA 13.4's headers need the conforming preprocessor**: `NVCC_APPEND_FLAGS=-Xcompiler /Zc:preprocessor`; and the 14.44 toolset of Visual Studio (`vcvars64.bat -vcvars_ver=14.44`).

The build, from the SageAttention 2.2.0 source (the `thu-ml/SageAttention` repository, the setup at its root) with the Python of the environment it is for:

```
call "...\VC\Auxiliary\Build\vcvars64.bat" -vcvars_ver=14.44
set CUDA_HOME=<the patched copy of the toolkit>
set CUDA_PATH=%CUDA_HOME%
set PATH=%CUDA_HOME%\bin;%PATH%
set TORCH_CUDA_ARCH_LIST=12.0
set MAX_JOBS=3
set NVCC_APPEND_FLAGS=-Xcompiler /Zc:preprocessor
set DISTUTILS_USE_SDK=1
<that environment's python> setup.py bdist_wheel -d <wheels folder>
```

About 20 minutes at 3 jobs on this PC. `TORCH_CUDA_ARCH_LIST=12.0` makes the kernels for Blackwell only; another GPU needs its own architecture in the list (`SUPPORTED_ARCHS` of `setup.py` names 8.0 to 12.0). Triton for Windows is the PyPI package
`triton-windows` (3.9.0.post30 here), kept as a wheel beside the other so the install needs no network.

## How the attention is applied

`tadamoo/media/ltx_launch.py` runs LTX Studio's `ltx2_infer.py` and, once the transformer is built, replaces `attention_function` of every `Attention` module with a call of `sageattn(q, k, v, tensor_layout="HND")` on the 32 heads of 128 (the shapes
LTX 2.5 uses, in HND layout, no mask); the worker's own code is otherwise untouched. A package that cannot be imported prints `[attention] WARNING ...` and the worker goes on with cuDNN. The measures that chose it: [speed-study.md](speed-study.md).
