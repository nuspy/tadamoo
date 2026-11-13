# The tools

Registered by `tadamoo/media/tools.py` for every conversation (Chat and Agent) whose agent was started with `--media-file`, for each kind
LTX Studio can make: a kind whose code, Python or weights are missing is not offered at all. Effect class `reversible` (a new file in the
working folder), not `untrusted`, no registry time limit: the client ends a job that runs too long, and Stop ends it at once. A call
returns when the file is made.

## generate_image

`prompt` (required), `width`, `height`, `steps`, `seed`, `negative`, `path`, `refs`. `refs` are up to 4 pictures of the working folder that the picture is conditioned on (an edit of what they
show); the prompt calls them `image 1`, `image 2`.

| | default | cap |
|---|---|---|
| width, height | 1024 | 256 to 2048, rounded to a multiple of 32, at most 2048x2048 pixels |
| steps | 20 | 1 to 50 |
| prompt | | 4000 characters |

## generate_video

`prompt` (required), `image`, `width`, `height`, `frames`, `fps`, `seed`, `negative`, `path`. `image` is a picture of the working folder that the clip starts from. The distilled model has a fixed schedule, so there are
no steps.

| | default | cap |
|---|---|---|
| width, height | 704 x 512 | 256 to 1280, rounded to a multiple of 64, at most 1280x720 pixels |
| frames | 65 | 9 to 161, rounded down to 8k+1 |
| fps | 25 | 8 to 30 |

A value above a cap is an error that names the cap; a rounding is applied and told in `adjusted`. Without a `seed` a new one is chosen
and returned.

## enhance_video

`path` (required, a video of the working folder), `scale`, `factor`, `mode`, `output`; at least one of `scale` and `factor`. Offered when the frame maker (RIFE) or the enlarger (FlashVSR+) of
LTX Studio is ready; asking for the one that is not ready answers `unavailable`. The comparison that led to these two is in [enhance.md](enhance.md).

| | default | cap |
|---|---|---|
| scale | the size is kept | 1 to 4 times the width and height |
| factor | the frame rate is kept | 2 to 4 times the frames a second |
| mode | turbo | turbo (faster) or base (finer) |

The picture is enlarged first, then the frames are made at the final size (`client.enhance`); each is a job with its own lease. The enlarged video keeps the sound of the source; the frame maker
keeps it too. The original is never changed; the copy is `media/<name>-enhanced-<date>.mp4` unless `output` says otherwise.

## Where the file goes

`path` is relative to the working folder; left out, it is `media/<kind>-<date>-<seed>.png` or `.mp4`. The extension is fixed by the kind,
a path that escapes the folder or is protected is refused like for any file tool, an existing file is never replaced, and the change is
recorded like a written file. The job's own files are in `<data>/media/<kind>-<time>-<seed>/`; they are deleted when the job succeeds, and
when it fails they are kept and the answer names the folder.

## The answer

`status` is one of:

- `ok`: `path`, `bytes`, `seconds`, `lease` (`owner`, `granted_mib`), `seed`, the size, `steps` or `frames` and `fps`, `adjusted`, and `show`,
  the line that puts it in the answer (`![caption](tadamoo-path:...)` for a picture, a link for a clip).
- `refused`: the server could not lend the VRAM. `reason` is the broker's, `note` says it in words and tells the model to tell the person
  and not to retry in a loop.
- `unavailable`: off in the settings, or LTX Studio lacks something (the note names it).
- `cancelled`, or `error` with the last lines of the model process.

`verify_output` (`tadamoo/attachments.py`) checks the signature of an mp4, mov or webm file, so a clip can be offered to the person as a file.
