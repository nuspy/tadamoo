# Heavy jobs: the local model sleeps, the conversations move

A video or a big picture can need more VRAM than the broker can lend even with the whole expert cache given up ([vram-broker.md](vram-broker.md),
answer `not_enough`). Then the job may ask for more: the local engine sleeps (`SLEEP`: its VRAM is free, its experts stay in RAM) and the
conversations use a provider the person chose, until the job ends. Only with the person's yes, never silently.

## What steps aside

Whatever holds the GPU for the local model and can be asked to (`serve/heavy_holders.py`), each brought back at the end, in reverse order:

| engine | steps aside by | comes back by |
|---|---|---|
| Basalt-Plus, Strata | `SLEEP`: its VRAM is free, its experts stay in RAM; nothing else may wake it. Where the driver cannot suspend a process (this PC) the engine is unloaded instead | `WAKE`, a few seconds; or loaded again (about 2 minutes), started when the job ends |
| llama.cpp started from TaDaMoo | the server is stopped (the model is unloaded) | started again with the same settings |
| LM Studio | `lms unload` on its loaded models | `lms load` of the same models |

vLLM, and a llama-server that was not started here, cannot be asked from here: they are not holders. With none up (a cloud service answers, or no
model is loaded) there is nothing to free: the broker only checks the free VRAM, `POST /api/heavy/begin` answers `no_local_engine` without asking the
person, and Settings says so. The lending of VRAM itself (`docs/vram-broker.md`) is Basalt-Plus and Strata only. An engine that refuses to step aside
brings back the ones that already had, and nothing moves to a provider. Every function on a local provider (the model of this server, LM Studio, vLLM,
llama.cpp) reads as the swap's provider meanwhile.

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
   (`SecureRegistry.begin_swap`), frees the engines (for Basalt and Strata `Service.sleep`, retried for a minute while a request still runs, and `Service.heavy_hold`). Any failure leaves everything as it was.
2. The swap is a file, `llm_swap.json` next to the providers file, read by every process that routes a function: the server and the agent
   sessions (processes of their own). Every function with no provider, or on the model loaded in this server, reads as the swap's provider;
   a function the person assigned to another provider keeps it. The saved assignments are never rewritten.
3. While the model is out, the Chat page's requests (header `X-Tadamoo-Function: chat`, from this PC only) go to the swap's provider with its key and
   model (`serve/heavy_forward.py`; an OpenAI-style provider; the page's own fields are dropped, the tokens are counted). The voice service's requests (the copilot, the replies of a meeting or a call, the avatar's direct brain) go the same way with the header `X-Tadamoo-Function: voice` and the voice token, to the provider of the `voice` function; archive passages that are not marked as cleared for cloud models are removed ([voice/knowledge.md](voice/knowledge.md)). A request from any other program on
   `/v1` is refused with 503 and the reason: the engine is not woken by it. Only the job's end wakes it.
4. When a job ends the engines stay out for `linger_s` (120 s by default, 0 to 900 in `heavy-swap.json`) in case another job follows; a second job in that
   time takes over the swap, so a chain of jobs (a newscast: several clips, the anchor) loads and unloads the model once. The conversations stay on the
   provider meanwhile.
5. The job extends the swap (`/api/heavy/<id>/touch`, every 30 s) and ends it (`/end`). A swap not touched for 90 s ends by itself, and the file
   lapses after 10 minutes without a touch, so a dead server cannot leave the conversations on the cloud. At the end the registry is released
   first, then the engines come back.

## The client

```python
with HeavyLease("video", 24000, "a clip for the newscast", minutes=5) as lease:
    if lease.gpu_ok:
        ...                       # lease.swapped: the conversations are on the provider
    else:
        ...                       # lease.reason: not_enough; lease.swap_reason: not_allowed, no_provider, timeout, busy, cannot_sleep
```

It asks the broker first and the swap only on `not_enough`, then the broker again. `release` gives the VRAM back and then ends the swap.

A job that runs real GPU kernels beside the loaded engine passes `exclusive=True` (the picture and video jobs do): the model steps aside first and the
VRAM is asked for afterwards. Measured on the RTX 5090 with an engine started with `--vram-elastic`: a lease of the same size taken by the Qwen-Image job
left the engine failing its next request ("verify: layer 0 never rang (an illegal instruction was encountered)", three times in three), while a lease
used by a torch process that only allocated and ran matrix products, and a lease nobody used, did not. So for such a job the borrowing is the fallback:
when the person does not allow the model to step aside, the VRAM is borrowed anyway and `risky` is set, with a message that the model may stop and
restart after the job. With no local engine up the job only takes the free VRAM.
Standard library only (`tadamoo/heavy.py`).

## Not done

An approval from the phone (the dialog is on the PC's page), a price estimate per provider, and a swap for a job that is not VRAM-bound.
