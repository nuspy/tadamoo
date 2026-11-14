---
name: talking-head
description: Make a person from a still photograph speak a recording, as one continuous video of any length, with LongCat-Video-Avatar 1.5 of LTX Studio, through TaDaMoo's media client (the avatar kind). Use when a task needs a presenter, a spokesperson or any portrait that speaks a given voice recording.
tags: avatar, talking head, presenter, lip sync, longcat, ltx
requires: python, ltx-studio
---

# Talking head

A person (a reference still) who speaks a recording, as one video as long as the recording. The code is `tadamoo/media/`; the kind is `avatar`, called through `tadamoo.media.client.run` (it has no agent tool: the newscast and
any other skill call it).

## When to use it

- A still and a voice recording become a speaking video: `avatar`.
- The still itself, its outfit or setting: `image-generation`, `image-editing`. The voice: `speech-synthesis`. Cutting the result into an edit: `video-editing`.

## The kind

| Kind | Parameters | Limits |
|---|---|---|
| `avatar` | `image` (still), `audio` (recording), `prompt` (performance), `negative`, `resolution` (480p or 720p), `seed` | the video lasts as long as the audio; the model has no guidance knob (8 steps, fixed) |

Details in [references/avatar.md](references/avatar.md). `scripts/make_take.py STILL AUDIO OUT.mp4 [--prompt TEXT] [--negative TEXT]` makes one take through the same client.

## Heavy-swap behaviour

The job uses most of the GPU. The server lends the VRAM through a lease; when the local language model holds it, the person (or the `allow` level) is asked and the model steps aside for the job and comes back after it
(`docs/heavy-swap.md`). A skill that makes many takes holds **one** swap for the whole production (`swap="held"` in `tadamoo.media.client.run`, `HeavyLease` around it, released in `finally`), never one per take; never start
a job while another session has the model loaded for work that must not be interrupted. A `refused` answer is told to the person and not retried in a loop.

## Measured costs (RTX 5090)

About 65 s of GPU for each second of speech at 480p int8 (a 13.7 s recording took 893 s).

## Quality notes that cost hours to find

- A talking head moves like the sound it hears: loud, sharp audio gives wide mouth shapes and gestures. Feed it a **tamed copy** of the voice (quieter, compressed, band-limited) and a still with the
  hands out of the picture; the real soundtrack is mounted over the result. Measure the result against a reference video (mouth opening, head motion) instead of trusting the eye.
- One long continuous take has no seams; a longer one drifts after about a minute (skin, hands), so a take per story is a good length.
- Generation is stochastic: fix the seed to repeat a result.

## Installation

Check first, install only what is missing, only when the person asks:

1. `python -m tadamoo.media.components check talking-head` prints what is present and the command for what is missing; it changes nothing.
2. `python -m tadamoo.media.components install talking-head` runs those commands and checks again.

Components: **LTX Studio** (a separate program, installed by hand) and the **LongCat Avatar environment** (its own Python environment, built by LTX Studio's `scripts/install_longcat_avatar.py`). Its int8 weights then come
from the Weights tab of LTX Studio, which verifies them; the check names what is still lacking.

## Speed

The job runs in LTX Studio's LongCat environment (torch 2.9.1+cu130, FlashAttention 2.8.3, triton-windows), as LTX Studio installs it. `python -m tadamoo.media.components check talking-head` also checks that `flash_attn` and `triton` import there and, only if one is missing, runs LTX Studio's own
accelerator installer for it (`scripts/install_accelerators.py`, which takes its precompiled wheel). The weights are int8 by default; the fp8 ones multiply on the GPU's own fp8 units instead of converting every layer to bf16: `python -m tadamoo.media.components install talking-head-fp8` (16 GB, only when
asked), then `avatar_precision` is `fp8` in the media settings. Numbers before and after: `docs/media/speed.md`, queue entry 91. A job that takes longer than the limit of its kind (`docs/media/runner.md`) is ended with an error of the step.
