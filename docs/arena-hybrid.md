# Hybrid arena: design

Not built. The hybrid arena sends one prompt to the local model and to a cloud model and puts the answers side by side with time to first token, tokens per second,
tokens used, cost and a quality verdict. It differs from everything else in Basalt in one way: the prompt text goes to a third party. That needs a decision of the
person, so this page says what the feature would need, not how it is done.

## Decisions that belong to the person

1. **What may be sent.** Two safe levels exist. (a) Only prompts shipped in the repository for benchmarking (public tasks, no personal data): no consent is needed
   per run, only the choice of provider. (b) Any text the person types in the arena: each run shows the exact text, the provider and the estimated cost, and
   sends nothing until they confirm. A prompt built from a conversation, an attachment, the memories or the vault is never offered to the arena.
2. **Which providers.** Only providers already added on the Providers page (keys stay in the credential manager). The arena is a new function there, `arena`, with
   `data: personal` so that the existing rule applies: a provider outside this PC needs the person's yes for that function.
3. **Who judges quality.** Options: a check that runs code (tests), a rubric the person writes, or a judge model. A cloud judge sends both answers out as well, so
   it is a second yes; the default is a local judge or tests.

## What it would need

- A route that runs the two sides one after the other (one GPU; the local side first) and streams both results; the cloud call reuses `harness.llm.client_for`,
  its daily token cap and its fallback rules (never to a less local route).
- A per-provider price entered by the person, because Basalt does not know current prices; without it the arena shows tokens, not money.
- A results page in Models next to the converter's benchmark: per run the prompt hash (not the text, unless the person keeps it), both answers, timings, tokens,
  and the verdict. Stored locally; nothing is reported anywhere.
- Tests with a fake cloud server (no network): both sides run in order, nothing leaves without confirmation, a refused or failing provider leaves the local result.

## Cost

About a day of work for level (a) with tests and a page; level (b) adds the confirmation step and the exact-text preview. The measurement itself (a real model
against a real cloud service) needs the PC and a key, and is not something an agent can run alone.
