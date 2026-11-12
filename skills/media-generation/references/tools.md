# The tools of `tadamoo/media` (summary of docs/media/)

Registered for every conversation whose agent was started with `--media-file`, for each kind LTX Studio can make; a kind whose code, Python or weights are missing is not offered.

## generate_image
`prompt` (required, 4000 characters), `width`, `height` (default 1024, 256 to 2048, multiple of 32, at most 2048x2048), `steps` (20; 1 to 50), `seed`, `negative`, `path`.
Reference pictures (`refs`, "image 1" in the prompt) condition the picture on a photograph: the identity and the studio of a presenter stay, the pose and the framing change with the words.

## generate_video
`prompt` (required), `width`, `height` (704 x 512; 256 to 1280, multiple of 64, at most 1280x720), `frames` (65; 9 to 161, rounded down to 8k+1), `fps` (25; 8 to 30), `seed`, `negative`, `path`.
The distilled model has a fixed schedule: no steps. A value above a cap is an error naming the cap; a rounding is applied and told in `adjusted`.

## The answer
`status`: `ok` (`path`, `bytes`, `seconds`, `lease`, `seed`, the size, `adjusted`, `show`), `refused` (the server could not lend the VRAM; tell the person, do not retry in a loop), `unavailable`
(off in the settings, or LTX Studio lacks something), `cancelled`, or `error` with the last lines of the model process.

## Where files go
`path` is relative to the working folder; an existing file is never replaced; the job's own files are in `<data>/media/<kind>-<time>-<seed>/` and deleted when the job succeeds.

## Settings (`media.json`)
`enabled`, `ltx_dir` (empty: `TADAMOO_LTX_STUDIO`, then `E:/AI/LTXStudio`), `video_model`, `image_mib`, `video_mib`, `avatar_mib` (11000, 25000, 25000 MiB asked of the broker), `wait_s` (600), `idle_s` (900).
