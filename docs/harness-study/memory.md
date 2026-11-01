# Memory across projects and conversations: study

What the person asked (1 November): one memory for everything, over the Chat and over vibe-coding projects, not tied to the folder being worked in. If a conversation two weeks ago was
about events in the EU, the harness must be able to refer to it and search that conversation's full context.

## What exists

- **Project notes** (`harness/memory.py`): SQLite FTS5, one file per project folder (hash of the canonical path) in `<user data>/memory`. The model keeps notes with `remember`, reads with `recall`.
  A conversation with no folder uses `chat-workspace`. Notes of one project are invisible from another.
- **The person's own memories** (`harness/personal_memory.py`): encrypted, global, long and short term, learned by the loaded model from what the person writes. On by default (decided 1 November), editable in Settings > Memories, one file for every chat and project.
- **Conversations in the conversations mode** (`serve/agent_sessions.py`, `harness/session.py`): each has a folder with `history.json` (live messages), `archive.jsonl` (the originals that compaction
  or masking removed) and `nodes.jsonl` (branches). So the full text exists on disk, plain, and nothing reads it across conversations.
- **The model-only chat page** (`serve/web/chat.js`): one conversation in the browser's localStorage, key `basalt.chat`. The server never sees it, so no memory or search can reach it.

## The gaps against the request

1. No store spans projects: a note written in one project cannot come back in another.
2. No tool searches past conversations; the server has the text and no index.
3. Matching is by words: "UE", "EU" and "Unione Europea" are three different words, and a note in one language does not answer a question in the other.
4. The model-only chat is not kept on the server at all.
5. Nothing is captured by itself: a project note exists only if the model chose to call `remember`.

## What the other two do (public descriptions, not run here)

- **Claude-Mem**: hooks on the session events capture what the agent did, a model writes a short observation for each, stored in SQLite FTS5 plus a vector index; recall is hybrid. Strength:
  capture needs no decision by the model. Cost: a summary call per observation, a second process for the vectors (orphaned processes are reported), and the summaries leave the PC when the model is a cloud one.
- **Obsidian vault**: plain Markdown with links, readable and editable by the person, no format of ours. Weakness: nothing keeps it current, and an old note is trusted as much as a new one.

## Proposed shape (to decide)

One global index, `<user data>/memory/global.db`, with three kinds of row, all tagged with where they came from (project folder or none, conversation id, turn, date):

- **Notes** (today's project notes, moved here with their project tag; a note may be marked "all projects").
- **Conversation chunks**: every conversation's turns cut into pieces of about 500 tokens, indexed as the turn ends, from the same files the conversation already writes (history, archive, branches).
  No model is needed; it is CPU only and can run at any time.
- **Conversation cards**: when a conversation goes quiet, the technical session writes one card (title, what was decided, the names and subjects, and the same subjects in the other language and
  in their short forms: "EU", "UE", "Unione Europea"). The card is what answers a question that shares no word with the text. It is a use of the technical session, off the main slot.

Two tools for the model, both untrusted data like `recall`: `search_past(query, since, project)` returns cards and chunks with conversation, turn and date; `open_past(conversation, turn range)` reads the
full text around a hit from the source file, so what the model quotes is the original and not a summary. The date is always shown; a note older than a limit is marked old.

A vector index is a later step, once the cards have been measured; it needs an embedding model and a process, which is the provider work already open.

## Privacy and limits

- The conversation files are already plain on disk; the index is no more exposed and is not encrypted (decided 1 November). The personal memories stay separate and encrypted.
- Only text the conversation already holds is indexed, with the redaction the session store already applies. Nothing goes to a route that is not this PC unless the person allows it, as for the personal memories.
- Routine conversations are left out by default. A switch on each routine in the GUI turns its indexing on; its runs are then indexed, tagged with the routine's name, so the harness knows what it did (decided 1 November). The routine's own definition (name, schedule, what it is for) is not a conversation and is known either way.
- The model-only chat stays and is indexed (decided 1 November): it posts each finished turn to the server, and the page stops saying that the conversation stays in the browser.
- A conversation can be marked private (button in the chat header): it is left out of the index and nothing is learned from it. "Forget" removes what was already taken: its pieces in the index and the notes learned only from it (each note keeps the ids of the conversations it came from; a note another conversation also taught stays, without this one).
- Every side task that reads a conversation cuts it through `harness/transcript.py`: a long item keeps its start and end, a long list its first and newest items, and a marker says what was left out. The learning of personal memories keeps, for each reading, counts of what it saw (messages, cuts, omissions) and an outcome: `no_entry` (text read, nothing worth keeping), `rejected`, `stored` or `unreadable` (the reply was not an answer). The record holds no words of the person.
- The personal memory can be exported to Markdown (Settings > Memories): one file per note with a small front matter, plus an index, in `memory-export` in the user data folder. It is a copy; the encrypted store stays the master and nothing is read back.

## Measure before building

A test set of questions whose answer is in an old conversation, with and without cards; the rule is written first (as for E1 to E9). The card's cost is one technical-session call per conversation.
