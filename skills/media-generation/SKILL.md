---
name: media-generation
description: Make pictures (with reference pictures), video clips and talking-head videos with the local models of LTX Studio through TaDaMoo's tools: generate_image, generate_video and the talking head (the avatar kind). Use when a task needs a generated picture, a short clip, a presenter who speaks a recording, or a reference-conditioned edit of a photograph.
tags: media, image, video, avatar, talking head, ltx, qwen-image, longcat
requires: python
---

# Media generation

Pictures, clips and talking heads made on this PC, by LTX Studio's own models, run as its own workers under a VRAM lease. The code is `tadamoo/media/`; the agent tools are
`generate_image` and `generate_video`; the talking head is the `avatar` kind that the newscast and any other skill calls through `tadamoo.media.client.run`.

## When to use it

- A picture from a description, optionally conditioned on reference pictures ("image 1" is the first): `generate_image`.
- A clip of 2 to 6 seconds from a description: `generate_video` (LTX-2.5 distilled).
- A person (a reference still) who speaks a recording, as one continuous video of any length: the `avatar` kind (LongCat-Video-Avatar 1.5).

Not for: editing the result into a finished video (use `video-editing`), making the voice (use `speech-synthesis`).

## The tools

| Tool or kind | Parameters | Limits |
|---|---|---|
| `generate_image` | `prompt`, `width`, `height`, `steps`, `seed`, `negative`, `path`; internally `refs` (reference pictures) | 256 to 2048 px, a multiple of 32, steps 1 to 50 |
| `generate_video` | `prompt`, `width`, `height`, `frames`, `fps`, `seed`, `negative`, `path` | 256 to 1280 px, frames 9 to 161 (8k+1), fps 8 to 30; 704x512, 65 frames by default |
| `avatar` | `image` (still), `audio` (recording), `prompt` (performance), `negative`, `resolution` (480p or 720p), `seed` | the video lasts as long as the audio; the model has no guidance knob (8 steps, fixed) |

Details in [references/tools.md](references/tools.md); the talking head in [references/avatar.md](references/avatar.md).

## Heavy-swap behaviour (read before a job)

Every kind uses most of the GPU. The server lends the VRAM through a lease (the broker); when the local language model holds it, the job asks the person (or the `allow` level) and the model
**steps aside** for the job and comes back after it ([references/heavy-swap.md](references/heavy-swap.md)). Consequences for a skill that makes many jobs: hold **one** swap for the whole
production (`swap="held"` in `tadamoo.media.client.run`, `HeavyLease` around it, released in `finally`), never one per job; never start a job while another session has the model loaded
for work that must not be interrupted. A `refused` answer is told to the person and not retried in a loop.

## Measured costs (RTX 5090)

Still with a reference photograph about 110 s; a 6.4 s clip about 90 to 150 s; the talking head about 65 s of GPU for each second of speech at 480p int8 (a 13.7 s recording took 893 s).

## Quality notes that cost hours to find

- A talking head moves like the sound it hears: loud, sharp audio gives wide mouth shapes and gestures. Feed it a **tamed copy** of the voice (quieter, compressed, band-limited) and a still with the
  hands out of the picture; the real soundtrack is mounted over the result. Measure the result against a reference video (mouth opening, head motion) instead of trusting the eye.
- Quality falls off after about 20 seconds of one render (skin, hands, the mouth): keep each take under 20 s, one recording for each, and start a new one at a cut of the edit (another angle, the presenter
  going into or coming out of the window over B-roll). The newscast does this by itself (`shot_max_s`, docs/newscast/shots.md).
- Generation is stochastic: fix the seed to repeat a result.

## Scripts

`scripts/make_take.py STILL AUDIO OUT.mp4 [--prompt TEXT] [--negative TEXT]` makes one talking-head take through the same client.
