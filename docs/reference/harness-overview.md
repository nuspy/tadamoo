> Reference: [README](../../README.md).

## The agent harness

The **harness** (`harness/`) is an agent that works on your files with the model loaded in TaDaMoo. It reads and edits a project,
runs commands, searches the web, fills in forms, keeps notes about what it learns, and asks you before anything it cannot undo.
It is a separate Python process: it talks to the running server through the public OpenAI-compatible API and imports nothing from
the engine, so it needs no GPU of its own. What it keeps (conversations, memory, audit log, checkpoints, approvals) lives in the user
data folder, never in your repository; the only thing it writes in the project is `.tadamoo/artifacts`, the full text of outputs it
had to shorten.

The reference is [docs/harness.md](../harness.md); a step-by-step guide is [docs/harness-manual.md](../harness-manual.md).

**Billion Context** (Chat settings, on by default) is a local add-on proxy that compresses long chat and agent conversations before they reach the model, with a silent fallback to direct calls; see [docs/billion-context.md](../billion-context.md).

An optional **security monitor** (Monitor > Security) watches the network connections and disk writes of the model processes and flags suspicious cases without blocking anything; see [docs/security-monitor.md](../security-monitor.md). The same page starts, stops and shows the Openweight Canary exfiltration test (add-on).

An optional **reverse engineering** switch in the Agentic coding tab adds the shipped REA toolkit (MCP server and skill) to the agent and lists the programs it can use, each installed only on a click; Windows support is experimental; see [docs/rea.md](../rea.md).

### Four ways in

All four build the agent in the same place (`tadamoo/stack.py`), so what one can reach the others can reach.

| Way in | What it is |
|---|---|
| **Chat** tab | Conversations with the agent. Each is a long-lived process with its own saved history, so a message does not pay for the start. This is the main way. |
| **Agent** tab | The conversation page, working in folders you add: each folder has its own conversations and a `.basalt` registry, and the agent's edits can be kept or undone block by block. |
| `python -m tadamoo -p "task"` or `tadamoo-agent` | The command line, for scripts: exit codes, `--json`, budgets. `tadamoo-agent.cmd` works from any folder. |
| `python -m tadamoo acp` | The agent of an editor that speaks the Agent Client Protocol, over stdio. |

### What it can do

- **Work on a project**: read, list, search, edit and write files; run shell commands; `repo_map` (the definitions of each source file,
  most relevant first) and `repo_diagram` (import and folder diagrams worked out from the files, with no model); language-server diagnostics and go-to-definition when a server is installed; run Python, JavaScript or
  TypeScript snippets; hand a task to a subagent. Files you attach (text, code, CSV, JSON, Word, Excel, PowerPoint, PDF, images; audio
  and video through the callAPIcall service) are read in pieces, and files the agent makes are checked before they are offered to you.
- **Use the web**: `search_web` (DuckDuckGo unless you set another engine; no key, and the words of each search go to DuckDuckGo) and
  `browse_url` (a page as lines of text, private addresses refused). With the browser tools it drives Edge or Chrome for forms,
  bookings and purchases.
- **Keep what it learns**: a memory of notes per project (below), `AGENTS.md` rules read from the folder up, skills, and reference
  documents offered as a fixed block or searched.
- **Reach other tools**: skills, plugins and MCP servers found on this PC (usable at once unless you turn them off; a read-only tool of an MCP
  server needs no question); a search for a missing one online, with the best candidates put to you (queued with a comparison when nobody is there, and installed only when you authorise one) and an install that never runs a
  package's scripts; remote MCP servers with OAuth sign-in; other agents through an ACP client and an A2A client; a council of
  advisers (as many as you add, each with its own route) that debate a hard question for a set number of rounds, with the cost shown first, from a box in the conversation or on the model's own initiative (`docs/council.md`); a different model for each function (the loaded one, another on this PC, a cloud service).
- **Handle money and identity**: the **Wallet** vault keeps cards, personal data and logins encrypted; the model writes placeholders and
  the value is put in at the last step, only toward the sites the entry names. Payments follow a limit you set (nothing until you set
  one), an optional waiver with caps, and a question above them. The second factor of each login is your choice. Real phone calls ask
  every time and are off by default.
- **Plan**: plans as graphs (actions, sensors, decisions, merges, tasks handed to you) shown in the **Plans** view; the Chat can read
  and propose changes to them, read what the planner's proposals would do and approve or reject them; a change has your yes below the Automatic level, and at Automatic (and for events, with a setting on by default) only the step that involves other people, a booking or a resource you do not control waits for you. Every
  plan has a card the incoming events are judged against; the Chat asks you only what the card lacks. A decision with no exit that holds
  passes to you instead of skipping in silence. The event filter (local; the cloud one only with your consent) runs from the start of the
  server, and re-planning by the local model is optional.
- **Run on its own schedule**: routines (a prompt, a folder, a schedule; always Automatic), evaluation (`python -m tadamoo.eval`, pass^k, cost
  and cache use), replay of a recorded session, and the energy a task used.

### The Chat's conversations

- **A column of conversations** at the left of the Chat: open an earlier one, delete it (a second press confirms), or star it so that it
  is listed apart, above the others; the arrow at its top closes it to a thin strip. The top row holds *New conversation*, *Branches* and
  the mode label at the left, and the kind of conversation, *Thinking* and *Settings* at the right.
- **Mode, with a label**: *Plan only*, *Read only*, *Ask first* or *Automatic*. A two-line label in the top row says which is on
  (*Shift + TAB to change mode:* in grey, the mode under it in bold and in its own colour, as Claude Code does); **Shift+Tab** in the
  message box goes to the next, with a conversation open it changes that one only, and *Automatic* asks nothing.
- **Thinking level** per conversation (the *Thinking* list, or `/effort`).
- **Slash commands**: typing `/` opens a menu of 31: on the page (`/clear`, `/mode`, `/plan`, `/copy`, `/export`, `/branch`, `/rewind`,
  `/stop`, `/diff`…), answered by the conversation's process (`/status`, `/context`, `/compact`, `/tools`, `/hooks`, `/mcp`,
  `/memory`, `/permissions`, `/audit`), requests worded for the model (`/init`, `/review`, `/security-review`, `/research`), and every skill found
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
  skill moves to another PC as one `.pskill` file and arrives ready to run. See the [manual](../harness-manual.md#17-personal-skills).
- **A message while it works** is steered into the task, queued, or treated as urgent; **Stop** means stop.
- **Branches, going back, comparing**: a tree of conversations, *Back to here* on any request (actions that stay done are listed
  first), and two continuations of one request side by side. Messages are shared immutable nodes, so a branch copies nothing; the
  node view shows what switching to a branch costs the model before you switch, and *Merge and summarize* has the model write one
  synthesis of two branches into a new conversation.
- **A copy of its own** (not offered from the Chat page, which has no project folder to copy): in a git folder a conversation can work in its own worktree, and you apply the changes you choose as staged
  changes, never a commit.
- **Failures are handled, not hidden**: a busy or unreachable server is retried; a process that dies is started again from its history;
  a tool that cannot be undone is never run again by a restart; after that the conversation is *blocked* and waits for you.

### What you control

- **What it may do without asking**: the four modes; command rules (`shell_command:pytest*`, a deny always wins, and a rule never
  covers a command that chains or redirects); the *Always allow* button in a question. An answer covers that one call (the same tool with
  the same arguments), not the whole tool, and *Always allow* is never offered as a wide rule for `python`, `powershell`, `curl` and the
  like (only for that exact command) or for `run_code`: a rule for those is one you write yourself. In *Automatic*, an irreversible action still asks
  once the conversation has read outside content and can reach a secret or a vault entry (the Rule of Two). The web is read freely for news; a web address, a search or a form asks only when the conversation has read outside content, touched private data (a stored credential, a file that holds keys, an attachment) and the action reaches the outside. Settings > Wallet sets how strict that is. A stored secret is never let into an address or a query, at any setting.
- **Hooks you write** (`hooks.json`): commands or fixed blocks on tool calls, turns and the session; a hook that cannot decide blocks.
- **Checkpoints and rewind**: before an action changes files, their previous content is kept outside the project (only the changed files, any folder, no size limit; off, on, or on without ever stopping the action);
  `python -m tadamoo.rewind`, `/rewind` and *Back to here* on a message restore it. Rewinding does not undo a command it ran, and says so.
- **An audit log**: one record per tool call (arguments hashed and masked, the decision, the outcome), in a hash chain you can verify.
- **Time limits and Stop**: every tool has a time limit (one hour unless set in `limits.json`); silent work is not killed unless you set a limit; Stop ends a running command and everything it started.
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
  no folder, so all its conversations work in `chat-workspace` inside the user data folder and share one memory; the Agentic coding tab, the
  command line and the editor use the folder you name, each with its own.
- **Keyword search only.** There is no stemming and no embeddings: "built" does not find a note that says "build". Because the search
  falls back to any word, a note that shares one word with your message can come back although it is not about it.
- **Notes are stored as written, in plain text, not encrypted and not masked.** Do not ask the agent to remember a password or a key.
- **A wrong or hostile note stays.** The agent can be talked into keeping a note by a page it read, and `remember` is a reversible
  action that asks nobody in *Ask first* or *Automatic*. It comes back later as data the model is told not to obey, which lowers the
  risk and does not remove it. The notes added at the end by the retrieval set the Rule of Two flag like `recall`.
- **No page to browse or edit the notes.** `/memory` reads them (each cut at 300 characters); to remove one, ask the agent to forget
  it, or delete the file. Nothing is shared between folders, users or PCs, and there is no export.

### Engines, providers, orchestration, plans, cron, web search and speed

- **Engines.** The top of the Loader has one card per engine: **Basalt-Plus** (Blackwell), **Strata** (other GPUs, run as a
  managed child), **llama.cpp** (a managed `llama-server`, with its own update step that downloads only after you confirm
  the plan), **LM Studio** and **vLLM** (servers already running on this PC). One engine is active at a time, and the others'
  tabs show only what the hardware can run.
- **Providers for each function.** Models > Providers lets each internal function use its own model: Chat, the agent's
  executor, the planner, context compaction, the technical session, evaluation, the judges that check answers and route
  messages, and each role of the council (set in the Council section of the same page). A function can use the loaded model, LM Studio, vLLM, llama-server, a cloud
  service (OpenAI, Claude, Gemini, Z.ai, DeepSeek, Kimi, Qwen, Nebius, NVIDIA, Grok and others), any OpenAI-compatible
  server, or the Claude and Codex command-line tools. A call that leaves the PC needs your consent and a master switch.
- **Conversations that work together.** A conversation can be named, message another one (`session_send`), read its status
  or start a background one (`session_spawn`: its own rules, a parent, a depth limit, a cap on live children). The
  server holds the messages, keeps a list of those that never arrived, and marks everything another conversation sends as
  data unless its sender may give orders. A conversation can have a day's token budget; a digest of what it said and took in
  lets an unattended run be read without its whole history. Orchestration tools are offered only with `--orchestration` or
  when found through `find_capability`.
- **Cron jobs.** A schedule (a cron expression or one moment) can start a conversation or deliver its message to an existing
  one or to a plan. Each fire has a run id written before it starts, a time to live that skips late fires and a dedup key, and
  a claimed fire does not run again after a restart. The Cron Jobs tab lists the latest results, runs a job now, and shows
  the messages that never arrived, to send again or dismiss.
- **Plans with more kinds of step.** Besides actions, sensors, decisions and merges: waits (a duration, a moment, a condition
  written as an expression or in words), variables, a loop, a question put to you, a result and a log. A step says what it
  produces, gets the values of the steps above it, and can repeat until its stop condition holds. Waits, variables, result
  and log steps are resolved by the plan itself, not by the model. A wait written in words is read by the technical session
  only when the values it watches have changed, with a pause that grows after each "no" and stops at an hour; without the
  session the plan says so. The plan clock makes one comparison per plan and opens only the plans that are due; a paused plan
  stands still. A plan that ended well can become a personal skill. The Plans page has a palette of step kinds (drag with a
  mouse or pen, or tap), and the plan's log and results read as a conversation to which you can reply.
- **Editing in words.** The Plans and Cron Jobs tabs take an instruction (and make a new job when none is chosen); it goes
  to one editing conversation of the harness, and a change below the Automatic level needs your yes. The tabs blink, with a
  dot and a spoken note, when something new came out since you last opened them. The reload commands are `/reload-mcp`,
  `/reload-skills` and `/reload-plugins`. Skills are installed (from the list that comes with the program, a folder, a GitHub address or a web page), turned on or off and
  removed in Settings > Skills or with `/install-skill`. The work the agent has open (long jobs with their subtasks, kept in the project folder, deleted when done) is in the Tasks tab and `/tasks`. The folder that holds the conversations can be moved from Settings (a verified
  copy that keeps the original, refused while a conversation runs).
- **Web search and browsing.** Settings > Web search chooses the search provider (DuckDuckGo with no key by default, SearXNG,
  Brave, Tavily, Exa, open-websearch) and the page reader (Firecrawl, Playwright, others), local or cloud, with keys in the
  credential store. The local programs install without Docker or administrator rights, start when a search needs them and
  stop when idle. A chosen provider that is not ready is reported to the model instead of being replaced silently. A page
  reaches the model as lines of text with its menus taken out; the browser checks every request of a page, not only the first
  address, and can keep a screenshot as a file of the conversation.
- **Telegram and memory.** A Telegram bot talks to the agent of the Chat with the same functions (long polling, a list of
  allowed users, no public address; [docs/telegram.md](../telegram.md)). Settings > Memories has a switch to learn and one to
  use for each memory, a weight for each section and the notes to read, correct or confirm; a conversation can be private, and
  forgotten together with the notes it taught, and the personal memory exports to Markdown.
- **Speed and economy.** In the engine: the draft head of speculative decoding overlaps with the work around it, a Q8_0 head
  can be screened in two stages that keep the exact top candidates, the commit Windows charges for GPU memory is given back
  by default, and two opt-in switches (`--route-prior`, `--route-tail-skip`) trade a little exactness for speed and are
  named in the start warning. In the harness: a short technical session beside the conversations takes the quick jobs, the
  conversation cache keeps only conversations worth parking (`--conversation-cache-min-tokens`), a cut summary is retried
  and then replaced by notes made by code, code stays whole in agent sessions and old code in the Chat reaches the model as
  a plain description, and a long answer is written in checked parts. Speed figures are the maintainer's own runs, with the GPU
  power limit stated; none is quoted here.

### What is not done

File and network isolation for commands (the job object limits resources only); a try of the ACP agent in a real editor (Zed, JetBrains) and
`terminal/*`; TaDaMoo as an A2A server (it is a client only); bank accounts through an aggregator; a test of Google Pay or an embedded Stripe form on a real site;
and a measure of which context strategy works best (it needs the real model). No real phone call was placed in a test, and Stripe and
Google Pay were not tried against the live services. The list with the reasons is at the end of [docs/harness.md](../harness.md).
