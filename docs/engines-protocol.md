# Engines protocol: Basalt and Strata

How Basalt's Python server drives its engine (`bin/basalt`), how Strata's own Python server drives its
engines (`strata`, `strata-q36`, `strata-dsv41`), and which way Tadamoo takes to run Strata as a second engine.
Sources: `serve/engine/process.py`, `serve/supervisor.py`, `serve/loader_api.py`, `serve/service.py` here; `serve/server.py`,
`serve/loader_api.py`, `serve/runconfig.py`, `serve/ready.py` in the Strata tree with the upstream merge.

## 1. Command by command

| Step | Basalt (`serve/`) | Strata (`serve/` of Strata) |
|---|---|---|
| Child | `basalt --serve <args>`, one resident process, stdin and stdout pipes (`serve/engine/process.py`) | `<exe> --serve <args>`, same shape (`server.py`, the `popen(...)` of the engine) |
| Which binary | one: `bin/basalt` | three, by model family: `strata` (qwen4exp), `strata-q36` (qwen35moe), `strata-dsv41` (deepseek41) |
| Argv source | `engine_args(cfg)` from `.basalt` settings and Loader values (`serve/engine/vision.py`) | the `args` list of a run-config JSON `{exe, args, cwd, tokenizer, model_name, arch}` (`runconfig.py`) |
| Flag sets | one set, probed from the binary (`serve/engine/flags.py`) | `strata` 232 flags; `strata-q36` 59 (no `--kv`, `--spec`, `--expert-cache`, `--vram-reserve-mib`); `strata-dsv41` 52 (`--reserve-mib`, `--no-expert-cache`, no `--kv`, `--spec`, `--mtp`) |
| Model | one `.basalt` file | a pack folder, or a GGUF for the Qwen3.6 binary |
| Tokenizer | the engine tokenizes (`INFO text_prompt=1`, `GENT`) | the server tokenizes from a tokenizer folder (`tokenizer` of the config) and sends ids |
| Readiness | stdout `INFO k=v` lines, then `READY <ctx> [stop]`, 900 s limit (`ENGINE_READY_S`) | the same `READY <ctx> [stop]` line, 900 s limit (`STRATA_ENGINE_READY_S`) |
| Server readiness | the port answers `GET /health` | `--ready-file <abs path>` (JSON instance record, `ready.py`), `GET /health`, `GET /v1/status` |
| Generate | `GEN`/`GENT`/`BGEN`/`SGEN` lines, `T <id>`, `DONE`, `STOP` mid-request | `GEN`/`BGEN` lines, `T <id>`, `DONE`, `STOP` when `READY ... stop` |
| VRAM probe | `VRAM [mib]` line | `VRAM` line (`server.py` `"VRAM`) |
| Images | `ENC <image> <out>` (vision encoder in or beside the engine) | its own vision path from the run config, `images.py` |
| Stop | `QUIT` line, job-object containment (`serve/util/winjob.py`) | `STOP` then `QUIT` lines, `winjob.py`, `--owner-fd` lifeline |
| Reload | `Supervisor.reload()` restarts the child with new argv (`serve/supervisor.py`) | `POST /load`, `POST /unload` of its server (`loader_api.py`) |
| Session save | Basalt session logic in `serve/service.py` | `SAVE <path>` / `RESTORE <path>` lines to the engine, `--slot-save-path`, its own session code |
| Port | 7232 (Basalt) | 7232 default; `--port` |
| Chat template | `serve/chat_template.jinja`, tool calls and thinking in `serve/api/` | its own `chat_template.jinja`, `dsml.py` (DeepSeek tool calls), reasoning in `reasoning_content` |
| Model names | `qwen3.8-flash-next` | `qwen3.8-flash-next`, `qwen3.6-35b-a3b`, `deepseek-v4.1-flash` (`FAMILY_MODEL_NAMES`) |
| HTTP API | `/v1/chat/completions`, `/v1/messages`, `/v1/responses`, `/v1/models`, `/api/*` | the same `/v1` routes plus `/v1/status`, `/load`, `/unload`, `/props`, `/slots` |

The stdin and stdout line protocol is the same family in both servers (Basalt is a fork of Strata). The differences
that matter are above the wire: how a model is named and read, which flags a binary takes, who tokenizes, and who
owns the chat template, tool calls and thinking.

## 2. The two ways

### (a) Basalt's supervisor drives `strata.exe` through an adapter

The adapter would turn the Loader values into Strata flags and make `ProcessEngine` talk to a Strata binary. It is
rejected. Every one of these differences blocks it:

1. The Loader values are `.basalt` centred. A Strata binary needs a pack folder or a GGUF, a tokenizer folder and an
   `arch`, none of which a `.basalt` setting holds.
2. Three binaries with three flag sets. The adapter would need a table per binary and a refusal per missing flag.
3. Tokenizing is on the other side: Basalt sends text (`GENT`), Strata expects ids from its own tokenizer files.
4. The chat template, tool-call parsing (DeepSeek DSML for `deepseek41`) and thinking handling differ per family and
   live in Strata's server. Re-doing them in `serve/api/` means two code paths that can drift.
5. Sampling defaults follow the model card of each family (`MODEL_CARD_SAMPLING`), Basalt's follow `.basalt`.
6. Vision is configured by the run config, not by `--vision-mmproj`.
7. Session save, slots and the parallel batch groups are decided by the server, not the binary.
8. Doing all of it is a re-implementation of the 9 933 lines of Strata's `serve/server.py` inside Basalt, and the
   result would be tested only against what Basalt thinks Strata does.

### (b) Basalt's Loader launches Strata's own server as a managed child (chosen)

`python -m serve.server --engine strata --config <run config> --lazy --ready-file <abs path> --host 127.0.0.1
--port <free port>` is started from `bin/strata` (working directory and `PYTHONPATH`), inside the job object. Basalt
then posts `/load` to it in a background thread and answers `/v1/*` by relaying to the child while it is active.

Why: the smaller surface. Basalt writes no Strata flags beyond the Loader controls the run config already
carries, owns no Strata template or tokenizer, and a change in Strata's server comes with the next copy of the files.
The cost is a Python process per run and one relay hop.

## 3. What Basalt does for (b)

| Piece | File | Behaviour |
|---|---|---|
| Registry | `serve/engines.py` | capability flags and Loader controls per model family (`FAMILIES`), per engine the union |
| Install | `serve/engine_install.py` | copies the binaries and server files from a local folder or `.zip` into `bin/strata/`, with progress; no download |
| Run | `serve/engine_host.py` | `HOST.activate/stop/status/forward`; builds the config, picks a free port, waits for the ready file, posts `/load` |
| Routes | `serve/handler.py` | `POST /api/loader/engines/<id>/install\|load\|stop\|source`, `GET .../models` |
| Profiles | `serve/profiles.py` | an optional `engine` key, written only when it is not `basalt` |
| GUI | `serve/web/engines.js` | install, load, stop, family-based control hiding |

Activation rules:

- One engine at a time. Loading a Basalt model stops the Strata child first; starting Strata unloads Basalt first.
  If Basalt is answering a request the load is refused with `busy` (nothing is cut).
- A load is refused only for a reason the person can act on: `not_installed`, `binary_missing`, `model_missing`,
  `model_unsupported`, `family_mismatch`, `control_unsupported`, `flag_unsupported`, `tokenizer_missing`,
  `config_unreadable`, or a hardware reason from `engines.compatible()`.
- Start failures keep the engine's own words (`start_failed`, `start_timeout`, `load_failed`, `spawn_failed`).
- Stop frees the port for Basalt and removes the child's process tree.

Relayed while the child runs (`serve/engine_host.py`):

- `GET`: `/v1/models`, `/models`, `/v1/status`, `/props`, `/slots`
- `POST`: `/v1/chat/completions`, `/v1/messages`, `/v1/messages/count_tokens`, `/v1/responses`,
  `/v1/images/generations`

Streams are passed through as they arrive. While the child loads, these routes answer 503 with the reason. `/health`
and all `/api/*` routes stay Basalt's own.

## 4. Capability flags (from the code, not from a run)

| Flag | qwen4exp (`strata`) | qwen35moe (`strata-q36`) | deepseek41 (`strata-dsv41`) |
|---|---|---|---|
| reserve_slider | yes | no | yes |
| context_kv | yes | yes | yes |
| speed_spec | yes | yes (as `--mtp`) | no |
| expert_cache | yes | no | yes |
| vision | yes | yes | yes |
| profiles | yes | yes | yes |
| calibrate | no | no | no |

Controls shown follow the chosen model's family: `max_context`, `kv`, `spec`, `expert_cache`, `vram_reserve`,
`parallel` for qwen4exp; `max_context`, `mtp`, `parallel` for qwen35moe; `max_context`, `expert_cache`,
`vram_reserve`, `parallel` for deepseek41. A tokenizer folder is always offered.

## 5. Differences that cannot be bridged

| Subject | What differs | What Basalt does |
|---|---|---|
| Model format | Strata reads packs and GGUF, Basalt reads `.basalt` | the Strata tab lists only models Strata reads; a typed path is accepted |
| Model name | the child answers with its family's name | `/v1/models` is the child's list, unchanged |
| Chat template, tools, thinking | owned by Strata's server; thinking arrives as `reasoning_content` | relayed untouched; Basalt's own agent harness parses what comes back as it does for any provider |
| Sampling defaults | per family from the model card | the child's defaults apply unless the request sets them |
| Vision | set by the run config | the Loader does not add vision flags |
| Calibrate | no Strata equivalent | hidden for Strata |
| Session save | Strata's own slots | not shared with Basalt's session files |
| `/v1/vram`, `/api/*` | Basalt-only | not relayed |
| API key | Basalt checks its key; the child binds to 127.0.0.1 | one check, at Basalt |
| Qwen3.6 GGUF files without `qwen35moe.nextn_predict_layers` | the binary refuses them | marked unusable in the model list with the reason |

## 6. Not verified

A run of Strata through the Loader (`/load`, relayed `/v1/chat/completions`) is not covered by a test. The tests use
a stub (`serve/tests/fake_strata/`) that honours the same start-up protocol: config file, ready file, `/load`,
`/v1/status`, `/v1/*`. The `strata-q36` binary alone has loaded a Qwen3.6 GGUF that carries the MTP layer and
generated; Qwen3.6 GGUF files without it are refused. Tokens per second through the relay were not measured.
