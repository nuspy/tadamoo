# Importing from callAPIcall

Settings > Voice > Import from callAPIcall brings the personas, avatars and cloned voices of a callAPIcall folder into TaDaMoo. The code is
`voice/importer.py` (it imports nothing of TaDaMoo; the stores are handed in) and `serve/voice_import.py` (the two routes, with TaDaMoo's
own stores). Nothing is read until the person names the folder and presses the button.

## Routes

Both are `POST` and only from this PC (`/api/voice/...`, with the server's secret like every change).

- `/api/voice/import/plan` `{source}` returns `{source, items, bytes, personal}` and writes nothing.
- `/api/voice/import/run` `{source, confirm: true, items: [{id, replace?, confirm_personal?}]}` does the chosen items and returns
  `{results: [{id, name, status, message?, warnings?, ...}], counts}`. Without `confirm: true` it is a `400`. At most 100 items.

`source` is an absolute folder, without `..`, that holds `avatar/persone` or a folder of cloned voices, and is not inside the TaDaMoo data
folder. The server never looks in a default place: the page suggests `C:\Projects\callAPIcall` in its field and reads nothing until the
button. Links inside the folder are not followed.

## What is read and what becomes of it

| In `<source>/avatar/persone/<Name>/` | In TaDaMoo |
|---|---|
| `manifest.json`: name, voice `{engine, voice}`, personality, system prompt | the personality (`personas/<slug>/persona.json`); a text longer than TaDaMoo keeps is cut and the item says so |
| `manifest.json`: `personality_from_docs` | the personality's short profile (1500 characters) |
| `manifest.json`: `knowledge_project` | not imported (no equivalent); noted on the item |
| `memory.json`: `rolling_profile` | one note for each paragraph in the personality's encrypted memory (long-term, origin `inferred`) |
| `memory.json`: `recent_sessions` | one short-term note each, dated; encrypted too |
| `docs/` | a knowledge archive "`<Name>` (callAPIcall)", linked to the personality |
| `rag/chunks.jsonl` | the same archive, only when no document could be read: the passages are grouped by source and written as text |
| `clips/*.mp4`, `poster.jpg` | `<user data>/voice/avatars/<slug>/`; the personality's `avatar` is the slug |
| `wav2lip/`, other folders, `rag/embeddings.npy` | left where they are (caches); the plan lists them and their size |
| `<source>/cloned_voices/` or `call-bridge/cloned_voices/` samples | `<models folder>/tts/voices/` |

The memory is written only through `tadamoo/personal_memory.py`, which encrypts it: no clear-text copy is made anywhere. The Telegram values
are not read here; they come from Hermes' `.env` in the Telegram section.

## Personal data and conflicts

- An item that holds memories, a video or photo, or a voice recording is marked `personal` with its reasons. The run does it only when
  that item has `confirm_personal: true` (a box on the page); otherwise its status is `needs_confirmation` and nothing is written.
- A persona whose slug already exists is a conflict: the plan says `exists` and the run skips it (`skipped`) unless the item says
  `replace: true`, which updates the fields and adds what is missing. A voice sample is never overwritten without `replace`.
- An item with a problem (no readable manifest, a field the store refuses) is listed with the reason and fails alone; the rest run.

## Idempotent

A second run with the same items skips what exists and changes nothing. A run with `replace` does not double anything: the memory notes
that say the same count as seen again, the archive is found by its name, a document already in it is not added again, and a clip of the
same size is not copied again. Nothing in the source is changed, and nothing is deleted anywhere.

## Tests

`tests/test_voice_importer.py` builds a callAPIcall folder in a temporary place (no real data is read) and checks the plan, the personal
confirmation, the encrypted memory (no clear text on disk), the archive, the media, the conflicts, the second run and the routes;
`serve/tests/test_voice_import_page.py` checks the page.

Not proven: a run on the real callAPIcall folder (the person starts it), PDF documents without `pypdf` or `PyMuPDF` (they are reported and the
passages are used instead), and the avatar files being used by an avatar (that comes with the avatar phase).
