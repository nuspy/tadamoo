---
name: video-generation
description: Make a short video clip from a description, or from a starting picture and a description, with LTX 2.5 distilled of LTX Studio, and make any clip smoother (real frames made between the real ones, RIFE) and larger (more pixels and detail, FlashVSR+), through TaDaMoo's generate_video and enhance_video tools. Use when a task or the person asks for a video, an animation, a b-roll clip, a picture brought to life, or a clip that looks small, soft or jerky.
tags: video, clip, text-to-video, image-to-video, frame rate, interpolation, upscale, ltx, rife, flashvsr
requires: python, ffmpeg, ltx-studio
---

# Video generation

Clips from words or from a picture, and the means to improve them. The code is `tadamoo/media/`; the agent tools are `generate_video` and `enhance_video`.

## When to use it

- A clip from a description: `generate_video` with `prompt`.
- A clip that starts from a picture (a photograph, an image from `image-generation`, an edit from `image-editing`): `generate_video` with `prompt` and `image`; the picture is the first frame.
- A clip that looks jerky or too small, or a final video that must be smoother or larger: `enhance_video`.
- Cutting, joining, camera moves and graphics over clips: `video-editing`. A person who speaks a recording: `talking-head`.

## generate_video

`generate_video(prompt, image, width, height, frames, fps, seed, negative, path)`; caps in [references/tools.md](references/tools.md).

- A clip is 704x512, 65 frames at 25 fps (2.6 s) by default; at most 1280x720 and 161 frames (6.4 s at 25 fps). Frames are rounded to 8k+1 and sizes to a multiple of 64; the answer says what was rounded.
- The model makes every frame it is asked for: a 25 fps clip has 25 different pictures a second (checked on the clips of a newscast run: no repeated frame). Its motion can still look stiff at small sizes, which is what
  `enhance_video` is for.
- The prompt says what happens: subject, action, camera, light. No sound is made.
- One clip is short on purpose: a longer scene is several clips joined (`video-editing`), each from its own prompt or from the last picture of the one before.

## enhance_video

`enhance_video(path, scale, factor, mode, output)`: a better copy of a video of the working folder; the original is kept. With neither `scale` nor `factor` it enhances the picture with FlashVSR+ in its Turbo mode: the one enhancer of the product (the newscast enhances every take with it). The copy has exactly the size, the frames and so the duration of the original: "better" means the blockiness removed and the detail restored, not more pixels.

- `factor` 2 to 4: the frames a second are multiplied by RIFE (25 becomes 50 at 2): new frames are made between the real ones, the length does not change, nothing is slowed down.
- `scale` 1 to 4: the size FlashVSR+ works at (2, the default: it works at twice the size and the result is brought back to the original size with a Lanczos downscale; 1: it restores at the original size). Before, `scale` multiplied the width and height by FlashVSR+ (`mode` turbo, faster, or base, finer): more pixels and the detail an AI enlarger infers, with the sound kept when the clip has any.
- Both: the picture is enlarged first and the frames are made after, at the final size, because the enlarger is the slow step and sees fewer frames that way.
- Choosing: smoothness first (`factor` 2), then size (`scale` 2). A video that goes into an edit at a lower size gains nothing from `scale`; the frame rate of the edit must be a multiple of the clip's (25 to 25 or 50), because
  dropping to 24 removes a frame a second and brings the hitch back.
- Outside a conversation: `scripts/enhance.py VIDEO OUT.mp4 [--scale N] [--factor N] [--mode turbo|base]`; a new clip: `scripts/make_clip.py "PROMPT" OUT.mp4 [--image PICTURE] [--frames N] [--seed N]`.

The comparison of the options (RIFE, FILM, Real-ESRGAN, SeedVR2, FlashVSR+, LTX's own upscalers, DLSS and NVIDIA's video SDKs) is in `docs/media/enhance.md` of the repository.

## Heavy-swap behaviour

Every job uses most of the GPU, each under its own lease (clip, enlarger, frame maker). The server lends the VRAM; when the local language model holds it, the person (or the `allow` level) is asked and the model steps aside and
comes back after the job (`docs/heavy-swap.md`). A skill that makes many jobs takes one swap for all of them (`swap="held"` in `tadamoo.media.client.run`, `HeavyLease` around it, released in `finally`). A `refused`
answer is told to the person and not retried in a loop.

## Measured costs (RTX 5090)

A 6.4 s clip about 90 to 150 s. RIFE and FlashVSR+ have no measure of ours yet; LTX Studio's own notes give RIFE 24 to 60 fps in about 3 s for a short clip and an AI enlarger a few seconds a second of video (see `docs/media/enhance.md`).

## Installation

Check first, install only what is missing, only when the person asks:

1. `python -m tadamoo.media.components check video-generation` prints what is present and the command for what is missing; it changes nothing.
2. `python -m tadamoo.media.components install video-generation` runs those commands and checks again.

Components: **LTX Studio** (a separate program, installed by hand; `ltx_dir` in the media settings or `TADAMOO_LTX_STUDIO`); **LTX 2.5 distilled NVFP4** with its text encoder and VAEs (about 17 GB of weights, fetched by LTX
Studio's downloader, `--ids ltx25-transformer-nvfp4`); **ffmpeg** on the PATH (`winget install Gyan.FFmpeg`); **RIFE** (the code ships with LTX Studio; the 24 MB `flownet.pkl`, `--ids rife-flownet-v4`; MIT);
**FlashVSR+** (its code, `scripts/install_flashvsrplus.py`, and about 7 GB of weights, `--ids flashvsr-plus-turbo`; Apache-2.0); **SageAttention 2.2.0** (`video-attention`: a copy of LTX Studio's LTX 2.5 environment in `ltx_attention_env` with the wheel of `ltx_wheels`;
`docs/media/sage-wheel.md` says where the wheel is and how to build it; without it the clips use cuDNN attention and say so in `warning`). The frame maker and the enlarger are optional: without them only `generate_video` is offered.

## Speed

Clips run LTX 2.5 distilled in nvfp4 (the GPU's fp4 matmul) with SageAttention 2 and TeaCache 0.03 (settings `ltx_attention`, `ltx_teacache`; chosen by our own measures, `docs/media/speed-study.md`); the prompt embeddings are cached and the decode is streamed in tiles. Nothing to load or install for speed: the environments are LTX
Studio's own (`docs/media/speed.md` says what each holds). `enhance_video` runs FlashVSR+ with the Triton block-sparse kernel of its own repository (through `tadamoo/media/flashvsr_launch.py`; LTX Studio's runner alone falls back to an SDPA mask that is refused at 2x), saved at crf 4, and RIFE in the inference environment, which has triton-windows, sageattention and xformers. A job that takes longer than the limit of its kind
(`docs/media/runner.md`) is ended with an error of the step.
