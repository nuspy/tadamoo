# Language check

A model made for one language corrects the grammar, spelling, punctuation and word order of a text in the Chat. Code: `serve/langcheck.py`, the routes under
`/api/language-check`, `serve/web/langcheck.js`.

## The list

Settings > Inference > *Language check*: for each language, a provider of the Providers page and, if the provider serves several models, the model name. The list is
`language_check.json` in the user data folder. The provider must run on this PC or its network (a llama.cpp, LM Studio or vLLM server, or any local OpenAI-compatible
one): the text of a conversation goes to it, so a cloud provider cannot be chosen. A language with no entry is not checked.

## In the Chat

Under every finished answer a **Check grammar** button opens a small panel: the language (the ones in the list), **Check**, and the corrected text to copy. The answer
itself is not changed. When the request was a translation into a language that is in the list (`Translate into Hungarian`, `traduci in ungherese`, or the Long
answer line `[long answer: mode=translate; language=…]`), the panel opens by itself on the new answer and offers the check. It does not open for answers read again from
an old conversation. The switch *After a translation… offer to check it* in the settings turns the offer off.

## What the model sees

The text is cut into parts of about 1,800 characters. Code blocks, inline code, maths, links, file paths, images and front matter are put away before the call and put
back after it (`serve/longform.py`, `protect`), so the model never sees them. The harness, not a model, checks each answer: not empty, not repeating itself, about as long
as the part (it corrects, it does not rewrite), every marker back. A part that fails is asked once more with what was wrong, then kept as written; the panel says how many.
