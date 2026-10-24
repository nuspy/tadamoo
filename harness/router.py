"""What to do with a message the person writes while the agent is still working on something.

Before the agent's next model call the message is looked at and sent one of three ways:

* **steer**: it changes, corrects or adds to the task in hand, so it goes into that task's conversation and the agent goes on
  with the new instruction;
* **queue**: it is a different request that can wait; it starts when the task in hand is done (or at once, beside it, when
  the model can answer several requests at the same time);
* **urgent**: it is a different request that cannot wait; the task in hand is put aside where it stands, the urgent one is
  done, and the task in hand goes on afterwards.

Clear words decide first (they cost nothing and cannot be wrong about what the person said); when there are none, a judge
decides, and when there is no judge or it cannot say, the message is steered: the person is talking to the agent, so the
likeliest thing is that they are talking about what it is doing. The page shows the decision with the way to change it.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Optional, Tuple

STEER, QUEUE, URGENT = "steer", "queue", "urgent"
HOWS = (STEER, QUEUE, URGENT)

# words of the person (Italian and English), matched on whole words. Urgency and "unrelated" are two different things: a strong
# urgency word says the request cannot wait, a weak one ("subito", "right now") may well belong to the task in hand ("write it
# right now"), so it only counts together with a judge's "this is a different request".
STRONG_URGENT = re.compile(r"\b(urgente|urgentissimo|prima di tutto|prima di continuare|urgent|asap|before anything else)\b", re.IGNORECASE)
WEAK_URGENT = re.compile(r"\b(subito|immediatamente|adesso|right now|immediately|now)\b", re.IGNORECASE)
LATER = re.compile(
    r"\b(quando hai finito|appena hai finito|dopo che hai finito|a fine lavoro|più tardi|non è urgente|con calma|"
    r"when you are done|when you're done|once you(?:'ve| have) finished|afterwards|later|no rush|not urgent)\b", re.IGNORECASE)

JUDGE_PROMPT = (
    "You route a new message that the user wrote while an assistant is still working on a task. Answer with exactly one word:\n"
    "steer - the message changes, corrects, adds to or asks about the task in hand;\n"
    "queue - it is a different request that can wait until the task in hand is done;\n"
    "urgent - it is a different request that must be done right now, before going on.\n"
    "When in doubt, answer steer.")


def judge_with_model(client: Any) -> Callable[[str, Dict[str, Any]], Optional[str]]:
    """A judge that asks the loaded model for the one word; None when it cannot tell."""
    def judge(text: str, context: Dict[str, Any]) -> Optional[str]:
        brief = f"Task in hand: {context.get('task', '')[:600]}\nWhat the assistant has done so far: {context.get('progress', '')[:600]}\nNew message: {text[:800]}"
        try:
            reply = client.chat_completion([{"role": "system", "content": JUDGE_PROMPT}, {"role": "user", "content": brief}],
                                           temperature=0.0, max_tokens=8, timeout=30.0, extra={"reasoning_effort": "none"})
            word = (reply["choices"][0]["message"].get("content") or "").strip().lower()
        except Exception:
            return None
        found = re.match(r"[a-z]+", word)
        return found.group(0) if found and found.group(0) in HOWS else None
    return judge


class Router:
    def __init__(self, judge: Optional[Callable[[str, Dict[str, Any]], Optional[str]]] = None) -> None:
        self.judge = judge

    def route(self, text: str, context: Dict[str, Any]) -> Tuple[str, str]:
        """(how, why): why is "words" (the person's own words decided), "judge" or "default"."""
        if LATER.search(text):
            return QUEUE, "words"
        verdict = self.judge(text, context) if self.judge is not None else None
        if verdict not in HOWS:
            verdict = None
        if STRONG_URGENT.search(text):
            return (STEER, "judge") if verdict == STEER else (URGENT, "words")
        if WEAK_URGENT.search(text) and verdict in (QUEUE, URGENT):
            return URGENT, "judge"
        if verdict is not None:
            return verdict, "judge"
        return STEER, "default"
