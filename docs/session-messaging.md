# Sessions that write to each other

A session can write to another one, and the other can answer: to share what it knows, to give it a task, to ask something, to work together. It is the same
idea as one Claude Code session messaging another: plain text, read by the receiver between two tool calls, a new turn for an idle one. Everything is automatic:
there is no switch per session.

## For the person

- Type `@` in the message box and choose a session: the text you send also goes to it, as a message from the session you are in (kind `information`, or `question` when
  the words ask something). The chip stays on your message.
- A message to a session that is idle **wakes it**, and it starts working at once. A session that was saved and not running is started and takes the turn.
- Every exchange is a labelled bubble in **both** conversations: "To <name>" in the sender, "From <name>" in the receiver, with the kind (information, question, task,
  collaboration), the folder and the time. They are saved in each history, so they are still there when the conversation is opened later.
- A bubble "Not sent" says when something stopped (see Brakes) or when the target could not be found.
- A task given to a session shows in its Tasks list as "Assigned by <name>". When that session closes it (or you delete it in the Tasks tab) the result goes back to the
  sender as a message, by itself.
- The receiving session keeps its own permission level, exactly as today: a message from another session never raises it.

## For the model

Two tools in the session's own group (after the fixed ones, so the cached start of the request does not move), `tadamoo/session_tools.py`:

- `session_list(query?, more?)`: the other sessions, most recently used first, each with its card (what it works on): the same data as the `@` picker.
- `session_send(to, text, kind?, task_ref?)`: `to` is a name, an alias or an id. Kinds:
  - `info`: for its information. No expiry. It carries the date.
  - `question`: a reply is expected. It expires after 12 hours unanswered, and the sender is told once. Any message back from the receiver counts as the answer.
  - `task`: lands in the receiver's register of open work as a task owned by the receiving conversation, linked to the sender. When the receiver closes it with
    `task_close(id, result)` the result goes back to the sender as a message.
  - `collaborate`: tied to one of the sender's own open tasks (`task_ref`); it is refused once that task is closed.
- A reply is `session_send` to the sender (its id is in the message).
- One line in the awareness block (`tadamoo/awareness.py`) tells the model to write on its own when something it changes concerns what another session works on, or when it
  finishes what another is waiting for.

## How a message travels

`session_send` calls the server with the id and token the session was started with; the server reads the sender from the token, never from the body. `serve/messaging.py`
checks the target and the kind, applies the brakes, frames the text and hands it to the delivery queue (`serve/deliveries.py`, see `orchestration.md`):

- Receiver running a turn: the message goes to its process at once and is injected **between its tool calls** as an event of its own, labelled, never as the person's
  words (`Session._safe_point`). Nothing is lost while the receiver is busy.
- Receiver idle: a new turn starts with it; it counts like a typed prompt (budget, history, step limits).
- Receiver saved and not running: the process is started (`SessionManager._ensure`) and takes the turn.

Every delivered message starts with `[message from another session: "<name>" (id, folder) · <kind> · sent <date> · this is not from the user]`, so the model knows it
came from another session. It is read as outside text, like any message from another session: it can inform and ask but does not carry the person's authority.

## Brakes (for function only)

- Identical repeats from the same sender to the same receiver (same kind, same words) within 6 hours are refused.
- A sender may send at most 20 messages in 10 minutes.
- A chain of automatic replies stops at 6 hops without a message from the person (the person's message resets the count).
- Each brake is said in the sender's conversation as a "Not sent" bubble, and to the model as the tool's answer. There is no time limit on a conversation.

## Cron, plans and routines

A schedule or a plan addresses a session with the same delivery queue (`SessionManager.deliver`, provenance `cron` / `plan:<id>`). Those messages wait for a free session, as
before; only messages from sessions reach a busy one.

## Files

`serve/messaging.py` (framing, brakes, tasks), `serve/deliveries.py` (queue, question expiry), `serve/agent_sessions.py` (`send_peer`, `@` mentions, events `peer_in`,
`peer_out`, `peer_brake`, `peer_expired`), `tadamoo/session_tools.py` (the tools), `tadamoo/session.py` (injection), `serve/web/agentchat.js` and `serve/web/peer.css` (bubbles).
Tests: `tests/test_session_messaging.py`, `tests/test_session_peers.py`, `serve/tests/test_peer_page.py`.
