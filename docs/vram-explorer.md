# VRAM Explorer

Monitor > Server LLM has two things for the GPU memory.

- **VRAM over time** (left column): the memory in use on each card, with a range of 5 minutes, 1 hour, 6 hours or 24 hours. One line per card;
  the scale is the card's total memory. Hover for the value and the time.
- **VRAM Explorer** (a tab beside *Requests and logs*): who holds the memory now. A bar with the biggest processes, then a table with
  the VRAM and the shared memory of each process, its GPU load and what is inside it (runtimes, model files, weights, KV cache and compute
  buffers of a local LLM). With several GPUs a menu picks the card.

## Where the numbers come from

| What | Source |
|---|---|
| The chart and the header of the Monitor | NVML, read once a second by `serve/util/telemetry.py` |
| The Explorer's table | The collector of gputaskman: on Windows the per-process counters of WDDM (*Local Usage*, *Non Local Usage*), on Linux NVML and the DRM files |

The two totals differ by a few hundred MiB (NVML and Windows count the card's memory differently). Within the Explorer:

- a number is **measured** (the process counters), **inferred** (`~`, from another evidence) or **estimated** (`~` on a weights or KV segment,
  worked out from the model file); `n/a` is a value the system does not give, never a zero;
- only measured bytes are summed. *Not attributed* is the card's use minus what the processes account for.

## The history

The server keeps one point per 10 seconds for each card, the **peak** of those 10 seconds, for 24 hours, in memory: it starts empty with
the server and is not written to disk. A longer range shows the peak of each wider interval, so a short spike is not lost.
`GET /api/vram/history?range=<seconds>` returns it (300, 3600, 21600 or 86400; any other value is brought into 10 to 86400).

## Cost

Nothing runs for the Explorer while its tab is closed. A collector is made at the first request, answers at most once a second and is
closed after two minutes without a request (`serve/vram_explorer.py`). A reading took about 60 ms on an RTX 5090 with NVML present.
`GET /api/vram/explorer?gpu=<index>` returns the whole reading as JSON. Both routes need the API key like the other Monitor routes.

## The imported code

`serve/gputaskman/` holds the Python modules of [gputaskman](https://github.com/shanselman/gputaskman) 0.4.5 (commit `a5fc03e`, MIT, Scott Hanselman)
unchanged, with its licence (`serve/gputaskman/LICENSE`, also in `THIRD_PARTY_LICENSES`). Left out: the terminal interface (`app.py`, which needs
Textual), the recorder and replayer, the comparison and summary commands, and its tests. To update it, copy the same modules from a newer
release and run `serve/tests/test_vram_explorer.py`. It needs `psutil` and, for NVIDIA devices, `nvidia-ml-py` (both in `requirements.txt`); without
`nvidia-ml-py` the device figures are missing on Windows, and on Linux no NVIDIA card is found at all (`serve/gputaskman/linux.py` lists them through NVML).
