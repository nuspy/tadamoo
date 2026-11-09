# Avatar acceleration

The avatar (MuseTalk, `avatar.md`) is the only voice function that uses the GPU for video. Acceleration lets it draw a smaller picture with fewer
frames, and brings the picture back to the size and the rate the outputs were configured for on this PC, after the worker and before the outputs.
It applies to the MuseTalk video only; Wav2Lip and visemes on the phone are not touched.

## Where it sits

```
worker (MuseTalk, small and slow) -> AvatarClient frame server -> Enhancer -> AvatarClient.frames subscribers
                                                                              (browser preview, virtual camera, other code)
```

The phone gateway's own socket (`POST /avatar/stream`) leaves the worker directly: it gets the worker's own size and rate, not the restored ones.
With a stage on, that is smaller and slower than with nothing on.

## The rule

The outputs get the **target**: the rate `avatar_fps` (25) and the height `avatar_output_height` (480), whatever the stages are. With a stage on
the worker is started rendering less and the enhancer restores the target (`voice/callbridge/enhance/settings.py`, `plan()`):

| | automatic | with a cap | restored by |
|---|---|---|---|
| height | `ceil(target / scale)`, scale 1.5, 2, 3 or 4 | `worker_height_cap` (120 to 2160), never above the target | the upscaler: every frame goes out at exactly the target size |
| rate | `ceil(target_fps / factor)`, factor 2 or 3 | `worker_fps_cap` (5 to 60), never above the target | the interpolator: `ceil(target_fps / worker_fps)` frames out for each frame in |

A cap of a stage that is off is ignored. The worker is never asked for less than a quarter of the target rate. The size and rate are the
worker's environment (`AVATAR_OUTPUT_HEIGHT`, `AVATAR_FPS`), so they apply the next time the avatar is loaded; a clip is prepared at the height it
will be drawn at, so the first load at a new height prepares it again. `GET /avatar/enhance` says `restart_needed` while the running worker
differs from the plan, and the frames then pass as they come (a frame already at the target height is not resized).

## Stages and providers

| Stage | Provider | What it is | Usable when |
|---|---|---|---|
| upscale | `none` | off | always |
| upscale | `basic` | OpenCV Lanczos resize on the CPU, **not neural** | numpy and OpenCV are importable |
| upscale | `rtx_video` | NVIDIA RTX Video SDK, super resolution | see below; **not runnable in this build** |
| interpolate | `none` | off | always |
| interpolate | `basic` | linear blend of two frames on the CPU, **not neural**: it smooths the pace and ghosts a little on movement | numpy and OpenCV |

Interpolation is causal. When frame k arrives, `n` frames go out between the previous one and k (the last is k), spread over the measured
interval between the two arrivals; the first goes at once. A frame is shown at most one worker period late. No blending across a pause longer than
half a second or a change of size.

A provider is a class with `upscale(img, out_w, out_h)` or `interpolate(a, b, alphas)` on numpy BGR frames, added with `providers.register()`
(`providers.py`). A neural interpolator (RIFE class) or another SDK plugs in there; its weights and licence are not decided and none is included.

`feed` never raises. A stage that fails on a frame is skipped for that frame and the frame goes out; three failures in a row switch the stage off
with the reason in `status.upscale.reason`. A provider the capability report calls unusable is not run, and the worker is not asked to render less
for it.

## Capabilities (`capability.py`)

`read_gpu()` reads NVML (`pynvml`), else `nvidia-smi`: name, compute capability (RTX 20 = 7.5, 30 = 8.6, 40 = 8.9, 50 = 12.0), driver, memory.
Everything else is a pure function of that reading, so tests pass a fake one. `rtx_video` is refused, in this order, with a plain reason:
not Windows, no NVIDIA GPU, generation unknown, compute capability under 7.5, a Blackwell card with a driver older than 570, no SDK path
(`needs_sdk`, with where to get it), a path to nothing (`sdk_not_found`), and last `binding_missing`.

`GET /avatar/enhance` (through `POST /api/voice/service/call`) gives `capabilities`, `settings`, `status` (plan, worker, per-stage state, timings:
mean milliseconds per stage, conversion and total against the frame budget, `over_budget`). Settings > Voice > Avatar acceleration shows every
choice; those that cannot run are disabled with the reason, and the measured cost is shown once frames have flowed.

Settings are `voice.json` `avatar.enhance` (`upscale`, `scale`, `interpolate`, `factor`, `worker_fps_cap`, `worker_height_cap`,
`rtx_video_path`), checked as a whole in `serve/voice_settings.py` before the file is replaced. `rtx_video` takes the scales 2, 3 and 4 only
(UNVERIFIED subset; the SDK's own list comes with its binding).

## The RTX Video SDK: what is real and what is not

NVIDIA's RTX Video SDK (Windows 64-bit, GeForce RTX; super resolution and artifact reduction on arbitrary video) is a developer download under
NVIDIA's terms. TaDaMoo never downloads, installs or bundles it. The person installs it and gives the path of its DLL in the card.

**The binding is not written.** The SDK's headers are not in this repository and not on this PC (a search of `C:` and `E:` for `nvsdk_ngx*`,
`sl.h` and RTX Video files found only the driver's runtime `nvngx*.dll` files), so no function signature is verified, and a wrong `ctypes`
signature crashes the process. `rtx_video.py` is a stub that raises `NeedsSdk` with the install instructions; all the checks before it are done.
To finish it: take the C API from the SDK's headers, declare `argtypes` and `restype` of each call, upload the frame to a surface, process,
read it back. Then run the benchmark below and decide with the rule at the end.

## Measuring: `python -m voice.enhance_bench`

```
python -m voice.enhance_bench --video clip.mp4 --out report.json --save-dir pics
```

It times every usable provider on synthetic frames and on the first `--count` frames of a video you supply: resolution in and out, milliseconds
per frame (mean, 95th percentile, worst), whether that fits the frame budget of `--fps`, GPU memory in use before and after, and a PSNR against
the original (the video's frames are shrunk by the scale and restored; the middle of three frames is rebuilt from the outer two). It lists
providers that cannot run with their reason, starts no worker and loads no model. PSNR does not say whether a face stayed faithful: look at
`--save-dir`.

## The DLSS experiment

DLSS 5 (announced March 2026, on RTX 50, RTX 40 promised later, not RTX 30) is a game-engine technology: it needs motion vectors and an engine
integration through NVIDIA Streamline or NGX on D3D12 or Vulkan, and it re-renders the look, which could change a real person's face. It is not
a filter for arbitrary video, and it cannot be driven from Python. The experiment is a separate native tool, not written here:

- a small C++ program with a D3D12 (or Vulkan) device and the Streamline SDK, built by the person with NVIDIA's headers;
- it reads decoded frames of the avatar's video (a clip, or frames the worker saved), estimates optical flow between them (NVIDIA's Optical Flow
  SDK, or any flow) and feeds the flow as motion vectors with a constant depth (a flat talking head has no real depth to give);
- it writes the output frames and a report: milliseconds per frame, GPU memory, and for each frame a comparison with the original;
- the quality that matters is the face: landmark distance and embedding similarity of the face against the original, and the person's own look.

What the person must do: download the Streamline SDK and the DLSS runtime from NVIDIA and accept their licence, build the tool, run it on a
clip, run `voice.enhance_bench` with the same clip for the basic providers, and compare the reports.

Decision rule: keep it only if the face stays faithful (no change of identity, teeth, eyes or skin that the original does not have) and the cost per
frame fits the frame budget with the avatar running. If the face drifts, drop DLSS and keep `rtx_video` super resolution, which does not re-render.

## Tests and what they leave out

`voice/tests/test_enhance_*.py` (settings and the rule, the capability matrix for RTX 20, 30, 40 and 50, non-NVIDIA, Linux, a driver too old and a
missing SDK, the pipeline on numpy frames, a failing provider, the client with a stand-in worker, the service route, the benchmark) and
`serve/tests/test_voice_enhance_*.py` (validation, the page). All with fake GPU readings and frames. Not proven here: any real GPU reading, the
SDK, the visual quality of the basic providers on a real face, the cost on a real avatar, and Linux.
