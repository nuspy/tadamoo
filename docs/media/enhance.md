# Smoother and larger videos: the study and the choice

`enhance_video` (tools: [tools.md](tools.md)) makes a copy of a video with more frames a second and/or more pixels. This page says why those two methods, what was compared, and what is not measured yet.
Every number is marked: **measured** (on files of this PC), **reported** (a source outside, named) or **estimate** (reasoned, not measured on the RTX 5090). No model was run to write this page.

## Does the video model make 24 frames a second?

It makes every frame it is asked for. The worker passes `fps` and `num_frames` to LTX 2.5 distilled; the newscast asks for 161 frames at 25 fps. Measured on the clips of the newscast run of 12 November:

| File | Container | Frames | Repeated frames |
|---|---|---|---|
| B-roll clip `broll/s1_c1.mp4` | 25 fps, 6.44 s | 161 | none (`mpdecimate` keeps all 161; the tenth percentile of the picture-to-picture change is 0.7 of the median) |
| Talking head `takes/s1_firm/take.mp4` | 25 fps, 79.3 s | 1983 | none (all kept) |

So the model is not producing fewer frames. What the eye sees as jerky has three likely causes:

1. **The 25 to 24 resampling.** The newscast composes at 24 fps (`fps` of the settings) and `videoedit.render.decode` takes the 25 fps sources through ffmpeg's `fps=24` filter, which drops one frame every second. Measured on the
   first 40 s of a take: the frames whose change from the previous one is more than 1.6 times that of their neighbours go from 0.7 % at 25 fps to 4.0 % at 24 fps, about one a second. That is a periodic hitch.
2. **Small motion at a small size.** The takes are 832x480 and the clips 1024x576; the final video is 794x530. A distilled model with 8 steps moves little and stiffly; more pixels and more frames help the eye.
3. **The distilled schedule itself**, which cannot be changed from outside.

LTX 2.5 can also generate at 24, 25, 48 or 50 fps (reported: Comfy's LTX-2.5 notes) and has a latent temporal x2 upscaler. Asking the model for 50 fps doubles the frames, the time and the memory of a clip, and halves the
seconds that 161 frames cover; the temporal upscaler is in LTX Studio's catalogue (`ltx25-temporal-upscaler`) but is not on disk and `ltx2_infer.py` does not use it.

## The options

| Option | What it does | Quality | VRAM | Time per second of video | Installation | Licence |
|---|---|---|---|---|---|---|
| **RIFE** (Practical-RIFE) | frames made between two real ones from optical flow | good on small motion (faces, camera drift); ghosting on fast or occluded motion | about 1 to 3 GB (estimate) | LTX Studio's test: 24 to 60 fps in 3 s for a short clip (**measured**, length not recorded); an uncontrolled report of about 140 fps throughput on an RTX 5080 (**reported**, SVP forum): well under 1 s per second (**estimate**) | **already here**: code in LTX Studio, `flownet.pkl` 24 MB | MIT (`workers/rife_pr/LICENSE`) |
| FILM (Google) | the same, with a model for large motion | better than RIFE on large motion; slower | a few GB (estimate) | several seconds per second (estimate) | pip package and weights, not installed | Apache-2.0 (reported) |
| NVIDIA Optical Flow SDK, NvOFFRUC | frames from the GPU's optical-flow unit | lower than RIFE (warping artefacts, no learned handling of occlusions); the fastest | almost none | a fraction of a second (estimate) | a C library from the SDK; no Python wrapper here; an ffmpeg filter was judged impractical by its developers (reported) | NVIDIA SDK licence |
| ffmpeg `minterpolate` | frames from motion estimation on the CPU | blends and ghosts | none | slow on the CPU | none | LGPL/GPL ffmpeg |
| **FlashVSR+** (Turbo or Base) | one-step video diffusion that enlarges up to 4x, streaming | high detail, consistent across frames; Base finer than Turbo | fits 24 GB at 4K without tiling (LTX Studio's note); the runner sizes itself to the free VRAM | FlashVSR: about 17 fps at 768x1408 on an A100 (**reported**, paper); LTX Studio validated 512 to 2048 in 22 s (**measured** by them, length not recorded); on Windows with a 5090 the runner uses PyTorch attention instead of the sparse kernels, so slower than the paper: 2 to 5 s per second of 1024x576 video (**estimate**) | **already here**: code and about 14 GB of weights (Turbo, Base, decoder) | Apache-2.0 (`Tools/FlashVSR_Plus/LICENSE`) |
| FlashVSR v1.1 | the same, first release | a little below the Plus; about twice as fast as SeedVR2 (LTX Studio's review) | more than the Plus (it reports -45 % for the Plus) | similar | here, 6.5 GB | Apache-2.0 |
| SeedVR2 3B (fp8) | one-step diffusion enlarger | the finest on soft or AI-made footage (LTX Studio's review) | 12 GB or more; offload on 32 GB for the 7B | the slowest of the group (estimate: 4 to 10 s per second) | here, 3.4 GB plus a VAE | Apache-2.0 |
| Real-ESRGAN (and the other ESRGAN models on disk) | enlarges each frame alone | sharp, but nothing ties one frame to the next: the detail flickers on video | 2 to 4 GB | LTX Studio's test: 68 s for a short clip (**measured**); about 0.1 to 0.3 s a frame (estimate) | here (`RealESRGAN_x2plus`, `x4plus`, UltraSharp) | BSD-3 (Real-ESRGAN, reported); each community model has its own |
| LTX 2.5 spatial and temporal upscalers | enlarge or double the frames inside the generator | consistent with the clip, real new motion, not interpolation | during generation | part of the generation | LTX 2.5's weights not on disk; not wired in the worker; only for LTX clips | LTX licence |
| DLSS (Super Resolution, Frame Generation) | in-game enlarging and frames | very good, but it needs the game engine's motion vectors, depth and colour per frame | | real time | **no route**: there is no function that takes a video file; the 50-series frame generation lives in the driver and the game | NVIDIA proprietary |
| NVIDIA Video Super Resolution (Maxine Video Effects SDK, the one behind RTX Video) | enlarges 1.33x to 4x, 90p to 2160p, and cleans compression | sharpens and removes artefacts; does not invent detail | small | a few ms a frame on an RTX 5090 (**reported**, the SDK's performance page) | the SDK's DLLs and a registration; a C++ API; no wrapper here | NVIDIA SDK licence and Open Model Licence (commercial use allowed, reported) |

## The choice

- **Frames: RIFE.** It is installed, MIT, takes seconds, and the motion of these clips is the kind it does well (a face that speaks, a slow camera). It turns 25 fps into 50 fps without changing the length.
- **Size: FlashVSR+ Turbo.** It is installed, Apache-2.0, tuned for Windows and Blackwell by LTX Studio's own runner, consistent from frame to frame (the per-frame ESRGANs flicker), and half the cost of SeedVR2. `mode=base` is
  the finer one; SeedVR2 stays the option for a single still-looking shot where time does not matter (not wired).
- **Order: enlarge first, then add frames.** The enlarger is the slow step and sees 25 frames a second instead of 50; RIFE then works at the final size.
- **Not chosen:** DLSS (no way to use it on a file); NvOFFRUC and Maxine (fast, but a native SDK to bind and a licence to read, for a quality below RIFE and FlashVSR+ on generated video); FILM (more install, slower, a gain only on
  large motion); LTX's own upscalers (not wired, not for the talking head, extra weights); 50 fps generation (twice the cost of every clip).

## What was built

`generate_video` takes an optional starting picture; `enhance_video(path, scale, factor, mode)` runs the two steps as jobs of the media worker, each under its own VRAM lease (`upscale_mib` 22000, `interpolate_mib` 4000), through LTX Studio's
own runners (`scripts/rife_run.py`, `workers/flashvsrplus_run.py`) in the environment LTX Studio names. Progress reaches the caller (the frame counter of RIFE); a stopped call ends the process and gives the lease back.
`python -m tadamoo.media.components check video-generation` says whether RIFE and FlashVSR+ are present and prints the commands for what is missing.

## Notes and what is not known

- Not measured: the time of either step on the RTX 5090 for these clips. The first real run, on one 6 s clip, gives it; the figures above are to be replaced by it.
- RIFE writes its intermediate file with OpenCV's `mp4v` encoder and then re-encodes with ffmpeg (x264, default quality): one more lossy generation than necessary. The newscast encodes again at the end.
- LTX Studio's own services add a signature and a visible watermark to what they deliver; the media worker, like for the clips, runs the runners directly and adds neither.
- The newscast's old 24 fps setting dropped a frame a second from 25 fps sources; it is now 25. Either the edit works at 25 fps (or 50 with `factor` 2), or the clips are asked at 24 fps; that setting belongs to the edit, not to this page. The newscast now works at 25 fps (its `fps` setting), so this no longer happens there; a sigla at another rate is converted once.
