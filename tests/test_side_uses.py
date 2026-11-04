"""The registry of the technical session's uses: switches, numbers, prompts, and the long jobs that can be shown and stopped."""
import json
import time

import pytest

from tadamoo import digest, side, side_uses


@pytest.fixture(autouse=True)
def clean():
    side_uses.reset()
    for f in (side_uses._dir() / side_uses.JOBS_DIR).glob("*.json") if (side_uses._dir() / side_uses.JOBS_DIR).exists() else []:
        f.unlink()
    (side_uses._dir() / side_uses.STOP_FILE).unlink(missing_ok=True)
    yield
    side_uses.reset()


class Quick:
    """A local server with a technical session of the given size."""

    def __init__(self, n=30000):
        self.n = n

    def side_context(self):
        return self.n


def test_every_use_is_on_with_its_default_numbers():
    for use in side_uses.USES:
        assert side_uses.enabled(use.id) == use.default_on
        for p in use.params:
            assert side_uses.param(use.id, p.key) == p.default
    assert side_uses.param("chat_abstract", "min_chars") == 4000


def test_a_use_switched_off_sends_no_call_to_the_session_and_one_on_names_itself():
    client = Quick()
    assert side.side_extra(client, "judge") == {"cache_slot": "side", "side_use": "judge"}
    side_uses.save("judge", on=False)
    assert side.side_extra(client, "judge") == {} and side.side_context(client, "judge") == 0
    assert side.side_extra(client, "router")["side_use"] == "router"                  # the others are not touched
    assert side.side_extra(client) == {"cache_slot": "side"}                           # a call with no name is as before


def test_numbers_are_checked_kept_and_reset():
    side_uses.save("triage", params={"timeout_s": 45})
    assert side_uses.param("triage", "timeout_s") == 45.0
    with pytest.raises(ValueError):
        side_uses.save("triage", params={"timeout_s": 5000})
    with pytest.raises(ValueError):
        side_uses.save("triage", params={"nothing": 1})
    with pytest.raises(ValueError):
        side_uses.save("nothing", on=False)
    with pytest.raises(ValueError):
        side_uses.save("triage", params={"max_tokens": True})
    side_uses.reset("triage")
    assert side_uses.param("triage", "timeout_s") == 30


def test_a_number_out_of_range_in_the_file_gives_the_default():
    side_uses.file_path().write_text(json.dumps({"triage": {"params": {"timeout_s": 99999, "max_tokens": "x"}}}), encoding="utf-8")
    assert side_uses.param("triage", "timeout_s") == 30 and side_uses.param("triage", "max_tokens") == 600


def test_only_the_free_text_prompts_can_be_replaced_and_reset_brings_the_shipped_one_back():
    shipped = side_uses.prompt_text("chat_abstract")
    assert "abstract" in shipped.lower()
    assert side_uses.system("chat_abstract", shipped) == shipped
    side_uses.save("chat_abstract", text="Write one line.")
    assert side_uses.system("chat_abstract", shipped) == "Write one line."
    view = {u["id"]: u for u in side_uses.view()}
    assert view["chat_abstract"]["prompt"] == "Write one line." and view["chat_abstract"]["prompt_changed"] and view["chat_abstract"]["changed"]
    with pytest.raises(ValueError):
        side_uses.save("judge", text="x")
    with pytest.raises(ValueError):
        side_uses.save("triage", text="x")
    side_uses.reset("chat_abstract")
    assert side_uses.system("chat_abstract", shipped) == shipped
    assert side_uses.system("triage", "default") == "default"


def test_each_use_shows_its_own_prompt_or_none_and_never_the_conversations():
    view = {u["id"]: u for u in side_uses.view()}
    assert view["triage"]["prompt"] and view["router"]["prompt"] and view["compaction"]["prompt"] and view["optimize"]["prompt"]
    assert view["judge"]["prompt"] == ""                                                # built from the question each time
    assert [u["id"] for u in side_uses.view()] == [u.id for u in side_uses.USES]
    assert all(not u["changed"] for u in side_uses.view())


def test_a_job_shows_how_far_it_is_and_goes_when_it_ends():
    with side_uses.job("compaction", "summary") as job:
        job.progress(3, 10)
        rows = side_uses.running()
        assert [(r["use"], r["done"], r["total"]) for r in rows] == [("compaction", 3, 10)]
    assert side_uses.running() == []


def test_a_job_file_nobody_touches_any_more_is_not_shown():
    job = side_uses.job("compaction", "summary")
    old = time.time() - side_uses.JOB_STALE_S - 5
    import os
    os.utime(job._path, (old, old))
    assert side_uses.running() == []
    job.close()


def test_stop_ends_the_jobs_running_but_not_the_ones_that_start_later():
    first = side_uses.job("compaction")
    assert not first.stopped()
    time.sleep(0.01)
    side_uses.stop_all()
    assert first.stopped()
    time.sleep(0.01)
    later = side_uses.job("compaction")
    assert not later.stopped()
    first.close()
    later.close()


def test_a_stopped_digest_ends_with_Stopped_at_the_next_call_and_is_told_its_progress():
    seen = []

    class Job:
        def __init__(self):
            self.calls = 0

        def progress(self, done, total):
            seen.append((done, total))

        def stopped(self):
            self.calls += 1
            return self.calls > 3

    text = "\n\n".join(f"paragraph {i} " + "word " * 300 for i in range(40))
    with pytest.raises(digest.Stopped):
        digest.digest(lambda i, t: "notes " + str(len(t)), "FINAL", text, 6000, job=Job())
    assert seen and seen[0][0] == 0 and seen[0][1] > 1
