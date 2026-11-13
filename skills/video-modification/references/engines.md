# What can change the content of a video on one 32 GB GPU

Studied on 14 November 2026 from LTX Studio's own code and registry (`core/video_edit_modes.py`, `video_edit_engines.py`, `video_inpaint.py`, `video_edit_ic.py`, `workers/ltx2_infer.py`) and from which weights are on the maintainer's PC.
**Nothing here was run on the GPU**: "fits" is what LTX Studio's own caps assume (a 32 GB card, `video_edit_modes.py`), not a measure of ours. The queue entry in `tests/QUEUE_daytime.md` measures it.

| Engine (LTX Studio) | What it does | On this PC | In TaDaMoo |
|---|---|---|---|
| LTX 2.3 inpaint LoRA t2v, with SAM2 + GroundingDINO | the object named by a word is followed through the clip and its area is made again from the prompt; outside the mask the original frame is kept at full size with a soft edge | yes | `replace` without `reference` |
| LTX 2.3 inpaint LoRA r2v (rank 32) | the same, with a photograph of the new thing as reference at every segment | yes (0.33 GB) | `replace` with `reference` |
| LTX 2.3 Edit-anything IC-LoRA | the whole clip is changed by words, the source video is the guide | yes | `edit` |
| LTX 2.3 ReStyle IC-LoRA (community, v0.1) | the look of a picture, which takes the place of the first frame of the guide | yes | `restyle` |
| LTX 2.3 union IC-LoRA (canny + a photograph at frame 0) | replace a person by the one in a photograph | yes | no: the composite with a person mask is a GUI path of LTX Studio that needs more than one job |
| MiniMax H3 with a mask (33B, int8) | replace a character, change the background behind a person, "vfx_edit" LoRA to edit by words | yes | no: it needs a block offload on 32 GB, runs in clips of 6 s, and its own catalogue note calls the edit LoRA experimental (identity drift, rewritten text and logos) |
| LTX 2.3 lipdub | new speech from a text, with matching lips | yes | no: it is a voice job, not a change of what is shown |
| Wan 2.1 I2V 14B fp8 | a clip from a picture | yes | not an editor |
| Wan VACE | video editing under a mask or from a reference (the model people mean by "video edit") | no, and not in LTX Studio's catalogue | no: a new download (tens of GB) and a new worker, to be decided by the person |
| Face and head swap (inswapper, head-swap LoRAs) | a face or a head replaced | yes | no: not an object swap |

## Why LTX 2.3 and not the 2.5 of the clips

The editing LoRAs were trained on LTX 2.3 and LTX Studio refuses them on 2.5 (`ic_lora_supported`: only the 2.3 release). The kind `modify` therefore runs its own checkpoint: the official 2.3 distilled one if there is one, else the official 2.3 dev one with LTX Studio's distilled LoRA
at 0.5 (the workflow Lightricks publishes). A fine-tune of 2.3 in the same folder is never taken by itself.

## What follows from the way it works

- The new area is generated, then the original is put back around it (`keep_background`): everything outside the mask is the original, so a shadow or a reflection outside it stays, and a new thing smaller than the old one leaves a hole that is filled by invention.
- A clip is cut in segments of 97 frames (3.9 s at 25 fps) joined with 9 frames of cross-fade; the photograph is given to every segment, which holds the identity better than giving it to the first only, but detail still drifts between segments.
- The mask is made from the first frame by GroundingDINO (a box from the words) and followed by SAM2. A word that names several things, or a first frame where the object is hidden, gives a wrong mask; the prompt and the target are the place to be exact.
- LTX 2.3 dev fp8 is 27 GB on disk; the worker decides by itself between keeping it in VRAM and streaming blocks from RAM, from the free VRAM it measures. The lease asks for 28000 MiB (`modify_mib`), a number that has not been measured.
