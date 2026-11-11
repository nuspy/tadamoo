# Heavy jobs: the local model sleeps, the conversations move

A video or a big picture can need more VRAM than the broker can lend even with the whole expert cache given up ([vram-broker.md](vram-broker.md),
answer `not_enough`). Then the job may ask for more: the local engine sleeps (`SLEEP`: its VRAM is free, its experts stay in RAM) and the
conversations use a provider the person chose, until the job ends. Only with the person's yes, never silently.

## Only for a local Basalt or Strata

The lending and the sleep need the engine's `VRAM`, `SLEEP` and `WAKE` commands, which only Basalt-Plus and Strata have. With a cloud service
answering (no local model loaded) there is nothing to lend or put to sleep; with llama.cpp or vLLM the server cannot ask them to. In those cases
a heavy job only checks the free VRAM (the broker's answer with no engine, or `unavailable`), `POST /api/heavy/begin` answers `no_local_engine` or
`unsupported_engine` without asking the person, and Settings says so. Nothing moves to a provider.

## The level

Settings > General > Heavy jobs, `heavy-swap.json` in the user data folder:

| level | what it does |
|---|---|
| `ask` (default) | every job asks: a dialog on any page names the job, its length, the provider, what leaves this PC and the cost |
| `allow` | image and video jobs go ahead without a question; any other kind still asks |
| `never` | the model never sleeps for a job; a job that does not fit is refused |

The dialog answers are "Allow this time", "Always allow images and video" (sets `allow`) and "Do not allow". The cost line says what the person
knows: the plan with that provider sets the price, the tokens used today and the provider's daily cap are shown; no price is guessed.

The provider is any chat provider outside this PC from the Providers page (`mode` and `provider` in the same file). The yes for a swap stands in for
the per-function consent for the cloud, for the time of the swap only; the Providers page and its consents are not touched.

## What happens

1. `POST /api/heavy/begin {kind, purpose, minutes}` waits for the answer (up to 10 minutes) and then swaps the registry
   (`SecureRegistry.begin_swap`), puts the engine to sleep (`Service.sleep`, retried for a minute while a request still runs) and sets
   `Service.heavy_hold`. Any failure leaves everything as it was.
2. The swap is a file, `llm_swap.json` next to the providers file, read by every process that routes a function: the server and the agent
   sessions (processes of their own). Every function with no provider, or on the model loaded in this server, reads as the swap's provider;
   a function the person assigned to another provider keeps it. The saved assignments are never rewritten.
3. A request for the local model meanwhile (another program on `/v1`, the Chat without the harness) is refused with 503 and the reason: the
   engine is not woken by it. Only the job's end wakes it.
4. The job extends the swap (`/api/heavy/<id>/touch`, every 30 s) and ends it (`/end`). A swap not touched for 90 s ends by itself, and the file
   lapses after 10 minutes without a touch, so a dead server cannot leave the conversations on the cloud. At the end the registry is released
   first, then the engine wakes.

## The client

```python
with HeavyLease("video", 24000, "a clip for the newscast", minutes=5) as lease:
    if lease.gpu_ok:
        ...                       # lease.swapped: the conversations are on the provider
    else:
        ...                       # lease.reason: not_enough; lease.swap_reason: not_allowed, no_provider, timeout, busy, cannot_sleep
```

It asks the broker first and the swap only on `not_enough`, then the broker again. `release` gives the VRAM back and then ends the swap.
Standard library only (`tadamoo/heavy.py`).

## Not done

An approval from the phone (the dialog is on the PC's page), a price estimate per provider, and a swap for a job that is not VRAM-bound.
