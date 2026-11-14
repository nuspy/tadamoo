---
name: video-modification
description: Change what a video that already exists shows, with AI and not with a cut: swap an object for another (the yellow Fiat 500 for a red Ferrari) from words or from a photograph of the new thing, change the whole video by words, or give it the look of a picture, with the LTX 2.3 editing LoRAs and the SAM2 tracker of LTX Studio, through TaDaMoo's modify_video tool. Use when a task or the person asks to replace, change or restyle something inside a filmed or generated clip. Not for cutting, joining or camera moves (video-editing) and not for making a clip from nothing (video-generation).
tags: video, edit, inpaint, replace object, restyle, sam2, ltx, vace
requires: python, ffmpeg, ltx-studio
---

# Video modification

A copy of a clip in which something has been made again: the original is kept, its sound is put back on the copy. The code is `tadamoo/media/` (`modify_job.py` is the plan of the job); the agent tool is `modify_video`.

## What it can do and what it cannot

Be plain about it with the person before spending the GPU.

- **It can**: replace one named object with another (`replace`), keeping the rest of every frame as it was; change the whole clip by words, such as the time of day or the season (`edit`); give it the look of a picture (`restyle`). It works on clips of at most 20 seconds, up to 1280 pixels on the long side.
- **It cannot** do it exactly. The new thing is invented again in every frame, from the prompt and the photograph; its shape, its details, a number plate, a logo, a text will drift and will not match the photograph to the pixel. A thing with another silhouette (a long low car in place of a short tall one) leaves a trace of the old one or hides part of the new one, unless `grow` is large enough. Shadows and reflections that fall outside the mask stay as they were. A subject that leaves the frame, is hidden for a moment or moves very fast may be lost by the tracker. The sound is the original's: the engine of the old car is still heard.
- **Works best**: a short clip, a steady or slowly moving camera, one clearly visible object, a new thing of a similar size and shape, a photograph of it seen from the same side.
- **Not offered here** (LTX Studio has them, TaDaMoo does not call them): replacing a person by a photograph, changing the background behind a person, new speech with matching lips. Wan VACE, the other model that edits video under a mask, is not installed and not in LTX Studio's catalogue. The comparison is in [references/engines.md](references/engines.md).

## How

`modify_video(path, prompt, mode, target, reference, strength, guide, grow, seed, output)`; the caps are in `docs/media/modify.md`.

- `replace`: `target` is what to swap, in a few English words ("yellow Fiat 500"); `prompt` says what is there instead and what it does ("a red Ferrari drives along the street, same light"); `reference` is a photograph of the new thing in the working
  folder (make it with `image-generation` or `image-editing`), or left out to work from the words alone. `strength` (0.1 to 1, default 0.8) is how closely it follows the photograph; `guide` (default 0.4) how closely it keeps the motion and the shape of
  the original; `grow` (default 24 pixels) how far the new area goes beyond the outline of the old object: more for a bigger new thing, less to keep more of the original.
- `edit`: `prompt` says what to change in the whole clip ("the same street at night, wet road"); `strength` 1 by default.
- `restyle`: `reference` is the picture whose look the clip takes; `prompt` is optional.
- Try a short stretch first (cut with `video-editing`), look at it, then the whole clip. A different `seed` gives another take on the same words.
- Outside a conversation: `scripts/modify_video.py VIDEO OUT.mp4 --mode replace --target "yellow Fiat 500" --prompt "a red Ferrari" [--reference ferrari.png]`.

## Heavy-swap behaviour

The job asks the server for most of the GPU under one lease (owner `modify`), first for the tracker and then for the model, both inside it; when the local language model holds the VRAM, the person (or the `allow` level) is asked and the model steps aside and comes back after the job
(`docs/heavy-swap.md`). A skill that makes several changes takes one swap for all of them (`swap="held"` in `tadamoo.media.client.run`, `HeavyLease` around it, released in `finally`). A `refused` answer is told to the person and not retried in a loop. The job is long:
a clip is made in segments of about four seconds and each takes minutes (see the measures in `docs/media/modify.md`: none of ours yet).

## Installation

LTX 2.3, not the 2.5 of the clips: the editing LoRAs exist for 2.3 only. Check first, install only what is missing, only when the person asks:

1. `python -m tadamoo.media.components check video-modification` prints what is present and the command for what is missing; it changes nothing.
2. `python -m tadamoo.media.components install video-modification` runs those commands and checks again.

Components: **LTX Studio** (a separate program, installed by hand); **ffmpeg** on the PATH; **LTX 2.3 and SAM2** (the dev checkpoint, its distilled LoRA, the spatial upscaler, the Gemma text encoder, SAM2 and GroundingDINO; about 60 GB, fetched by LTX Studio's downloader,
`--ids ltx23-dev-fp8,ltx23-distilled-lora-384,ltx23-spatial-upscaler,ltx23-gemma-te,sam2-hiera-large,grounding-dino-base`); **the other LoRAs** (the photograph one and the look one by the downloader, `--ids ltx23-inpaint-r2v,ltx-iclora-restyle`). The two community LoRAs
that have no entry in LTX Studio, `ltx23_inpaint_masked_t2v_rank128_v1_10000steps.safetensors` (replace from words) and `ltx23_edit_anything_global_rank128_v1_9000steps_adamw.safetensors` (edit), are put by hand in the `LTX-2` folder of LTX Studio's LoRA folder; the check names them.
`edit_model` in the media settings names another LTX 2.3 checkpoint; a fine-tune is never taken by itself.

## Speed

LTX 2.3 runs in LTX Studio's `ltx2_venv` (torch 2.8.0+cu128, triton-windows 3.4.0, sageattention 2.2.0 installed but off: plain attention is the faster on Blackwell) with the fp8 checkpoint. The nvfp4 checkpoint of the same model (20 GB, smaller in VRAM) is the one to try for speed:
`python -m tadamoo.media.components install video-modification-nvfp4`, then `edit_model` is `ltx-2.3-22b-dev-nvfp4.safetensors`; it is not measured with these LoRAs. Do not install anything of this skill on a PC that has no room (60 GB). `docs/media/speed.md`, queue entry 91.
