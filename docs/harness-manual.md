# The agent harness: user manual

*English* · [Italiano](harness-manual.it.md)

This is the guide to **using** the agent. [docs/harness.md](harness.md) is the reference: every option, route and rule, and what is not
done. The engine needs no manual and has none here.

- [1. What it is](#1-what-it-is)
- [2. Before you start](#2-before-you-start)
- [3. Your first conversation](#3-your-first-conversation)
- [4. Choosing what it may do](#4-choosing-what-it-may-do)
- [5. Commands that start with /](#5-commands-that-start-with-)
- [6. While it works](#6-while-it-works)
- [7. Reviewing and undoing](#7-reviewing-and-undoing)
- [8. Memory](#8-memory)
- [9. Rules, context and reference documents](#9-rules-context-and-reference-documents)
- [10. Skills, plugins and MCP servers](#10-skills-plugins-and-mcp-servers)
- [11. The web, the browser, the Wallet and the phone](#11-the-web-the-browser-the-wallet-and-the-phone)
- [12. Plans](#12-plans)
- [13. The Agent tab](#13-the-agent-tab)
- [14. The command line and the editor](#14-the-command-line-and-the-editor)
- [15. Hooks](#15-hooks)
- [16. Routines, evaluation and replay](#16-routines-evaluation-and-replay)
- [17. When something goes wrong](#17-when-something-goes-wrong)
- [18. What it does not do](#18-what-it-does-not-do)

## 1. What it is

The harness is an agent: you tell it what you want, and it reads files, changes them, runs commands, searches the web and tells you
what it did. It uses the model that is loaded in Basalt, so everything stays on your PC unless you choose a cloud provider for a
function. It is a separate Python process that talks to the server through its public API.

Four ways to use it, all with the same abilities:

| Way in | Use it when |
|---|---|
| **Chat** tab, *With tools* | you want a conversation: ask, correct, go on tomorrow. The main way. |
| **Agent** tab | you want one job done in a folder you name, with a log and a way back. |
| `python -m harness` / `basalt-agent` | you want it in a script, with exit codes and JSON. |
| `python -m harness acp` | you want it as the agent of an editor that speaks the Agent Client Protocol. |

## 2. Before you start

1. **A model must be loaded.** If none is, a notice under the top bar says so and opens *Models*; load one there.
2. **Python and the packages** are those of the repository's `.venv` (see the README, *Requirements*). The app starts the server for
   you; the agent's processes are started by the server.
3. **Where things are kept.** In the user data folder (`%APPDATA%\BasaltPannello\`): conversations, memory (`memory\`), the audit log,
   checkpoints, approvals, your `hooks.json`. Never in your project. The only thing the agent writes in a project is `.basalt\artifacts`.
4. **Keep the API key private.** If the server has an `--api-key`, the pages and the agent use it; never open the server beyond
   127.0.0.1 without one.

## 3. Your first conversation

1. Open the **Chat** tab. Under *Mode* choose **With tools**. The three kinds are:
   - *With tools*: the agent reads and changes files in its folder, searches, runs commands and keeps your plans. The conversation is saved.
   - *Question only*: it answers without any tool. Saved too.
   - *Model only*: a plain chat with the loaded model, kept in this browser; no tools, no folder.
2. Look at the label beside **New conversation**: it says what the agent may do without asking (*Ask first* at the start). Section 4.
3. Write in the box and press **Enter** (**Shift+Enter** adds a line). The agent works in the background and the page shows, in order,
   what it wrote, what it thought and each tool it used, with the result. **Stop** ends the turn.
4. When it needs you, it asks: a command to run, a plan to approve, a choice. The answer is a button; you can also write instead.
5. Come back later: the conversation list keeps every conversation. A conversation idle for 30 minutes lets go of its process and wakes
   from its saved history when you write again.

**Which folder does it work in?** The Chat tab sends no folder, so a Chat conversation works in `chat-workspace` inside the user data
folder. Its file tools stay inside that folder. To work on a project of yours, use the **Agent** tab or the command line (sections 13
and 14), or attach the files to the conversation: drag them onto the box or use the attach button. Text, code, CSV, JSON, Word, Excel,
PowerPoint, PDF, images, audio and video are accepted; a long file shows its start and the agent reads the rest in pieces.

**Files it makes.** When the agent produces a file for you, the page checks it (JSON parses, CSV rows line up, Office files open, an
image opens) and only then gives it a *Save* button.

**Thinking level.** The *Thinking* list sets how long the model reasons for this conversation. `/effort` does the same, and the list
follows it.

## 4. Choosing what it may do

Four modes, from the most careful to the freest:

| Mode | What the agent may do |
|---|---|
| **Plan only** | Read. It cannot change or run anything; it ends by presenting a plan, which you approve or not. |
| **Read only** | Read. It asks before anything else. |
| **Ask first** | Edit files in its folder; it asks before it runs a command or does anything it cannot undo. The start. |
| **Automatic** | Runs commands without asking. Use it only in a folder you can lose. |

**Change it while you work.** Press **Shift+Tab** in the message box: the mode goes to the next one (Ask first, Plan only, Read only,
Automatic, and round) and the label shows it (*Shift + TAB to change mode: actual …*). Pressing the label does the same. With a
conversation open it changes that conversation only, from its next action; with none open it changes the mode the next one starts
with. Going to **Automatic** shows a highlighted risk label and asks twice (*I understand the risk*, then *I read the previous message
carefully and understand the risk*); if you refuse, the cycle passes over it.

**Rules for commands.** In *Chat → Settings → What the agent may do without asking* (for new conversations) or under *More options* in
the Agent tab, write one rule per line, such as `shell_command:python -m pytest*`. A matching command runs without asking. A **deny**
rule always wins. A rule never covers a command that chains or redirects another (`pytest && rm -rf ~` is not `pytest`). In a question,
the **Always allow …** button adds a rule for you.

**The Rule of Two.** Even in *Automatic*, an irreversible action asks again once the conversation has read content from outside (a
web page, an MCP tool, a phone call, a recalled note) *and* can reach a secret or a vault entry. This is checked at every action.

**What the modes are not.** They are not an operating-system sandbox. A command runs with your rights on your files and network. The
agent's commands have a scrubbed environment, no keyboard input, a time limit, capped output and, on Windows, a job object (memory
and process caps; everything it started ends with it). If a folder matters to you, use *Ask first*, or keep a copy.

## 5. Commands that start with /

Type `/` at the start of a message and a menu opens. **Up/Down** choose, **Tab** completes, **Enter** runs a command that needs no
argument, **Escape** closes. A message that starts with a space is sent as written; an unknown command stays in the box with a note.

| Command | What it does |
|---|---|
| `/help` (`/?`) | lists the commands |
| `/clear` (`/new`, `/reset`) | a new empty conversation |
| `/effort [none\|low\|medium\|high\|auto]` | how long the model thinks; also changes the *Thinking* list |
| `/mode [plan\|read\|ask\|auto]` | the mode (Automatic asks its two confirmations) |
| `/plan [what]` | plan mode, then your request |
| `/permissions` (`/allowed`) | the mode, the rules and the refusals; also opens the settings |
| `/settings` (`/config`) | the Chat settings |
| `/model` | says which model is loaded (it is changed in *Models*) |
| `/status` | mode, thinking, turns, context use, tools, notes, MCP servers |
| `/context` | how full the context is |
| `/compact` | turns the older half of the conversation into a summary; only when nothing is under way |
| `/tools`, `/hooks`, `/mcp` | what the conversation can call, which hooks are active, which MCP servers started |
| `/memory [words]` | the last ten notes, or a search |
| `/copy [n]` | copies the last answer (or the nth from the end) |
| `/export` | saves the conversation as a text file |
| `/rename name` | renames it |
| `/branch` (`/fork`), `/branches` | copies the conversation at a point; shows the tree |
| `/resume` (`/sessions`) | opens an earlier conversation |
| `/rewind` (`/undo`, `/checkpoint`) | points at *Back to here* on a request |
| `/stop` | stops the turn |
| `/diff` (`/changes`) | the changes in the conversation's copy of the folder |
| `/plans` (`/tasks`), `/wallet` | open those views |
| `/init` | asks the agent to write an `AGENTS.md` for the folder |
| `/review`, `/security-review` | asks it to review the changes in the folder |
| `/research question` (`/search`) | asks it to read the sources themselves and say which pages it could not open |
| `/name text` | every skill found on this PC (needs approval first) |

Left out on purpose: the commands of Claude Code's account, terminal or cloud (`/login`, `/usage`, `/theme`, `/vim`, `/ide`…).

## 6. While it works

You do not have to wait. A message sent while the agent works is looked at before its next model call, and goes one of three ways:

- **Steer** (the default): it changes or adds to the task, so it goes into the task and the agent goes on with the new instruction.
- **Queue**: a different request that can wait ("when you are done", "later"). It starts after the task. Two buttons let you *Do it
  now* or *Remove* it.
- **Urgent** ("urgent", "asap", "before anything else"): the task is put aside where it stands, the urgent request is done in a
  conversation of its own, and the first task goes on afterwards.

**Stop means stop**: it ends the task, a command in progress and everything it started, and drops what waited.

**If it fails.** A busy or unreachable server is retried (after 2, 5, 15, 30, 60 seconds). A process that dies is started again from
its history, up to three times in ten minutes. A tool that cannot be undone is never run again by a restart: the conversation tells
you it may already have run. After that, or when the server refuses for good, the conversation is **blocked** and waits for you:
*Try again*, close it, or just write.

## 7. Reviewing and undoing

- **Changes.** The *Monitor* lists every edit the agent made, block by block, with *Keep* and *Undo* (press twice). A block is undone
  only while the file is exactly what the agent wrote, so your own work is never overwritten. Edits made by a shell command are listed
  too.
- **Back to here.** Each request of yours has this button (press twice). The conversation returns to just after that request and a
  copy of what came later is kept. The files are not touched: undo them from the Changes panel. Actions that stay done (a command, a
  call, a payment, a browser action) are listed first, and you must press again to go back anyway.
- **Checkpoints.** Before the first change of each turn the folder is saved outside the project. `python -m harness.rewind
  --workspace D:\proj` lists them and `--to ID` restores one; the Agent tab has *Go back*. A rewind takes a safety snapshot first, so it
  can be undone. It does not undo a command the agent ran, and says so.
- **Branches.** `/branch` copies a conversation at a request; *Branches* shows the tree. *Run both and compare* sends one request down two
  continuations, one after the other, and shows the two answers and the files each changed, side by side.
- **A copy of its own.** In a git folder, a conversation can work in its own worktree (*Work on a copy of the folder*). What it changes
  waits there; you look at each file's diff and apply the ones you choose, as **staged** changes, never a commit. Throwing the copy away
  removes it and its branch.
- **The audit log** has one record per tool call: the tool, a hash of the arguments, a masked preview, the decision, the outcome and the
  time. It is tamper-evident (a hash chain), not tamper-proof.

## 8. Memory

The agent can keep short notes about a project and read them in later conversations.

- **Tell it what to keep.** It writes a note only when it decides to, so ask: *"remember that the build uses --arch=native"*. It keeps
  a note of at most 2,000 characters with a category; the same text is kept once; there is room for 5,000 notes per project.
- **What comes back by itself.** At each message you write, up to four notes that match your words are added after your message, as
  data. The setting is *Injection → Retrieval at the end* in the Chat settings (the default); with the other two choices nothing is
  added and the agent calls `recall` when it wants to.
- **See and search.** `/memory` shows the last ten notes (each cut at 300 characters), `/memory words` searches, `/status` counts them.
- **Remove one.** Ask the agent to forget it (it uses the note's id), or, with no conversation open, delete the file `memory\<16 hex digits>.db` in the user data
  folder (and the `-wal` and `-shm` files beside it, if any) to forget the whole project.
- **Which notes belong together.** One file per folder path (case ignored). All **Chat** conversations work in the same folder, so they
  share one memory; each folder of the Agent tab or the command line has its own; a conversation working in a git copy of its own has
  another one again. There is no export and nothing is shared between PCs.
- **What it is not.** The search is by words, not by meaning: "built" does not find "build", and a note that shares one word with your
  message can come back although it is not about it. Notes are kept as written, in plain text, unencrypted; do not ask it to remember
  a password or a key. A note it was talked into keeping by a page it read will come back, as data it is told not to obey; read
  `/memory` now and then.
- **Permissions.** In *Ask first* and *Automatic* it writes and deletes notes without asking; in *Read only* it asks before writing;
  in *Plan only* it can only recall.
- **Off.** `--no-memory` on the command line removes the tools and the retrieval.

## 9. Rules, context and reference documents

- **`AGENTS.md`** (else `CLAUDE.md`, else `GEMINI.md`), searched from the folder up to the repository root, is part of every prompt.
  `/init` asks the agent to write one.
- **Long conversations.** Old tool results are replaced by a short placeholder only when the context nears its budget, oldest first,
  with the full text saved and named; errors are never cut. `/context` shows the use; `/compact` summarises the older half on demand.
  The Chat settings choose the policy for a full context: *Stop at the limit*, *Sliding window* or *Compact*.
- **Context patterns.** The Chat settings have six switches (injection, rewriting, pruning, summary, branches, versioning), each with a
  description of what it does, its advantages, drawbacks and what it does to the server's cache of the start. Leave them as they are
  until you have a reason.
- **Reference documents.** On the command line, `--reference DIR` offers documents as a fixed block (fast after the first turn, but it
  fills the context) or searched (small context, passages arrive as untrusted data); `auto` picks by size.
- **Cache.** The start of the prompt is kept byte-identical from turn to turn so the server reuses its cache. That is why notes and
  plan summaries come after your message, never in the system block.

## 10. Skills, plugins and MCP servers

- **Where they come from.** Basalt reads, and never runs, the skills, plugins and MCP servers installed for other tools on this PC
  (`~/.claude/skills`, the project's `.claude/skills`, plugins, `.mcp.json` and the like).
- **Nothing is active until you approve it.** In *Settings → Capabilities* open an entry, read its text (a skill) or its command (a
  server) and approve. The approval is tied to what you read; if the file changes, it is *to approve* again.
- **Using them.** The agent searches (`find_capability`), loads an approved skill (`load_capability`) or calls an approved server's tool
  (`call_capability`, which asks like any action that cannot be undone). From the Chat you can call a skill with `/name text`.
- **Missing something?** The agent can look online for an MCP server or a skill (`find_online_capability`), show you the best five
  with a score, their warnings and the command that would run, and install the one you choose. Nothing runs at install; the score
  measures popularity and upkeep, **not safety**. The settings are in *Settings → Capabilities → Search online*. Turning on "install the
  best one by itself" shows a risk label and asks twice.
- **Remote MCP servers** that need a sign-in show a **Sign in** button (OAuth); the token stays in the Windows credential manager.

## 11. The web, the browser, the Wallet and the phone

- **Search and read.** `search_web` uses DuckDuckGo unless you set another engine, with no key; the words you search go to DuckDuckGo.
  `browse_url` reads a page as text and refuses addresses that are not on the internet (this PC, your network) unless you allowed them in
  the browser settings. Everything it reads is marked untrusted.
- **The browser.** With the browser tools the agent drives Edge or Chrome to fill in forms, book and buy. Whether conversations have a
  browser, whether it shows a window, whether it may open pages of this PC and whether it keeps sign-ins is set in the **Wallet** view;
  each risky choice has a highlighted label and two questions.
- **Secrets.** The agent writes `{{secret:NAME}}` where a credential is needed and never sees it; the value is filled in at the last
  moment and masked in what comes back. **The vault** (Wallet) holds cards, personal data and logins encrypted; the agent writes
  `{{vault:ENTRY.FIELD}}`, and the value goes only toward the sites the entry names.
- **The second factor** of each login is your choice: none, a code you type each time (the conversation stops and asks), or the TOTP
  secret so Basalt makes the code itself. The last lets anyone who can use Basalt on this PC sign in; you are told once.
- **Payments.** The limit is yours to set, to any amount; until you set it, every payment asks you. A **waiver** lets the agent pay by
  itself up to a cap per payment and a cumulative cap until it expires, after a dictated declaration and two dialogs with your name
  typed again; above the caps it asks again. A payment can be stopped from the Wallet at any moment, and then the card's fields are no
  longer released. Whatever a merchant's form then charges is bounded by the card's own limit, not by Basalt.
- **Phone calls** are off by default and, once on, ask you **every time**, even in *Automatic*. The call opens by saying an AI
  assistant is calling on your behalf. No real call has been placed in a test.
- **What this does not protect from.** The agent runs as your own user. A program running as you can reach more than the vault can
  hide; the commands the agent runs cannot name the vault, the payment files or the browser's sign-ins, but that is a fence for a
  model led astray, not a wall. For a real separation, run the agent as another Windows user.

## 12. Plans

The **Plans** view shows plans as graphs: actions, sensors (they wait for an event), decisions, merges, and tasks handed to you. A step
can be moved, retried (with a limit and a wait) and rewound; rewinding over actions that stay done lists them first.

- In the Chat the agent can read plans and, **with your yes**, create and change them. The yes is either a quotation of your own words
  in your last message, or a question made from the real change (the real title, steps and diff) that you answer with a button. A plan
  must be read before it is changed, and a plan that moved on meanwhile is refused until it is read again.
- `ask_user` ends a turn with a question and buttons; what you press or write is your next message.
- **Events from the world** can be fed to plans (`POST /api/plans/events`) or read from RSS feeds, a mailbox (headers only) or a Telegram
  bot. The event pump and the cloud filter are **off** until you turn them on in *Settings*; the cloud filter also needs your consent and
  a key, replaces names, and does not send health data unless you tick it.
- Nothing in a plan changes behind your back: the planner only prepares proposals, which you approve.

## 13. The Agent tab

For one job in a folder you name. Write the **folder** (the full path) and **what it should do**, choose the mode (as in section 4),
and press *Start*. Under *More options* are the most steps and the rules for commands. The page shows the run's state and a readable log,
questions appear at the top of the right column (*Approve* or *Deny*; one nobody answers in 15 minutes counts as a refusal), and the
Monitor shows the edits. *Stop* ends the agent and everything it started. *Go back* lists the folder's checkpoints. One run at a time.

## 14. The command line and the editor

```powershell
python -m harness -p "fix the failing test in calc.py" --workspace D:\work\proj --json
basalt-agent -p "fix the failing test" --workspace D:\work\proj      # basalt-agent.cmd: from any folder
basalt-agent acp --workspace D:\work\proj                             # for an editor that speaks ACP over stdio
```

Common options (all of them are in [harness.md](harness.md#running-the-agent)):

| Option | Meaning |
|---|---|
| `--permission-mode plan\|read_only\|default\|auto` | as the modes in section 4 |
| `--allow PATTERN`, `--deny PATTERN` | command rules; a deny wins |
| `--interactive` or `--ask-server` | ask on the terminal, or on the server's pages, before risky actions, payments and plans |
| `--max-turns`, `--timeout-seconds`, `--max-tokens-budget` | stop after this much |
| `--tool-timeout [NAME=]SECONDS` | per-tool time limit; 0 for none |
| `--secret-env NAME[=host,host]` | lets the agent write `{{secret:NAME}}` |
| `--capabilities`, `--lsp`, `--browser`, `--phone`, `--wiki`, `--council` | switch on those tool groups |
| `--hooks FILE`, `--workspace-hooks` | your hooks |
| `--no-memory`, `--no-checkpoints` | switch those off |

A run with nobody to ask refuses what needs a question (add `--interactive`). It cannot type a second-factor code, so a login that
needs one fails there. **Exit codes**: 0 finished, 1 error, 2 bad usage, 3 turn limit, 4 budget, 5 stuck in a loop, 6 ended on a
question for you, 7 put aside before it was done, 130 cancelled. With `--json` the result has the stop reason, the changes, token and
cache counts, the number of refused calls and the path of the audit log.

In an editor, the agent follows ACP version 1: messages, thoughts, tool calls and edits as diffs, and permission requests that the
editor puts to you. Plan mode asks the editor to approve the plan, with a button for each mode it can go on in.

## 15. Hooks

Hooks run on events and can block a tool call. Put them in `hooks.json` in the user data folder (or the file given with `--hooks`);
`.basalt\hooks.json` of a project is read only with `--workspace-hooks`, because a folder under work should not name commands by itself.

```json
{"hooks": [
  {"event": "pre_tool", "tool": ["write_file", "edit_file"], "match": {"path": "*.env"}, "action": "block", "reason": "env files are off limits"},
  {"event": "pre_tool", "tool": "shell_command", "command": "python check_command.py", "timeout": 10},
  {"event": "turn_end", "command": "python notify.py"}
]}
```

Events: `pre_tool`, `post_tool`, `turn_end`, `session_start`, `session_end`. A `pre_tool` command lets the call pass with exit code 0
and blocks with exit code 2 (what it printed is the reason). A hook that cannot decide (another exit code, a timeout, not allowed to
run) blocks too. A file with a mistake stops the agent from starting, with the reason. `/hooks` shows what is active.

## 16. Routines, evaluation and replay

- **Routines** run a prompt on a schedule (`every 30m`, `daily 08:30`, `weekdays 08:30`, `mon,wed,fri 18:15`) in a folder and a mode.
  A routine is created **disabled** and read-only; only one runs at a time and only while no conversation is busy. They are managed
  through `/api/routines`; there is no page for them yet.
- **Evaluation**: `python -m harness.eval --set core --k 5 --gpu-power-limit-w 450 --out report.md` runs each task k times on a fresh
  folder and reports pass@k, pass^k, cost and cache use. These are development comparisons, not headline numbers.
- **Replay**: `python -m harness.replay trace.jsonl --mode auto` runs a recorded session again with the recorded model answers and
  reports the first place where the tool calls differ. It tests the harness, not the model.

## 17. When something goes wrong

| What you see | Why | What to do |
|---|---|---|
| A notice that no model is loaded | the server has nothing loaded | *Models*, choose one, *Load* |
| The conversation is *blocked* | the server refused for good, or the process kept failing | read the note, then *Try again* or close it |
| "Nobody can confirm in this run" | a headless run met something that needs a question | add `--interactive`, or an `--allow` rule |
| A command was refused although a rule allows it | the command chains or redirects another | one plain command per call |
| A tool was stopped after 15 minutes | the default time limit | `--tool-timeout NAME=SECONDS` |
| The search says it needs a check | DuckDuckGo limits automatic searches | wait, set another engine, or `BASALT_WEB_SEARCH=off` |
| A slash command stays in the box | the name is unknown | `/help`; start with a space to send it as text |
| "The skill … is not approved yet" | the skill is *to approve* | *Settings → Capabilities* |
| A page was refused | it is on this PC or your network | allow it in the browser settings, if you mean to |
| Every payment asks you | the limit is 0 until you set it | the Wallet view |
| `/compact` says to wait | the agent is working | wait for the turn to end, or Stop |
| It forgot what you told it | it did not keep a note, or the folder differs | see section 8 |
| A restart interrupted an irreversible tool | it may already have run | check the result yourself, then go on |

## 18. What it does not do

- It does not isolate files and network for commands: *Automatic* belongs in a folder you can lose.
- Basalt is an A2A **client** only; it does not receive tasks from other agents. ACP has no `fs/*` and no `session/load`.
- It does not read bank accounts through an aggregator, and Stripe and Google Pay checkouts were not tried on live services.
- Audio and video need the callAPIcall service; without it the file is kept and the agent says so.
- Nothing here measures which context strategy is best; that needs the real model.

The full list, with the reasons, is at the end of [harness.md](harness.md).
