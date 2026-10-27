<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/basalt-lockup-dark.png">
    <img src="assets/basalt-lockup-light.png" alt="Basalt" width="480">
  </picture>
</p>

# Basalt Win+GUI: this fork

*English* · [Italiano](README.it.md)

**Basalt Win+GUI** is a fork of **[Basalt](https://github.com/jesdga95/basalt)** by jesdga95 (MIT license), which is itself
a fork of **[Strata](https://github.com/Niko1221/Strata)** by Niko1221 (MIT license). The engine, the `.basalt` model
format, the converter and the server come from Basalt; the copyright notices of both projects and of the code they
include (ggml, llama.cpp and others) are kept in [LICENSE](LICENSE) and [THIRD_PARTY_LICENSES](THIRD_PARTY_LICENSES).
The model files are derived from Qwen3.8-Flash-Next and are under the
[Qwen Community License 1.0](https://huggingface.co/Qwen/Qwen3.8-Flash-Next/blob/main/LICENSE), not under MIT.

What this fork adds: a native **Windows 10/11** port of the engine (the Linux code paths are unchanged), and the
pieces that make it usable on a desktop: a native app that opens on a web Loader which predicts memory and speed
before loading, a configuration wizard that finds the best values for each PC, load profiles, a model library that
finds, verifies and converts models, and start/stop scripts. The upstream README follows below this section, unchanged.

## Differences from the base Basalt

Everything this fork changes, at a glance. The engine, the `.basalt` format, the converter and the OpenAI/Anthropic API
are Basalt's; what follows is added on top, and the Linux code paths are unchanged. Details of each point are further
down and in [docs/WINDOWS.md](docs/WINDOWS.md). The fork includes the upstream Basalt up to 2026-10-17.

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
- **Basalt Win+GUI** (`BasaltWinGUI.exe`): a desktop app (WebView2 window, tray icon, single instance) that starts the
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

**5. Server and API**
- Default port **7232** (the base's examples disagreed with its code); `/api/settings` reads and writes the config file;
  `/api/loader/*`, `/api/wizard/*`, `/api/profiles`, `/api/models/*` and `/api/reqlog*` for the interface.
- Structured output (`response_format`) tries again with the error fed back (`structured_retries`, default 2); the retry
  message says what was wrong and the one change it needs (a cut-off or repeating answer is never sent back), and the reasoning streams while only the
  validated answer is held. The server's body limit and its refusals reach the client whole.

**6. Scripts, languages and data**
- `Avvia-Basalt.ps1` / `Ferma-Basalt.ps1`: `-gui` (default), `-browser`, `-server` for agents, with the same option names
  as Strata's; they attach to a running server and check memory with the Loader's plan before loading.
- English by default, Italian on request, in the app, the page, the scripts and the command-line tools; settings live in
  the user data folder, apart from the caches.

**7. An agent harness, a planner and a revised interface shell** (`harness/`, [docs/harness.md](docs/harness.md))
- A separate process (`python -m harness -p "task"`, or as an editor's agent over ACP) that talks to the server's public
  API: a tool loop with permission levels, a plan mode, hooks, file checkpoints, a hash-chained audit log, a command
  sandbox, a secrets broker, fenced untrusted content, context masking, memory, skills with approval by hash, MCP and
  reference documents (fixed block or search). A **vault** keeps cards, personal data and logins encrypted and the model only writes placeholders that are filled in at the last step (the agent runs as your own user, so what that does and does not protect is in `docs/harness.md`);
  the agent can drive a browser to fill in forms and book, and can pay within limits you set in the **Wallet** view (automatic up to the
  caps you choose, a question above them); the second factor of a login is your choice. Real phone calls are gated and off by default.
- A proactive **planner**: plans as graphs with decisions, merges and tasks handed to you, a fast event filter (cloud only
  with your consent, names replaced) and re-planning by the local model, shown in a **Plans** view.
- An **evaluation** (`python -m harness.eval`) that reports pass^k, cost and cache use, and the pages' shell: interface zoom
  for very wide screens, a command palette (Ctrl+K), Alt+number views and a settings index.
- In the Chat: **Shift+Tab** changes the mode, shown in a label by *New conversation*; a message that starts with `/` is a command
  (30 of them, skills included); the thinking level belongs to the conversation; and the **memory** now really keeps notes between
  conversations, with its effects and limits written out. All of it is in [The agent harness](#the-agent-harness) and in the
  [manual](docs/harness-manual.md).

## How it works

- **One model.** Basalt runs Qwen3.8-Flash-Next (GGUF architecture `qwen4exp`): 48 layers, 512 routed experts per
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

- **Native Windows port** (details in [docs/WINDOWS.md](docs/WINDOWS.md)): MSVC + CUDA 13.1 build, a platform layer
  for files, mappings and positional reads, `VirtualLock` and commit-aware memory budgets (Windows charges every
  allocation, VRAM included, to the commit), pinned arenas sized to WDDM's shared-memory budget, unbuffered reads with
  an I/O completion port, the CPU pool's topology from `GetLogicalProcessorInformationEx` with performance cores
  first on hybrid processors, `basalt serve` in a kill-on-close job object. `--no-pdl` launches every kernel without
  programmatic dependent launch, should a driver misbehave.
- **The app, BasaltWinGUI.exe ("Basalt Win+GUI"), is the main interface.** It starts the server without loading a
  model and opens on the web Loader in its own window (WebView2), so the app and the browser have exactly the same
  settings; a tray icon keeps Basalt Win+GUI running when the window is closed.
- **Web UI** (`/`, English or Italian, see [Language](#language)): Chat, Monitor, Hardware, the **Loader** (*Carica
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
- **Scripts** `Avvia-Basalt.ps1` / `Ferma-Basalt.ps1` with the same option names as Strata's: `-gui` (the default:
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
- **Data folders**: settings in `%APPDATA%\BasaltPannello\` (copied once from `~\.cache\basalt`, never moved).
- **Name**: the app is **Basalt Win+GUI** (`BasaltWinGUI.exe`, new icon); the engine, API, files and scripts keep their
  names.
- **`-server` with a server already running** asks whether to reuse it or restart it (`-riusa`, `-riavvia`, 60 s, no
  answer = reuse).
- **English by default**, Italian on request, for the app, the page, the scripts and the command-line tools.
- **Conversion and verification** from the model library (GGUF → `.basalt`, SHA-256 check), in the background.

## The agent harness

The **harness** (`harness/`) is an agent that works on your files with the model loaded in Basalt. It reads and edits a project,
runs commands, searches the web, fills in forms, keeps notes about what it learns, and asks you before anything it cannot undo.
It is a separate Python process: it talks to the running server through the public OpenAI-compatible API and imports nothing from
the engine, so it needs no GPU of its own. What it keeps (conversations, memory, audit log, checkpoints, approvals) lives in the user
data folder, never in your repository; the only thing it writes in the project is `.basalt/artifacts`, the full text of outputs it
had to shorten.

The reference is [docs/harness.md](docs/harness.md); a step-by-step guide is [docs/harness-manual.md](docs/harness-manual.md).

### Four ways in

All four build the agent in the same place (`harness/stack.py`), so what one can reach the others can reach.

| Way in | What it is |
|---|---|
| **Chat** tab | Conversations with the agent. Each is a long-lived process with its own saved history, so a message does not pay for the start. This is the main way. |
| **Agent** tab | The conversation page, working in folders you add: each folder has its own conversations and a `.basalt` registry, and the agent's edits can be kept or undone block by block. |
| `python -m harness -p "task"` or `basalt-agent` | The command line, for scripts: exit codes, `--json`, budgets. `basalt-agent.cmd` works from any folder. |
| `python -m harness acp` | The agent of an editor that speaks the Agent Client Protocol, over stdio. |

### What it can do

- **Work on a project**: read, list, search, edit and write files; run shell commands; `repo_map` (the definitions of each source file,
  most relevant first); language-server diagnostics and go-to-definition when a server is installed; run Python, JavaScript or
  TypeScript snippets; hand a task to a subagent. Files you attach (text, code, CSV, JSON, Word, Excel, PowerPoint, PDF, images; audio
  and video through the callAPIcall service) are read in pieces, and files the agent makes are checked before they are offered to you.
- **Use the web**: `search_web` (DuckDuckGo unless you set another engine; no key, and the words of each search go to DuckDuckGo) and
  `browse_url` (a page as lines of text, private addresses refused). With the browser tools it drives Edge or Chrome for forms,
  bookings and purchases.
- **Keep what it learns**: a memory of notes per project (below), `AGENTS.md` rules read from the folder up, skills, and reference
  documents offered as a fixed block or searched.
- **Reach other tools**: skills, plugins and MCP servers found on this PC (each one *to approve* until you read and approve it, tied to
  a hash of what you read); a search for a missing one online, with the best candidates put to you and an install that never runs a
  package's scripts; remote MCP servers with OAuth sign-in; other agents through an ACP client and an A2A client; a council of
  advisers for hard questions; a different model for each function (the loaded one, another on this PC, a cloud service).
- **Handle money and identity**: the **Wallet** vault keeps cards, personal data and logins encrypted; the model writes placeholders and
  the value is put in at the last step, only toward the sites the entry names. Payments follow a limit you set (nothing until you set
  one), an optional waiver with caps, and a question above them. The second factor of each login is your choice. Real phone calls ask
  every time and are off by default.
- **Plan**: plans as graphs (actions, sensors, decisions, merges, tasks handed to you) shown in the **Plans** view; the Chat can read
  and propose changes to them, and every change has your yes. An event filter and re-planning by the local model are optional.
- **Run on its own schedule**: routines (a prompt, a folder, a schedule, a mode), evaluation (`python -m harness.eval`, pass^k, cost
  and cache use), replay of a recorded session, and the energy a task used.

### The Chat's conversations

- **A column of conversations** at the left of the Chat: open an earlier one, delete it (a second press confirms), or star it so that it
  is listed apart, above the others; the arrow at its top closes it to a thin strip. The top row holds *New conversation*, *Branches* and
  the mode label at the left, and the kind of conversation, *Thinking* and *Settings* at the right.
- **Mode, with a label**: *Plan only*, *Read only*, *Ask first* or *Automatic*. A two-line label in the top row says which is on
  (*Shift + TAB to change mode:* in grey, the mode under it in bold and in its own colour, as Claude Code does); **Shift+Tab** in the
  message box goes to the next, with a conversation open it changes that one only, and *Automatic* asks the two confirmations of the
  risk dialog the first time only, until the app is closed.
- **Thinking level** per conversation (the *Thinking* list, or `/effort`).
- **Slash commands**: typing `/` opens a menu of 30: on the page (`/clear`, `/mode`, `/plan`, `/copy`, `/export`, `/branch`, `/rewind`,
  `/stop`, `/diff`…), answered by the conversation's process (`/status`, `/context`, `/compact`, `/tools`, `/hooks`, `/mcp`,
  `/memory`, `/permissions`), requests worded for the model (`/init`, `/review`, `/security-review`, `/research`), and every skill found
  on the PC as `/name text`.
- **It always knows the time.** Before each request the agent is given the person's local date, time and timezone by the harness
  itself, so it never has to call a tool to ask, and the start of the prompt is not touched (the server keeps its cache). Each message
  of the Chat, the technical ones too (a tool call, a note), shows its time at the left: the hour in bold, the date under it, in
  small type.
- **Sources open outside.** After a news item or a fact taken from the web, a small *source ↗* button opens that page in the system's
  default browser (also after an address written in the text, and for the sites a search or a page-reading step used).
- **Personal skills.** Press *Make a skill* under a request, check the requests that belong to the task (or write `/start-learn-skill` and
  `/stop-learn-skill`), and the harness keeps what you did as a Python script with the parts that change as parameters: marked by you with
  `# a phrase #`, or found from your words and dates. Run it again from the *Skills* button or with `/skilluse`, with a form that reads dates in
  words. Most of it is plain code, so it costs no tokens; the model is used once to write it and then only where the language must be understood. A
  skill moves to another PC as one `.pskill` file and arrives *to approve*. See the [manual](docs/harness-manual.md#17-personal-skills).
- **A message while it works** is steered into the task, queued, or treated as urgent; **Stop** means stop.
- **Branches, going back, comparing**: a tree of conversations, *Back to here* on any request (actions that stay done are listed
  first), and two continuations of one request side by side.
- **A copy of its own** (not offered from the Chat page, which has no project folder to copy): in a git folder a conversation can work in its own worktree, and you apply the changes you choose as staged
  changes, never a commit.
- **Failures are handled, not hidden**: a busy or unreachable server is retried; a process that dies is started again from its history;
  a tool that cannot be undone is never run again by a restart; after that the conversation is *blocked* and waits for you.

### What you control

- **What it may do without asking**: the four modes; command rules (`shell_command:pytest*`, a deny always wins, and a rule never
  covers a command that chains or redirects); the *Always allow* button in a question. In *Automatic*, an irreversible action still asks
  once the conversation has read outside content and can reach a secret or a vault entry (the Rule of Two).
- **Hooks you write** (`hooks.json`): commands or fixed blocks on tool calls, turns and the session; a hook that cannot decide blocks.
- **Checkpoints and rewind**: the workspace is saved before the first change of a turn, outside the project and within size limits;
  `python -m harness.rewind`, `/rewind` and *Back to here* on a message restore it. Rewinding does not undo a command it ran, and says so.
- **An audit log**: one record per tool call (arguments hashed and masked, the decision, the outcome), in a hash chain you can verify.
- **Time limits and Stop**: every tool has a time limit (15 minutes unless set); Stop ends a running command and everything it started.
- **What it is not**: an operating-system sandbox. A command runs with your rights, with a scrubbed environment, no stdin, a time limit,
  capped output and, on Windows, a job object. Use *Automatic* only in a folder you can lose.

### Memory: what was wrong, what it does now

The memory is a notebook the agent keeps per project and reads in later conversations. The first version of it did not work in practice.

**What was wrong.** The harness searched the notebook at every message, but nothing ever wrote to it, so it always searched an empty
database. The notes it would have found went into the system message, which changes the start of the prompt and makes the server
re-read the conversation. And the search broke identifiers such as `sm_120` into pieces and wanted every word of your message to be in
the note.

**What it does now.**
- The model has three tools. `remember` keeps a short note (at most 2,000 characters, with a category; the same text is kept once;
  at most 5,000 notes per project), `recall` searches the notes and `forget` deletes one by its id. The tool descriptions say what they
  are for; the system prompt does not mention them.
- At every message you write, the harness searches the notes with your words and, if any match, adds up to four of them **after your
  message** as data, marked untrusted. The system block is never touched, so the server keeps the cached start of the conversation.
  This is the Chat setting *Injection → Retrieval at the end* (the default). With *Fixed prefix* or *On request* nothing is added by
  itself and the model has to call `recall`.
- The search is full-text (SQLite FTS5): identifiers are kept whole, a question whose words are not all in a note still finds it
  (first every word, then any word), best match first.
- One file per project folder, in the user data folder: `memory\<16 hex digits>.db`. `/memory` lists the latest ten notes (or
  searches, `/memory words`), and `/status` counts them. `--no-memory` on the command line removes the three tools and the retrieval.

**Effects.**
- A later conversation in the same folder knows the decisions, conventions and facts the agent chose to keep, without reading the
  whole project again, and the cached start of the conversation is not disturbed when a note is added.
- It costs tokens at the end of the context: up to four notes, each up to 2,000 characters, on every message that matches.
- *Ask first* and *Automatic* let the agent write and delete notes without asking. *Read only* asks before it writes one. *Plan only*
  refuses `remember` and `forget` and allows `recall`.
- What `recall` returns is outside content: it counts toward the Rule of Two and no quotation taken from it can confirm a change to
  a plan. Notes added at the end by the retrieval are fenced the same way and also stop a quotation from confirming a plan change.

**Limits.**
- **A note is kept only if the model decides to keep it.** There is no automatic extraction, and a small model may seldom call `remember`.
  Ask it to ("remember that …").
- **The memory belongs to one folder path** (the full path, case ignored). Another folder, a renamed or moved folder, or a conversation
  that works in its own copy of a git folder has a different memory: notes made in a copy stay in the copy's file, and the file of a
  discarded copy stays behind. This follows from how the file is named; it was read in the code, not measured. The **Chat** tab sends
  no folder, so all its conversations work in `chat-workspace` inside the user data folder and share one memory; the Agent tab, the
  command line and the editor use the folder you name, each with its own.
- **Keyword search only.** There is no stemming and no embeddings: "built" does not find a note that says "build". Because the search
  falls back to any word, a note that shares one word with your message can come back although it is not about it.
- **Notes are stored as written, in plain text, not encrypted and not masked.** Do not ask the agent to remember a password or a key.
- **A wrong or hostile note stays.** The agent can be talked into keeping a note by a page it read, and `remember` is a reversible
  action that asks nobody in *Ask first* or *Automatic*. It comes back later as data the model is told not to obey, which lowers the
  risk and does not remove it. The notes added at the end by the retrieval do not set the Rule of Two flag the way `recall` does.
- **No page to browse or edit the notes.** `/memory` reads them (each cut at 300 characters); to remove one, ask the agent to forget
  it, or delete the file. Nothing is shared between folders, users or PCs, and there is no export.

### What is not done

File and network isolation for commands (the job object limits resources only); a try of the ACP agent in a real editor (Zed, JetBrains) and
`terminal/*`; Basalt as an A2A server (it is a client only); bank accounts through an aggregator; a test of Google Pay or an embedded Stripe form on a real site;
and a measure of which context strategy works best (it needs the real model). No real phone call was placed in a test, and Stripe and
Google Pay were not tried against the live services. The list with the reasons is at the end of [docs/harness.md](docs/harness.md).

## Requirements

- An **NVIDIA Blackwell GPU** (compute capability 10.x or 12.x: GeForce RTX 50 series, RTX PRO 6000, B200). The
  engine's kernels are built only for Blackwell; on any other GPU the Loader, the wizard, the start script and the app
  say so before starting, and nothing is loaded.
- **Windows 10 or 11 x64** (WDDM driver) or **Linux x86-64** for the engine, the server and the web UI. The scripts and
  the app are Windows only.
- Enough RAM for the experts that do not fit in VRAM (about 50 GiB for the 3-bit files, with resident experts).
- Python 3.10 or newer with the packages of `requirements.txt`, in a virtual environment `.venv` in the repository
  folder (`python -m venv .venv` then `.venv\Scripts\pip install -r requirements.txt`).
- For the app: the .NET 8 Desktop Runtime and Microsoft Edge WebView2 Runtime (without WebView2 the app opens the
  web UI in the default browser).
- Building: Windows in [docs/WINDOWS.md](docs/WINDOWS.md) (Visual Studio v143 tools, CUDA 13.1, CMake, Ninja), Linux
  in [Build](#build) below. The app: `dotnet publish tools\windows\desktop -c Release -o tools\windows\desktop\publish`.

## Starting Basalt Win+GUI

```powershell
.\Avvia-Basalt.ps1                                             # the app (-gui): the Loader first, nothing loaded yet
.\Avvia-Basalt.ps1 -browser                                    # the server tied to this window, the UI in the browser
.\Avvia-Basalt.ps1 -server --config "Machine Default"          # detached, no UI: returns once the model is loaded
.\Avvia-Basalt.ps1 -server --model "D:\Models\Qwen3.8-Flash-Next-IQ3_XXS.basalt" --port 7240 --vram-libera 2
.\Avvia-Basalt.ps1 -prova                                      # every check, nothing started, nothing written
.\Ferma-Basalt.ps1 --port 7240                                 # stop only that server (its process tree)
```

Every option is accepted with one or two dashes and in any case (`-gui`, `--gui`, `-GUI`).

| option | meaning |
|---|---|
| *(none)* = `-gui` | opens `BasaltWinGUI.exe`, passing it `--config`, `--model`, `--port` and `--vram-libera`; the browser does **not** open, the address is printed and stays available. Without the built app the script says how to build it and uses `-browser` |
| `-browser` | the server tied to this window (Ctrl+C or closing the window stops server and engine, and frees the RAM), the web UI opened in the browser |
| `-server` | the server alone, detached, no app and no browser; the script returns when `/health` says the model is loaded. For agents; `Ferma-Basalt.ps1` stops it |
| `--config "<profile>"` | the load profile; without it, the default profile (below) |
| `--model "<file .basalt>"` | the model to serve, as a full path |
| `--port N` | the port; wins over the profile's (default 7232) |
| `--vram-libera GB` | the VRAM to leave free for other programs, in GB (`1.5` or `"1,5"`), instead of the profile's; `--vram-reserve-mib N` is the same in MiB |
| `-converti` | with `-browser` / `-server`, when the model is a GGUF to convert: show the plan, ask, convert, then serve the new file (in the app: the Loader's **Convert…**) |
| `-riusa` / `-riavvia` | with `-server`, the answer given in advance when a server is already running with other loading parameters: reuse it, or stop it and start again with the new ones (below) |
| `--lang it\|en` | the language of the messages for this run (it also sets `BASALT_LANG` for the server and the app it starts) |
| `-prova` | every check, nothing started; `-browser` / `-server` work on a copy of the config file |
| `-file FILE` | the config file (default `basalt_config.json` beside the script; the app creates one without a model on a new installation) |

`-Sfondo` and `-SoloServer`, the old names, still work as `-server` and print a note. `Ferma-Basalt.ps1` takes
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
    `S`). With no answer within **60 seconds** (`BASALT_ASK_TIMEOUT_S` changes it), or with nobody to ask (an agent with
    redirected input), the running server is **reused**: a server is never stopped without an answer. Restarting stops
    only that server's process tree (`Ferma-Basalt.ps1 --port N`) and starts it again on the same port unless `--port`
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

Directly, on Windows or Linux: `bin\basalt.exe serve --config basalt_config.json [--port N]` (`bin/basalt serve` on
Linux; every option is in [Settings](#settings---config) below and in `python -m serve.server --help`).

### The app

`tools\windows\desktop\publish\BasaltWinGUI.exe` (window and tray: "Basalt Win+GUI"; the file name differs from the
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
Loader), **Start the server alone (for agents)…** (the same as `Avvia-Basalt.ps1 -server`, in place of the app's server),
**Turn off**. If the server cannot start (a port in use, a missing build), the app shows the reason, the end of the log
and a native start panel as a fallback. With the server running, the same UI is at `http://127.0.0.1:7232/` in any
browser.

### Configuration wizard and Machine Default

**Configuration wizard** in the Loader (also `python -m serve.loader wizard` for agents, below) finds the best
values for this PC; the app distributed on other PCs uses it to adapt to each machine. Why: the right values depend
on the VRAM, the RAM, the commit limit, the cores and on which card shows the desktop. Fixed values would either waste
speed on a large PC or fail to load on a small one (or make the page file grow). The wizard computes each value from what
it detects, says why, and can check it with a real measurement.

1. **This PC**: RAM, the commit limit and its use, the page file (Windows-managed files may grow: Basalt Win+GUI
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

The **Download models** panel of the Loader offers Qwen3.8-Flash-Next in the versions that run on Basalt: 23 files from
Hugging Face (and 6 more shown as not compatible, with the reason). Each card says what the file is and how it differs
from the base model, its size, its quantization, and how close it is to the full model with the **source of that
figure** (Basalt's own measurement against Q8_0, the authors' own, Strata's or none: the references differ and must not
be mixed). The model picker shows the same one-line hint for a model it knows.

- **Ready `.basalt` files** (no conversion; `jesdga/Qwen3.8-Flash-Next-Basalt`: Q2_0, IQ3_XXS, IQ3_S, UD-Q4_K_XL, Q8_0;
  and one uncensored by a third party) load as soon as the download ends.
- **GGUF sets** (ISTA-DASLab GSQ-RCO IQ2_XS and the Coder, Swift 1.5, Unsloth UD, the uncensored SC117 and OrcaRouter
  files) are downloaded with their vision projector and then show up as **To convert**; the conversion is the one
  described above, with its own confirmation. The GGUF folder can be deleted afterwards.
- **Uncensored** versions are marked and carry the author's warning. OrcaRouter's repository is **gated**: the panel
  explains how to get a Hugging Face account, accept the terms and make a read token, and takes the token for the
  session only (`HF_TOKEN` or the file `huggingface-cli login` writes are read too). Basalt never saves, shows or logs
  a token, and sends it only to Hugging Face, never along a redirect to another host.
- **A download starts only from its plan**: the files at a pinned revision with their sizes and SHA-256 as Hugging Face
  reports them, the disk and its free space, the access, and the license to accept (Qwen Community License 1.0, plus
  the Swift Open License for Swift files; the repositories that tag themselves Apache-2.0 are treated as the base
  model's license). Files are written as `.part` and renamed only after the SHA-256 matches; an interrupted or stopped
  download continues from where it stopped.
- **Not offered**: Unsloth UD-Q5_K_XL, UD-Q6_K_XL and UD-IQ1_S, and OrcaRouter's IQ2_XXS and Q2_K/Q3_K, because their
  expert formats have no kernel in Basalt; and `Qwen3.8-35B-A3B`, which is not an official Qwen model but a community
  distillation into another architecture (Qwen3.6-35B-A3B: 40 layers, hidden size 2048, 256 experts) that the engine,
  written for Flash-Next's shapes, cannot run.

Entries marked "Not tried in Basalt" have expert formats the engine's lists accept but have not been run through Basalt.
The catalog was read from the repositories on 2026-10-17 (`serve/catalog.py`); sizes are read again when a download is
planned. The routes (this PC and the server's own page only): `GET /api/models/catalog | download | hf`, `POST
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

Per user, never in the repository, in the **user data folder**: `%APPDATA%\BasaltPannello\` on Windows,
`~/Library/Application Support/Basalt/` on macOS, `$XDG_DATA_HOME/basalt/` (`~/.local/share/basalt/`) elsewhere, or
`BASALT_DATA`. It holds `profiles.json` (load profiles, inference presets, the default profile), `models.json` (the
models folders, the default server model, the last output folder), `machine.json` (the wizard's answers: the share of
RAM and commit, the VRAM to leave free, where the monitor is), `calibration\<model>.json` (the measured speeds and
memory), `prefs.json` (the language) and the app's `desktop.json`. Caches that can be rebuilt stay in `%USERPROFILE%\.cache\basalt\` (`~/.cache/basalt/`, or
`BASALT_CACHE`): the header scan, the logs of conversion and verification jobs, the tokenizer and vision files
unpacked from a model, the engine log. Versions before this one kept the settings in the cache folder: they are copied
(not moved) to the user data folder the first time. `basalt_config.json` beside the scripts is written by them and by
the Loader from the chosen profile and is not tracked.

## Using it as a server (API)

The server speaks the OpenAI and the Anthropic APIs at `http://127.0.0.1:<port>` (default port **7232**):

| endpoint | API |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions: messages, tools (function calling), images when vision is on, `response_format`, `stream` |
| `POST /v1/responses` | OpenAI Responses, **stateless**: send the whole conversation each time (`previous_response_id` and `GET /v1/responses/<id>` are not kept) |
| `POST /v1/messages` | Anthropic Messages: tools, thinking blocks, images, `stream` |
| `POST /v1/messages/count_tokens` | Anthropic token count: the same prompt `/v1/messages` would read, not run |
| `GET /v1/models` | the served model and every other model of the models folders it could serve (OpenAI or Anthropic shape) |
| `GET /v1/models/{id}` | one of them |
| `GET /health` | `{"service": "basalt", "loaded": true, ...}` once the model is in; `"standby": true` while a server started by the app waits for the Loader |

**Start it for agents** with `.\Avvia-Basalt.ps1 -server` (it returns once the model is loaded; add `-riusa` in
unattended scripts) and stop it with `.\Ferma-Basalt.ps1`. `basalt.pid` holds the PID, port and profile of a server
started by a script or the app.

**Model name.** The served model's id is the `--alias` (or `"alias"` in the config file's `server` section), else the
name stored in the model file. The `model` field of a request may be anything (the server runs one model at a time)
and the answer carries the served name, with one exception: a name that `GET /v1/models` lists as another, unloaded
model is refused (below).

**The model list.** `GET /v1/models` lists the served model first (`"status": {"value": "loaded"}`, or `"unloaded"`
while it loads on the first request), then every `.basalt` file of the models folders that the Loader shows as
**ready** (a GGUF to convert or a file Basalt cannot run is not listed), each with `"status": {"value": "unloaded"}`.
The id of such a model is its file name without `.basalt` (with a short hash after it when two folders hold the same
file name): the name to pick in a client. Each entry has `created` (the file's date), `owned_by`, `display_name`, the
context length when the file says it (`context_length`; the served model's `meta.n_ctx` is the context it runs with,
another model's `meta.n_ctx_train` the longest it was trained for), `architecture.input_modalities` (`image` when the
file has a vision projector) and, only on a server bound to 127.0.0.1, the file's `path`. The folders are scanned
from the files' headers on a thread, at most every 30 seconds, so neither the list nor a chat request waits for the
disk; the list works in standby too (no model loaded: only the folders' models). A request that names a model the
list shows as unloaded (by its id, its file name or its path) gets a 404 that says how to load it, instead of an answer
from the loaded model: OpenAI `{"error": {"code": "model_not_found", ...}}`, Anthropic
`{"type": "error", "error": {"type": "not_found_error", ...}}`.

```sh
# OpenAI shape
curl http://127.0.0.1:7232/v1/models
# {"object": "list", "data": [
#   {"id": "Qwen3.8 Flash Next Uncensored", "object": "model", "created": 1791904651, "owned_by": "basalt",
#    "display_name": "Qwen3.8 Flash Next Uncensored", "status": {"value": "loaded"}, "meta": {"n_ctx": 131702},
#    "context_length": 131702, "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
#    "path": "D:/Modelli/Qwen3.8-Flash-Next-Uncensored-IQ3_XXS.basalt"},
#   {"id": "OrcaRouter-IQ3_XXS", "object": "model", "created": 1791904651, "owned_by": "basalt",
#    "display_name": "OrcaRouter", "status": {"value": "unloaded"}, "context_length": 262144,
#    "meta": {"n_ctx_train": 262144}, "quant": "IQ3_XXS", "path": "D:/Modelli/OrcaRouter-IQ3_XXS.basalt", ...}]}

# Anthropic shape: with an anthropic-version header (or x-api-key without Authorization)
curl http://127.0.0.1:7232/v1/models -H "anthropic-version: 2023-06-01"
# {"data": [{"type": "model", "id": "Qwen3.8 Flash Next Uncensored", "display_name": "Qwen3.8 Flash Next Uncensored",
#            "created_at": "2026-10-13T15:17:31Z"},
#           {"type": "model", "id": "OrcaRouter-IQ3_XXS", "display_name": "OrcaRouter",
#            "created_at": "2026-10-13T15:17:31Z"}],
#  "has_more": false, "first_id": "Qwen3.8 Flash Next Uncensored", "last_id": "OrcaRouter-IQ3_XXS"}

# one model, in either shape (an id with spaces is URL-encoded); 404 model_not_found / not_found_error otherwise
curl http://127.0.0.1:7232/v1/models/OrcaRouter-IQ3_XXS
curl http://127.0.0.1:7232/v1/models/OrcaRouter-IQ3_XXS -H "anthropic-version: 2023-06-01"

# a chat that names the unloaded model
curl http://127.0.0.1:7232/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model": "OrcaRouter-IQ3_XXS", "messages": [{"role": "user", "content": "hi"}]}'
# 404 {"error": {"message": "The model 'OrcaRouter-IQ3_XXS' is not loaded (this server is serving 'Qwen3.8 Flash
#   Next Uncensored'). Load it in the web page's Loader (choose it in the model list, then Load), or start the server
#   with it: .\Avvia-Basalt.ps1 --model \"D:/Modelli/OrcaRouter-IQ3_XXS.basalt\"", "type": "invalid_request_error",
#   "param": "model", "code": "model_not_found"}}
```

TODO (possible future: load on request): the server does not switch models by itself when a request names another
listed model; it only says how to load it. Loading the asked model on request is left for later.

**API key.** The server listens on 127.0.0.1 only and needs no key: clients that insist on one can send any value.
`--api-key KEY` (command line only, never in a config file) makes `/v1/*` require it as `Authorization: Bearer KEY`
or `x-api-key: KEY`; it is needed to listen on another address (`--host 0.0.0.0`) and to let web pages of other
origins use the server. The scripts and the app start the server on 127.0.0.1 without a key.

**Streaming.** `"stream": true` streams server-sent events in each API's own format (OpenAI `chat.completion.chunk`,
Responses events, Anthropic `message_start` / `content_block_delta` / ...), reasoning included as
`reasoning_content` (OpenAI) or `thinking` blocks (Anthropic).

**Reasoning.** The model thinks by default (the template's high level). OpenAI: `"reasoning_effort": "none" | "low" |
"medium" | "high"` (Responses: `"reasoning": {"effort": ...}`). Anthropic: `"thinking": {"type": "disabled"}`, or
`"enabled"` with `budget_tokens` (under 2048: low, under 8192: medium, else high). `reasoning_budget_tokens: N` on
any endpoint closes the thinking after N tokens and lets the model answer.

```sh
curl http://127.0.0.1:7232/v1/chat/completions -H "Content-Type: application/json" -d '{
  "model": "basalt", "messages": [{"role": "user", "content": "Say hello in three languages."}],
  "max_tokens": 512, "reasoning_effort": "low"}'

curl -N http://127.0.0.1:7232/v1/messages -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" -d '{
  "model": "basalt", "max_tokens": 1024, "stream": true,
  "messages": [{"role": "user", "content": "Write a haiku about basalt."}]}'

curl http://127.0.0.1:7232/v1/responses -H "Content-Type: application/json" -d '{
  "model": "basalt", "input": "What is 17 * 23?", "reasoning": {"effort": "none"}}'
```

(In PowerShell use `curl.exe` and put the JSON in a file: `-d "@body.json"`.)

Python, with the official SDKs (`pip install openai anthropic`):

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:7232/v1", api_key="none")   # any key, unless --api-key was given
r = client.chat.completions.create(model="basalt", messages=[{"role": "user", "content": "Hello!"}],
                                   max_tokens=512, reasoning_effort="low")
print(r.choices[0].message.content)

for chunk in client.chat.completions.create(model="basalt", stream=True,
                                            messages=[{"role": "user", "content": "Count to five."}]):
    print(chunk.choices[0].delta.content or "", end="", flush=True)

print(client.responses.create(model="basalt", input="Name three volcanic rocks.").output_text)
```

```python
import anthropic

client = anthropic.Anthropic(base_url="http://127.0.0.1:7232", api_key="none")
msg = client.messages.create(model="basalt", max_tokens=1024,
                             messages=[{"role": "user", "content": "Hello!"}])
print(msg.content[-1].text)                      # a thinking block may come first

with client.messages.stream(model="basalt", max_tokens=1024, thinking={"type": "disabled"},
                            messages=[{"role": "user", "content": "Count to five."}]) as s:
    for text in s.text_stream:
        print(text, end="", flush=True)

print(client.messages.count_tokens(model="basalt", messages=[{"role": "user", "content": "Hello!"}]).input_tokens)
```

**Claude Code** talks to the Anthropic endpoint:

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:7232"
$env:ANTHROPIC_AUTH_TOKEN = "none"                 # any value; the key given with --api-key, if any
$env:ANTHROPIC_MODEL = "basalt"                    # any name: the server has one model
$env:ANTHROPIC_SMALL_FAST_MODEL = "basalt"
claude
```

(On Linux/macOS: `export ANTHROPIC_BASE_URL=http://127.0.0.1:7232` and so on.) Agent tools send long prompts:
choose a context of at least 65,536 tokens in the load profile, and keep `--parallel 1` unless several agents work at
once (the context is shared between the slots, see [`--parallel`](#concurrent-requests---parallel-n)).

**Structured output** (`/v1/chat/completions`): `"response_format": {"type": "json_object"}` or `{"type":
"json_schema", "json_schema": {"name": ..., "schema": {...}}}`. The answer is checked; when it is not valid JSON, or
does not match the schema, the request is generated again with the error fed back, up to `structured_retries` times
(0 to 5, default 2; 0 turns it off). The new try is the same conversation plus one user message that starts with the
error. An answer that was valid JSON (or had one plain syntax fault) and repeated nothing is sent back as the
assistant's turn, and the message asks for the one change the error names and nothing else; an answer that was cut
off or wrote an entry over and over is never sent back, so a model that looped is not shown its loop again (the
message then has the error, the JSON path where the text broke and the repeated entry, once, with its count). No
message holds a count of characters. Only the validated
answer is held until the check. Reasoning streams as it is written, rejected tries included; a rejected try is
announced by a chunk with an empty `delta` and `"structured_output": {"attempt": 1, "discarded": true, "reason": "...",
"retrying": true}`, its answer is never sent, and the final chunk carries the validated answer only, with
`"structured_output": {"validated": true, "attempts": 2, "discarded": [{"attempt": 1, "reason": "..."}]}` (a
non-streaming reply returns the last try's reasoning and answer, with the same field). `GET /v1/status` tells it under
`structured_output`: `content_buffered` and `reasoning_streamed` are true, and `stream_buffered` (kept for older
clients) means the validated answer is held until the check. `/v1/responses` with a JSON `text.format` streams its
reasoning too and checks the answer once, without a retry; `/v1/messages` has no structured output.

**Busy and errors.** With `--parallel 1` requests wait their turn in arrival order. While the engine is starting, or
while a server started by the app is in standby (no model loaded yet), a request gets a 503 that says why: try again
later, or load a model in the Loader. A prompt longer than the context is refused with a 400 that names the context.

## Loading settings, one by one

Set in the Loader (each with its reason and the live prediction), saved in load profiles, written to the `server` /
`engine` sections of `basalt_config.json`. They take effect when the engine is (re)loaded.

| setting | what it does | how to choose |
|---|---|---|
| **Model** (`server.model`) | the `.basalt` file to serve | from the model library; a GGUF is converted first |
| **Port** (`server.port`) | the HTTP port, default 7232 | change it only to run two servers or when 7232 is taken |
| **Parallel requests** (`server.parallel`, 1-8) | requests served at once in the engine's batch slots | 1 for one user or one agent (fastest per request); more only for several agents at once. The context is shared: each slot gets `max-context / N` |
| **Full context per slot** (`server.slot-full-context`) | every slot gets the whole context instead of a share | costs N times the KV cache, taken from the expert cache: leave off unless each agent really needs the full length |
| **Image input** (`server.vision`) | `off`, `dynamic` (borrows expert-cache slots only while it reads an image: no standing VRAM), a GPU number such as `0` (encoder always on that card, about 2.2 GiB of its VRAM; the old names `on` and `peer` still read as GPU 0 and GPU 1), `cpu` (no VRAM, slow) | `dynamic` when the file has a projector, else `off` |
| **Context** (`engine.max-context`) | the longest conversation, in tokens (up to 262,144) | the longest that fits ("Valori sicuri" and the wizard pick it); a shorter one leaves more VRAM for experts (a little faster) and needs less commit |
| **KV cache** (`engine.kv`) | the precision of the attention cache: `fp16`, `int8`, `q4_0`, `k8v4` (INT8 keys, Q4_0 values) | `q4_0`: a quarter of fp16's memory, so long contexts fit; `int8` or `k8v4` for slightly more precise attention at more memory |
| **Prompt chunks** (`engine.prefill`) | how long prompts are read: `auto` (the largest chunk the expert cache can lend), `auto:8192/16384/32768` (a ceiling), or a fixed size | `auto` |
| **Verify window** (`engine.spec`, 0-8) | the longest window: the MTP drafter proposes up to N-1 tokens (fewer when it is unsure), the model checks them in one pass; 0 = off | 7 when the file has a drafter (the wizard measures 5, 7 and 8); the output is the same at any value, only the speed changes |
| **Expert cache** (`engine.expert-cache`) | expert slots kept in VRAM: `auto` (all the VRAM left after the reserve) or a number | `auto` |
| **Resident experts** (`engine.resident-experts`) | the experts not in VRAM are copied into RAM once at load, instead of being read from the file | on when they fit in RAM with the free share kept (they take about 50 GiB for the 3-bit file); off on PCs with less RAM, at a cost in speed |
| **VRAM to leave free** (`engine.vram-reserve-mib`) | VRAM the engine never touches (desktop, driver, other programs) | see [VRAM to leave free](#vram-to-leave-free); the wizard's rule: 0.5 GB + 1.5 GB if the GPU drives the main monitor (about 0.4 GB for a secondary one) |
| **PLE table** (`engine.ple-io`) | the per-layer n-gram table: `direct` (read from the SSD as needed, never in RAM), `ram` (locked in RAM at the start: RAM but no commit), `mmap` (a test arm) | `ram` when it fits with the free share kept, else `direct`; on a slow or rotational disk prefer `ram` |
| **CPU workers** (`engine.pool-workers`) | the threads that compute the experts not in VRAM | the performance cores plus half the efficiency cores, less one (one core stays for the server and the desktop) |
| **KV kept in VRAM** (`engine.kv-resident`, 0 or from 20,480 cells) | only this many cells of each layer's KV stay in VRAM, the whole K/V is pinned in RAM and the VRAM it frees goes to experts; a context that fits in these cells is not streamed | very long contexts on a small card; slower the more is streamed; not with `k8v4`. The plan moves the KV pool from the VRAM figure to the RAM figure |
| **Context extension** (`engine.rope-scaling`: `none`, `linear`, `yarn`) | extends the context past the trained length | fixed for the run and it changes the output; empty = the model file's |
| **Extension factor** (`engine.rope-scale`, 1 or more) | the factor for `linear` and `yarn` | empty = the model file's, else 1 |
| **RoPE frequency base** (`engine.rope-freq-base`, 0 or above 1) | the raw frequency base, 0 = the model's own | leave empty unless you know the value you want; fixed for the run |
| **CPU share of the prompt experts** (`engine.prefill-cpu-share`, 0-1) | the share of the streamed prompt experts the CPU computes; 0 has the GPU stream them all | 0.6 by default; lower on a weaker CPU |
| **Adaptive swaps** (`engine.adapt-swaps`) | the most experts one adaptive round swaps into the main GPU's expert cache; 0 = off | 96 by default; with swaps on, the output is not bit-identical from run to run |
| **RAM budget for experts** (`engine.resident-budget-gib`, GiB) | at most this many GiB of experts copied to RAM, the most used first | empty = automatic; the plan's RAM figure is limited to it; the engine refuses a budget that leaves less than its RAM floor free |
| **Experts in the file mapping** (`engine.mmap-experts`) | no resident RAM copy: the experts are read through a file mapping; it wins over resident experts and the RAM budget | usually slower (it depends on the OS page cache); off by default |
| **Elastic expert cache** (`engine.vram-elastic`) | the expert cache in segments, so `POST /v1/vram` can lend VRAM to other programs between requests | off unless another program needs VRAM at times |
| **Give back the GPU memory's commit** (`engine.release-vram-commit`, Windows) | gives back the system commit WDDM charges for the GPU memory | off; the engine can hang if another program then pushes its VRAM out |
| **Expert profile file** (`engine.expert-profile-save`, advanced) | the file the engine saves what the adaptive cache learned to, on exit and periodically | empty = not saved; `tools/convert/convert.py --profile` reads it |
| **Profile save interval** (`engine.expert-profile-save-every`, minutes, advanced) | how often the profile is saved; 0 = on exit only | 10 by default |
| **Prompt checkpoints** (`engine.prompt-cache`) | conversation checkpoints kept between requests, about 118 MB of RAM each per slot; 0 reads every prompt from the start | 6 by default; the plan counts them |
| **Checkpoint every** (`engine.prompt-cache-every`, tokens) | also checkpoint every N freshly read prompt tokens; 0 = off | 16,384 by default |
| **First-turn checkpoint** (`engine.prompt-cache-root`, tokens) | checkpoint the end of the system prompt when it is at least this long; 0 = never | 2,048 by default |
| **Parked conversations** (`engine.conversation-cache-mib`, MiB) | RAM budget for parked conversations, so separate chats come back without re-reading; 0 = off | 0 by default; the plan counts the whole budget |
| **Parked conversations, at most** (`engine.conversation-cache-slots`) | how many conversations are parked at a time | 4 by default; 0 turns the cache off |
| **RAM kept free when parking** (`engine.conversation-cache-min-free-mib`, MiB) | the physical RAM floor kept free when a conversation is parked | 2,560 by default |
| **Grammar** (`server.grammar`) | holds every tool call and JSON response format to its schema inside the engine | on by default; applies at the next Load |
| **Default thinking budget** (`server.reasoning-budget-tokens`) | the thinking budget for requests that name none; 0 = none | keep it below the requests' `max_tokens`; applies at the next Load |

One more rule is per PC, not per profile: the **share of RAM and commit kept free** for other programs (15% unless the
wizard was told otherwise, 5-50%, at least 2 GB). Every plan, "Valori sicuri" and the wizard keep it free, and a load
that would need the page file to grow is refused.

## Chat (inference) settings, one by one

In the chat's settings panel; saved per client, and as named **inference presets** shared through the server (the
**Machine Default** preset is the wizard's). A request carries only the fields that are set, so everything left empty
uses the **model's own values** (from the source GGUF's `general.sampling.*`, shown as placeholders), then the
engine's. API clients send the same fields in their requests.

| setting | request field | what it does | guidance |
|---|---|---|---|
| **Thinking** (beside the message box; effort) | `reasoning_effort` | how much the model reasons before answering: High (default), Medium, Low, Off | High for hard problems, Low/Off for quick replies and helper calls (much faster) |
| **Reasoning budget** | `reasoning_budget_tokens` | after N thinking tokens the thinking is closed and the model answers; empty or 0 = no limit | 2,000-8,000 to bound slow answers; too low cuts the reasoning short |
| **Max output tokens** | `max_tokens` | the longest answer, thinking included | leave empty for the context's room; set it for short, predictable replies |
| **System prompt** | first `system` message | instructions kept at the head of every conversation | short and specific |
| **Stop strings** | `stop` | the answer ends before any of these strings | for formats that end with a marker |
| **Temperature** | `temperature` | randomness: 0 always takes the likeliest token | the model's value (1.0 for Qwen3.8-Flash-Next); 0.6-0.8 for steadier code |
| **Top-p** | `top_p` | samples only from the likeliest tokens that together reach this probability | the model's (0.95) |
| **Top-k** | `top_k` | samples only among the k likeliest tokens (1-64) | the model's (20) |
| **Min-p** | `min_p` | drops tokens less likely than this share of the top one | 0-0.05 |
| **Repetition penalty** | `repetition_penalty` | makes recent tokens less likely (1 = off) | 1-1.1; higher degrades code and lists |
| **Presence penalty** | `presence_penalty` | a flat penalty for any token already used (0-2) | 0; up to 1.5 if answers loop |
| **Frequency penalty** | `frequency_penalty` | a penalty that grows with each use (0-2) | 0 |
| **Penalty window** | `penalty_last_n` | how many recent tokens the penalties look at | 64 (the engine's) |
| **Seed** | `seed` | the same seed with the same settings repeats an answer | empty for varied answers |
| **Context policy** | (chat page) | what the chat does when the conversation outgrows the context: **Stop at the limit**, **Sliding window** (drops the oldest turns), **Compact** (the model summarizes the middle turns at the threshold; the system prompt, the first message and the newest two turns stay verbatim) | Compact for long sessions; Stop when every word must stay |
| **Compact at (% of context)** | (chat page) | the share of the context that starts compaction (30-100%, default 80%) | 80% |
| **Structured output** | `response_format` | Text, JSON object, or JSON schema (with a name and the schema) | for programs that parse the answer |
| **Tries after a rejected answer** | `structured_retries` | how many times an invalid JSON answer is generated again, with the error fed back (0-5, default 2) | 2 |

**Which preset a client opens with**: the one it used last, if it still exists; otherwise it follows the current
default (Machine Default, else the model's values). Settings changed by hand are kept, and the panel says which preset
is in use. **Model defaults** puts the model's own values back; **Restore Machine Default** the wizard's.

## Calibration

The Loader predicts the answer and prompt speeds of every plan. The prediction starts from a reference measurement and
scales it by the expert-cache hit rate (how many experts the plan keeps in VRAM), the verify window and the prompt
chunk. **Calibrate** in the Loader (also run automatically at the first load of a model) measures the running
configuration with three requests: an answer without reasoning (512 tokens), one with reasoning (1,024 tokens) and a
long prompt (about 8,000 tokens). The result goes to `calibration\<model>.json` in the user data folder, keyed by the
model and the settings. From then on the predictions of that model start from the calibration of exactly the same settings, else
from the model's newest one, instead of the built-in reference, and the Loader says which one it used. The wizard's measurement is the same calibration, repeated
for the verify windows it tries. Next to the speeds, the file keeps the RAM and commit the load
really took (see "Measured memory").

## Language

English by default; Italian on request. One choice serves the app, the web page, the scripts and the command-line
tools, in this order: `--lang it|en` (scripts and app, for that run) > the `BASALT_LANG` environment variable > the
choice saved in **Settings → Language** on the web page (or with `python -m serve.lang set it|en|auto`, kept in
`prefs.json` in the user data folder) > English. The language of Windows is not used. The Monitor and Hardware pages
and some technical server messages are in English only.

---

# Upstream Basalt README

The rest of this file is the README of [Basalt](https://github.com/jesdga95/basalt) by jesdga95, as it was when this
fork was made.

Basalt is a local inference engine for the **Qwen3.8-Flash-Next** mixture-of-experts model. It targets one thing:
Linux with an NVIDIA Blackwell GPU (sm_120). The experts that do not fit in VRAM live in system RAM (and optionally on
a second GPU used as a peer expert tier), so a large MoE model runs on a single workstation.
Ready-to-run `.basalt` packs: see [Models](#models).

It is a fork of [Strata](https://github.com/Niko1221/Strata) (MIT), stripped down to that one target. See
[THIRD_PARTY_LICENSES](THIRD_PARTY_LICENSES).

<p align="center">
  <img src="docs/img/frontend.gif" alt="The Basalt web UI while a reply streams" width="720">
</p>

<p align="center"><sub>The web UI at <code>/</code> while a reply streams: live decode speed, draft accept rate, context, GPU load and the request log.</sub></p>

## What is in the repository

- `src/`, `include/`: the C++/CUDA engine (one binary, `basalt`).
- `serve/`: a Python server with an OpenAI- and Anthropic-compatible API, and a web UI (chat, monitor, settings).
- `tools/convert/`: the model-file converter (`convert.py`), its section builders and the drafter fetcher (`mtp_fetch.py`).
- `tools/`: the .basalt reader and writer (`model_file.py`), the PPL/KLD quality gate (`ppl_gate.py`), the greedy-identity
  check and the tokenizer.
- `tests/`: parity and unit tests.

## Requirements

- Linux x86-64 with AVX2 (AVX-512 is used when present).
- An NVIDIA Blackwell GPU (compute capability 12.0) and CUDA 13.
- CMake 3.24 or newer and a C++20 compiler.
- Enough system RAM to hold the experts that do not fit in VRAM.
- Python 3.10 or newer for the server: `pip install -r requirements.txt`.

## Build

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j        # bin/basalt, bin/basalt-bench, bin/basalt-vision
```

### Tests

The tests are off by default. Configure with `-DBUILD_TESTING=ON`, build, then run `ctest`:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
cmake --build build -j
ctest --test-dir build
```

The kernel parity tests need the GPU; the file-format, cache and memory unit tests do not. `expert_parity` and `pool_test`
need local expert data (`experts.bin` in the source directory, not shipped), and `platform_memory_test` needs a locked-memory
limit raised first (`ulimit -l`); without that, those three fail.

`basalt-bench` reads its pre-tokenized prompts from `data/bench` (token ids for the Qwen3.8-Flash-Next tokenizer: the
three decode prompts and a prefill filler); run it from the repository root or pass `--fixtures DIR`. `basalt-bench
--help` says how the files are made.

The build needs nothing outside this repository and fetches nothing. The parts of ggml and llama.cpp that Basalt uses
(quantization formats, CPU dot products, the prompt path's MMQ kernels) are extracted into `src/` and `include/basalt/ggml/`,
with their MIT licence in `THIRD_PARTY_LICENSES`.

## Run

```sh
bin/basalt serve --model /path/to/model.basalt --host 127.0.0.1 --port 7232 [--alias NAME]
```

The server listens on 127.0.0.1 by default; do not expose it beyond that without `--api-key`.

Its web UI is at http://127.0.0.1:7232/: **Chat** streams from the server's own `/v1/chat/completions` (the conversation stays in the browser), **Monitor** and **Hardware** show the live statistics, and **Settings** edits the main fields of the `--config` file (port, parallel, vision, max-context, kv, spec, expert-cache, prefill) through `GET`/`POST /api/settings`. Values are checked with the server's and engine's own rules, every other key in the file is kept, and the old file is saved as `FILE.bak`. Saving needs the API key when one is set and works only from the PC the server runs on; the changes apply when the server is started again.

### Settings: `--config`

The engine's built-in defaults are untuned (4096 context, fp16 KV, no speculation, no expert cache), and the model
file carries no engine flags. Put every setting in one `basalt_config.json` and pass `--config`:

```json
{
  "server": {
    "model": "model.basalt",
    "alias": "Qwen 3.8 FN",
    "gpu": "0,1",
    "host": "127.0.0.1",
    "port": 7232
  },
  "engine": {
    "expert-cache": "auto",
    "prefill": "auto",
    "spec": 7,
    "max-context": 262144,
    "kv": "int8",
    "resident-experts": true,
    "vram-reserve-mib": 700
  }
}
```

```sh
bin/basalt serve --config basalt_config.json                     # everything from the file
bin/basalt serve --config basalt_config.json --max-context 32768  # the command line wins
```

### Request log: `--request-log`

The **LOGS** tab of the web page shows, for each request of `/v1/chat/completions`, `/v1/responses` and `/v1/messages`:
what came in (endpoint, client, parameters and the body as received), the reasoning and the answer as they were
written (every structured try, rejected ones included), each try's outcome and the repair message sent for the next
one, the times (prompt and cached tokens, prefill, first token, speed, duration), why it ended, a cancel or an error,
the hardware figures the server already reads while a request runs, and the lines the server and the engine print. It
is kept in memory only, never written to disk, never sent anywhere, and served with the same protections as the rest of
the interface (the API key when one is set).

It is on by default. `--request-log off` (`"request_log": false` in the `server` section of the config file) turns it
off; `--request-log-mib N` (`"request_log_mib": N`, default 32) sets the memory it may use, after which the oldest
entries are dropped and the page is told how many. `GET /v1/status` reports `request_log: {"enabled", "capacity_chars"}`.

| Endpoint | |
|---|---|
| `GET /api/reqlog?after=<id>&max_chars=<n>` | the events with an id above `after`, oldest first: `{"enabled", "epoch", "last", "events": [{"id", "t", "req", "kind", "data", "text"}], "skipped", "dropped": {"events", "chars"}, "capacity_chars", "held_chars"}`. With the log off: `enabled: false` and no events (HTTP 200). `kind` is `request`, `reasoning`, `content`, `attempt`, `repair`, `timing`, `finish`, `cancel`, `error`, `gpu`, `engine` or `server` |
| `GET /api/reqlog/export?format=text\|jsonl` | the whole log as a downloaded file, `basalt-requests-YYYYmmdd-HHMMSS.log` or `.jsonl` |
| `POST /api/reqlog/clear` | empties the log, `{"cleared": true}`; JSON from the server's own page only |

Every setting, with its type, default and example configs: [docs/configuration.md](docs/configuration.md).

### Concurrent requests: `--parallel N`

`--parallel N` (1..8, also `"parallel"` in the config's `server` section) serves N requests at once in the
engine's batch slots; the default 1 is the single-stream path. Further requests wait in arrival order (a later one
may go first only to resume a slot that holds its conversation, a few times at most). A slot is freed when its
request finishes, is cancelled or its client leaves. `/metrics` reports `live.slots` (`total`, `active`,
`queue_depth`). As in llama.cpp, `--max-context` is shared: each slot holds `--max-context / N` tokens (`--parallel 4
--max-context 262144` gives four slots of 65536; ask for `--max-context 1048576` for four of 262144). The solo
path, used while one request runs alone, has the same length, so a request that fits a slot never outgrows it, and
one that does not is refused with a 400 naming that length. `/slots`, `/props` and `/v1/models` report the context
each slot has. `--slot-full-context` (also `"slot-full-context"` in the config's `server` section) gives every slot
the whole `--max-context` instead, at N times the KV cache; the startup line says so. The slots' KV cache, and one
solo session of the same length, come out of the expert cache.

On one GPU (not with `--kv-resident` or `--vram-elastic`) the KV cache is elastic: each session backs its first 32768
cells at startup (the K/V pools, the indexer's block keys, the RoPE tables and the draft layer's K/V alike), and the
rest of that VRAM holds the expert cache's last slots until a request needs it. A request backs its prompt plus 16384 cells before the prompt is read (and 16384 more whenever decode runs past that) by taking
those slots back; their experts become CPU misses read from their RAM copies, which the RAM copy keeps for exactly this.
The next request that needs less gives the cells back and the cache refills the slots from RAM. A short request so
decodes with the cache a 32k context would have, whatever `--max-context` is. The startup line `elastic KV: ...` says
how much of the cache is lent this way, and each move is logged (`elastic KV: N cells backed ...`).

While other slots are decoding, a new prompt is read beside them: the prompt path runs on its own thread, the slots'
decode windows keep running (their kernels take the higher stream priority), and the few steps that touch the draft
layer or the output run between two windows. A long prompt therefore does not stop the other requests. A prompt that
arrives while no other slot is decoding is read as in the single-stream path.

At startup the server prints one `[server] kv cache:` line per device with all the session state placed there, for
example `12.5 GiB on cuda2 (solo 262144 tokens + 2 slots x 262144 tokens + draft K/V 0.5 GiB)`. With elastic KV (the
default on one GPU) it reads `0.6 / 4.4 GiB on cuda0 (elastic)`: the memory backed at startup, and what the cache can
grow to at `--max-context`. Because that memory is
taken from the expert cache, the engine warns (`basalt warning: the N batch slots' sessions ... take X GiB of CUDA0
...`) when it leaves the cache short of the expert set, or at under half of what it would hold without the slots, and
names the remedies: a smaller `--max-context`, no `--slot-full-context`, fewer `--parallel` slots. If a slot's session
does not fit at all, the engine says which slot (`WARNING: --batch N: slot K's session does not fit`) and starts with
the slots that did fit; if fewer than two fit, `--batch is off` and the server runs one request at a time.

- `server` holds the server's options (`python -m serve.server --help`), named as the flags without the dashes:
  strings or numbers, `true`/`false` for a switch (`lazy`, ...), a list for the repeatable ones
  (`allowed-host`, `cors-origin`). `model` and `tokenizer` paths are relative to the file.
- `engine` holds engine flags (`bin/basalt --help`), the same way: a string or number, `true` for a switch, `false`
  to leave it off, a list for a flag with several values (`"control-vector-layer-range": [4, 44]`). Any flag the engine accepts works, on the command line as well (`--kv int8`, `--resident-experts`).
  A name the engine does not accept stops the server at startup, as does one in the wrong section.
- Precedence: command line > config file > built-in defaults.
- The API key is command line only (`--api-key`); a config file that sets it is refused.
- `"relaxed-drafting": 0.3` (or `true` for the default 0.5; `--relaxed-drafting [P]` on the command line) trades
  exactness for speed under sampling: a draft the model gives probability P or more is kept even when the model's own
  draw would have been another token. Lower P is faster and further from the model's true sampling distribution; the
  measured speed and cost per value are in [docs/precision.md](docs/precision.md#relaxed-drafting). Off by default:
  without it, speculative decoding returns exactly what plain sampling would. Greedy (temperature 0) requests,
  `logit_bias`, JSON `response_format`, control tokens and everything inside a tool call stay exact. A request can set
  its own value with `"relaxed_drafting": P` (or `false` for exact). Single-stream decoding only; `--parallel` slots
  stay exact.
- The startup line `[server] engine: ...` lists the settings that came from the file or the command line; without
  `--config` it says the built-in defaults apply.

### Prompt chunks: `--prefill`

A prompt is read in chunks. `--prefill auto` (the usual setting) takes the largest chunk whose buffers the expert cache
can lend: the buffers are carved from the cache's last slots for the length of the prompt and the experts in those slots
are copied back afterwards, so a short prompt lends only what it needs. A bare `auto` reads a prompt in the fewest equal chunks of at most 16384 tokens, because
a chunk's cost per token stops falling past a few thousand tokens; it is still limited to what the lend can fund, which with
`--resident-experts` is the lent slots whose experts the RAM copy holds plus a fixed amount of file reading (7 GiB), and without
`--resident-experts` is 8192. `--prefill auto:N` (N = 8192, 16384 or 32768) sets the ceiling yourself; a number sets a
fixed chunk. A prompt longer than one chunk is split into equal chunks (a short tail chunk costs nearly a whole pass over
the streamed experts). The startup line `prompt chunk auto: N tokens` says what was
chosen.

### Memory floors

Basalt refuses settings that would leave the machine without memory, and never shrinks what you asked for:

- `--vram-reserve-mib` below 256 is refused at startup: with less VRAM free once everything is loaded, a later
  allocation can make the driver page GPU memory and requests stall.
- When some experts are read from the SSD (they fit in neither the GPUs nor the RAM copy), the RAM copy leaves a
  floor free for the OS file cache: the RAM margin (2 GiB or 5% of the RAM, the larger) plus 3.5 GiB plus two layers'
  experts, which is what one prompt chunk reads from the file. The startup line `RAM floor ...` gives the figures.
  When every expert has a place, only the margin is kept, as before.
- A server's RAM copy also leaves room for what serving takes after startup: the conversation checkpoints (each the
  model's whole running state, `--prompt-cache` of them per slot plus one), `--conversation-cache-mib`, one image's
  buffers and a request's own buffers. The startup line `RAM ... free, guard ...` gives the figures.
- An explicit `--resident-budget-gib` that would leave less than that floor free is refused at startup; the error
  states the RAM free and the largest safe value ('max').
- While serving, a request that finds MemAvailable below the margin plus 3.5 GiB (the margin alone when no expert is
  read from the SSD) stops with an error (`RAM low (... free < ... guard): request stopped`, also returned to the
  client); the server keeps running and takes the next request.

### Vision: `--vision`

Image input is one server option, `--vision off | dynamic | cpu | N` (config: `"vision": "0"` in the `server` section), where
N is a GPU number as `nvidia-smi` numbers them. It is `off` by default. The encoder is Basalt's own and shares its kernels
in every mode, so all of them give bitwise identical embeddings. The old names `on` and `peer` are still read from saved
Loader settings, as GPU 0 and GPU 1. The Loader shows each card with its name.

| mode | where the encoder runs | VRAM cost | fresh-image latency (IQ3_S, 400 W, measured by the original project) |
| --- | --- | --- | --- |
| `off` | no image input | none | |
| `N` | sidecar `bin/basalt-vision` on card N, always loaded | about 2.2 GiB of the main card's expert cache, or about 1.8 GiB of a second card's | main card: TTFT 0.77 s at 1k image tokens, 1.4 s at 3k, 1.8 s at 4k; second card: 0.86 s, 2.1 s, 2.9 s |
| `dynamic` | inside the engine, on the main GPU | none at rest (0.87 GiB of pinned RAM) | 0.77 s, 1.45 s, 1.8 s |
| `cpu` | sidecar on the CPU | none | about 6 s at 1k, 40 s at 3k, 70 s at 4k |

`dynamic` borrows up to 1.55 GiB of expert-cache slots per image (the prompt path's lend), uploads the weights from the
pinned copy (about 30 ms), encodes on a side stream, and refills the same slots from RAM (about 60 ms), so the cache is
identical afterwards. Other `--parallel` slots pause for the encode. An image already seen is served from the
encoder cache in tens of milliseconds in every mode.

```json
{ "server": { "gpu": "0,1", "vision": "1" } }
```

A card number uses the sidecar, which the main build also produces (`tools/vision` also builds on its own); the
server looks for it next to `bin/basalt`. `dynamic` needs no sidecar.

The image encoder is Basalt's own, written for this model's vision tower (no llama.cpp or ggml): bf16 tensor-core
products and flash attention with fp32 accumulation on the GPU, AVX-512/AVX2 on the CPU. It replaces llama.cpp's
mtmd encoder as a drop-in, is faster than it at every image size, and stays closer to an fp32 reference: mtmd's fp16
attention drifts on large images, ours does not.

## The `.basalt` model file

A model is one file. It holds everything the engine, the server and the vision sidecar load: the weights, the
tokenizer and chat template, the MTP drafter, the expert profile, the 8-bit hyper-connection copies, the vision
projector, and the default sampling. It carries no engine flags: those come from `--config` or the command line.

- **Layout:** a 64 KiB header (magic `BASALTMF`, version, a table of up to 511 named sections, each with its offset,
  size and SHA-256), then the sections, each aligned to 2 MiB so it can be memory-mapped or read with `O_DIRECT`.
- **Sections:** `meta` (JSON: geometry, the source GGUF metadata, every tensor's type, shape and offset,
  sampling, provenance and license), `tokenizer/*`, `dense`, `experts` and `experts.layout` (the routed
  experts, laid out per layer), `gguf` (every non-expert source tensor), `mtp/*` (the drafter), `profile`, `hc8` and
  `vision/*`. The authoritative description is [include/basalt/core/model_format.hpp](include/basalt/core/model_format.hpp).
- **No requantization:** tensors are copied byte for byte from the source GGUF. The only exception is
  `--compat-bf16`, for GGUFs that store the small projections the engine reads as BF16 in another type; it converts
  them and records that in `meta`.

Build one from the source GGUF shards (give shard 1; the others are found beside it):

```sh
python3 tools/convert/convert.py --gguf MODEL-00001-of-0000N.gguf --mmproj MMPROJ.gguf --out MODEL.basalt \
    [--mtp-source DIR [--mtp-dense FILE]] [--draft-vocab FILE] [--profile FILE] [--hc8 FILE] [--compat-bf16] [--name NAME]
```

`python3 tools/convert/convert.py --help` describes every input.

## Models

Ready-to-run `.basalt` packs are on [Hugging Face](https://huggingface.co/jesdga/Qwen3.8-Flash-Next-Basalt). A pack
you convert yourself will be significantly slower than the official `.basalt` files.

Quality measured with `basalt-bench --quality`: 20 chunks of wikitext-2 (512 tokens each), scored through the decode
path against the Q8_0 reference. Lower KLD and higher top-1 agreement are closer to Q8_0. How precision is handled and how quality is measured:
[docs/precision.md](docs/precision.md).

| model | source | file | experts | PPL | KLD mean | same top-1 |
|---|---|---:|---:|---:|---:|---:|
| GSQ-RCO-Coder IQ1_M (256 experts) | [ISTA-DASLab](https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-Coder-GGUF) | 57.4 GiB | 23.4 GiB | | 1.03 | 69% |
| GSQ-RCO Q2_0 | [ISTA-DASLab](https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF) | 64.9 GiB | 31.6 GiB | 2.971 | 0.486 | 80.6% |
| GSQ-RCO IQ3_XXS | [ISTA-DASLab](https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF) | 74.4 GiB | 40.0 GiB | 2.535 | 0.277 | 86.8% |
| UD-Q2_K_XL | [Unsloth](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF) | 76.5 GiB | 42.9 GiB | 2.755 | 0.323 | 84.3% |
| UD-IQ3_XXS | [Unsloth](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF) | 79.4 GiB | 45.3 GiB | 2.564 | 0.244 | 87.0% |
| GSQ-RCO IQ3_S | [ISTA-DASLab](https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF) | 81.0 GiB | 46.8 GiB | 2.387 | 0.177 | 89.4% |
| UD-Q3_K_XL | [Unsloth](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF) | 86.8 GiB | 52.0 GiB | 2.457 | 0.160 | 89.3% |
| UD-Q4_K_XL | [Unsloth](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF) | 106.7 GiB | 71.7 GiB | 2.321 | 0.068 | 93.0% |
| Q8_0 | [Unsloth](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF) | 178.3 GiB | 119.5 GiB | 2.282 | reference | |

The IQ3_S, IQ3_XXS and Q2_0 packs carry an MTP layer fine-tuned on the model's own output (self-distillation), which
raises draft acceptance. Output is unchanged, because verify decides every token. Q4_K_XL and Q8_0 keep the original
MTP layer. `--mtp-dense FILE` embeds that tuned layer (the `mtp/dense` section's raw bytes, checked against the
source layout) and records it as self-distilled, with its SHA-256, in `meta.mtp`. The tuned layer ships with the
packs, inside the model file; the converter input is only needed to build a pack yourself.

The model files are derived from Qwen3.8-Flash-Next and are distributed under the
[Qwen Community License 1.0](https://huggingface.co/Qwen/Qwen3.8-Flash-Next/blob/main/LICENSE), not Basalt's MIT
license. The GSQ-RCO quants come from IST Austria's Deep Algorithms and Systems Lab; please cite the
[GSQ](https://arxiv.org/abs/2604.18556) and [RCO](https://arxiv.org/abs/2605.00649) papers if you use them.

## Community benchmark runs

Rows submitted to the original project (`docs/community/benchmarks.json` of `jesdga95/basalt`, copied on 2026-10-26, 25 runs), all taken with
`basalt-bench --community`, so they compare with each other: prompt reading speed on a 16k-token prompt (tokens/s) and decode speed (tokens/s),
each with the GPU power limit it ran at. Credit to the people who submitted them. The two rows at the end are **our own development
measurements** on this fork (Windows, RTX 5090, 600 W, 127.6 GiB RAM) and are **not the same method**: decode is a chat request with
temperature 0.6 and the drafter on, prompt reading is a 13.5k-token repeated text, and the model is the OrcaRouter Uncensored build of the
same architecture. Compare the rows of one source with each other, and these two with the others only as an order of magnitude.

| GPU | Power | RAM GiB | OS | Quant | Prompt 16k tok/s | Decode tok/s | Submitted by |
|---|---|---|---|---|---|---|---|
| 2x RTX 5090 32 GB | 500 W | 123 | Linux | NVFP4 | 7,740 | 461 | neuro |
| RTX 5060 Ti + RTX 5090 32 GB | 400 W | 121 | Linux | NVFP4 | 7,741 | 438 | jesdga95 |
| RTX 5090 32 GB | 575 W | 62 | Linux | IQ3_S | 5,677 | 376 | knoopx |
| RTX 5090 32 GB | 500 W | 62 | Linux | IQ3_S | 8,859 | 375 | drakiss79 |
| RTX 5090 32 GB | 500 W | 62 | Linux | IQ3_XXS | 8,784 | 564 | drakiss79 |
| RTX 5090 32 GB | 475 W | 247 | Linux | NVFP4 | 9,556 | 312 | giveen |
| RTX 5090 32 GB | 500 W | 62 | Linux | NVFP4 | 9,289 | 305 | drakiss79 |
| RTX 5090 32 GB | 500 W | 191 | Windows | NVFP4 | 2,431 | 199 | HenryPDT |
| RTX 5090 32 GB | 400 W | 121 | Linux | NVFP4 | 7,732 | 317 | jesdga95 |
| RTX 5090 Laptop GPU 24 GB | 175 W | 47 | WSL2 | IQ3_XXS | 3,892 | 243 | pyj99_fr |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | IQ3_S | 3,226 | 187 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | IQ3_S | 3,236 | 158 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | IQ3_XXS | 3,169 | 202 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | IQ3_XXS | 2,894 | 189 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | NVFP4 | 4,072 | 131 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | NVFP4 | 3,316 | 96 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | Q4_K_XL | 2,983 | 72 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | UD-Q4_K_XL | 2,866 | 104 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | UD-Q5_K_XL | 2,981 | 77 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | UD-Q6_K_XL | 2,674 | 66 | foolio |
| 2x RTX 5060 Ti 16 GB | 180 W | 60 | Linux | IQ3_S | 2,370 | 161 | KimDaeHwan99 |
| RTX 5060 Ti 16 GB | 180 W | 121 | Linux | IQ3_S | 2,427 | 137 | jesdga95 |
| RTX 5060 Ti 16 GB | 180 W | 121 | Linux | NVFP4 | 2,687 | 92 | jesdga95 |
| RTX 5080 16 GB | 360 W | 88 | WSL2 | IQ3_S | 3,180 | 138 | lindworm-hermes2026 |
| RTX 5080 16 GB | 360 W | 88 | WSL2 | Q4_K_XL | 2,835 | 78 | lindworm-hermes2026 |
| RTX 5090 32 GB | 600 W | 127.6 | Windows | IQ3_XXS (this fork, own method) | 7,319 | 179 (exact) / 195 (relaxed 0.3) | this fork |
| RTX 5090 32 GB | 600 W | 127.6 | Windows | IQ4_XS (this fork, own method) | 5,206 | 100 (exact) / 117 (relaxed 0.3) | this fork |


## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

New and modified code is licensed under the **Basalt Win+GUI Community and Commercial License** (see [LICENSE](LICENSE)), free of charge for individuals and for businesses with annual gross revenue under $10,000,000 USD (enterprise commercial license required at or above $10M). Pre-existing upstream code (Strata, upstream Basalt, ggml) remains under its original MIT license (see [THIRD_PARTY_LICENSES](THIRD_PARTY_LICENSES)).
