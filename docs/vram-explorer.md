# VRAM Explorer

Monitor > **VRAM Explorer** is a page of its own, beside Server LLM, Hardware, LOGS and Security. It has two parts.

- **VRAM over time**, with two modes (the switch at the top) and a range of 5 minutes, 1 hour, 6 hours or 24 hours:
  - *Total*: the memory in use on each card, one line per card, the scale is the card's total memory;
  - *By process*: the same memory stacked by process (the eight biggest by name, the rest as *Other processes*, and *Not attributed*,
    what no process accounts for). Hover shows each process at that time.
- **Processes**: who holds the memory now. A bar with the biggest processes, then a table with the VRAM and the shared memory of each
  process, its GPU load and what is inside it (runtimes, model files, weights, KV cache and compute buffers of a local LLM). The list scrolls
  inside its own box and keeps its header, so a long list is all reachable. With several GPUs a menu picks the card.

## Where the numbers come from

| What | Source |
|---|---|
| The *Total* chart and the Monitor header | NVML, read once a second by `serve/util/telemetry.py` |
| The list and the *By process* chart | The collector of gputaskman: on Windows the per-process counters of WDDM (*Local Usage*, *Non Local Usage*), on Linux NVML and the DRM files |

The two totals differ by a few hundred MiB (NVML and Windows count the card's memory differently). In the list and the *By process* chart:

- a number is **measured** (the process counters), **inferred** (`~`, from another evidence) or **estimated** (`~` on a weights or KV segment,
  worked out from the model file); `n/a` is a value the system does not give, never a zero;
- only measured bytes are summed. *Not attributed* is the card's use minus what the processes account for.

## The history

- **Total**: the server keeps one point per 10 seconds for each card, the **peak** of those 10 seconds, for 24 hours, in memory.
  `GET /api/vram/history?range=<seconds>` returns it (300, 3600, 21600 or 86400; any other value is brought into 10 to 86400).
- **By process**: a recorder takes a reading of every card every 10 seconds and keeps a day of them, in memory. It starts at the first request
  of the page (the Monitor asks once when it loads) and ends with the server. A bucket of a longer range is the **mean** of its readings, so the
  processes add up to the total. Only holders of 4 MiB or more are kept; processes with the same name are one series.
  `GET /api/vram/processes?gpu=<index>&range=<seconds>` returns it.

Both start empty with the server and are not written to disk.

## Cost

A reading takes about 60 ms on an RTX 5090 with NVML present, so the recorder is about 0.6% of one core. The list asks every 2 seconds, only
while the page shows. A collector is made at the first request for an adapter and closed after two minutes without one, which the recorder
prevents once it runs (`serve/vram_explorer.py`). `GET /api/vram/explorer?gpu=<index>` returns the whole reading as JSON. All three routes need
the API key like the other Monitor routes.

## The imported code

`serve/gputaskman/` holds the Python modules of [gputaskman](https://github.com/shanselman/gputaskman) 0.4.5 (commit `a5fc03e`, MIT, Scott Hanselman)
unchanged, with its licence (`serve/gputaskman/LICENSE`, also in `THIRD_PARTY_LICENSES`). Left out: the terminal interface (`app.py`, which needs
Textual), the recorder and replayer, the comparison and summary commands, and its tests. To update it, copy the same modules from a newer
release and run `serve/tests/test_vram_explorer.py`. It needs `psutil` and, for NVIDIA devices, `nvidia-ml-py` (both in `requirements.txt`); without
`nvidia-ml-py` the device figures are missing on Windows, and on Linux no NVIDIA card is found at all (`serve/gputaskman/linux.py` lists them through NVML).
