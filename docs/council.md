# The council

A council is a debate between advisers with roles, chaired by a moderator, that gives the model a verdict on a hard question before it answers
(`tadamoo/council.py`). It works in the Chat and in the Agent, in the page and in the Android app. The avatar and voice path has no tools and has no council.

## The debate

A round is every enabled adviser speaking in turn, each having read what was said before, then the moderator summing up in at most 250 words (agreement,
disagreement, recommendation) and ending with `CONSENSUS: YES` or `CONSENSUS: NO`. The debate stops when the moderator says yes, or at the last round,
where the moderator gives a recommendation anyway; a missing or unclear line counts as no. The speakers run one after the other, never together: each
answers the others, and a local model has one GPU. An adviser that fails is reported and the others go on; with fewer than two answers in a round the debate
ends. What the advisers wrote is untrusted text for the others, the moderator and the model: nothing in it is an instruction.

## The roles

Models > Providers > Council is the one place where the council is edited. It holds:

| Setting | Meaning |
|---|---|
| Advisers | Any number (up to 200), each with an `enabled` box. The enabled ones form the council, in the order shown. At least two are needed. |
| Role | An id that never changes (a slug of the first name), a name, a job text, an optional personality and a route. |
| Personality | The slug of a personality (Settings > Personalities). While it exists its system prompt and personality text become the job, so a colleague such as the critic is defined once. When it is gone the stored job is used. |
| Route | A provider of the registry, or empty for the model loaded in TaDaMoo. |
| Moderator | One role with the same fields, no `enabled` box. |
| Rounds | 1 to 6, default 3. |
| Cap on model calls | 0 for none (default), else 3 or more. |
| The model may convene it (`council_auto`) | `off`, `ask` (default) or `auto`. |

A fresh install has a proposer, a critic and an engineer, all enabled, and a moderator, all on the loaded model.

## Routes and what leaves the PC

A role is the function `council:<role id>` of the provider registry (the moderator is `council:moderator`). Its route is the assignment of that function and
its consent is the consent for that function, so each role has its own yes for personal data going to a provider outside this PC, its provider's daily cap
and the same fallback rule as any function (only to a route that is as local or more). Nothing is saved if one route is not allowed; taking a role's yes
back puts it back on the loaded model; removing a role removes its route and its yes. The Providers table of functions does not list the roles.

## Cost and the cap

The cost of a debate is worked out before it runs (calls, tokens read and written, calls outside this PC) and is shown in the Council section and in the
description of the tool. When a debate would pass a cap that is not zero, it loses rounds first, then advisers (never below two, the first ones in the
list stay), and the result says what was taken away. The cap never cuts in silence. Early consensus costs less than the figure shown.

## Using it

- **The box.** *Council* in the header of a conversation (Chat with tools, Question only, and the Agent) and in the options of the Android app. It is kept
  on the conversation record on the PC and stays on until it is turned off; a new conversation starts with it off. While it is on, the harness debates
  each real question at the start of the turn (not a command, not the answer to a card, not an acknowledgement of a word or two), before the model's first
  step. The debate is the tool `ask_council` called for the person: it passes the same gate, shows as a tool block that opens on the debate, is kept in the
  conversation as untrusted data, and the model then answers with the verdict read. The model-only chat has no harness and has no box.
- **`/council <question>`** debates one question once, whatever the box says. The question, without the command, is the message. Written on its own it
  explains itself.
- **By words.** The model is told, in its self block and in the tool's description, to call `ask_council` itself when the person doubts an answer or asks for
  a second opinion, advisers, a council, a board or a review from several views, in any language and any phrasing. There is no phrase to learn.
- **By itself.** When its own answer does not satisfy it, or the question is hard or has high stakes: with `ask` it first asks the person with `ask_user`,
  naming the cost; with `auto` it calls directly; with `off` it never does, and calls only when the person asks in words or the box is on. The tool stays
  in the list in every case, so the prompt prefix does not change.

Stop ends a debate between two speakers. Roles, routes, rounds, cap and `council_auto` are read again at each debate; the description of the tool and the
self block are the ones of the start of the conversation.

## Settings from the old council

The old council had three fixed advisers and a chair (functions `council_1` to `council_3` and `council_chair`). The first time the settings are read their
roles, routes and consents are moved to the new shape (ids made from the names, all enabled); nothing of the old shape is kept or read afterwards.

## Measuring it

Whether a debate helps has not been measured: `python -m tadamoo.eval --set council` runs six decisions about code (five with a flaw a reviewer finds, one
sound). Run it without `--council` and with it, and compare; a difference under 3 points is noise until it repeats.
