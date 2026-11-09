# Permission levels of the channels

The agent can be reached from three channels: the **Chat** page (Chat and Code tabs), the **voice** (the harness brain of the voice service,
push-to-talk, calls and the phone's voice gateway) and the **Telegram** bot. Each channel has one permission level, kept by the server in one file,
and every turn that comes from a channel runs at most at that channel's level. Code: `tadamoo/permission_level.py` (levels, channels, the store),
`serve/handler.py` (route, the rule at each request), `serve/agent_sessions.py` (`hold_level`, `hold_channel`), `tadamoo/telegram_bot.py`.

## The levels

From the least to the most the agent does without asking (`tadamoo/permissions.py`, `tadamoo/stack.py` `PERMISSION_MODES`):

| Level | Words on the page | What runs without a question |
|---|---|---|
| `plan` | Plan only | read-only tools; any other call is refused until the person approves the plan |
| `read_only` | Read only | read-only tools; every other call asks |
| `default` | Ask first | reads and reversible changes in the folder; commands and irreversible actions ask |
| `auto` | Automatic | everything |

At every level a deny rule refuses, and a text from outside that gave the AI orders stops every call with an effect until the person allows it.
Below Automatic a call an allow rule covers, or a listed check command, runs without a question; otherwise `phone_call` always asks, and the
Rule of Two asks about an outward call after untrusted content and private data. At Automatic only the Rule of Two still asks.

## Where the levels are kept

`permission_levels.json` in the user data folder holds `{channel: level}` for the channels the person set, replaced whole on each change. A channel
never set is at `default` (Ask first): the level a new conversation of each of the three gets when nothing was chosen.

| Channel | Set in | Read at |
|---|---|---|
| `chat` | Chat settings, "What the agent may do without asking"; the mode chip and Shift+Tab with no conversation open | every request of the page |
| `voice` | Voice tab and Settings > Voice | every request with the voice service's token |
| `telegram` | Settings > Voice > Telegram, "Permission level" | every Telegram message |

The page keeps the Chat's level in the browser (`tadamoo.chat.perm`) only to paint it before the server answers; the server's value wins. A level
this browser had chosen is handed to the server once, when the server has none for the Chat.

## The rule

- **A request never raises its level.** A new conversation of a channel starts at the channel's level, or at a lower one it names. A request
  that names a higher level is refused with `403` and `"code": "level_above_channel"`; nothing is made or changed. The same holds for a mode
  change of a conversation and for the variants of a comparison. The level itself changes only through `POST /api/permission-level`.
- **Lowering is free.** The Chat may lower one conversation for itself (the chip, `/mode`, Shift+Tab); it can never go above the Chat's level.
- **Each turn is held to its channel.** When a turn starts, the conversation's mode is set to the channel's level for a conversation of that
  channel (it follows the setting, up or down), and lowered to it for a conversation of another channel. So a Telegram message in a
  conversation moved there from the Chat runs at Telegram's level, a voice turn sent into a Chat conversation runs at the voice's level, and a
  Chat turn in a conversation started at a level the Chat no longer has runs at the Chat's level. The Chat never raises a conversation by itself.
- **A changed voice or Telegram level applies at once** to the open conversations of that channel (`hold_channel`); the Chat's conversations
  take a lowered level at their next turn.
- A conversation records its channel (`channel` of the record: `chat`, `voice`, `telegram`). Telegram's own conversation of a chat is the one
  `telegram.json` binds to it; any other conversation it talks to was moved there.

Text the agent reads from outside (a web page, a forwarded or quoted Telegram message, a file, a transcript) is still fenced as data
(`tadamoo/untrusted.py`), and the voice never lets a meeting's or a call's words reach the harness: analyses and the copilot use the direct brain.

## Route

`GET /api/permission-level` answers `{"levels": {...}, "set": {...}, "order": [...], "channels": [...]}`; `?channel=voice` answers
`{"channel", "level", "set"}`. `POST /api/permission-level {"channel": "telegram", "level": "read_only"}` sets one and answers the same object
as the GET plus `channel` and `level`. Refusals: `400` with `bad_channel`, `bad_level` or `bad_body`; `403` from another PC, from the listener
for phones, or from a page of another site (the origin guard, [server security](server-security.md)). The voice service's token does not open it,
nor the mode route of a conversation (`serve/voice_service.py` `SERVICE_ROUTES`). Each change writes one log line:
`[server] permission level of telegram: read_only`.

## Other settings files

- `voice.json` holds no level, and `/api/voice/brain` refuses a `permission` field. A `brain.permission` found in the file is taken once
  (`voice_settings.take_permission`) when the server has none for the voice, and removed from the file either way.
- `telegram.json` holds no level, and `/api/telegram/settings` refuses unknown fields. A `mode` found in the file is taken once when the
  server has none for Telegram (`Settings.take_mode`), and removed from the file either way; the allow-list, the home channel and the chats
  stay as they are.

## Outside these three channels

- **Phones** (the listener for phones, the Android app) keep their own limit in Settings > Phones (`serve/remote.py` `max_mode`).
- **The Agent tab's runs** (`POST /api/agent/runs`, Automatic unless asked otherwise), a trace replay (nothing runs) and the conversations the
  server starts by itself (plans, routines, the tab editor) are not a channel here: they run at the level they were given.
- A program on this PC that calls the agent routes without the voice token is taken as the Chat.

## Tests

`serve/tests/test_permission_level.py` (store, route, Chat and voice rules, page words), `serve/tests/test_telegram_api.py` and
`tests/test_telegram_bot.py` (Telegram's level, the handover from `telegram.json`, a moved conversation), `serve/tests/test_voice_brain.py`,
`voice/tests/test_brains_harness.py`.
