# The runner

`tadamoo/media/client.py` runs `tadamoo/media/worker.py` with the Python that runs the server or the agent, and reads what it prints. The
worker is standard library only; the lease client (`tadamoo/vram_lease.py`) is read by its path.

## The job file

`client.run` writes `job.json` in the folder of the output file and starts `python worker.py --job job.json`:
`{kind, ltx_dir, url, mib, wait_s, output, request, python, video_model}`. `request` is what `limits.py` made of the parameters; `url` is the
server's address, from which the lease is asked; `python` is a Python the person named instead of LTX Studio's.

## Lines on stdout

| line | when |
|---|---|
| `{"event": "start", "kind"}` | the worker is up |
| `{"event": "lease", "ok", "granted_mib", "reason", "message", "message_it"}` | the answer of the server |
| `{"event": "refused", "reason", "message", "message_it"}` | no lease: the worker ends with code 4 and the model never starts |
| `{"event": "progress", "step", "steps"}` | a progress bar of the model process (`3/8 [`) |
| `{"event": "log", "line"}` | a line of the model process that starts with `[` |
| `{"event": "done", "path", "bytes", "seconds", "lease"}` | the file exists |
| `{"event": "error", "message"}` | preflight failed (a weight is missing, a Python is not there), or the model process ended without a file; the message carries its last lines |

The client turns them into `{path, bytes, seconds, lease}`, or raises `Refused` (with `reason` and `message_it`), `Cancelled` or `MediaError`.

## Order inside the worker

1. Preflight: LTX Studio's registry says which Python and script to run and which weights are missing. A missing weight ends the job
   before any lease is asked.
2. The lease: owner `image` or `video`, `mib` from the settings (11000, 25000), `wait_s` from the settings (600: the broker queues the
   caller first come first served). The lease is kept alive while the worker lives and given back when it ends.
3. The model process, in LTX Studio's folder, with `HF_HUB_OFFLINE=1`. Its parameters are written next to the output.
4. The file is checked to exist; the job ends.

## Cancel, silence, time

- A cancel closes the worker's stdin. The worker looks at the pipe twice a second, kills the model process and what it started
  (`taskkill /T` on Windows), gives the lease back and exits with code 3. If it has not gone in 5 seconds the client kills its tree.
- `idle_s` (900): a worker that prints nothing for that long is ended. Model loads print lines, so this is a hang, not a slow load.
- `total_s`: 1 hour for a picture, 4 hours for a clip, the wait for the lease included.
- A cancel while the worker still waits in the broker's queue ends the process, but the server may still grant the lease to the dead
  caller; it runs out by itself after `hold_s` (60 s) because nobody touches it.

## What is checked without a GPU

`tests/test_media.py` runs the real worker against a folder that looks like LTX Studio (`tests/media_fake.py`: its `core` registry and two
scripts that load nothing) and a broker on a local port: the lease asked and given back with the right owner and size, a refusal, no
server, missing weights, a failing model process, a cancel (the model process is gone, the lease is back), silence, total time.
