---
name: image-editing
description: Edit or restyle a photograph with the local Qwen-Image 2.1 model of LTX Studio, using one to four reference pictures ("image 1", "image 2") through TaDaMoo's generate_image tool. Use when a task or the person asks to change a photo (clothes, background, light, pose, style), to keep the same person or place in a new picture, or to combine several photographs.
tags: image, photo, edit, reference, retouch, qwen-image, ltx
requires: python, ltx-studio
---

# Image editing

A new picture conditioned on photographs: the identity, the studio or the style of the references stay, and the pose, the framing, the clothes or the setting change with the words. It is the same model as `image-generation`, given the
photographs. The code is `tadamoo/media/`; the agent tool is `generate_image` with `refs`.

## When to use it

- Change what a photograph shows (outfit, background, season, light, expression) and keep the person: `refs` with that photograph.
- Put the person of one photograph in the place or the style of another: two references.
- A picture from words alone: `image-generation`.

## How

`generate_image(prompt, refs=["in/photo.png", ...], width, height, steps, seed, negative, path)`; `refs` are files of the working folder (at most 4). The prompt calls them `image 1`, `image 2`, in the order given, and says what
changes and what stays: "Image 1: the same woman and the same studio, wearing a green linen dress, seen a little from the left." Say what must not change in words; the model keeps what the prompt does not ask to change.

Size, steps, seed, the answer and the limits are those of `image-generation`. Outside a conversation, `scripts/edit_image.py REF [REF ...] OUT.png --prompt "TEXT" [--width N] [--height N] [--seed N]` does the same through the client.

A different seed gives a different take on the same words: fix the seed to repeat a result, change it to see another.

## Heavy-swap behaviour

The same as `image-generation`: most of the GPU, one lease, the local model steps aside and returns; many edits take one swap for all of them, released in `finally`. A `refused` answer is told to the person and not retried in a loop.

## Measured cost

About 110 s for a picture with one reference photograph on an RTX 5090, the model already loaded.

## Installation

The same components as `image-generation`; check first, install only what is missing, only when the person asks:

1. `python -m tadamoo.media.components check image-editing`
2. `python -m tadamoo.media.components install image-editing`

LTX Studio is installed by hand; the Qwen-Image 2.1 NVFP4 model is fetched by LTX Studio's own downloader (`scripts/download_weights.py --ids qwen21-dit-nvfp4`).
