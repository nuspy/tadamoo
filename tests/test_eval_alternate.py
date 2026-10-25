"""The comparison of two conversations taking turns, against a model that keeps a single prompt cache."""
import time

from harness.eval import alternate as alt


class OneCache:
    """A model with one prompt cache: a request reads again whatever differs from the last prompt it saw."""

    def __init__(self):
        self.last = None
        self.log = []

    def chat_completion(self, messages, on_delta=None, **kw):
        prompt = messages[0]["content"]
        cached = len(prompt.split()) if prompt == self.last else 0
        self.last = prompt
        time.sleep(0.002 if cached else 0.03)                                 # reading again takes longer
        total = len(prompt.split()) + 12
        self.log.append((prompt[:16], cached))
        if on_delta:
            on_delta("content", "x")
        return {"choices": [{"message": {"content": "pumice"}}], "usage": {"prompt_tokens": total, "prompt_tokens_details": {"cached_tokens": cached}}}


def test_taking_turns_loses_the_cache_and_working_apart_keeps_it():
    out = alt.compare(OneCache(), rounds=4, prefix_words=200, sleep=lambda s: None)
    turns, apart = out["taking turns"]["summary"], out["one after the other"]["summary"]
    assert turns["requests"] == 6 and apart["requests"] == 6                 # the first request of each conversation is not counted
    assert turns["cached_share"] == 0.0 and apart["cached_share"] > 0.9
    text = alt.report(out, 4, 200, 450, "a-model")
    assert "| taking turns | 6 |" in text and "| one after the other | 6 |" in text and "450 W" in text and "a-model" in text
    assert "0% of the prompt from the cache against" in text


def test_every_run_has_starts_nothing_has_cached_before():
    a, b = alt.long_start("A", 100), alt.long_start("A", 100)
    assert a != b and a.startswith("Conversation A-") and len(a.split()) > 100


def test_a_run_with_no_second_requests_says_so_and_does_not_divide_by_zero():
    out = {"taking turns": {"summary": {"requests": 0}, "rows": []}, "one after the other": {"summary": {"requests": 0}, "rows": []}}
    assert "| taking turns | 0 | - |" in alt.report(out, 1, 100, None)
