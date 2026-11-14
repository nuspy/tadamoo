# The message box: "/" commands and "@" mentions

The Chat and the Agentic coding tab share one message box (`#prompt`), so what is here works the same in both. Both lists are keyboard and touch friendly: items are at least 44 px
high, the box is a `combobox` and each list a `listbox` (`aria-activedescendant` follows the chosen item), and nothing needs a mouse.

## "/" commands (`serve/web/slash.js`)

Typing `/` at the start of the message shows every command that can be run with it (the page's own, the ones the conversation answers, and the skills found on this PC); letters
typed right after it filter the list. **Up** and **Down** move through the list (it scrolls, and wraps), **Tab**, **Right** (with the caret at the end) and **Enter** choose: the
command is written in the box, and a command that takes no argument runs at once on Enter. **Escape** closes it. A message that starts with a space is sent as it is.
`/help` lists the commands. The newer ones: `/install-skill` (`docs/skills-manager.md`), `/tasks` (`docs/task-register.md`).

## "@" mentions (`serve/web/mention.js`)

Typing `@` at the start of the message or after a space opens the list of the **other** conversations (this one is left out): ten at a time, the most recently used first, with the
folder (or "Chat") and what the conversation is about (its card). Letters typed after the `@` filter the list by the name and the card (capitals and accents do not matter). The list
scrolls; at the last of the ten, Down (or scrolling to the end) brings the next ten. The same keys choose: Tab, Enter or Right. Escape closes. When nothing matches, Enter sends the
message as it is.

A chosen conversation becomes the text `@name` in the message and a **chip** above the box (with a cross that takes it out). The chip is the stable reference: it holds the
conversation's id. When the message is sent, the ids of the chips whose `@name` is still in the text go with it as `mentions`; the user event comes back with `mentions: [{id, name}]`
and the message shows them under its text. **Nothing is delivered to the mentioned conversation yet**: without the feature that routes it, a mention is simply kept in the message.

## HTTP

```
GET  /api/agent/sessions/mentions?exclude=<id>&q=<letters>&offset=0&limit=10
     -> {sessions: [{id, name, summary, updated, folder, state}], offset, next, total}     newest use first; closed conversations are left out
POST /api/agent/sessions/<id>/send   {text, mentions?: [ids]}     each id once, not the conversation itself, none that is gone
```

Tests: `tests/test_mentions_api.py`; `serve/tests/test_mention_page.py` (a real browser, real key events, in the Chat and in Agentic coding at 360x640, 390x844 and 1280x800);
`serve/tests/test_slash_commands.py`.
