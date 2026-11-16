# Speed: what each video job runs on, and what we reuse from LTX Studio

TaDaMoo does not carry its own accelerators. Every job runs LTX Studio's own worker in the Python environment LTX Studio keeps for that model, with the versions and the choices LTX Studio made and measured
(`E:\AI\LTXStudio`, `scripts/install_*.py`, `scripts/install_accelerators.py`, `core/attention_backend.py`). What follows is what those environments hold on this PC (read on 14 November with `pip list`, nothing run on the GPU).

| Job (kind) | Environment | torch | Attention | Weights and matmul | Caches and memory |
|---|---|---|---|---|---|
| Picture (`image`) | `diffusers_next_venv` | 2.9.1+cu130 | PyTorch SDPA; flash-attn 2.8.3 and triton-windows 3.5.1 installed | Qwen-Image 2.1 DiT and text encoder in nvfp4 | the encoder shares the GPU for the few seconds of the job |
| Clip (`video`, LTX 2.5) | a copy of `lingbot_venv` (`ltx_attention_env`) with SageAttention 2.2.0 and triton-windows; `lingbot_venv` itself has neither | 2.14 nightly +cu130 | SageAttention 2 by default, cuDNN SDPA without it ([sage-wheel.md](sage-wheel.md)); TeaCache 0.03 | distilled transformer nvfp4, `nvfp4_scaled_mm` (the GPU's fp4 matmul) | prompt-embedding cache on, streaming VAE decode in tiles |
| Change of a clip (`modify`, LTX 2.3) | `ltx2_venv` | 2.8.0+cu128 | PyTorch SDPA by LTX Studio's choice; triton-windows 3.4.0 and sageattention 2.2.0 installed but Sage is off | dev checkpoint fp8, `fp8_scaled_mm`; nvfp4 also on disk | distilled LoRA at 0.5, prompt cache, the worker chooses by itself between resident and streamed blocks |
| Talking head (`avatar`) | `LongCatAvatar\venv` | 2.9.1+cu130 | flash-attn 2.8.3 (the weights' config says `enable_flashattn2`); block-sparse off (no Windows kernel); triton-windows needed by its module | int8 weight-only (dequantized to bf16 at every layer), or fp8 with the GPU's `torch._scaled_mm`; 8 steps distilled | LoRA fused into the weights and KV cache offload both decided from the free VRAM, VAE in tiles |
| Enlarge (`upscale`, FlashVSR+) | consolidated FlashVSR environment | 2.11+cu128 | PyTorch SDPA ("generic"): neither Block-Sparse nor the Triton backend exist on Windows | one-step diffusion | decode and cache offload chosen from the VRAM |
| Frames (`interpolate`, RIFE), tracking (SAM2) | `inference_server_venv` | 2.11+cu128 | triton-windows 3.7.1, sageattention, xformers installed | | |

## What LTX Studio measured and decided (checked by our own study: see [speed-study.md](speed-study.md))

Our measures on this PC, 14 November: the "PyTorch flash kernel" is in fact cuDNN (the Windows torch has no flash SDPA); Sage is not slower end to end (Sage 2: 4 % faster transformer, Sage 1 the same), it just gains little because attention is 4 % of the time;
TeaCache and a first-block cache do speed up stage 1 (about 1.2 times on the transformer at a threshold that keeps the picture within the noise of the backends); torch.compile of the blocks loses (about 1.6 times slower). The old statements follow as LTX Studio wrote them.

- **Attention on Blackwell (superseded for the clips by our own study: SageAttention 2 is the default, [speed-study.md](speed-study.md)).** LTX Studio's statement: `core/attention_backend.py`: 49 frames, 8 steps on an RTX 5090, flash/SDPA 127 s against SageAttention 178 s; the default is `flash`, Sage only by choice. So no Sage is added to the LTX jobs, and the check does not ask for it.
- **TeaCache** exists in the LTX worker (`teacache_thresh`, off there) and is meant for the modes with many steps; we run it at 0.03 on the 12-step clips (setting `ltx_teacache`), chosen on the review page.
- **torch.compile** is used by LTX Studio for one model only (Krea realtime, with flex-attention) and it is turned off on purpose for quantized GGUF; it is not in the LTX video, LongCat or FlashVSR+ paths. `triton-windows` is in four of the environments and was
  never the limit; adding compilation would be a change to LTX Studio's workers, not to ours, and nothing measured says it pays.
- **FP4 and FP8 matmul** are the speed lever LTX Studio uses: the clips run `nvfp4_scaled_mm`, the 2.3 dev checkpoint `fp8_scaled_mm`.

## What was added

- **`avatar_precision`** (media settings, `int8` by default, `fp8` as the option). The talking head runs int8 weight-only: at every layer the weight is converted to bf16 before the matmul. LTX Studio's fp8 variant of the same model multiplies in fp8 with `torch._scaled_mm` and
  falls back to the bf16 conversion if the GPU cannot. Its weights (16 GB) are not on this PC: `python -m tadamoo.media.components install talking-head-fp8`, asked for by name, then `avatar_precision: fp8`. Quality and speed are to be measured against int8 (queue entry 91).
- **`edit_model`** can name the nvfp4 LTX 2.3 checkpoint (`ltx-2.3-22b-dev-nvfp4.safetensors`, 20 GB, on this PC) in place of the fp8 one: `components install video-modification-nvfp4` brings it where it is missing. Not measured with the editing LoRAs.
- **The accelerators of the talking head as a component.** `talking-head-accel` checks that `flash_attn` and `triton` import in the LongCat environment and, only if one is missing, runs LTX Studio's own `scripts/install_accelerators.py --venv <that python> --only ...`, which takes the precompiled wheel
  it keeps (FlashAttention has no Windows wheel on PyPI). On this PC both are there, so nothing runs.

## What is not added, and why

- Sage, FlashAttention or Triton in LTX Studio's own LTX 2.5 environment: never touched. Sage 2 lives in a copy of it (`video-attention`).
- Block-sparse attention of FlashVSR+: the Triton kernel of the FlashVSR+ repository (`operators/flashvsr_attn`) does run on Windows with triton-windows, and with it 2x from 896x512 works (speed-study.md); LTX Studio's runner does not use it.
- A model kept loaded between jobs: LTX Studio does keep persistent servers for its own app, but a TaDaMoo job is one process under one lease that frees everything when it ends, so the language model can come back; inside a job the pipeline is already kept between segments.
  A reload costs seconds to a minute against minutes of work.
- `torch.compile` of the DiTs: see above.

Measures before and after are queue entry 91 of `tests/QUEUE_daytime.md`.

## Models kept loaded across a production (`resident`)

A newscast makes a dozen LTX jobs and as many enhancements. A process for each job starts Python, reads the model and builds it again, and loses what the worker keeps in memory: the LTX 2.5 worker keeps the encoded prompts for the life of its process, the pipeline in a cache,
and FlashVSR+ keeps its model once loaded. In **resident mode** (the media setting `resident`, on by default for a production; `resident_reload_every` sets after how many jobs the model process is renewed) the production orders the work by model and keeps each
model loaded for all its jobs:

1. the stills (image jobs, one process each), the voice (XTTS and Whisper loaded for the stage), then
2. **all the LTX jobs** (the takes, the interlude, the B-roll clips) in one **session**: `client.Session` runs `worker.py --session`, which asks for the VRAM once and keeps one model process (`ltx_launch.py --resident`: the worker imported once, a job for each line of its input, the save quality
   of each job brought in its own line); the session is closed when the last LTX job is done and its lease given back;
3. **all the enhancements** in a second session over `flashvsr_launch.py --resident` (LTX and FlashVSR+ do not fit in the VRAM together: the switch is made once, not twice for each take);
4. the edit.

A job in a session is the job a process of its own would run: the same parameters, the same files, the same pictures (measured: the same bytes for the same inputs, the first job and a later one). Keep the number of different prompts small: the prompt encoding is kept
by prompt, so a take with a prompt seen before skips it. A model process that dies or hangs ends the session and a new one is opened for the job again (what was made is on disk, so a production that stops goes on from its files); the manifest says so in `notes`. At the end
every session is closed, the lease is given back and the language model can return. `manifest.resident` has the sessions (how many workers were opened, how many jobs) and `saved_s`, the seconds not spent on starting models, counted at the cold start measured per job
(`produce.COLD_START_S`: LTX 18 s, enhancement 24 s).

Measured on this PC (GPU at 600 W): an 8.4 s take 50 s in a process of its own and 32 s in the loaded one; its enhancement 70 s and 46 s; the first job of a session pays the load (106 s for a 12.8 s take, 89 s for its enhancement).
On a mini edition (five takes, the interlude, one B-roll clip; the LTX jobs of the run in the loaded process): the five renders took 289 s in processes of their own and 200 s in one session (74, 30, 38, 33, 25 s; the first pays the load), the interlude's generation 143 s and 79 s, the B-roll clip 92 s and 28 s (the prompt of a clip that is not the first costs no encoding of a prompt seen before). The enhancements of that run were not measured in the session (the run was stopped); the single measures above are what there is. The pictures of the first job of a session and of a process of its own are the same bytes, and so are a later job's.

**To turn it off:** the media setting `resident` (false) in `media.json` of the user data folder, or in `/api/media`; a production then runs one process for each job as before. `resident_reload_every` renews the model process after that many jobs.
