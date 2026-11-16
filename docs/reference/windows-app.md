> Reference: [README](../../README.md).

## Starting TaDaMoo Win+GUI

```powershell
.\Avvia-TaDaMoo.ps1                                             # the app (-gui): the Loader first, nothing loaded yet
.\Avvia-TaDaMoo.ps1 -browser                                    # the server tied to this window, the UI in the browser
.\Avvia-TaDaMoo.ps1 -server --config "Machine Default"          # detached, no UI: returns once the model is loaded
.\Avvia-TaDaMoo.ps1 -server --model "D:\Models\Qwen3.8-Flash-Next-IQ3_XXS.basalt" --port 7240 --vram-libera 2
.\Avvia-TaDaMoo.ps1 -prova                                      # every check, nothing started, nothing written
.\Ferma-TaDaMoo.ps1 --port 7240                                 # stop only that server (its process tree)
```

Every option is accepted with one or two dashes and in any case (`-gui`, `--gui`, `-GUI`).

| option | meaning |
|---|---|
| *(none)* = `-gui` | opens `TaDaMoo.exe`, passing it `--config`, `--model`, `--port` and `--vram-libera`; the browser does **not** open, the address is printed and stays available. Without the built app the script says how to build it and uses `-browser` |
| `-browser` | the server tied to this window (Ctrl+C or closing the window stops server and engine, and frees the RAM), the web UI opened in the browser |
| `-server` | the server alone, detached, no app and no browser; the script returns when `/health` says the model is loaded. For agents; `Ferma-TaDaMoo.ps1` stops it |
| `--config "<profile>"` | the load profile; without it, the default profile (below) |
| `--model "<file .basalt>"` | the model to serve, as a full path |
| `--port N` | the port; wins over the profile's (default 7232) |
| `--vram-libera GB` | the VRAM to leave free for other programs, in GB (`1.5` or `"1,5"`), instead of the profile's; `--vram-reserve-mib N` is the same in MiB |
| `-converti` | with `-browser` / `-server`, when the model is a GGUF to convert: show the plan, ask, convert, then serve the new file (in the app: the Loader's **Convert…**) |
| `-riusa` / `-riavvia` | with `-server`, the answer given in advance when a server is already running with other loading parameters: reuse it, or stop it and start again with the new ones (below) |
| `--lang it\|en` | the language of the messages for this run (it also sets `TADAMOO_LANG` for the server and the app it starts) |
| `-prova` | every check, nothing started; `-browser` / `-server` work on a copy of the config file |
| `-file FILE` | the config file (default `tadamoo_config.json` beside the script; the app creates one without a model on a new installation) |

`-Sfondo` and `-SoloServer`, the old names, still work as `-server` and print a note. `Ferma-TaDaMoo.ps1` takes
`--port N` and `--file FILE` the same way.

**When a server is already running** (one per installation, on any port):

- `-gui` opens the app, which asks **Connect to the running server** or **Change model** (stop only that server's
  process tree, then choose in the Loader).
- `-browser` opens the browser on it; a `--model` or `--config` that differs is not applied, and the script says so.
- `-server` resolves the loading parameters asked for (`--config`, else the default profile; `--model`;
  `--vram-libera`; `--port`) exactly as for a new start, and compares them with the running server's (the model file,
  every load setting, the port).
  - **The same**: it prints the address, model and profile and returns (exit code 0).
  - **Different**: it lists the differences (`engine.max-context: 65536 -> 131072`) and asks **Reuse the running
    server** or **Restart with the new parameters**: in a window when there is a desktop, else at the console (`R` /
    `S`). With no answer within **60 seconds** (`TADAMOO_ASK_TIMEOUT_S` changes it), or with nobody to ask (an agent with
    redirected input), the running server is **reused**: a server is never stopped without an answer. Restarting stops
    only that server's process tree (`Ferma-TaDaMoo.ps1 --port N`) and starts it again on the same port unless `--port`
    asks for another.
  - **`-riusa`** and **`-riavvia`** give the answer in advance, for agents and scheduled tasks; with `-prova` the
    script only says what it would do.

**Which model starts** with `-browser` / `-server`: the first of these that is set: `--model`; the model saved in the
load profile; the **default server model** (chosen in **Settings → Default server model**);
the model already in the config file. When none is set, the script stops with exit code 1 and says how to choose one.
The memory check uses the Loader's own plan; a start that would need the page file to grow is refused, as is a GPU the
engine does not support (exit code 1).

**The default load profile**: the one chosen with **Make default** in the Loader, else **Machine Default** once
the wizard has saved it, else **Valori sicuri**.

Directly, on Windows or Linux: `bin\basalt.exe serve --config tadamoo_config.json [--port N]` (`bin/basalt serve` on
Linux; every option is in [Settings](upstream-basalt.md#settings---config) below and in `python -m serve.server --help`).

### The app

`tools\windows\desktop\publish\TaDaMoo.exe` (window and tray: "TaDaMoo Win+GUI"; the file name differs from the
engine's `basalt.exe` on purpose; the
same options as the script: `--config`, `--model`, `--port`, `--vram-libera`, `--file`). On opening it starts the
server in its own job (it ends with the app) **without loading a model** and shows the web **Loader**:

1. the first time, the models folders (**Browse…** opens the Windows folder dialog; the usual places found on this
   PC are offered);
2. the model, its status, **Verify** and **Convert…**;
3. while this PC has no Machine Default, the **configuration wizard** is offered (it can be skipped, or turned off);
4. the load profile and every value, the VRAM to leave free, the prediction, then **Load**.

Loading another model than the one the server was started with saves it and starts the server again with it; the page
reconnects by itself. Closing the window hides it to the tray icon: **Open**, **Reload with other settings…** (the
Loader), **Start the server alone (for agents)…** (the same as `Avvia-TaDaMoo.ps1 -server`, in place of the app's server),
**Turn off**. If the server cannot start (a port in use, a missing build), the app shows the reason, the end of the log
and a native start panel as a fallback. With the server running, the same UI is at `http://127.0.0.1:7232/` in any
browser.

### Configuration wizard and Machine Default

**Configuration wizard** in the Loader (also `python -m serve.loader wizard` for agents, below) finds the best
values for this PC; the app distributed on other PCs uses it to adapt to each machine. Why: the right values depend
on the VRAM, the RAM, the commit limit, the cores and on which card shows the desktop. Fixed values would either waste
speed on a large PC or fail to load on a small one (or make the page file grow). The wizard computes each value from what
it detects, says why, and can check it with a real measurement.

1. **This PC**: RAM, the commit limit and its use, the page file (Windows-managed files may grow: TaDaMoo Win+GUI
   never makes them grow), the performance and efficiency cores, the NVIDIA GPU, and the video adapters with the one that shows
   the **main monitor**. A PC may drive its monitor from a separate card (an integrated GPU, for example) and keep the
   NVIDIA GPU for compute; the GPU may also have a **secondary** monitor, on or switched off. The wizard shows what it
   detected and asks you to confirm or correct it.
2. **What to leave to the other programs**: the share of RAM and commit (15% proposed, 5 to 50%, never less than 2 GB;
   it becomes this PC's rule for every plan, changed later in **Settings → Memory and start**) and the VRAM: 0.5 GB for the driver, plus 1.5 GB when the GPU drives
   the main monitor (or about 0.4 GB for a secondary one), rounded up to 256 MiB. What other programs use on the GPU
   now is shown, not added: the expert cache sizes itself to the VRAM free at the load, which leaves it to them.
3. **The model** and its own chat values.
4. **The computed values**, with their reasons and the prediction: KV cache q4_0, prefill auto, the drafter at spec 7
   when the file has one, resident experts and the PLE table in RAM when they fit, CPU workers from the
   processor, the longest context that fits.
5. **Measure** (optional, about 5-10 minutes, only with less than 75% of the commit limit in use): the values are
   loaded, the real memory after the load is compared with the plan (a shorter context, then no resident experts,
   while it is above), the speed is calibrated, and the
   maximum verify windows 5, 7 and 8 (set at the engine's start: one reload each); the fastest is kept (a tie keeps the lower
   window) and every result is shown.
6. **Save**: the load profile **Machine Default** (with the model; it becomes the default) and the chat preset
   **Machine Default** (the model's sampling values). **Restore Machine Default** puts them back: in the Loader next
   to **Restore safe values**, and in the chat settings next to **Model defaults**.

Without questions, for agents (the detected and proposed answers unless given; the measurement needs the Loader):

```powershell
.venv\Scripts\python -m serve.loader wizard --model "D:\Models\Qwen3.8-Flash-Next-IQ3_XXS.basalt" `
    --monitor none --free-share 0.15 --vram-libera 2 --save        # --json for one JSON object
```

**The chat's settings** stay per client: the app and each browser keep their own. A client opens with the preset it
used last, if it still exists; otherwise it follows the current default (Machine Default, else the model's values).
Settings changed by hand are kept, and the panel says which preset is in use.

### Models: folders, conversion, verification

**Settings → Models folders** holds one or more **models folders** (the Loader asks for them the first time), for
example the folder of your `.basalt` files and LM Studio's download folder; **Settings → Default server model** is the
model the scripts and the app start without `--model`. The Loader picks the model to load from them. Every `.basalt` and `.gguf` under them is described from its header alone and cached;
nothing reads the weights. Each model gets a status:

- **Ready**: a `.basalt` file the engine can open. **Verify** checks every section's SHA-256 in the background.
- **To convert**: a GGUF set of Qwen3.8-Flash-Next with every shard present and expert formats the GPU kernels
  cover (for example IQ3_XXS, IQ3_S, Q2_0, Q8_0 and the Unsloth UD quants). **Convert…** first shows a dialog with the
  size of the new file, the disk and its free space; it runs only after that confirmation, with enough free space and
  the commit below 75% of its limit. The vision projector (`mmproj`) in the same folder goes into the file; the MTP
  drafter comes from a drafter folder already on disk or is fetched from the Qwen checkpoint (`mtp_fetch.py`). The
  source GGUF's recommended sampling values (`general.sampling.*`) become the file's chat defaults; for files
  converted before, the server reads them from the copy of the source header kept in the file.
- **Not compatible**: anything else, with the reason (another architecture, expert formats without kernels such as
  static Q2_K experts or Q4_0 expert downs, a missing shard, an unfinished download). Projector and drafter files are
  listed as attachments of their model.

The same is available from a terminal: `python -m serve.models list | json | dirs | set-dirs DIR ... | default --set
FILE | resolve [--model FILE] [--profile NAME] | convert --model GGUF [--out DIR]`.

### Download models

The **Download models** panel of the Loader offers Qwen3.8-Flash-Next in the versions that run on Basalt-Plus: 23 files from
Hugging Face (and 6 more shown as not compatible, with the reason). Each card says what the file is and how it differs
from the base model, its size, its quantization, and how close it is to the full model with the **source of that
figure** (Basalt-Plus's own measurement against Q8_0, the authors' own, Strata's or none: the references differ and must not
be mixed). The model picker shows the same one-line hint for a model it knows.

- **Ready `.basalt` files** (no conversion; `jesdga/Qwen3.8-Flash-Next-Basalt`: Q2_0, IQ3_XXS, IQ3_S, UD-Q4_K_XL, Q8_0;
  and one uncensored by a third party) load as soon as the download ends.
- **GGUF sets** (ISTA-DASLab GSQ-RCO IQ2_XS and the Coder, Swift 1.5, Unsloth UD, the uncensored SC117 and OrcaRouter
  files) are downloaded with their vision projector and then show up as **To convert**; the conversion is the one
  described above, with its own confirmation. The GGUF folder can be deleted afterwards.
- **Uncensored** versions are marked and carry the author's warning. OrcaRouter's repository is **gated**: the panel
  explains how to get a Hugging Face account, accept the terms and make a read token, and takes the token for the
  session only (`HF_TOKEN` or the file `huggingface-cli login` writes are read too). TaDaMoo never saves, shows or logs
  a token, and sends it only to Hugging Face; a redirect to any other http or https host is followed without it.
- **A download starts only from its plan**: the files at a pinned revision with their sizes and SHA-256 as Hugging Face
  reports them, the disk and its free space, the access, and the license to accept (Qwen Community License 1.0, plus
  the Swift Open License for Swift files; the repositories that tag themselves Apache-2.0 are treated as the base
  model's license). Files are written as `.part` and renamed only after the SHA-256 matches; an interrupted or stopped
  download continues from where it stopped.
- **Not offered**: Unsloth UD-Q5_K_XL, UD-Q6_K_XL and UD-IQ1_S, and OrcaRouter's IQ2_XXS and Q2_K/Q3_K, because their
  expert formats have no kernel in Basalt-Plus; and `Qwen3.8-35B-A3B`, which is not an official Qwen model but a community
  distillation into another architecture (Qwen3.6-35B-A3B: 40 layers, hidden size 2048, 256 experts) that the engine,
  written for Flash-Next's shapes, cannot run.

Entries marked "Not tried in Basalt-Plus" have expert formats the engine's lists accept but have not been run through Basalt-Plus.
The catalog was read from the repositories on 2026-10-17 (`serve/catalog.py`); sizes are read again when a download is
planned. The routes (this PC only): `GET /api/models/catalog | download | hf`, `POST
/api/models/download-plan | download | download-cancel | hf-token`.

### Model converter (Models > Model converter)

Convert a GGUF set to a `.basalt` file with the choices that decide how it will behave: the **expert profile** (the one found next to the
model, none, a file, or another model's), the **drafter** (Qwen's original layer, none, or the layer fine-tuned on the model's own output that
the official packs carry, read from the pack with range requests, not downloaded whole), the **image projector** (the folder's, none or a
file) and the BF16 rounding. The plan shows the command, the steps, the size and the disk, and refuses a wrong choice before anything is
written.

Next to it, every ready model can be **tested** in a server of its own with the same settings for all (so the figures compare): the memory it
takes at load (RAM, commit, VRAM) and the load time, decode speed with exact sampling and with relaxed drafting 0.5, 0.3 and 0.2 (and without
the drafter), prompt reading speed, an image request, and 100 to 300 maths questions (gsm8k) scored on the final number. The test needs the GPU:
a loaded model is unloaded first, and the Loader's own check must say the model fits. A test already made with the same settings is **offered
for reuse** (a dialog asks, step by step: reuse or test again); results are kept in the user data folder (`bench`) with the settings and the
power limit they were measured under, and the quality result is shared by files with the same weights. The comparison shows the models you tick,
and the published figures of the official packs as references, as a **star** (every axis from the worst to the best of the models shown, the
outer edge always better), **bars** (one panel for each measure, values at the tip), **points** (two measures against each other, a third as
the size) or a **table**; every mark has the same figures in its hover text, and a model keeps its colour.

### VRAM to leave free

**VRAM to leave free** (in the Loader and in the wizard; `engine.vram-reserve-mib`, saved in profiles) is
the VRAM the engine leaves untouched for the desktop, the browser, the driver and other programs. With the expert
cache on `auto`, everything else holds experts. Moving the control recomputes, live: the VRAM left for experts, how
many experts fit (for example "13,066 of 24,576 experts in VRAM (53%)"), the expected expert-cache hit rate, the
predicted answer and prompt speeds, and the commit the load needs.

- **The trade-off**: more VRAM left free means fewer experts on the GPU (a little slower, since the misses run on the
  CPU) and, on Windows, a smaller commit (VRAM counts toward it). Less VRAM left free is faster until the desktop or
  another program runs short.
- **How to choose**: the wizard's rule above; the page names the programs on the GPU and warns when less than 1 GB
  stays free on a GPU that drives the main monitor. Leave more while you game or edit video on the same GPU.

**Measured memory.** After every real load the server compares the RAM and commit the load really took (the system's
readings before and after) with the plan of the same values, and keeps the difference with the model's calibrations;
from then on the plans of that model add what was above (shown as "measured at the last load").

### "Valori sicuri" (safe values): computed, not fixed

The built-in profile is computed each time from the hardware found and the chosen model, with the same rules as
every plan and the wizard: this PC's share of RAM and commit stays free; the VRAM reserve is the wizard's (or its rule,
with 1 GB for the monitor until the wizard confirms where it is, 1.5 GB in all); the KV cache is q4_0 and the context the longest that
fits (up to 262,144); the drafter is on (spec 7) only when the file has one, image input only when it has a
projector; resident experts and the PLE table in RAM only when they fit; the CPU workers are the performance cores plus
half the efficiency cores, less one (on a processor without efficiency cores, the physical cores less one). Each value
comes with its reason in the Loader. As an example, on an RTX 5090 (32 GB, 1 GB used by other programs) with 128 GB of
RAM and a processor with 8 performance and 16 efficiency cores, the 3-bit file gives: 262,144 tokens, q4_0, spec 7,
expert cache auto, resident experts, 1.5 GB left free, 15 CPU workers, image input dynamic.

### Where the settings live

Per user, never in the repository, in the **user data folder**: `%APPDATA%\TaDaMoo\` on Windows,
`~/Library/Application Support/TaDaMoo/` on macOS, `$XDG_DATA_HOME/tadamoo/` (`~/.local/share/tadamoo/`) elsewhere, or
`TADAMOO_DATA`. It holds `profiles.json` (load profiles, the default profile), `presets.json` (the chat presets; copy the file to move them), `models.json` (the
models folders, the default server model, the last output folder), `machine.json` (the wizard's answers: the share of
RAM and commit, the VRAM to leave free, where the monitor is), `calibration\<model>.json` (the measured speeds and
memory), `prefs.json` (the language) and the app's `desktop.json`. Caches that can be rebuilt stay in `%USERPROFILE%\.cache\tadamoo\` (`~/.cache/tadamoo/`, or
`TADAMOO_CACHE`): the header scan, the logs of conversion and verification jobs, the tokenizer and vision files
unpacked from a model, the engine log. Versions before this one kept the settings in the cache folder: they are copied
(not moved) to the user data folder the first time. `tadamoo_config.json` beside the scripts is written by them and by
the Loader from the chosen profile and is not tracked.
