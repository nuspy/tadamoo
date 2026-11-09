# Personalities

A personality gives a conversation a name, a manner of speaking and a memory of its own. It is made in Settings > Personalities and chosen
from a menu in the header of Chat and Code.

## What a personality holds

Stored as `personas/<slug>/persona.json` in the user data folder (plain text, no secrets):

| Field | Meaning | Limit |
|---|---|---|
| `name` | Display name. Letters, digits, spaces, `. ' - _`; starts with a letter or digit. | 40 characters |
| `voice` | `{engine, voice}`: ids of the voice service. Not checked against anything. | 80 each |
| `avatar` | A reference string. Empty until avatars are connected. | 200 |
| `personality` | How it is and how it talks, free text. | 8000 |
| `system_prompt` | What it must always do, free text. | 8000 |
| `knowledge` | Ids of the knowledge archives it draws on, linked from the Knowledge tab (`docs/rag.md`). The page shows their names (from `GET /api/rag`) and the id of one the list does not know. | 50 ids |
| `language` | Code such as `en` or `it`, or empty. | |
| `profile` | A short summary of what it knows about the person, written by the person. | 1500 |
| `learn` | Whether it learns from its conversations (see below). On unless switched off. | on or off |

`slug` is the lower-case ASCII form of the name (`Maria Rossi` becomes `maria-rossi`) and is also the folder name. It does not change when
the personality is renamed. At most 100 personalities. Unknown fields, control characters, names that Windows reserves (`con`, `nul`, ...)
and any name that is not a plain slug are refused: no request can reach a path outside `personas/`. Files are written to a temporary file,
flushed and renamed over the old one.

## Memory

Each personality has its own store, `personas/<slug>/memories.bin`: the same format, sections and encryption as the person's own memories
(`tadamoo/personal_memory.py`, AES-256-GCM, key derived from the device key in the vault folder). Notes are long-term (identity, place,
people, tastes, ...) or short-term (facts, searches, topics, projects). Nothing is written in clear.

The page shows the notes and lets the person add, edit, confirm a guess, delete a note, or clear all of them (a second press is needed).
Memory settings (switches, weights, time windows, "allow cloud") are the defaults of the person's own memories; a personality's notes stay
on this PC unless a request is served by a provider other than this PC's model and the personality's `memory-settings.json` allows it.
Conversations write only the notes described under "Learning from conversations".

Deleting a personality asks for `confirm: true` and removes its folder, memory included.

## In a conversation

Recall is the person's own `Recall` class, with another data-block name (`persona-memory`) and closing line: only notes that match the
request go in, each once per conversation, as data and never as instructions.

**Agent conversations** (`/api/agent/sessions`, the Chat tab with tools and the Code tab). The choice is a `persona` field of the session
record: given when the session is created, changed with `POST /api/agent/sessions/<id>/persona`, kept when the conversation is reopened.
The server starts the harness with `--personas-dir` and `--persona <slug>` (and the key folder). The harness pins a `persona` block in the
system prompt (after "About yourself") built from `system_prompt`, `personality`, `language` and `profile`, and adds the matching notes to
the per-request memory message after the person's own. A change while the conversation runs swaps the recall and the pinned block in the builder, leaves the running system
message as it is (the prompt cache stays valid), and puts a `[harness]` note before the next message that carries the new block and says it
replaces any described earlier (or that there is none now), the way the pictures levels are told. A conversation that starts again takes the
new choice from the flags.

**Model-only chat** (`chat.js`, mode "Model only"). The page asks `POST /api/personas/<slug>/compose` with the text of the request and adds
the returned personality block and matching notes to the `system` message it sends to `/v1/chat/completions`. The choice lives in the browser
(`tadamoo.chat.persona`) until "New conversation". If the personality cannot be loaded the message is not sent and the reason is shown.

The text of a personality is the person's own and is trusted like their system prompt. It does not change tools, permissions or safety rules.

## Learning from conversations

When a conversation with a personality ends, one model call (`tadamoo/persona_learning.py`) writes a title and 3 to 5 sentences about it and
merges them into a rolling profile of the person of about 200 words (at most 1500 characters), the way the call bridge's memory does. Both are
kept in the personality's encrypted memory, marked `source: "conversation"` and shown with a "learned" tag on the page:

| What | Where | Kept |
|---|---|---|
| Summary of the conversation, dated | short-term `topics`, origin `inferred` | the newest 10; the oldest go |
| Rolling profile | long-term `card` (the person's own card is another note and stays) | one, rewritten each time |

The profile is pinned in the personality's block ("what you wrote about the person yourself", context, not instructions) when the long-term
memory is in use, the `card` section does not weigh `off`, and the request stays on this PC or `cloud_ok` is set. A profile the person
confirmed or edited is theirs: learning no longer rewrites it. The summaries come back through the usual recall, as data.

**When.** An agent conversation tells `serve/persona_learning_worker.py` at the end of each turn; it is over after `idle_s` (the settings
use `persona_learning`, 300 s) without another turn. The job waits in the personality's encrypted queue, so a restart finds it again. The
model-only chat hands its lines over with `POST /api/personas/<slug>/learn` when "New conversation" is pressed (the page's only addition).
A conversation that goes on is learned from again, from the lines after the last time, and still counts as one conversation.

**Which model.** The loaded model on this PC (`TadamooClient` on this server), never a cloud route: if the client reports a route off this PC
and the personality's memory settings say `cloud_ok: false` (the default), the job is dropped. The call goes to the technical session when
the text fits it, otherwise it waits for the card to be free (the same `gpu_free` rule as the person's own memories). It never loads a model.

**Who teaches it nothing.** A private conversation, a routine's or an unattended one, a conversation that now speaks as another
personality, and a personality with the switch "Learn from conversations" off (`learn` in `persona.json`) or with both learn switches of its
memory off. Marking a conversation private drops its waiting job; "forget" on a conversation removes the summary learned from it (the
profile is a blend and stays; "Forget what was learned" removes it).

**What the model reads.** Only the person's lines and the personality's answers. Taken out before the call: tool results, any message
flagged `untrusted` (the notes and archive passages put in with a request), every `<untrusted_data>` block (web pages, files, attachments,
forwarded Telegram messages, quoted text), the `<documents>` block of an attached file, `[harness]` notes and summaries of old turns; each
leaves `[text from outside removed]`. The prompt says facts about the person come only from the person's lines.

**What it may write.** Each sentence of the title, summary and profile is dropped if it looks like a secret or an order to an assistant
(`memory_extract.looks_unsafe`) or repeats a run of 6 words from the text that was taken out, so an instruction in a page does not become
a note even if the model repeats it. A summary with nothing left is refused whole and nothing is kept.

**The page.** In the memory block of a personality: the switch, "Learned from N conversations; last time ...", and "Forget what was
learned from conversations (N)" (two presses; `memory/forget-learned` with `confirm: true`), which removes the learned notes except
those confirmed or edited by the person, and starts the count again. The lines already learned stay recorded, so the same conversation is
not learned from twice.

## Routes

All under `/api/personas`, from this PC only (403 otherwise). callAPIcall's `/personas` paths, answered by the same store without the memory, are in [the callAPIcall routes](voice/api.md).

| Route | |
|---|---|
| `GET /api/personas` | list with note counts |
| `GET /api/personas/<slug>?q=&kind=&section=` | the personality and its notes |
| `POST /api/personas` | create |
| `POST /api/personas/<slug>/update` | change the fields named |
| `POST /api/personas/<slug>/delete` | `{confirm: true}` |
| `POST /api/personas/<slug>/memory/<item\|confirm\|delete\|clear\|forget-learned>` | the notes (`clear` and `forget-learned` need `confirm: true`) |
| `POST /api/personas/<slug>/learn` | `{conversation, messages: [{role, text}]}` from the model-only chat; `{queued}` |
| `POST /api/personas/<slug>/compose` | `{goal}` returns `{system, memory}` for a conversation without a harness |

## Code

`tadamoo/personas.py` (store, block, `PersonaUse`), `serve/personas_api.py`, `serve/web/personas.js` and `personas.css`; learning:
`tadamoo/persona_learning.py`, `serve/persona_learning_worker.py`, `PersonalMemory.mark_learned/trim_learned/forget_learned`.
Tests: `serve/tests/test_personas_api.py`, `serve/tests/test_personas_page.py` (with `personas_knowledge_dom_check.js`, the page on a stand-in DOM), `serve/tests/test_personas_learning.py`, `tests/test_persona_prompt.py`,
`tests/test_persona_learning.py`.
