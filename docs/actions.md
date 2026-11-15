# Required actions: one list of what is missing from the person

Questions and tasks that wait for a person come from several places. `serve/actions.py` reads them all, when asked, into one list that the web page, the desktop app and the Android app show.
Nothing here keeps a copy of a question: an item that is answered anywhere (the conversation's own page, the plan's page, another device) is no longer in the list.

## Sources

| Source | What | Answered by |
|---|---|---|
| `session` | a confirmation of a conversation (`ask`, including security authorizations with their `facts`, `tool`, `arguments`), a question left for later (`pending`), a card of one question | `SessionManager.answer(sid, ask_id, value)` |
| `plan` | what a plan asks during a run: approval (yes/no), a value (text, number, choice), a task (done, with a note) | event `plan:answer` |
| `proposal` | a change of a plan that waits to be approved or refused | events `plan:approve` / `plan:reject` |
| `routine` | a request a scheduled routine left for the person (listed once: not when its conversation lists the same question) | `dismiss` (it removes it from the list) |
| `approval` | a call of an unattended run on the server's board, waiting for the person's yes | `ApprovalBoard.decide` |

A card of several questions is `kind: "card"` with `open: {session}`: it is answered in its conversation.

## API

`GET /api/actions?channel=web|desktop|android` -> `{actions: [...], count, new}`. An entry:
`{id: "<source>:<reference>", source, title, title_en?, context (the conversation's or the plan's name), text, kind (confirm | choice | text | task | card), options: [{value, label, label_en}],
free (it takes written text), free_hint (text | number | code), free_prefix?, created_at, notified, details (facts, tool, arguments, explain, instructions, changes...), open?, proposed?}`.
Oldest first, open entries only. `/api/attention` carries `actions: {count}`.

`POST /api/actions/<id>/answer` `{value, text}`: `value` is one of the options, `text` what the person writes when the entry takes it (a task's note, a value, a code). It is sent to the source.
The first answer wins: an item that is not waiting any more gives `409 {error: {code: "gone"}}`; a wrong answer `400 {code: "bad_answer"}`.

## Announced once

`notified` is one flag for each channel. A client that shows a notification, a bubble or a dialog for an entry says so with `POST /api/actions/notified` `{ids, channel}`; from then on that entry
is `notified: true` on that channel and is not announced again. There are no reminders. Marks are kept in `actions_notified.json` and forgotten when the entry is not open any more.

Tests: `tests/test_actions.py`, `serve/tests/test_api_actions.py`.
