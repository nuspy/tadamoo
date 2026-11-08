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
| `knowledge` | Ids of knowledge archives. Empty until archives exist. | 50 ids |
| `language` | Code such as `en` or `it`, or empty. | |
| `profile` | A short rolling summary of what it knows about the person. | 1500 |

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
Conversations do not write notes yet.

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

## Routes

All under `/api/personas`, from this PC only (403 otherwise).

| Route | |
|---|---|
| `GET /api/personas` | list with note counts |
| `GET /api/personas/<slug>?q=&kind=&section=` | the personality and its notes |
| `POST /api/personas` | create |
| `POST /api/personas/<slug>/update` | change the fields named |
| `POST /api/personas/<slug>/delete` | `{confirm: true}` |
| `POST /api/personas/<slug>/memory/<item\|confirm\|delete\|clear>` | the notes (`clear` needs `confirm: true`) |
| `POST /api/personas/<slug>/compose` | `{goal}` returns `{system, memory}` for a conversation without a harness |

## Code

`tadamoo/personas.py` (store, block, `PersonaUse`), `serve/personas_api.py`, `serve/web/personas.js` and `personas.css`.
Tests: `serve/tests/test_personas_api.py`, `serve/tests/test_personas_page.py`, `tests/test_persona_prompt.py`.
