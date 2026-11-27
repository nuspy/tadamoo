"""What one reply holds on the route a conversation runs on (serve/longform.py `Capacity`): the reply length of the model or provider the client
calls now (a number written by hand, else the measured one, tadamoo/llm/client.py `length_limit`) and the words a token makes
(`words_per_token`); with none, the general numbers of serve/longform.py. A text longer than its words is written in parts of that size."""
from __future__ import annotations

from typing import Any

from serve import longform as lf


def capacity(client: Any, words_per_token: int = 0) -> lf.Capacity:
    """The capacity of the route `client` calls now; `words_per_token` is the conversation's setting (percent, 0: the general one)."""
    ask = getattr(client, "length_limit", None)
    try:
        return lf.capacity(int(ask()) if callable(ask) else 0, words_per_token)
    except Exception:                                   # a client that cannot say has no reply length to give
        return lf.capacity(0, words_per_token)


def min_words(client: Any, words_per_token: int = 0) -> int:
    """About how many words one reply holds, to the hundred (for a text the model reads)."""
    return int(round(capacity(client, words_per_token).words, -2))
