# Heavy jobs: the local model sleeps (summary of docs/heavy-swap.md)

A video or a big picture can need more VRAM than the broker can lend even with the expert cache given up. The job then asks for more: the local engine steps aside (its VRAM is free, its
experts stay in RAM, or it is unloaded where the driver cannot suspend it) and the conversations use the provider the person chose until the job ends.

- `POST /api/heavy/begin` | `/touch` | `/end`; in code `tadamoo.heavy.HeavyLease(owner, 1, "reason", minutes=..., kind="video", base_url=...)` with `step_aside()` and `release()`.
- Levels (Settings > General > Heavy jobs): `ask` (default: a dialog names the job, its length, the provider and the cost) or `allow` (image and video jobs go ahead).
- With no local engine up, `begin` answers `no_local_engine`: there is nothing to free and the job takes the GPU as it is.
- A skill that makes several jobs takes **one** lease for all of them, calls `touch` while it works, and releases it in `finally`; the model returns while CPU-only editing goes on.
- Never start a heavy job while someone else's work depends on the loaded model unless they said go: a heavy job unloads it by design.
