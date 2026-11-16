> Reference: [README](../../README.md).

## Differences from the base Basalt

Everything this fork changes, at a glance. The engine, the `.basalt` format, the converter and the OpenAI/Anthropic API
are Basalt's; what follows is added on top, and the Linux code paths are unchanged. Details of each point are further
down and in [docs/WINDOWS.md](../WINDOWS.md). The fork includes the upstream Basalt up to 2026-10-17.

**1. Windows 10/11 support (the engine did not run there)**
- MSVC 14.44 + CUDA 13.1 build for sm_120, with guards against environment variables that silently change the build.
- A platform layer for Windows: memory and **commit-aware budgets** (Windows charges every allocation, VRAM included,
  to the commit), `VirtualLock`, pinned arenas sized to WDDM's shared-memory budget, mappings, positional reads, parallel
  and **unbuffered reads** with an I/O completion port, one `PrefetchVirtualMemory` call for a batch of mapped experts.
- The CPU pool follows the Windows topology (`GetLogicalProcessorInformationEx`), **performance cores first** on hybrid
  processors, with the host's CPU Set restored afterwards.
- `basalt serve` runs in a **kill-on-close job object**; NVML, CPU and RAM readings on Windows; no `SO_REUSEADDR` (a
  second server on a used port fails instead of sharing it); `--no-pdl` (programmatic dependent launch off at run time);
  a fix for legacy-stream copies that non-blocking streams could read too early on WDDM.
- The CPU image encoder builds with MSVC; the test suite builds and runs on Windows (91 of 92 C++ tests pass; the other
  needs an AVX-512 CPU).

**2. An advanced graphical interface (the base only had the Monitor and Hardware pages)**
- **TaDaMoo Win+GUI** (`TaDaMoo.exe`): a desktop app (WebView2 window, tray icon, single instance) that starts the
  server and opens the web UI; the same UI works in any browser, English or Italian, on phones too.
- **Chat** with the model's own sampling values, presets, a context meter and context policies (stop, slide or have the
  model compact), structured output; **Settings** for the app's preferences, beside the base's Monitor and Hardware.
- **Loader**: every loading parameter in one place, with a **memory plan** (VRAM, RAM and commit needed against what is
  free now), a **speed prediction**, calibration, load profiles, and loading or reloading the engine inside the running
  server. **Configuration wizard** and **Machine Default**: the best values for this PC, saved as a profile.
- **LOGS**: every request as the model saw it (what came in, the reasoning and the answer as written, each structured
  try, times, why it ended) plus the server's and the engine's lines, kept in memory, exportable. A second section,
  **Session traces**, replays a Chat or Agent conversation with its recorded answers (nothing runs), saves it as JSONL with
  secrets masked, and verifies the hash-chained audit ledger of its folder.
- **Download models**: a catalog of 23 downloadable Qwen3.8-Flash-Next files (ready `.basalt`, GGUF to convert, uncensored
  versions; 6 more are shown as not compatible), with what differs from the base and how close each is to the full model, and the source of that figure), downloaded
  with resume and SHA-256 checks.

**3. Memory and loading management**
- Memory plans for VRAM, RAM and commit that also count the RAM copies of the prompt path's lent slots; the
  **measured** RAM and commit of every real load are kept per model and added to later plans.
- The **VRAM reserve** depends on whether the GPU drives the main monitor; the engine's minimum of 256 MiB is enforced
  everywhere; "Valori sicuri" are **computed from this PC and the model** instead of fixed.
- `--listen-first` (the page answers while the model loads), `--standby` (nothing loaded until the Loader's Load),
  `--relaunch-on-model-change` (a Load of another model restarts the server with it); a reload re-reads the sampling
  defaults and refuses a bad value before stopping the running engine.
- A first start that fails says the engine's reason; a vision projector with F16 or BF16 tensors where the encoder reads F32 or BF16 is rewritten at the
  load (once, kept in the cache, same weights); one the engine cannot read at all turns image input off, with the reason.

**4. Models**
- A **models library** of one or more folders: a header-only scan marks every model Ready, To convert or Not compatible
  with the reason; **conversion and verification** in the background (disk and commit checked first).
- A `.basalt` can be given a real **expert profile without converting again**, and the Loader says when it has only the
  plain fill order. `/v1/models` lists the folders' ready files.
- **A second engine, Strata**, for what Basalt-Plus does not run: other GPU families and the Qwen3.6 and DeepSeek models. The Loader
  shows one Models tab per engine this PC can run, installs it from a local folder or archive (no download yet), and runs
  Strata's own server as a managed child, relaying `/v1/*` to it. One engine is active at a time. Details and limits:
  [docs/engines-protocol.md](../engines-protocol.md). Windows and Linux. What was run and what was not: [Compatibility](voice-and-platform.md#compatibility).

**5. Server and API**
- Default port **7232** (the base's examples disagreed with its code); `/api/settings` reads and writes the config file;
  `/api/loader/*`, `/api/wizard/*`, `/api/profiles`, `/api/models/*` and `/api/reqlog*` for the interface.
- Structured output (`response_format`) tries again with the error fed back (`structured_retries`, default 2); the retry
  message says what was wrong and the one change it needs (a cut-off or repeating answer is never sent back), and the reasoning streams while only the
  validated answer is held. The server's body limit and its refusals reach the client whole.

**6. Scripts, languages and data**
- `Avvia-TaDaMoo.ps1` / `Ferma-TaDaMoo.ps1`: `-gui` (default), `-browser`, `-server` for agents, with the same option names
  as Strata's; they attach to a running server and check memory with the Loader's plan before loading.
- English by default, Italian on request, in the app, the page, the scripts and the command-line tools; settings live in
  the user data folder, apart from the caches.

**7. An agent harness, a planner and a revised interface shell** (`harness/`, [docs/harness.md](../harness.md))
- A separate process (`python -m tadamoo -p "task"`, or as an editor's agent over ACP) that talks to the server's public
  API: a tool loop with permission levels, a plan mode, hooks, file checkpoints, a hash-chained audit log, a command
  sandbox, a secrets broker, fenced untrusted content, context masking, memory, skills, MCP and
  reference documents (fixed block or search). A **vault** keeps cards, personal data and logins encrypted and the model only writes placeholders that are filled in at the last step (the agent runs as your own user, so what that does and does not protect is in `docs/harness.md`);
  the agent can drive a browser to fill in forms and book, and can pay within limits you set in the **Wallet** view (automatic up to the
  caps you choose, a question above them); the second factor of a login is your choice. Real phone calls are gated and off by default.
- A proactive **planner**: plans as graphs with decisions, merges and tasks handed to you, a fast event filter (cloud only
  with your consent, names replaced) and re-planning by the local model, shown in a **Plans** view.
- An **evaluation** (`python -m tadamoo.eval`) that reports pass^k, cost and cache use, and the pages' shell: interface zoom
  for very wide screens, a command palette (Ctrl+K), Alt+number views and a settings index.
- **Questions and suggestions**: the agent asks with a card (choices, a row of your own, a recommended option first), looks at each request before it works, and offers next-message chips that only fill the box; unattended runs never wait ([docs/intake.md](../intake.md)).
- A request for a text too long for one reply (a chapter, a report, a long translation) is written in checked parts: the Agentic coding tab estimates the length, plans the parts, writes them and checks and corrects the whole by itself, after a card offers it; the **Long answer** button in the Chat and the Agentic coding tab asks for it by hand. Images, links and code are kept exactly; a reply cut by the output limit shows a Continue button ([docs/longform.md](../longform.md)).
- A **Target Loop** button in the Chat and the Agentic coding tab: describe a target (long texts, numbered and bulleted lists in every prompt box), and the agent works through tasks, has the result judged against evidence, and goes on with what is not met until it is, it stalls, or you stop it ([docs/target-loop.md](../target-loop.md)).
- In the Chat: **Shift+Tab** changes the mode, shown in a label by *New conversation*; a message that starts with `/` is a command
  (30 of them, skills included); the thinking level belongs to the conversation; and the **memory** now really keeps notes between
  conversations, with its effects and limits written out. All of it is in [The agent harness](harness-overview.md#the-agent-harness) and in the
  [manual](../harness-manual.md).
