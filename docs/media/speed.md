# Speed: what each video job runs on, and what we reuse from LTX Studio

TaDaMoo does not carry its own accelerators. Every job runs LTX Studio's own worker in the Python environment LTX Studio keeps for that model, with the versions and the choices LTX Studio made and measured
(`E:\AI\LTXStudio`, `scripts/install_*.py`, `scripts/install_accelerators.py`, `core/attention_backend.py`). What follows is what those environments hold on this PC (read on 14 November with `pip list`, nothing run on the GPU).

| Job (kind) | Environment | torch | Attention | Weights and matmul | Caches and memory |
|---|---|---|---|---|---|
| Picture (`image`) | `diffusers_next_venv` | 2.9.1+cu130 | PyTorch SDPA; flash-attn 2.8.3 and triton-windows 3.5.1 installed | Qwen-Image 2.1 DiT and text encoder in nvfp4 | the encoder shares the GPU for the few seconds of the job |
| Clip (`video`, LTX 2.5) | `lingbot_venv` | 2.14 nightly +cu130 | PyTorch SDPA (its flash kernel); no triton, sage or flash-attn installed | distilled transformer nvfp4, `nvfp4_scaled_mm` (the GPU's fp4 matmul) | prompt-embedding cache on, streaming VAE decode in tiles |
| Change of a clip (`modify`, LTX 2.3) | `ltx2_venv` | 2.8.0+cu128 | PyTorch SDPA by LTX Studio's choice; triton-windows 3.4.0 and sageattention 2.2.0 installed but Sage is off | dev checkpoint fp8, `fp8_scaled_mm`; nvfp4 also on disk | distilled LoRA at 0.5, prompt cache, the worker chooses by itself between resident and streamed blocks |
| Talking head (`avatar`) | `LongCatAvatar\venv` | 2.9.1+cu130 | flash-attn 2.8.3 (the weights' config says `enable_flashattn2`); block-sparse off (no Windows kernel); triton-windows needed by its module | int8 weight-only (dequantized to bf16 at every layer), or fp8 with the GPU's `torch._scaled_mm`; 8 steps distilled | LoRA fused into the weights and KV cache offload both decided from the free VRAM, VAE in tiles |
| Enlarge (`upscale`, FlashVSR+) | consolidated FlashVSR environment | 2.11+cu128 | PyTorch SDPA ("generic"): neither Block-Sparse nor the Triton backend exist on Windows | one-step diffusion | decode and cache offload chosen from the VRAM |
| Frames (`interpolate`, RIFE), tracking (SAM2) | `inference_server_venv` | 2.11+cu128 | triton-windows 3.7.1, sageattention, xformers installed | | |

## What LTX Studio measured and decided (checked by our own study: see [speed-study.md](speed-study.md))

Our measures on this PC, 14 November: the "PyTorch flash kernel" is in fact cuDNN (the Windows torch has no flash SDPA); Sage is not slower end to end (Sage 2: 4 % faster transformer, Sage 1 the same), it just gains little because attention is 4 % of the time;
TeaCache and a first-block cache do speed up stage 1 (about 1.2 times on the transformer at a threshold that keeps the picture within the noise of the backends); torch.compile of the blocks loses (about 1.6 times slower). The old statements follow as LTX Studio wrote them.

- **Attention on Blackwell: the plain PyTorch kernel.** `core/attention_backend.py`: 49 frames, 8 steps on an RTX 5090, flash/SDPA 127 s against SageAttention 178 s; the default is `flash`, Sage only by choice. So no Sage is added to the LTX jobs, and the check does not ask for it.
- **TeaCache** exists in the LTX worker (`teacache_thresh`, off) and is meant for the modes with many steps; the clips here are an 8-step distillation, so it stays off.
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

- Sage, FlashAttention or Triton in the LTX 2.5 environment: LTX Studio measured the plain kernel faster and left them out.
- Block-sparse attention of FlashVSR+: the Triton kernel of the FlashVSR+ repository (`operators/flashvsr_attn`) does run on Windows with triton-windows, and with it 2x from 896x512 works (speed-study.md); LTX Studio's runner does not use it.
- A model kept loaded between jobs: LTX Studio does keep persistent servers for its own app, but a TaDaMoo job is one process under one lease that frees everything when it ends, so the language model can come back; inside a job the pipeline is already kept between segments.
  A reload costs seconds to a minute against minutes of work.
- `torch.compile` of the DiTs: see above.

Measures before and after are queue entry 91 of `tests/QUEUE_daytime.md`.
