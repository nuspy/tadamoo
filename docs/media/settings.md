# Settings, route and announcement

## The settings block

`media.json` in the user data folder, `tadamoo/media/settings.py`:

| field | default | meaning |
|---|---|---|
| `enabled` | true | the switch |
| `ltx_dir` | "" | the folder of LTX Studio; empty: `TADAMOO_LTX_STUDIO`, then `E:/AI/LTXStudio` |
| `image_python`, `video_python` | "" | a Python to use instead of the one LTX Studio names for the model |
| `video_model` | "" | file name of the LTX 2.5 checkpoint; empty: `ltx-2.5-22b-distilled-transformer-nvfp4.safetensors` |
| `ltx_attention` | sage2 | the attention of the LTX 2.5 transformer: `sage2` (SageAttention 2.2.0, the default), `cudnn` (PyTorch's own, nothing to install), `sage1`, `efficient`, `fa4`, `fa2`, `xformers` (each needs its package in the copy below; [speed-study.md](speed-study.md)) |
| `ltx_teacache` | 0.03 | the TeaCache threshold of the clips (the worker's `teacache_thresh`): 0 is off, up to 0.2; higher skips more steps and costs more detail |
| `ltx_attention_env` | "" | the folder of the environment with the attention package: a copy of LTX Studio's LTX 2.5 one, never LTX Studio's own; empty: `TADAMOO_LTX_ATTENTION_ENV`, then `E:/AI/Tools/ltx25_sage2` |
| `ltx_wheels` | "" | where the wheels the component installs are kept; empty: `TADAMOO_LTX_WHEELS`, then `E:/AI/Tools/tadamoo_wheels` ([sage-wheel.md](sage-wheel.md)) |
| `resident` | true | the newscast keeps one lease and one loaded model for all its LTX jobs and then for all its FlashVSR+ jobs (the media phase ordered by model) instead of one process for each job ([speed.md](speed.md)); false: as before |
| `resident_reload_every` | 25 | a loaded model process is ended and started again after this many jobs (0: never), against memory that grows |
| `image_mib`, `video_mib`, `avatar_mib` | 11000, 25000, 25000 | the VRAM each kind asks the server for |
| `upscale_mib`, `interpolate_mib` | 22000, 4000 | the same for the enlarger (FlashVSR+) and the frame maker (RIFE) |
| `modify_python`, `edit_model`, `modify_mib` | "", "", 28000 | the Python for the LTX 2.3 environment, the LTX 2.3 checkpoint file name (empty: the official distilled, else the official dev one), and the VRAM `modify_video` asks for ([modify.md](modify.md)) |
| `time_factor_audio`, `time_factor_video`, `time_min_s` | 3, 2, 120 | the longest a job may take is the factor times the median of what jobs of its kind took for each second of audio or video, times its length; at least `time_min_s` seconds ([runner.md](runner.md)) |
| `wait_s` | 600 | how long a job may wait in the broker's queue |
| `idle_s` | 900 | how long the worker may print nothing |

## `/api/media`

`GET` answers `{settings, ltx_dir, image, video, tools}`; each kind is `{ready, python, python_ok, script_ok, missing, error}`. `ready`
means LTX Studio's code, its Python and the weights are all there; `tools` lists the ones a new conversation offers. The check asks the
worker (`worker.py --check`), which reads LTX Studio's registry; the answer is kept for a minute and forgotten at every change.
`POST` takes the fields above (this PC only), answers 400 with the reason for a bad value and changes nothing, else answers like `GET`.
Settings > General shows one read-only line with the same facts, in English and Italian.

## What the conversation is told

The agent is started with `--media-file`. At its start it asks `usable()` for each kind and registers the tools of the kinds that can be
made. The fixed block "what you are" lists what is absent (`tadamoo/awareness.py`): when neither kind can be made it says "no picture or
video making (LTX Studio is not set up, or the person turned it off in the media settings)". The tools are therefore decided when the
conversation starts; a change in the settings reaches a conversation that is already running only as an error text from the tool.
