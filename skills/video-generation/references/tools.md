# The video tools of `tadamoo/media` (summary of docs/media/)

Registered for every conversation whose agent was started with `--media-file`, for each kind LTX Studio can make; a kind whose code, Python or weights are missing is not offered.

## generate_video
`prompt` (required, 4000 characters), `image` (a picture of the working folder that the clip starts from), `width`, `height` (704 x 512; 256 to 1280, multiple of 64, at most 1280x720), `frames` (65; 9 to 161,
rounded down to 8k+1), `fps` (25; 8 to 30), `seed`, `negative`, `path`. The distilled model has a fixed schedule: no steps. A value above a cap is an error naming the cap; a rounding is applied and told in `adjusted`.

## enhance_video
`path` (required, a video of the working folder), `scale` (1 to 4: the size FlashVSR+ works at; the result always has the size of the video), `factor` (2 to 4 times the frames a second), `mode` (`turbo` or `base`), `output`. With neither, `scale` 2 in `turbo` mode (the product's one enhancer; the copy has the size, the frames and the duration of the original).
The jobs run one after the other, each under its own lease (`upscale` 22000 MiB, `interpolate` 4000 MiB asked of the broker); the answer lists the `steps` that ran.

## The answer
`status`: `ok` (`path`, `bytes`, `seconds`, `lease`, `seed`, the size, `adjusted`, `warning` when the clip was made with cuDNN attention because SageAttention 2 is not installed: tell the person, `show`), `refused` (the server could not lend the VRAM; tell the person, do not retry in a loop), `unavailable` (off in the settings,
or LTX Studio lacks something: the note names it), `cancelled`, or `error` with the last lines of the model process.

## Where files go
`path` is relative to the working folder; an existing file is never replaced; the job's own files are in `<data>/media/<kind>-<time>-<seed>/` and deleted when the job succeeds.

## Settings (`media.json`)
`enabled`, `ltx_dir` (empty: `TADAMOO_LTX_STUDIO`, then `E:/AI/LTXStudio`), `video_model`, `ltx_attention` (`sage2`), `ltx_teacache` (0.03), `ltx_attention_env`, `ltx_wheels`, `video_mib`, `upscale_mib`, `interpolate_mib`, `wait_s` (600), `idle_s` (900).
