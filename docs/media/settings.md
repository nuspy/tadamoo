# Settings, route and announcement

## The settings block

`media.json` in the user data folder, `tadamoo/media/settings.py`:

| field | default | meaning |
|---|---|---|
| `enabled` | true | the switch |
| `ltx_dir` | "" | the folder of LTX Studio; empty: `TADAMOO_LTX_STUDIO`, then `E:/AI/LTXStudio` |
| `image_python`, `video_python` | "" | a Python to use instead of the one LTX Studio names for the model |
| `video_model` | "" | file name of the LTX 2.5 checkpoint; empty: `ltx-2.5-22b-distilled-transformer-nvfp4.safetensors` |
| `image_mib`, `video_mib`, `avatar_mib` | 11000, 25000, 25000 | the VRAM each kind asks the server for |
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
