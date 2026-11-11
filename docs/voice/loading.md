# Loading order and VRAM of the voice functions

The voice functions that use a model are `stt` (speech to text), `tts` (speech synthesis) and `avatar`. Each is wanted on or off, with a model
and a placement, and the voice service holds what is in memory. `serve/voice_loader.py` compares the two, says what loads and unloads and whether
it fits, and does it; `voice/functions.py` is the service side. The Loader's own planning (`serve/loader.py`) reads the free VRAM
from the GPU, and the functions are loaded before it reads; the commit they will take is counted in it too (see "The commit of the voice functions").

## What is wanted: `voice.json`

Per service, next to `selected`, `device` and `memory` (see `models.md`):

```json
{"services": {"tts": {"selected": "kokoro", "device": "auto", "memory": "vram", "enabled": true}}}
```

`enabled` is a boolean, written by `POST /api/voice/enabled {service, enabled}`. Without it a service is off, except `stt`, which is on when its
model is installed. Speech to text, speech synthesis and the avatar are set only in Models > Voice services; Settings > Voice has a pointer to it
and the cards of the helper services (diarization, voice activity, embeddings).

## The service side

`GET /functions` (bearer token, like every route of the service) returns, per function: `state` (`unloaded`, `loading`, `loaded`, `error`),
`model`, `engine`, `device`, `memory`, `enabled`, `installed` (and `reason` when not), `loaded_with` (the model and placement it was loaded
with), `estimated_mib`, `measured_mib`, `error`, and `free_vram_mib` for the first GPU.

`POST /functions/apply {"functions": {"stt": {"on": true, "model": "...", "device": "...", "memory": "...", "estimated_mib": 812}, ...}}`:

1. what is off, and what changes model or placement, is unloaded first, so its memory is free before anything enters;
2. what is on and not loaded is loaded in the order `stt`, `tts`, `avatar`, one at a time, inside callbridge's VRAM arbiter
   (`engines/vram.py`);
3. the free VRAM is read before and after each step; the answer is `{"report": {function: {action, before_free_mib, after_free_mib,
   measured_mib, state, error}}, "free_vram_mib"}`.

The loads are the existing warm-up code: the shared STT process with the chosen Whisper size, the TTS router with the engine made active and
warmed up with a short sentence, the avatar worker started (with its persona prepared when one is set).

- A model that is not installed leaves its function unloaded with the plain reason (`<Model> is not installed: install it from Settings >
  Voice.`); the others still load. Nothing is downloaded.
- A function that fails keeps its own `error`; the others go on.
- `measured_mib` is the fall of the free VRAM during the load (the rise during an unload). It is `null` when a reading is missing, when the
  function runs on the CPU or in RAM, when the load failed, or when the figure came out negative. The reading uses `pynvml`, else
  `nvidia-smi`; with neither it is `null`. A figure is never guessed.
- A second apply while one runs is refused (409 `busy`); while the components are suspended nothing loads (409 `suspended`).
- STT, dictation and the clipboard are not part of the VRAM plan: when the service starts, an enabled STT with its model installed loads at
  once. Switching it off unloads the helper; dictation starts it again on first use.

## The server side

`GET /api/voice/functions` (this PC only) returns the wanted state, the service's live state (or `service.running: false` with the reason), the
Loader's reading of the GPU (`vram.free_mib`, `total_mib`, `reserve_mib`, what the running model holds), the estimate of the chosen set and
what is left for the model after the reserve, `pending_reload` and `job`.

`POST /api/voice/functions/plan {"desired"}` returns the diff and changes nothing: per function `action` (`load`, `reload`, `unload`, `keep`,
`off`, `missing`), `need_mib`, `recover_mib`, `free_mib`, `reserve_mib`, `left_for_llm_mib`, `llm_reload` and `fits` (`null` when a size is
unknown). Estimates are the saved measurement of that model and placement, else the catalog's size, else unknown; a CPU or RAM placement is 0.

`POST /api/voice/functions/apply {"desired", "confirm": true}` (without `confirm: true` it is refused) writes the choice to `voice.json`
and runs a job on a thread that holds the supervisor's job slot (phase `voice`), so no reload or calibration runs meanwhile. An answer of 202
carries the job; `{"status": "nothing"}` means no change; 422 `does_not_fit` refuses additions that do not fit unless `force` is true.

| Case | What the job does |
|---|---|
| Adding, model running | unloads the model, applies on the service, records what was measured, loads the model again (the Loader's reload, which reads the VRAM that is left and keeps its reserve). The model is loaded again whatever the apply's outcome |
| Adding, no model running, or only CPU/RAM functions | applies on the service; the model is not touched |
| Removing | unloads on the service and does not touch the model; `pending_reload` records the VRAM recovered |
| The Loader's Load | functions that are on and not loaded load first, then the Load runs again with the slot held, so its plan reads the VRAM they left |
| Another model | the server ends and is started again by its owner; the service is not asked anything and stays loaded |

`POST /api/voice/functions/reload-llm {"confirm": true}` ("Change services in memory") loads the model again with all the free VRAM minus the
reserve and clears `pending_reload`; without `confirm: true` it is refused. The model is never loaded again without a confirmation, except
as the second half of an add the person confirmed or the Loader's own Load.

A service that is not running gives 503 with `code: "voice_service_down"` on plan and apply, and nothing is changed or written. The
Loader's Load goes on without it.

Measured VRAM is kept in `voice-measured.json` (user data folder) per model, with the placement it was measured for.

## The pages

Models > Voice services shows each function (model, engine, weight, placement, state, measured and estimated VRAM), the free VRAM, the
reserve, what the chosen set needs and what is left for the model. Each function is a card of its own with the switch, the model, the device and
the memory; each choice is written to `voice.json` as it is made (`/api/voice/enabled`, `/select`, `/placement`). A model that is not installed
offers its download on the card (`voicedl.js`, shared with Settings > Voice). The card's Apply is for that function alone (`only` in
`/api/voice/functions/plan` and `/apply`: the other functions stay as they are loaded) and asks for a confirmation that names what loads and
unloads and, when it applies, that the model is loaded again. After a removal the "Change services in memory" button appears (in the header and on the tab) and its confirmation
says what is already unloaded, the VRAM recovered, and that the model loads again using all the free VRAM minus the reserve.

## Avatar on request

The avatar is the one function that can load when it is asked for instead of with the Loader's Load: `services.avatar.load` in `voice.json` is
`on_request` (the default) or `at_load`, written by `POST /api/voice/load-mode {service: "avatar", mode}` and chosen on the avatar card (a select
next to the placement; a second one sets the idle time). The profile keeps it with the other voice choices (`avatar.load`). STT and TTS stay
loaded at Load. The service starts no avatar worker for an on-request avatar (`avatar_preload` is off).

- The Loader's Load leaves an on-request avatar out: its item is `action: "on_request"`, it counts 0 MiB, and no model reload is planned for it.
  The card's own Apply (`only: "avatar"`) still loads it, the old way (the model is unloaded and loaded again).
- `POST /api/voice/avatar/ensure {wait_s?}` (this PC, or a phone with its key) is what asks for it (`serve/voice_avatar_ensure.py`). The avatar must be
  on in Models > Voice services. It asks the broker ([../vram-broker.md](../vram-broker.md), through the voice functions' shared lease, see below) for the avatar's measured or catalogue size (3000 MiB
  when none is known), then loads only the avatar (`voice_loader.apply_lent`: the model is not touched, the fit check is the lease's, no
  "reload the model" banner) and answers `{ok: true, state: "loaded", lent_mib, lease}`. A placement on the CPU or in RAM borrows nothing.
- A refusal is `{ok: false, reason, message, message_it}` with the broker's reasons (`unavailable`, `busy`, `not_enough`, ...) or `avatar_off`,
  `not_installed`, `voice_service_down`, `load_failed`; the lease is returned and nothing is left half loaded. The avatar does not fall back to the
  CPU by itself: the placement is the person's choice (set it to CPU in the card to run without VRAM, if the avatar engine supports it).
- While it is loaded the server keeps the lease (a watcher, every 5 seconds). The avatar is in use while a phone is on the gateway, the preview has
  a viewer, a voice session with the avatar runs, or the virtual camera runs. After `avatar.idle_unload_s` without use (300 s by default, 30 to
  86400, set with `POST /api/voice/avatar`) the avatar is unloaded (`voice_loader.unload_lent`, which leaves `voice.json` as it is) and the lease
  is released, so the expert cache refills. An avatar unloaded by hand, or a lease that is lost, ends the watching the same way.
- The Android app calls the route when the Avatar tab opens (and when the avatar is switched on), shows "Loading the avatar on the PC" until it
  answers, requests no video before that, and shows the refusal's text; the voice does not wait. A PC without the route is taken as a yes.

## Voice first, then the model: every start of the engine

The rule is in one place, `voice_loader.ensure_first(svc)`, and every start of the model's engine calls it before the engine sizes its cache:
`Service._ensure_loaded` (a lazy start, the first request, `--listen-first`, an engine that ended), the Supervisor's reload
(`_reload_engine`, after the old engine is closed: profile use, the Loader's reload), a server started without `--standby`, `--lazy` or
`--listen-first` (before the engine process is spawned: `voice_loader.Booting` stands in for the service that does not exist yet), and the
Loader's Load (`before_load`, which runs the job and calls the Load again). A mock engine has nothing to start and leaves the service alone.

- It loads the functions that are on, load with the Load (`at_load`) and are not loaded, in one apply, and waits for them. A function that is
  loading under another job is waited for (up to 10 minutes). An avatar that loads on request is not loaded.
- It is idempotent: with nothing to load, or with the voice service down, it returns at once and says nothing.
- It never fails the start. A function that fails is one log line (`voice function tts did not load: ...`) and the model starts all the same; a
  function whose state is `error` is not tried again by the next start (the Loader's own Load does try it).
- If the functions and the model do not fit the Windows commit together, the functions are skipped with one line and the model starts (the Loader's
  Load refuses earlier, below).

## The commit of the voice functions

The Loader's plan checked the VRAM but not the commit, so a Load could load the functions (a GPU allocation is charged to the commit in full, and the
processes' host copies come on top) and then be refused for commit. Now each item of the plan carries `commit_mib` and `commit_source`: the commit
measured when one function loaded alone (`commit_mib` next to its measured VRAM in `voice-measured.json`), else the VRAM estimate times 2 (`rule`, a
rough rule), else 0 for a CPU or RAM placement, else unknown.

- `POST /api/voice/functions/plan` adds `commit_need_mib`, `commit_ok` (`null` when the commit cannot be read) and, when false, `commit_reason`
  and `commit_reason_it`. A model that loads again after the functions is part of the check; the Load's own check (`loader.plan`) is the one used. The
  card's confirmation shows the reason, and Apply answers 422 `not_enough_commit` unless `force` is true.
- `POST /api/loader/plan` (and the Loader's page) counts the functions that will load first as commit in use (`voice_commit` in the plan, `voice` in the
  commit problem, whose text says how much of it is the voice functions'). `POST /api/loader/load` refuses a Load that would fail for commit with 422
  `does_not_fit` before anything loads, naming the numbers; `force` loads anyway.

## A function added while the model runs

With the engine started with `--vram-elastic` (`engine.vram-elastic`), a function added or changed in Models > Voice services does not unload the
model: `apply` asks the broker ([../vram-broker.md](../vram-broker.md)) for the VRAM of the additions (measured, else catalogue, else 3000 MiB), loads
them with the plan's fit check replaced by the lease (`voice_loader.apply_lent`'s way) and answers `lent: {used: true}`. If the broker refuses
(`unavailable`, `busy`, `not_enough`) or the engine does not lend, today's way runs (`lent: {used: false, refused}`): the model is unloaded, the functions
load, the model loads again. The plan says `lend_possible` and the confirmation says which of the two will happen.

The broker lends to one holder at a time, so the voice functions share one lease: `serve/voice_lend.py`, owner `voice`, sized to the sum of the
functions that borrowed (the avatar on request uses it too). A function that joins makes it larger (the old lease is returned and a new one asked for,
so the cache refills for a moment); one that unloads, by hand or because the avatar was idle, makes it smaller, and the last one gives it back, so
the expert cache refills. A function that did not load keeps no share. The lease is kept alive every 15 seconds while a share is held; a lease the
broker forgot is asked for again, and when that fails the functions keep running without one. Removing a borrowed function leaves nothing to reload
(no `pending_reload`); a function loaded with the model, not borrowed, is removed as before.

## Load at start

`POST /api/loader/autoload` (this PC only, 403 otherwise) is the Load with the Machine Default, or the default load profile when there is none: it
writes that profile's voice functions to `voice.json` and runs the Loader's Load (the answer is the Load's, with `profile`), so the voice functions
load first and the model after, whatever the profile. The desktop app calls it once after the server answers in standby when it was started with
`--load` or the icon's check is on ([../windows-scripts.md](../windows-scripts.md)).

## The commit of the GPU memory

Under WDDM every GPU allocation is charged its full size to the process commit (`docs/WINDOWS.md`). The processes of the voice engines give it
back, as the engine does (`voice/callbridge/engines/vram_backing.py`, standard library only): every TTS helper, the STT helper and the avatar
worker decommit the eviction backing of what they allocate, a region only when none of its pages is in RAM and only once, so content an eviction
wrote is never discarded. The helpers sweep before the first request and after each one; the avatar worker every 5 seconds. Each sweep that gives
something back writes one line to the log (`vram_backing: N MiB of commit given back in M regions`). `"keep_vram_commit": true` in `voice.json`, or
`TADAMOO_KEEP_VRAM_COMMIT=1` in the environment, keeps the commit as it is.

The same risk as the engine's applies: with the backing released, an allocation another program's CUDA work evicts has nowhere to go and its process
can hang until it is stopped. Keep the commit when another program allocates GPU memory while the voice engines are loaded.

## The load profiles

A load profile saved from the Loader keeps the voice functions as `voice.json` holds them: `{"voice": {"stt": {"on", "model", "device", "memory"},
"tts": ..., "avatar": {..., "load"}}}`. Using the profile (the Loader's Use, the Machine Default, the start with a profile, `profiles apply` from the start
script) writes them back into `voice.json`; they load with the model or when applied on their cards. A profile saved with `"voice": null`, and
profiles from before, leave the voice settings alone. An import drops a voice block whose model this PC does not list.

## What the tests cover, and what they cannot

`voice/tests/test_functions.py` (apply order, one load at a time, failures, the measurement and its null paths, the offline rule, the
routes), `serve/tests/test_voice_avatar_ensure.py` (the avatar on request: the Loader leaves it out, ensure, the refusals, the idle unload),
`serve/tests/test_voice_first.py` (voice first on every start path, a failing voice not failing the start, the commit estimate and plan, the Load
refused before anything loads, the lending path and its fallback, the automatic load) and
`serve/tests/test_voice_loader.py` (the plan, adding, removing, reload, the Loader's Load, a different model, a service that is
down, the call to the service over loopback) and `serve/tests/test_voice_loader_page.py` (the page). They use a stand-in service with a fake
GPU. Not proven without a GPU: the real load order of the engines, the NVML readings, the reload of the model in the VRAM that is left.
