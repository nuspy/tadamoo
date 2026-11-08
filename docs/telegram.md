# Telegram bot

A Telegram bot that talks to the agent of the Chat, with the same functions. It reads messages by long polling (`getUpdates`, no webhook), as the Hermes
gateway does, so it needs no public address. Code: `tadamoo/telegram.py` (API client, settings, token, helpers), `tadamoo/telegram_bot.py` (what an update
becomes, what the conversations say back), `serve/telegram_service.py` (the thread, the settings the page edits, the move of a conversation). Page:
Settings > Voice > Telegram (`serve/web/telegram.js`) and the button in the header of Chat and Code.

## Switching it on

1. Make a bot with BotFather, paste its token in Settings > Voice > Telegram and press Save. The token goes to the credential store under `telegram/bot`;
   the page is only told whether one is set. Test calls `getMe`.
2. Add the numeric Telegram user ids that may talk to the agent (the allow-list).
3. Switch on "Listen to Telegram". It is off until the person turns it on, so TaDaMoo never takes this bot's messages away from another program unasked. Once on,
   it starts with the server and listens whenever a token is kept.

"Import from Hermes" reads `%LOCALAPPDATA%\hermes\.env` on the PC (`TADAMOO_HERMES_ENV` names another file; elsewhere `~/.hermes/.env`) for
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS` (comma-separated ids; entries that are not ids are skipped and counted) and `TELEGRAM_HOME_CHANNEL`. The token goes
to the credential store, the ids join the allow-list, the home channel is set. The answer lists which values were found, never their content. The file is only read.

A bot has one reader at a time. While the Hermes Telegram gateway runs with the same token, Telegram answers 409 to one of the two. The status then reads "Another
program reads this bot", the poll waits 30 seconds and tries again, and nothing crashes. Switching Hermes' gateway off is the person's job: TaDaMoo never edits
Hermes' files. A second bot for TaDaMoo avoids the clash.

## Who is let in

The sender id of every update is checked against the allow-list before anything else is read. Anyone else gets no answer and reaches nothing. They are listed in
"Senders that were ignored" with their id, user name and the first line of one message (printable characters, 60 at most; nothing else is kept) and one log line
a minute at most. "Authorise" adds the id to the allow-list. A message older than an hour is not acted on. The first time the bot starts it skips what was waiting
on Telegram's side; later starts continue from the offset kept in `telegram/telegram_offset.json`.

## What a message becomes

- A chat has its own conversation of the agent, made on its first message at the permission level set here (the same four levels as the Chat). `/new` starts a new one.
- The person's own text is their prompt. A forwarded message, a message they reply to and every file are data: forwarded and quoted text is fenced like text from
  the web (`tadamoo/untrusted.py`), files go through the attachments store, and a message with links says that what is found at them is data.
- Photos and documents are saved as attachments (20 MB at most, the Bot API limit). Voice notes, video and the like are not read yet: the bot says so.
- Commands: `/start`, `/help`, `/new`, `/stop` (cancels the running turn), `/where` (which conversation this chat talks to, and the level). The bot answers in
  Italian to a person whose Telegram language is Italian.

## What comes back

- The answer builds up in one message that is edited as the text grows, at most once every 1.5 seconds, with the typing sign while the agent works. A text over 4096
  characters (counted in UTF-16 units, as Telegram does) goes on in a new message, cut at a line or a space.
- A 429 sets a wait for that chat (`retry_after`) and the message is sent when it ends; a 403 or 400 drops that message and goes on.
- A permission question arrives as a message with Yes and No. Only an allowed person's press counts, in the chat the question was asked in. A phone call, a payment,
  an action that cannot be undone and orders found in outside text show the summary and need a press on Confirm as well. A code, a card of questions or any
  other kind of question is left to the TaDaMoo page, with a note in the chat. The buttons never offer "always allow".

## Move to Telegram

The button in the header of Chat and Code (shown only when the bot runs and a chat is known: the allowed people's own chats, a chat they wrote from, the home
channel) records `telegram_chat` on the open conversation (`POST /api/agent/sessions/<id>/telegram`, `{"chat": id}` or `{"chat": null}`). The chat is told first, so
a chat Telegram refuses is not recorded. From then on the conversation's answers go to that chat, what the person writes there goes to this conversation instead of
the chat's own, and what they write on the page is echoed there. A chat holds one moved conversation. "Bring back here" ends it; so does `/new`. The conversation
list shows a "Telegram" badge on a moved conversation. Nothing the conversation said before the move is sent.

## Routes

All are for this PC only (loopback) and need the key when one is set: `GET /api/telegram`, `POST /api/telegram/settings|token|allow|pairing|import|test|send-test`.
The token is in no answer, no log line and no error text (a refused call's description has it replaced by `***`).

## Not proven without a real bot

The tests use a stand-in of the Bot API on 127.0.0.1 and never reach Telegram. Not proven: real Telegram limits and error texts, a real 409 against Hermes'
gateway, group chats and topics, the real format of a forwarded message, a download of a real file, the arrival of a voice note.
