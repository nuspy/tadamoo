# Speed study: what makes the LTX 2.5 clip and the enlargers faster on this PC

Measured on 14 November 2026 on the RTX 5090 of this PC (power limit 600 W), Windows 11, with the job of the motion matrix: the winning clip `ltx25t_12` (same still, same 6.9 s recording smoothed, same seed and prompt, 896x512, 169 frames at 25 fps,
12 steps, saved at crf 4), run through LTX Studio's own worker (`workers/ltx2_infer.py`) in a copy of its LTX 2.5 environment. Nothing of LTX Studio's folders or environments was changed: every experiment ran in a copy under `E:\AI\speedlab\`
or a launcher of ours (`tools/speed_lab/`). Raw numbers, logs and every clip: `E:\AI\Output\speed_study\20261114\` (`summary.md`, `summary.json`, `metrics.json`, `<cell>\run<N>\`).
What LTX Studio chose or measured is not taken as true here: every claim below is a measure of this study.

## What a job costs (the baseline, torch 2.14 nightly, cuDNN attention)

About 63 s at the best, 66 to 86 s on a busy PC (10 runs; the PC was in use). Inside it: two builds of the transformer from disk (stage 1 and stage 2), 17 s together; the transformer passes, 29 s (stage 1: 48 passes of 0.57 s, four guided passes per step;
stage 2: 3 passes, 2 s); the rest (start of Python, VAE, saving) about 17 s. Peak VRAM 17.5 GiB of PyTorch (19 to 21 GiB on the whole GPU).
A prompt the embedding cache has not seen costs another **38 s** (Gemma encoder) and 24 GiB: 104 s for the job; a new environment starts with an empty cache. That is the largest single item after the passes, and none of the systems below touches it.
Attention is small: one call is 0.46 ms (stage 1) and 6.9 ms (stage 2) in cuDNN, so attention is about 4 % of the transformer time; the rest is the 4-bit matrix multiplications.

## The systems, one by one

"Kernel" is the speed of the backend alone on the job's shapes (32 heads of 128, 2464 and 9856 tokens) against cuDNN, `tools/speed_lab/attn_micro.py`. "Job" is the transformer time of the whole job (median of the runs) and the seconds of the job
(minimum - median of the runs); "PSNR" is the clip against the baseline clip (two exact backends differ by 29 to 31 dB, SSIM 0.93 to 0.95: that is the noise floor of this sampler). Sharpness: the face measure of `tests/motion_matrix.py`, baseline 96.1.

| System | Installs and runs here | Kernel (stage 1 / 2) | Job: transformer s, seconds | PSNR, SSIM, sharpness |
|---|---|---|---|---|
| cuDNN SDPA (torch's, the baseline) | yes, it is what runs today (the LTX package puts cuDNN first) | 1.0 / 1.0 | 29.3 s, 63 - 73 | - |
| PyTorch SDPA "flash" backend | no: the Windows build of torch has no flash kernel (`can_use_flash_attention` is false) | - | - | - |
| PyTorch SDPA memory-efficient | yes | 0.39 / 0.42 | 32.5 s, 69 (1 run) | 30.9 dB, 0.951, 93.6 |
| PyTorch 2.14 nightly (the current one) | yes | - | 29.3 s | - |
| PyTorch 2.15.0 | there is no final release: nightly 2.15.0.dev20260928 (cu130) installed and runs | the same | 29.7 s, 66 - 75 | 29.6 dB, 0.948, 95.6 |
| PyTorch 2.11 (cu130, Python 3.11) | yes (the first runs of a new environment miss the embedding cache: 80 to 107 s) | the same | 29.8 s | 30.9 dB, 0.952, 94.3 |
| FlashAttention 2 | no wheel for 2.8.3 on this Python/torch; the prebuilt 2.8.4 (sm120, torch 2.11, in `E:\AI\Wheels`) runs | 0.98 / 0.93 | 28.8 s, 63 - 64 | 30.9 dB, 0.946, 96.8 |
| FlashAttention 4 4.0.0b39 | yes, with a stand-in for the POSIX `fcntl` module (its dependency `quack` imports it); runs on sm_120 | 0.95 / 0.99 | 31.9 s, 67 - 68; on torch 2.15 31.4 s | 30.8 dB, 0.949, 96.3 |
| xformers | 0.0.36 on PyPI is a pure-Python wheel (no Windows binary); the prebuilt 0.0.35 (sm120, `E:\AI\Wheels`) runs | 0.98 / 0.93 | 28.9 s, 63 | 30.9 dB, 0.946, 96.8 |
| SageAttention 1.0.6 | yes (Triton) | 1.30 / 1.60 | 29.3 s, 65 - 66 | 27.7 dB, 0.914, 101.4 |
| SageAttention 2.2.0 | no wheel for Python 3.12 / torch 2.14 / cu130; built from source here (see the last section): kernel and clip the same as the cu128 `cp310-abi3` build that LTX Studio's inference environment holds (which also loads in a copy once `cudart64_12.dll` is beside it) | 1.53 / 2.31 | 27.7 s, 62 - 63 (3 runs of the source build 61.8 - 72.5) | 28.9 dB, 0.935, 96.7 |
| SageAttention 3 | built from source here (same fixes), but every launch fails with `CUDA error: misaligned address`, also at 2048 tokens: not usable (not diagnosed) | - | - | - |
| FlashInfer 0.7.4 | installs with `--no-deps` (its `nccl-extensions` has no Windows build) and imports; its JIT writes an invalid `build.ninja` on Windows (`expected build command name`): no kernel runs | - | - | - |
| NVIDIA Transformer Engine 2.20.2 | no: PyPI has only a placeholder and a source package; the build is Linux-only | - | - | - |
| Triton 3.9.0 (`triton-windows` 3.9.0.post30) | yes; used by FlashAttention 4, Sage 1 and torch.compile; alone it changes nothing | - | - | - |
| `torch.compile` of the blocks (inductor, `CompilationConfig`) | runs, but it loses: 155 s cold, 106 - 113 s warm, the passes 60 - 114 s. `reduce-overhead`, `max-autotune` and `capture` are refused by ltx_core (they need GPU-resident weights; the worker's builder reloads the weights for each stage) | - | 69 s | 30.0 dB, 0.939, 92.5 |
| NVFP4 / FP8 matmul | already in the baseline (`nvfp4_scaled_mm`, comfy-kitchen) | - | - | - |
| cache-dit, Nunchaku/SVDQuant, SpargeAttn, radial attention | not tried: cache-dit and Nunchaku are for diffusers pipelines and models (not LTX 2.5 through `ltx_pipelines`); SpargeAttn and radial attention have no wheel and need the CUDA build below | - | - | - |

### Step caches (stage 1 only; the first and last step and stage 2 always run)

LTX Studio's `teacache_thresh` keeps one state for the four guided passes of a step, so a skipped pass reuses another pass's result. `tools/speed_lab/caches.py` keeps one cache per pass.
The change of the first block's output from step to step on this 12-step distilled schedule is 0.08 to 0.24 (relative L1), so thresholds below 0.08 never skip.

| Cache | Passes skipped | Job: transformer s, seconds | PSNR, SSIM, sharpness |
|---|---|---|---|
| LTX Studio TeaCache, 0.03 | many (its own count is not printed) | about 21 s, 50.5 (1 run) | 26.2 dB, 0.892, 106.4 |
| First-block cache 0.05 / 0.0 | 0 of 48 | 30 s (no gain) | identical clip |
| First-block cache 0.10 and 0.13 | 12 of 48 | 23.5 s, 58 - 59 | 28.2 dB, 0.936, 92.9 |
| First-block cache 0.17 | more | 20.5 s, 56 (1 run) | 26.7 dB, 0.902, 94.5 |
| TeaCache per pass 0.05 / 0.10 | 0 / more | 30 s / 21.7 s, 57 - 58 | identical / 26.0 dB, 0.893, 95.8 |

### Two more changes of the job (they change the picture, not only the speed)

| Change | Transformer s, seconds | PSNR, SSIM, sharpness |
|---|---|---|
| two guided passes (cfg only, no STG, no modality) | 18.2 s, 54 - 60 | 25.7 dB, 0.883, 101.4 |
| one pass (cfg 1) | 12.4 s, 50 - 56 | 26.4 dB, 0.897, 107.1 |

## The winning combination

**SageAttention 2 + first-block cache 0.10 on stage 1.** Runs in turn on the quiet PC: baseline 62.8 / 63.3 / 65.5 s (median 65.5 of 7, min 62.8), the pair 57.5 / 57.6 / 57.7 s (source-built Sage 2; three runs, no spread); the transformer time went from 28.9 s to 22.5 s (1.29 times),
the job by 8 to 12 % (1.09 to 1.14 times). The clip differs from the baseline by 28.0 dB / SSIM 0.930 (the noise floor between two exact backends is 29 to 31 dB) and its face sharpness is 96.2 against 96.1; the regions (eyes, centre, hair) are within 0.2 of the baseline.
Each half alone: the cache 1.2 times on the transformer (job 58 - 64 s), Sage 2 1.04 times (job 62 - 67 s). Attention alone does not change the length of a job here: the faster kernels (Sage 2 is 2.3 times faster than cuDNN on stage 2) are 4 % of the time.
The faster things are the ones that change the picture more (TeaCache of LTX Studio, fewer guided passes); the study leaves that choice to the person who judges the clips on the review page.
Not measured and the largest item left: the 38 s of the text encoder for a new prompt, and the 17 s of the two transformer builds.

How to switch it on:
- **Clip job** (`tadamoo/media/worker.py`, `video_plan`): run `tools/speed_lab/ltx_bench.py` in place of `ltx_launch.py` (it does the same save-quality patch with `LTX_X264_CRF`) in the Python of an environment that has the Sage 2 build,
  with `SL_ATTN=sage2` and `SL_CACHE=fb:0.10`. It exec's LTX Studio's worker in the same process, so nothing else changes. `SL_ATTN=sage1|fa4|fa2|xformers|cudnn`, `SL_COMPILE`, `SL_GUIDER=cfg|one` are the other switches.
- **Enlargers**: `tools/speed_lab/flashvsr_run.py` with `SL_VSR_KERNEL=triton` and `SL_VSR_CRF=4` (below). SeedVR2 needs no switch.
No production code was changed by this study.

## FlashVSR+ at 2x from 896x512: why it failed, and the fix

Reproduced: LTX Studio's runner forces `hardware_profile="generic"`, i.e. the attention is PyTorch's SDPA with an explicit expanded block mask; at 2x the mask would have more than 2 billion entries and `FlashVSR_Plus/models/attention_backends/kernels.py`
refuses (`SDPA block-mask fallback would allocate too many mask entries`), whatever the number of frames (33 frames fail the same). The repository itself ships a Triton block-sparse kernel (`operators/flashvsr_attn`, plain Python on Triton) that
LTX Studio's runner never uses ("no Windows kernel" was assumed). With `triton-windows` (already in the inference environment) it works: `tools/speed_lab/flashvsr_run.py` puts that folder on the path for the process, asks for `triton_block_sparse`
(a missing kernel is an error, not a silent return to SDPA) and saves at crf 4 (FlashVSR+'s own writer saves at libx264's default, crf 23, which loses the detail it makes).

## The four versions of the clip (`E:\AI\Output\speed_study\20261114\enhance\review.html`)

Sharpness: variance of the Laplacian of three face regions found with MediaPipe landmarks, each cut at the clip's own resolution and scaled to 256 px wide (the unenhanced region is small and is enlarged, so its numbers are lower by construction).

| Version | Size, frames | Seconds (whole process) | VRAM peak, GPU / PyTorch | Eyes / centre / hair |
|---|---|---|---|---|
| a) not enhanced | 896x512, 169 | 0 | - | 9.2 / 2.4 / 7.9 |
| b) SeedVR2 7B 2x | 1792x1024, 169 | 156 | 26.9 GiB / not printed | 45.2 / 6.3 / 45.1 |
| c) FlashVSR+ Turbo 2x | 1792x1024, 166 | 54 (44.5 inference + 7 load) | 12.7 GiB / 8.3 GiB | 69.4 / 6.8 / 138.3 |
| d) FlashVSR+ Turbo 1.5x | 1344x768, 166 | 31 | 10.3 GiB / 6.3 GiB | 34.6 / 4.5 / 66.0 |

SeedVR2 was already on the PC (`E:\AI\Tools\SeedVR2`, numz's CLI, models in `E:\AI\Models\SeedVR2`): nothing was downloaded. The 7B fp8 checkpoint (`seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors`, 7.9 GB) with the fp16 VAE (0.47 GB)
was used; the 3B fp16 (6.3 GB) and 3B fp8 (3.2 GB) are there too. Batches of 57 frames, VAE tiled (without tiling 17 frames already ran out of memory at 1792x1024), PNG frames then x264 crf 4 (the CLI's own mp4 writer is OpenCV's `mp4v`).
FlashVSR+ gives 166 frames of 169 (the last 3 are not produced; the first 166 match the source frame by frame). The clips, numbers and a page with notes and "Pubblica" are for a person to judge; the numbers above say how much fine contrast there is, not whether it is right.

## Not possible here, and why

- SageAttention 2 and 3 from source: they do build, after four fixes found one by one, so the failures were the toolchain's and not the PC's. (1) `cudafe++` (CUDA 13.1 and 13.4, MSVC 14.44 and 14.51) dies with 0xC0000409, a stack overflow:
  in a copy of the toolkit (`E:\AI\speedlab\cuda131`, not the installed one) `editbin /STACK:268435456` on the copy of `cudafe++.exe`. (2) The setup scripts ask for `-std=c++17` after torch's `-std=c++20`, and nvcc takes the last: c++20 in a copy of `setup.py`.
  (3) `-Xptxas=-v` (and Sage 3's `--ptxas-options=--verbose`) makes `ptxas` crash with an access violation: taken out. (4) `NVCC_APPEND_FLAGS=-Xcompiler /Zc:preprocessor` for CUDA 13.4's headers, and the 14.44 toolset of Visual Studio (`vcvars64.bat -vcvars_ver=14.44`). Sage 2.2.0 then builds (cp312 wheel, 20 minutes at 3 jobs) and runs; Sage 3 builds and fails at run.
- FlashInfer: the generated ninja file is invalid on Windows. Transformer Engine: Linux-only build. FlashAttention 3: for Hopper (sm 9.x), not sm_120.
- Linux-only items were not tried in WSL: WSL2 with an Ubuntu (Python 3.12) is there and sees the GPU libraries (`/usr/lib/wsl/lib/libcuda.so`), but the jobs run on Windows and a Linux result would not be the one the job gets; no toolchain or torch was installed in it.
- `capture`, `reduce-overhead` and `max-autotune` compile modes: refused by ltx_core with the worker's weight loading.

## Where the numbers and the code are

`tools/speed_lab/`: `ltx_bench.py` (the launcher), `run_cell.py` and `sweep.py` (one timed run, a list of cells), `caches.py` (first-block and per-pass TeaCache), `attn_micro.py` (kernel micro-benchmark), `metrics.py` (face sharpness, three regions, PSNR/SSIM),
`summarize.py`, `build_speed_review.py`, `flashvsr_run.py`, `enhance_compare.py`, `build_enhance_review.py`, `timed_run.py`, `review_page.py`. Environments (copies, E:\AI\speedlab): `ltx25` (as LTX Studio's, torch 2.14), `ltx25x` (+ triton 3.9, FlashAttention 4, Sage 1),
`ltx25_s2` (+ the cu128 Sage 2 of LTX Studio's inference environment), `ltx25_s2b` (+ Sage 2 built here), `ltx25_s3` (+ Sage 3 built here, not usable), `ltx25_t215` (torch 2.15 nightly), `ltx25_t211` (torch 2.11, Python 3.11, FlashAttention 2, xformers), `ltx25_fi` (FlashInfer).
