# Changing what a video shows

The tool `modify_video` makes a copy of a clip of the working folder in which something has been made again: an object swapped for another, the whole clip changed by words, or the look of a picture taken. It is the fifth kind of job of
the media runner (`modify`, after `image`, `video`, `avatar`, `upscale`, `interpolate`), and the skill that explains it to the model is `video-modification` ([../../skills/video-modification/SKILL.md](../../skills/video-modification/SKILL.md)). The study of what else could
do it is in [../../skills/video-modification/references/engines.md](../../skills/video-modification/references/engines.md).

**Nothing in this page was run on the GPU.** The code was written from LTX Studio's own functions and checked against stand-ins; the real run is entry 89 of `tests/QUEUE_daytime.md`.

## Parameters

`path` (required, a video of the working folder), `mode`, `prompt`, `target`, `reference`, `strength`, `guide`, `grow`, `seed`, `output`.

| | default | cap |
|---|---|---|
| `mode` | replace | `replace` (an object swapped), `edit` (the whole clip changed by words), `restyle` (the look of a picture) |
| `target` | | replace only, required: what to swap, in a few English words, 200 characters |
| `prompt` | | required for replace and edit, 4000 characters; optional for restyle |
| `reference` | | a picture of the working folder: the new thing (replace, optional), the look to take (restyle, required); not for edit |
| `strength` | 0.8 replace, 1.0 edit and restyle | 0.1 to 1 |
| `guide` | 0.4 | replace only, 0.1 to 1: how closely the motion and the shape of the original are kept |
| `grow` | 24 | replace only, 0 to 64 pixels: how far the new area goes beyond the outline of the old object |
| clip length | | at most 20 seconds (checked by the worker, which reads the clip); 9 frames at least |

A parameter that does not belong to the mode (a `target` in edit, a `guide` in restyle) is an error, not ignored. The answer is like `enhance_video`'s: `status`, `path` (`media/<name>-<mode>-<date>-<seed>.mp4` unless `output` says), `bytes`, `seconds`, `lease`, `mode`,
`seed`, `show`; `refused`, `unavailable`, `cancelled` and `error` as for the other tools.

## What the job does

1. **Plan** (`tadamoo/media/modify_job.py`, before the lease, no GPU): LTX Studio's registry gives the LTX 2.3 environment and what is missing; the checkpoint is the one `edit_model` names, else the official 2.3 distilled one, else the official 2.3 dev one
   (never a fine-tune, never a GGUF); the clip is read with ffprobe, copied into the job's own folder, and its size, frame count (8k+1) and rate are taken from it.
2. **Lease**: owner `modify`, `modify_mib` (28000, not measured), the wait of the settings.
3. **replace**: SAM2 and GroundingDINO (LTX Studio's `inference_server` environment, `video_segment.py`) find `target` in the first frame and follow it; the masks and the frames go to `source_track/` in the job folder. Then LTX Studio's `ltx2_infer.py` runs in its LTX 2.3
   environment with the inpaint LoRA (r2v with a `reference`, t2v without), the parameters of `core.video_inpaint.inpaint_params`: guide 0.4, CFG 6, mask grown by `grow`, softened by 6, the second stage, the original kept outside the mask. A clip over 97 frames is cut in
   segments of 97 with 9 of overlap, the photograph given to each.
   **edit**: the Edit-anything LoRA with the source as guide, at most 1280 pixels on the long side, segments of 121 frames with 9 of overlap above that.
   **restyle**: `core.video_edit_ic.build_job` makes the guide (the picture in place of the first frame) and the parameters of the ReStyle IC-LoRA.
4. **After**: the sound of the original is put under the result (`video_edit_ic.mux_audio`: video copied, sound re-encoded); the job's folder is deleted when the job succeeds, and kept with the answer naming it when it fails.

The tracking files SAM2 writes next to its input are therefore in the job folder, not in the person's folder. The model process and the tracker are both registered with the worker, so Stop ends either and the lease is given back.

## Settings and components

`modify_mib` (28000), `modify_python` (a Python for the LTX 2.3 environment instead of LTX Studio's), `edit_model` (the file name of the LTX 2.3 checkpoint; empty: the official distilled, else the official dev) in `media.json`
([settings.md](settings.md)). The tool is offered when `usable("modify")` says the kind is ready: LTX Studio's code, the 2.3 environment, a 2.3 checkpoint and its distilled LoRA when it is a dev one, the text encoder, SAM2, GroundingDINO and the inpaint LoRA from words. The two
other LoRAs (photograph, look) and the Edit-anything one are checked by the job of the mode that needs them, with the message of LTX Studio naming the file.

`python -m tadamoo.media.components check video-modification` lists the components, `install` runs the downloads LTX Studio can do (about 60 GB: the dev checkpoint 27 GB, its distilled LoRA 7 GB, the Gemma text encoder 24 GB, the rest under 3 GB) and says which two community LoRAs have
no download entry and are put by hand.

## Checked without a GPU

`tests/test_media_modify.py` runs the real worker against a stand-in LTX Studio whose editing modules do no work: the checkpoint taken (the official dev one, never a fine-tune; the one the settings name; an error when it is not there), the LoRA each mode is given, the tracked object and
the copy it is tracked in (nothing is left beside the original), the segments of a long clip, the sound put back, the refusal of a clip over 20 seconds before the lease is asked, the tool through the registry, and what `usable` says when something is missing.
