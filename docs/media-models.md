# Image and video models: the small catalogue

Models for pictures and video that fit beside the language model on one 32 GB GPU. Sizes are the weights on disk; VRAM is what a worker
needs while it runs. **Measured** means run on the maintainer's RTX 5090; everything else is read from the LTX Studio project
(`E:\AI\LTXStudio`, its `data/weights_catalog.json` and workers) and is an estimate. The weights and workers belong to LTX Studio; TaDaMoo
runs the two it chose as one subprocess per job under a lease, and offers them to the Chat and the Agent as `generate_image` and
`generate_video` ([media/README.md](media/README.md)). The other rows are the catalogue of what else would fit.

The VRAM comes from the lease broker (`docs/vram-broker.md`): a heavy job runs alone, the expert cache shrinks for its duration and the
language model is slow meanwhile. Where the model does not fit even then, the local engine can sleep (`SLEEP`/`WAKE`) and the text side can
run on a provider the person authorised.

## Images

| Model | Weights | VRAM | Notes |
|---|---|---|---|
| Qwen-Image 2.1, DiT nvfp4 + text encoder nvfp4 | 4.2 GB + 6.3 GB | **10.5 GB measured** | 1024x1024, 20 steps in about 8 s, loads in 5 s; quality very good on a newsroom portrait. Licence "Qwen Research": not commercial. Worker `workers/qwen_image21_infer.py`, profile `nvfp4`. |
| Qwen-Image 2.1, fp8 or int8 | 7.1-7.3 GB + 9.4 GB encoder | about 14 GB | Same worker; larger, closer to the reference. |
| FLUX.3 Klein | DiT 6.3 GB, encoder 8.9 GB | estimate: per phase, the models do not coexist | "FLUX Kommunity" licence: commercial output only below a revenue limit. |
| FLUX.1 with nunchaku fp4 | 6.6 GB | 9.5 GB measured by LTX Studio | Needs a Blackwell card. |
| Upscalers: ESRGAN family, SeedVR2 3B fp8, RIFE | 0.03-3.2 GB | up to about 4 GB | The smallest and the most useful with little VRAM. |

The text encoder (Qwen3-VL-8B) runs once per prompt and is the larger part of the VRAM. The wish was to keep it in RAM (fp8 storage, about
9 GB, computed in bf16), which needs a CPU copy of the encoder that is not on the disk (about 16 GB bf16) and a worker that can run it there:
LTX Studio's worker loads the nvfp4 encoder on the GPU only. What is built is that worker as it is (the 10.5 GB path, lease 11000 MiB); no
encoder was downloaded. Another way to a lower peak, not built: encode the prompt first, free the encoder, then load the DiT (about 7 GB
at the peak; the pipeline takes `prompt_embeds`), which would need a worker of TaDaMoo's own and a real run to prove it.

## Video

| Model | Weights | VRAM | Notes |
|---|---|---|---|
| LTX-2.5 distilled, transformer nvfp4 | 18.7 GB | **23.8 GB peak measured** | 704x512, 65 frames (2.6 s) in 150 s including a 54 s prompt encode that is cached afterwards. Runs through `core/ltx_video_job.generate()`; prompt adherence is moderate. Needs the local language model asleep or most of its cache lent. |
| LTX-2.5 / 2.3 distilled GGUF Q4_K_M | 16.4 GB | about 13.5 GB (LTX Studio's cache) | Smaller footprint, slower. A Q2_K of 8.2 GB is listed in the catalogue but is not on this PC and its quality is not assessed. |
| Self-Forcing 1.3B (Wan 2.1 1.3B) | about 6 GB | estimate about 8 GB, block-swap available | Apache-2.0. 480p clips, few steps. Not run here. |
| LingBot 1.3B fp8 | 8 GB | its own check wants about 22 GB resident | Not for this PC. |

## How LTX Studio loads and unloads (ideas worth reusing)

- Lazy load with an LRU manager that demotes an unloaded model to CPU RAM instead of destroying it (`inference_server/model_manager.py`).
- Shared components with reference counts: T5, CLIP, VAE (`component_store.py`).
- Three placements: all on the GPU, model offload, sequential offload; fp8 layer-wise casting; VAE tiling.
- One process per job, which exits when the job ends and frees everything.
- An inter-process GPU lock file (`data/gpu.lock`) and a per-process VRAM ledger. A TaDaMoo component must respect that lock when both
  run on one card: another CUDA program that allocates while the engine idles can evict it (`docs/WINDOWS.md`, the commit section).

## Licences

Not uniform and often not declared. Qwen-Image 2.1: research, non commercial. FLUX.3 Klein: Kommunity licence. Self-Forcing, RIFE and
FlashVSR_plus: Apache or MIT. FLUX.1, Z-Image, ERNIE and LTX: not declared in the LTX Studio catalogue; check the source before use.
