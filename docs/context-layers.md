# The layers that keep a conversation inside the context

Six things shorten what the model reads. They run in this order as the context fills; each one is told what the ones before it did.

1. **Limits on tool output** (`harness/context.py`, TokenJuice, JSON and HTML trimming): the output is cut when it is made.
2. **Masking of old results** (`harness/context.py`): from 70% to 80% of the budget, an old tool result is replaced by a note that names the file holding its full text.
3. **Compaction of older steps** (`harness/compaction.py`): near 85% of the budget, everything between the head and the newest steps becomes one record. The summary patterns of `harness/context_patterns.py` (summary, sliding window) leave the same record.
4. **The Chat's policy for a full context** (`serve/chatctx.py`): at 80% the middle turns of a chat become one record, or the oldest turns are dropped, or sending stops.
5. **Old long replies as a short record** (Chat only, `serve/chatctx.py` and `serve/web/chat.js`): see below.
6. **Billion Context**, the local proxy ([billion-context](billion-context.md)): by default it only keeps large tool outputs aside and cuts in an emergency; it does not fold old turns itself unless the setting is switched on.

The summaries of 3, 4 and 5 are written in the technical session ([compaction](compaction.md#the-technical-session)); a text larger than the session is read in parts, each in a call of its own that is checked and, if it is not good, made again (`harness/digest.py`).

## One mark for every compaction

Whatever wrote it, a compacted record carries the same mark (`harness/compacted.py`), as a field on the message (`compacted`, never sent to the model) and as a first line in its text, so it is found again after a restart, in a page's record or in a message that went through another program:

    [Compacted record: generation 2; 14 messages, about 31200 tokens, written here in about 900; originals: <path>]

The line does not say which part of the program wrote the record: it says that older text was compacted, how much, and where the exact text is. Under it, the notes are data and never instructions, and numbers, paths and code are to be checked against the originals.

The originals are the messages the record replaces, saved whole (a masked result comes back with its full text) next to the saved results; in the Chat they are the conversation the person sees. The record names where they are.

## Compacting again

Every layer may compact again what an earlier one compacted, with these limits:

- **From the originals.** When the originals can be read, the new record is written from them and not from the earlier notes, so a summary is never a summary of a summary and nothing decays with the number of rounds. The generation still counts up.
- **Without originals** (the file is gone, or the message came from elsewhere), an earlier record is folded in as notes, and only while it is worth it: a record that kept more than half of its source, or has been folded three times, or is under 300 tokens, is *final* and stays as it is. Notes left out of a compaction by this rule are kept in place.
- **Never** the recent zone, the first message, pinned messages or the person's latest request.
- Every compaction still has to free enough to be worth the call (`MIN_GAIN`) and runs the identifier audit.

A record made by dropping turns (the sliding window) is final: there are no notes to compact.

## Old long replies as a short record (Chat)

After a long reply (4000 characters or more by default; Settings > Technical session) ends, the technical session writes an abstract of it: what it answered, decided or asked, with names and figures exactly, without the explanations, the step-by-step instructions and the code. The conversation on screen and in the saved copy keeps the reply. For the model, once more than the newest *N* turns (6 by default) have followed a reply, the abstract is sent in its place, under the same mark without the rules line. Not done when there is no technical session (the call would take the conversation's own slot), for a reply the abstract does not shorten by half, for a reply that was edited or continued after the abstract was written, or in the Agent, which needs its code. Settings: Chat settings > *Old answers as a short record*.

## What the summaries are told to keep

The same rules in every summary prompt (`compacted.KEEP_RULES`): compress only what the next step no longer needs; keep word for word the paths with line numbers, signatures, exact error text, values and every decision with its reason; leave out logs once the result is taken from them, repeated reads, dead ends (keeping what they taught) and the way to a conclusion; when room is short keep the goal, then the decisions, what was made, the conclusions, the lessons.

## Where the layers can disagree

- **Switching one off in the middle.** The record of the conversation is Basalt's; the proxy only changes what goes out. With its folding off (the default) there is nothing to undo. If it is turned on and then off, the next request is the conversation as Basalt holds it, and a compaction is due at once if it no longer fits.
- **The proxy restarts or dies.** The request goes direct with the conversation as Basalt holds it; the same rule.
- **The technical session is off.** Summaries are written in an ordinary slot and a text larger than the window loses its middle, as before `harness/digest.py`.
- **Not measured yet.** Which summary is better (Basalt's, the proxy's, a mix), what *N* and the 4000 characters should be, and how much of the cache each layer costs are open questions for experiment E9 ([experiments](experiments.md)).
