# LTX 2.5 audio-to-video: every parameter there is, and the checkpoints on this PC

Read on 14 November 2026 from LTX Studio's worker (`E:\AI\LTXStudio\workers\ltx2_infer.py`), its job builder (`core/ltx_video_job.py`), its release registry (`core/ltx_release.py`) and the installed package (`ltx-pipelines` 1.2.0, `ltx-core` 1.2.0 in
`E:\AI\Tools\lingbot_venv`, read with `inspect`). Nothing was run for this page. "Used now" is what the matrix of 14 November sent (`tests/motion_matrix.py`: 896x512, 169 frames, 25 a second, the 8-step runs); "default" is the worker's or the package's own.
"In our client" says whether `tadamoo/media/limits.py` and the tool `generate_video` take it (**tool**), the media client takes it in the request of a job but the tool does not (**request**), or it is not reachable (**no**).

## How an audio-to-video job goes

The job's parameters (a JSON file) go to the worker; with an `audio_path` the worker builds `A2VidPipelineTwoStage` (the same class for LTX 2.3 and 2.5) and calls it:

1. The recording is decoded (`audio_start_time`, `audio_max_duration`); its length is the length of the clip.
2. **Stage 1** makes the video **at half the size asked for** (896x512 asked: 448x256), with `steps` steps of the scheduler's own sigmas and the guider (`guidance`, `stg`), the first picture as a conditioning at frame 0.
3. The latent is enlarged x2 by the spatial upscaler.
4. **Stage 2** refines it at the full size in **3 fixed steps** (sigmas 0.9094, 0.7250, 0.4219, 0), the first picture conditioning again.
5. The VAE decodes in tiles, the video is saved by PyAV (x264), the recording given is mixed in as the sound.

Two things follow that the matrix showed: the number of steps is honoured (the log counted 8/8, 12/12, 20/20 for stage 1, then 3/3), and the **sharpness is bound by stage 1 at half size and by the upscaler**, not by the saving: saving at crf 4 instead of 19 made the file 13 times larger (0.75 to 9.7 Mbit/s) and the face sharpness went from 93 to 99.

## The parameters of the job

| Parameter | Used now | Default | What it does | In our client |
|---|---|---|---|---|
| `prompt` | the presenter prompt | "" | What happens; the text encoder is Gemma 4 | tool |
| `negative` | the presenter negative | "worst quality, blurry" | What to keep out (used by the guider; see `guidance`) | tool |
| `width`, `height` | 896, 512 | 768, 512 (worker) | The size of the **final** video; rounded to a multiple of 64 by the worker (latent steps are 32 px); stage 1 runs at half | tool (256 to 1280, at most 1280x720) |
| `num_frames` | 169 | 121 | Frames, 8k+1; for audio-to-video the length of the recording is what counts (`audio_max_duration` cuts it) | tool (9 to 161; the matrix went round the cap with a request) |
| `fps` | 25 | 25 | Frames a second; the audio is cut to `frames / fps` | tool (8 to 30) |
| `seed` | 20261114 | 42 | The noise | tool |
| `steps` | 8 (12, 20 tried) | 8 in `build_params`; 30/40 in the worker's own fallbacks | Steps of stage 1; stage 2 stays 3 | request |
| `guidance` | 3.0 | 3.0 | CFG scale of the video guider (`cfg_scale`): 3 doubles the cost of a step with the negative prompt. The catalogue notes of the distilled fine-tunes say CFG 1 and 8 steps; not tried here | **no** (`build_params` has it, the plan does not pass it) |
| `stg` | 1.0 | 1.0 | Spatio-temporal guidance scale (perturbs block 29); 0 turns it off | no |
| (guider constants) | rescale 0.7, modality 3.0, stg block 29 | the same | `rescale_scale`, `modality_scale`, `stg_blocks`: written in the worker, not parameters of it | no |
| `image_path` | the still | none | The first picture, a conditioning at frame 0 (both stages) | tool (`image`) |
| `ref_strength` | 1.0 | 1.0 | How hard the first picture binds the first frame; below 1 the model reinterprets it; `i2v_method: "inplace"` caps it at 0.6 | no |
| (image crf) | 33 | 33 (package; 18 for LTX 2.4) | The compression the first picture is given before it conditions: written in the worker's call, not a parameter | no |
| `keyframes` | none | none | Pictures at other frames: `[{path, frame, strength}]` | no |
| `audio_path` | the recording, or its smoothed copy | none | Turns the job into audio-to-video; stereo is made of a mono file | request |
| `audio_start_time` | 0 | 0 | Where in the recording to start (seconds); the long-clip chunks set it | no |
| `audio_max_duration` | none | none | Cut the recording after this many seconds | no |
| `enhance` | off | false | The prompt is rewritten by Gemma before use | no |
| `model` | `ltx-2.5-22b-distilled-transformer-nvfp4.safetensors` | the release's checkpoint | The transformer file (the 2.5 release is a split pack: text encoder, video VAE, audio VAE with vocoder, spatial and temporal upscalers, duration head, diffusion decoder, each its own file) | settings (`video_model`) |
| `ltx_release` | 2.5 | 2.3 | Which release: it chooses the Python (`lingbot_venv` for 2.5), the components, `sigma_driven` | fixed to 2.5 by the plan |
| `quant` | nvfp4 (`nvfp4_scaled_mm`, chosen from the file name) | `fp8_cast`; `fp8_scaled_mm` for fp8 files | How the transformer multiplies: the GPU's fp4 or fp8 units, or a cast | no (automatic) |
| `offload` | auto (all resident when the VRAM allows) | auto | `none`, `cpu` (blocks streamed from RAM), `disk` | no |
| `gguf_transformer` | none | none | A GGUF transformer in place of the safetensors (only when chosen) | no |
| `loras` | none | [] | `[{name, strength}]` LoRAs applied to the transformer | no |
| `decode_lowmem` | off | off | Smaller VAE tiles; in 2.5 the package decides (auto tiling) and this does nothing | no |
| `emb_cache` | on | on | Keeps the encoded prompt for the next job | no |
| `teacache_thresh` | 0 (off) | 0 | TeaCache: skips steps that change little; meant for many-step modes | no |
| `chunk_frames`, `overlap_frames` | none | 0 | A long clip is made in pieces (the recording is sliced by `audio_start_time`); the audio-to-video pieces are joined without a cross-fade | no |
| `native_audio` | off | off | Keep the sound the model makes; with an `audio_path` the given recording is the sound anyway | no |
| `ltx_te`, `ltx_te_lora` | none | none | Another text encoder (e.g. an uncensored Gemma 4) or a LoRA on it | no |
| `crf`, `crf_preset` | 4, medium (hq runs); not set (first runs) | crf 19, preset veryfast, written in the worker | How the video is saved; set from outside by `tadamoo/media/ltx_launch.py` because the worker has no parameter for it. What LTX Studio needs is a `video_crf`/`video_preset` in `_encode_video_pyav` | request |
| `output`, `resume` | a path, off | | Where to write; reuse the pieces of a stopped chunked job | internal |

Other modes of the same worker (not audio-to-video): `control_video`, `control_type`, `control_strength` (IC-LoRAs), `edit_lora`, `inpaint_*`, `mask_*`, `retake`, `start_time`/`end_time`, `msr_*`/`references` (several subject pictures), `lipdub_*`, `hdr_*`, `t2a`.
`modify_video` uses several of them (docs/media/modify.md).

## What the package takes and the worker does not pass

`ltx_pipelines` 1.2.0, `A2VidPipelineTwoStage`: these exist and none is reachable from a job file today; each needs a line in the worker (LTX Studio's) or a launcher like `ltx_launch.py`.

| Argument | Default | What it does |
|---|---|---|
| `stage_1_sigmas` | the scheduler's, for `steps` steps | The schedule of stage 1 by hand; the distilled one is 1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0 (8 steps) |
| `stage_2_sigmas` | 0.9094, 0.7250, 0.4219, 0 | The refinement of stage 2: more steps or other values change the detail after the upscale |
| `max_batch_size` | 1 | Guider passes batched together (memory for speed) |
| `vae_dtype` | the model's | Precision of the VAE decode |
| `tiling_config` | auto | Size and overlap of the decode tiles |
| `enhance_static_cache` | false | A static cache for the prompt enhancer |
| `color_space` | none | An HDR colour space for the output |
| constructor `compilation_config` | none | `torch.compile` of the transformer (mode, backend inductor, dynamic) |
| constructor `alloc_trim_strategy` | trim | When the allocator gives memory back (`trim` or `defer`) |
| constructor `diffvae_optimization` | chunked_eager | How the diffusion VAE runs: `chunked_eager`, `chunked_compile`, `combined_compile`, `blackwell_dsl` |
| constructor `prompt_enhancer_gemma_root` | none | The Gemma the enhancer uses |
| constructor `distilled_lora`, `spatial_upsampler_path` | the release's | The LoRA a dev checkpoint needs; the x2 spatial upscaler (the 2.5 file, falling back to the 2.3 one) |
| `MultiModalGuiderParams.skip_step` | 0 | Skips the guided pass at some steps |

Size limits: the worker has no ceiling of its own (the VRAM is the ceiling); the video latent steps are 32 pixels and 8 frames (+1), the worker rounds to 64 pixels. Our tool caps at 1280x720 and 161 frames. **Since stage 1 is half the size, a face is made from about a quarter of the pixels it ends with**: a larger request
(1280x704, stage 1 at 640x352) is the cheapest thing to try for sharpness, and the one the numbers point to; it has not been tried.

## Checkpoints on this PC (`E:\AI\Models\diffusion_models` and `...\LTX`)

LTX Studio's own catalogue (`data/weights_catalog.json`) says what each is. The release that runs in LTX Studio's 2.5 environment is chosen by the file name.

| File | Size | What it is |
|---|---|---|
| `diffusion_models/ltx-2.5-22b-distilled-transformer-nvfp4.safetensors` | 18.7 GB | **LTX 2.5, official (Lightricks), distilled, nvfp4.** The one TaDaMoo uses for clips and for the audio-to-video runs |
| `LTX/ltx2.5-Stubelius_remix_v1_distilled_int8_convrot.safetensors` | 21.5 GB | **Third-party fine-tune** of 2.5: "Stubelius Remix v1", 10Eros v1.5 + the distilled LoRA + a reinforcement-learning merge fused, distilled (8 steps, CFG 1), int8 ConvRot weights; adult-content model, LTX-2.x Community licence |
| `LTX/ltx2.5-uncensored-v1.1-distilled-int8-native.safetensors` | 21.5 GB | **Third-party fine-tune** of 2.5: "Uncensored Turbo v1.1", 10Eros (0.7) + DMD distillation + IC-LoRA + detailer + img2vid fused (4 to 8 steps, CFG 1), int8; adult-content model; accepts `[VISUAL]`, `[SPEECH]`, `[SOUNDS]` in the prompt |
| `LTX/ltx2.5-uncensored-v1.1-distilled-Q8_0.gguf` | 22.3 GB | The same "Uncensored Turbo v1.1" as a GGUF Q8_0 (community quantization); LTX Studio's note says the GGUF loader of 2.5 was still to be validated |
| `LTX/ltx-2.3-22b-dev-fp8.safetensors` | 29.1 GB | LTX **2.3**, official dev, fp8 (used by `modify_video`) |
| `LTX/10Eros_v1.5_fp8mixed_experimental_learned.safetensors`, `ltx10eros_v14_2989669.safetensors` | 30.2, 29.2 GB | LTX **2.3** third-party fine-tunes "10Eros" v1.5 and v1.4 (adult content) |
| `LTX/PinkCherry_FineTune_fp8scaled_v1_8_LTX23.safetensors` | 27.6 GB | LTX **2.3** third-party fine-tune "PinkCherry" v1.8 (adult content) |
| `LTX/_ltx23_aux_components.safetensors` | 4.1 GB | The parts a 2.3 transformer-only file needs (VAEs, text projection): a companion, not a model |
| `diffusion_models/ltx-2-3-22b-dev-Q6_K.gguf` | 16.6 GB | LTX 2.3 dev as a GGUF Q6_K (community) |

The three 2.5 files other than the official one are not taken by TaDaMoo by themselves: `video_model` in the media settings names the 2.5 file, and nothing but the official one has been run here. They are distilled for CFG 1, which the worker does not set today (`guidance` stays 3.0).
