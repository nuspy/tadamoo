---
name: image-generation
description: Make a picture or a photograph from a description with the local Qwen-Image 2.1 model of LTX Studio, through TaDaMoo's generate_image tool. Use when a task or the person asks for a picture, an illustration, a still, a photo of something or someone, a poster or a reference image for a clip.
tags: image, picture, photo, generate, qwen-image, ltx
requires: python, ltx-studio
---

# Image generation

A picture from words, made on this PC by LTX Studio's Qwen-Image 2.1 (NVFP4) as its own worker under a VRAM lease. The code is `tadamoo/media/`; the agent tool is `generate_image`.

## When to use it

- A picture, photo, illustration or still from a description: `generate_image`.
- A picture that keeps a person, a place or a style from existing photographs, or changes one: that is `image-editing`.
- A clip that starts from the picture: `video-generation`.

## The tool

`generate_image(prompt, width, height, steps, seed, negative, path)`

| Parameter | Meaning | Limits |
|---|---|---|
| `prompt` | what to show: subject, place, light, style, in a few sentences (English works best) | 4000 characters |
| `width`, `height` | pixels, rounded to a multiple of 32 | 256 to 2048, at most 2048x2048; 1024x1024 by default |
| `steps` | denoising steps; more is slower | 1 to 50; 20 by default |
| `seed` | the same seed, prompt and size give the same picture | left out: one is chosen and returned |
| `negative` | what to keep out | optional |
| `path` | where to save it, relative to the working folder | left out: `media/image-<time>-<seed>.png`; an existing file is never replaced |

The answer has `status`: `ok` (`path`, `seed`, size, `show`), `refused` (no VRAM to lend: tell the person, do not retry in a loop), `unavailable` (what is missing, see Installation), `cancelled`, `error`.

Outside a conversation, `scripts/make_image.py "PROMPT" OUT.png [--width N] [--height N] [--steps N] [--seed N] [--negative TEXT]` makes one picture through the same client.

## Heavy-swap behaviour

The job uses most of the GPU. The server lends the VRAM through a lease; when the local language model holds it, the person (or the `allow` level) is asked and the model steps aside for the job and comes back after it
(`docs/heavy-swap.md`). A skill that makes many pictures takes one swap for all of them (`swap="held"` in `tadamoo.media.client.run`, `HeavyLease` around it, released in `finally`), never one per picture.

## Measured cost

About 110 s for a picture with a reference photograph on an RTX 5090, the model already loaded.

## Installation

Check first, install only what is missing, and only when the person asks:

1. `python -m tadamoo.media.components check image-generation` prints what is present and, for what is missing, the command to run. It changes nothing.
2. `python -m tadamoo.media.components install image-generation` runs those commands and checks again.

Components: **LTX Studio** (a separate program, found through `ltx_dir` in the media settings or `TADAMOO_LTX_STUDIO`; it is installed by hand) and **Qwen-Image 2.1 NVFP4** (the model, its text encoder, VAE and
configuration, fetched by LTX Studio's own downloader `scripts/download_weights.py --ids qwen21-dit-nvfp4`, which also fetches what it depends on and verifies the files).
