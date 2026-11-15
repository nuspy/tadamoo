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

## The clients

- **Web** (`serve/web/actions.js`): the tab *Required actions* with the number on it, the list with buttons and a field for text, what caused a security authorization (opened when the entry
  carries orders found in a text). A new entry that arrives while the person is on another tab shows in a dialog, one at a time (channel `web`); what was waiting when the page opened is only
  counted. What a plan asks keeps its own window (`human.js`), so the dialog does not show it again. Test: `serve/tests/test_actions_page.py`.
- **Desktop app** (`tools/windows/desktop/MainForm.cs`): when the window is hidden in the tray or behind others the page sends `{type: "actionsNew"}` (channel `desktop`) and the tray icon shows one
  balloon; clicking it opens the window on the tab. In front, the page's dialog is used.
- **Android** (`mobile/lib/src/actions.dart`, `action_notifier.dart`): the tab *Actions* (the Jobs are its second part, so the bar keeps five places) with the number on its icon. While the app is open
  the list is looked for every 8 s and what it shows counts as announced. In the background only standard notifications tell, from one periodic look of WorkManager (about every 15 minutes, only
  with a network, no service stays running; no Firebase): one notification for each entry not announced yet (channel `android`), with up to three buttons (the answers of the entry; a written answer
  is an inline reply; more answers than fit means the tap opens the app). A button or a reply is sent to `POST /api/actions/<id>/answer` from the notification itself. Android 13 and later ask for
  the permission `POST_NOTIFICATIONS`. Test: `mobile/test/actions_test.dart`.

Tests: `tests/test_actions.py`, `serve/tests/test_api_actions.py`.
