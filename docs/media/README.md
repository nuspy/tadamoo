# Pictures and video on request

The Chat and the Agent make pictures and clips with two tools, `generate_image` and `generate_video`. The models are LTX Studio's own
(`E:\AI\LTXStudio`; see [../media-models.md](../media-models.md) for what they are and what they need). TaDaMoo adds the job runner, the
lease and the tools. A heavy job runs alone: it asks the server for the VRAM first ([../vram-broker.md](../vram-broker.md)), the language
model may be very slow meanwhile, and without the lease nothing starts.

| file | what |
|---|---|
| [runner.md](runner.md) | the worker, its JSON lines, the lease, cancel and timeouts |
| [tools.md](tools.md) | the two tools, their parameters and caps, where the file goes, what the answer holds |
| [settings.md](settings.md) | the settings block, `/api/media`, the Settings line, how a conversation announces the tools |

## How a job goes

```
tool call -> tadamoo/media/tools.py        checks the parameters (limits.py), picks the path in the working folder
          -> tadamoo/media/client.py       starts worker.py as a subprocess, reads its lines, ends it on Stop / silence / time
          -> tadamoo/media/worker.py       preflight, lease, then LTX Studio's own worker script in LTX Studio's own Python
          -> the model process             one process per job; it ends when the job ends and frees everything
```

- **LTX Studio is not imported by the server or the agent.** Its `core` registry (where the weights are, which Python each model uses, how
  the video parameters are built) is read by the worker subprocess, which adds the folder to its path; the model scripts run as LTX Studio
  runs them (`workers/qwen_image21_infer.py`, `workers/ltx2_infer.py`). Its REST API is not used: it has its own job queue and GPU lock,
  and the lease must be TaDaMoo's.
- **No second torch.** The worker needs only the standard library; the model processes use LTX Studio's environments
  (`diffusers_next_venv` for pictures, `lingbot_venv` for LTX 2.5).
- **Pictures:** Qwen-Image 2.1 with DiT and text encoder both nvfp4 (`qwen21-dit-nvfp4`, `qwen21-te-nvfp4`), the 10.5 GB path measured by
  LTX Studio, lease 11000 MiB. The text encoder is not kept in RAM: LTX Studio's worker has no CPU path for it, so the encoder shares the
  GPU with the DiT for the few seconds of the job.
- **Video:** LTX 2.5 distilled, transformer nvfp4 (`ltx-2.5-22b-distilled-transformer-nvfp4.safetensors`), 704x512 and 65 frames unless
  asked otherwise, lease 25000 MiB (23.8 GB peak measured).
- **When the VRAM is not lent** the answer is `refused` with the broker's reason (`not_enough`, `busy`, `unavailable`, `no_engine`,
  `disabled`, `engine_died`) and its message. Nothing falls back: the nvfp4 models have no CPU path. Putting the language model to sleep,
  or routing it to a provider, is up to the caller.
