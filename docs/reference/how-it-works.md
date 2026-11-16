> Reference: [README](../../README.md).

## How it works

- **One model.** Basalt-Plus runs Qwen3.8-Flash-Next (GGUF architecture `qwen4exp`): 48 layers, 512 routed experts per
  layer, 10 of them active per token. At 3 bits the experts alone are about 50 GiB, more than a 32 GB GPU holds.
- **Expert tiers.** The experts live in three places. An **expert cache in VRAM** holds as many as fit (`--expert-cache
  auto`: all the VRAM left after the reserve, see below), filled in the order of an expert profile so the most used ones
  are on the GPU; a **second GPU** can serve as a peer tier (`--gpu 0,1`); the rest are computed by the **CPU**
  (AVX2/AVX-512 kernels). With `--resident-experts` the experts that are not in the VRAM cache are copied into RAM once,
  instead of being read from the file when needed. Routing is skewed, so a cache holding a third of the experts already
  answers about 98% of the lookups.
- **The `.basalt` file.** One file per model: a 64 KiB table of named sections (each with its SHA-256), then the
  weights, tokenizer, chat template, drafter, expert profile and vision projector. It is built from the GGUF shards by
  `tools/convert/convert.py`, which copies the tensors byte for byte (no requantization).
- **The MTP drafter.** The model's own multi-token-prediction layer proposes the next tokens and the main model
  verifies them in one pass; the output is identical to plain decoding because verify decides every token. `spec 7`
  is a verify window of 7 tokens (up to 6 drafts). The ready-made packs carry an MTP layer tuned on the model's own
  output (self-distilled), which raises the acceptance.
- **Prompt lend.** A long prompt is read in large chunks whose buffers are borrowed from the last slots of the expert
  cache for the length of the prompt; those slots are refilled afterwards (`--prefill auto`).

## What this fork changes

- **Native Windows port** (details in [docs/WINDOWS.md](../WINDOWS.md)): MSVC + CUDA 13.1 build, a platform layer
  for files, mappings and positional reads, `VirtualLock` and commit-aware memory budgets (Windows charges every
  allocation, VRAM included, to the commit), pinned arenas sized to WDDM's shared-memory budget, unbuffered reads with
  an I/O completion port, the CPU pool's topology from `GetLogicalProcessorInformationEx` with performance cores
  first on hybrid processors, `basalt serve` in a kill-on-close job object. `--no-pdl` launches every kernel without
  programmatic dependent launch, should a driver misbehave.
- **The app, TaDaMoo.exe ("TaDaMoo Win+GUI"), is the main interface.** It starts the server without loading a
  model and opens on the web Loader in its own window (WebView2), so the app and the browser have exactly the same
  settings; a tray icon keeps TaDaMoo Win+GUI running when the window is closed.
- **Web UI** (`/`, English or Italian, see [Language](settings.md#language)): Chat, Monitor, Hardware, the **Loader** (*Carica
  modello* in Italian) and Settings. Each setting lives in one tab:
  - **Loader**: everything decided when the model is loaded (the model, max context, KV cache, prompt reading, the MTP
    drafter and its window, the expert cache, resident experts, **VRAM to leave free**, the PLE table, CPU workers,
    parallel slots, image input, the port), the load profiles and the **default profile** (★, Machine Default,
    Valori sicuri: this is where the default loading values live), the wizard, calibration and Load.
  - **Settings**: the app's own preferences: language, the models folders, the **default server model**, the share of
    RAM and commit left to other programs, whether the wizard is offered at start. A *Loading parameters* line shows
    what runs now (for example "256K · q4_0 · spec 7 · 4 GB VRAM free") and the profile it matches, with **Open the
    Loader** to change it.
  - **Chat → Settings** (the chat panel): the inference values (sampling, reasoning, context policy, structured
    output) and their presets. **Monitor** and **Hardware** only show.
  - The Loader edits every engine setting, shows the VRAM, RAM and commit the plan needs next to what is free now
    (a share of the RAM and of the commit limit always stays free for other programs, 15% unless you choose another;
    the page file must never grow) and the predicted speed; it loads the model, or reloads the engine inside the
    running server. Calibration measures the real speeds per model and setting.
  - The **configuration wizard** finds the best values for this PC and saves them as **Machine Default** (below).
  - **Load profiles** (engine settings, port, model) and **inference presets** (chat settings), shared by the UI, the
    scripts and the app; "Valori sicuri" is a built-in profile **computed from this PC and the chosen model**.
  - Chat with the **model's own sampling values** (from the source GGUF), **context policies** (stop at the limit,
    sliding window, or compaction by the model itself) and **structured output** (`response_format` JSON / JSON
    Schema) validated and generated again with the error fed back (`structured_retries`, default 2).
  - **Model library**: one or more models folders, a header-only scan that marks every model *Ready*,
    *To convert* (a supported GGUF) or *Not compatible* (with the reason), conversion and verification in the
    background, and the default server model.
- **Server**: default port 7232, `--listen-first` (the page answers while the model loads), `--standby` (the page
  answers and nothing is loaded until the Loader's Load), `--relaunch-on-model-change` (a Load of another model ends
  the server with exit code 75 so its owner starts it again with that model), `/api/settings`, `/api/loader/*`,
  `/api/wizard/*`, `/api/profiles`, `/api/models/*`.
- **Scripts** `Avvia-TaDaMoo.ps1` / `Ferma-TaDaMoo.ps1` with the same option names as Strata's: `-gui` (the default:
  the app), `-browser`, `-server` (for agents), `--model`, `--config "<profile>"`, `--port`, `--vram-libera GB`,
  `-riusa` / `-riavvia`, `--lang it|en`.

### Changes since the previous version of this README

- **One set of options** for the scripts and the app (`-gui` default, `-browser`, `-server`, `-prova`, `-converti`,
  `--config`, `--model`, `--port`, `--vram-libera`), the same names as Strata's; the old `-Sfondo` / `-SoloServer`
  still work.
- **The app first**: the app starts the server **in standby** (nothing loaded) and opens on the web Loader; folders are
  chosen with the Windows folder dialog; a native panel is the fallback when the server cannot start. Loading another
  model restarts the server with it (exit code 75) and the page reconnects.
- **Real defaults**: the chat starts from the **model's own sampling values** (from the source GGUF); the load values
  come from the tested rules of "Valori sicuri", computed for this PC and model.
- **Configuration wizard and Machine Default** (load profile + chat preset), with **Restore** buttons, a
  command-line version for agents, and the default precedence: chosen by hand > Machine Default > Valori sicuri.
- **Main vs secondary monitor**: the VRAM reserve depends on whether the NVIDIA GPU drives the main monitor (1.5 GB more),
  a secondary one (about 0.4 GB) or none; what other programs use is shown, not added.
- **Measured memory**: the RAM and commit a real load took are kept per model and added to later plans.
- **Data folders**: settings in `%APPDATA%\TaDaMoo\` (copied once from `~\.cache\tadamoo`, never moved).
- **Name**: the app is **TaDaMoo Win+GUI** (`TaDaMoo.exe`, new icon); the engine, API, files and scripts keep their
  names.
- **`-server` with a server already running** asks whether to reuse it or restart it (`-riusa`, `-riavvia`, 60 s, no
  answer = reuse).
- **English by default**, Italian on request, for the app, the page, the scripts and the command-line tools.
- **Conversion and verification** from the model library (GGUF → `.basalt`, SHA-256 check), in the background.
- **A server of its own, and a window on it**:
  - **Install on Linux** with `deploy/linux/install.sh`: three separate products in one installation (the harness with its web page, the Basalt-Plus engine, the Strata engine), a systemd service, the listening address and port, a public name, an optional server key and ready recipes for nginx, Caddy and Nginx Proxy Manager. The harness runs with no engine at all (`--standby`), through the providers, and can serve only the API (`--no-web`). Docker files and a Helm chart for Kubernetes and k3s (an NVIDIA GPU already set up in the cluster) are in `deploy/`. See [docs/deploy.md](../deploy.md).
  - **Settings > Network** in the page: where the server listens, the names it answers to (`allowed-hosts`), its public name, and a Test button that goes through a proxy as a client would. `X-Forwarded-Proto` and `X-Forwarded-Host` are believed only from a proxy on a private address.
  - **The desktop app connects to a remote server**: a list of servers (name, `http://` or `https://` address, key, kept per user and protected with DPAPI), a "This PC" entry, a Test button; for a remote server the Loader is hidden. `--gui-only` starts the window alone, and a GUI-only install for Windows is in `tools/windows/gui_only/`. See [docs/desktop-servers.md](../desktop-servers.md) and [docs/windows-gui-only.md](../windows-gui-only.md). On a Mac the app is compiled from the repository (prepared in `deploy/mac/`, built later).
  - **The server key can be changed** from the page, from the desktop app and from a connected client (`POST /api/server-key`, with the current key); it applies at once, with no restart, and the clients update the key they hold.
  - **The Android app** takes a name address (`http://name[:port]` or `https://name[:port]`) as well as an IP address, with the key editable.
- **Required actions**: one list of everything that waits for you (permission questions, plan tasks and decisions), with buttons and a field for a text answer, in the web page, in the Android app (with standard notifications, once per item) and as a tray balloon on the desktop. See [docs/actions.md](../actions.md).
- **Newscast**: an optional interlude between stories (fixed camera, lines you choose), filler footage by place, resident models, no text in the generated pictures, an evidence card on by default, and a plan that runs by itself (it takes the decisions on known values and rewords the lines the voice check refuses).
- **Plans update without losing your values**: an installed plugin updated from the repository keeps the parameter values you set.
