# Loading order and VRAM of the voice functions

The voice functions that use a model are `stt` (speech to text), `tts` (speech synthesis) and `avatar`. Each is wanted on or off, with a model
and a placement, and the voice service holds what is in memory. `serve/voice_loader.py` compares the two, says what loads and unloads and whether
it fits, and does it; `voice/functions.py` is the service side. The Loader's own planning (`serve/loader.py`) is unchanged: it reads the free VRAM
from the GPU, and the functions are loaded before it reads.

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
"tts": ..., "avatar": ...}}`. Using the profile (the Loader's Use, the Machine Default, the start with a profile, `profiles apply` from the start
script) writes them back into `voice.json`; they load with the model or when applied on their cards. A profile saved with `"voice": null`, and
profiles from before, leave the voice settings alone. An import drops a voice block whose model this PC does not list.

## What the tests cover, and what they cannot

`voice/tests/test_functions.py` (apply order, one load at a time, failures, the measurement and its null paths, the offline rule, the
routes), `serve/tests/test_voice_loader.py` (the plan, adding, removing, reload, the Loader's Load, a different model, a service that is
down, the call to the service over loopback) and `serve/tests/test_voice_loader_page.py` (the page). They use a stand-in service with a fake
GPU. Not proven without a GPU: the real load order of the engines, the NVML readings, the reload of the model in the VRAM that is left.
