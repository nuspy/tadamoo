"""E6 fixture: the four long-answer tasks and the source texts they need, generated from fixed seeds (no large files are stored).

Tasks: three texts to write (3000, 6000 and 10000 words), a translation of a manual that holds protected content (code, an image, a link, inline
code, a table, citation keys), and an analysis of an incident report with planted incident ids. Each task says what the scorer needs.
"""
from __future__ import annotations

import random
from typing import Any, Dict, List

THINGS = "scheduler uploader collector gateway archive indexer exporter watcher balancer dispatcher".split()
ACTIONS = ("retries a failed transfer after {n} seconds", "records every attempt in the audit log", "refuses a batch larger than {n} items",
           "keeps the last {n} results in memory", "writes a checkpoint every {n} minutes", "drops a duplicate message that arrives twice",
           "waits for the next window when the queue holds more than {n} entries", "reports its state to the monitor once a minute")
REASONS = ("so that an operator can follow what happened", "because a silent failure is worse than a slow one", "to keep the load even across the day",
           "so that a restart never repeats finished work", "since the network is not always there", "to leave room for the urgent jobs")

TOPICS = ["running a small fleet of field data loggers", "keeping a community library catalogue accurate over many years",
          "planning the maintenance of a small harbour"]

CODE = "```python\ndef backoff(attempt, base=2):\n    return min(300, base ** attempt)\n```"
IMAGE = "![queue diagram](img/queue-flow.png)"
LINK = "[the full guide](https://docs.example.org/guide/queues)"
INLINE = "`max_batch_items`"
TABLE = "| Setting | Default | Unit |\n| --- | --- | --- |\n| retry_after | 30 | seconds |\n| max_batch | 500 | items |"
CITE = "[S1, S2]"
PROTECTED = [CODE, IMAGE, "https://docs.example.org/guide/queues", INLINE, TABLE, CITE]


def sentence(rng: random.Random) -> str:
    action = rng.choice(ACTIONS).format(n=rng.choice([3, 5, 12, 30, 60, 250]))
    return f"The {rng.choice(THINGS)} {action} {rng.choice(REASONS)}."


def paragraph(rng: random.Random, sentences: int = 5) -> str:
    return " ".join(sentence(rng) for _ in range(sentences))


def manual(words: int, seed: int = 3) -> str:
    """An English manual of about `words` words with headings and every protected item once."""
    rng = random.Random(seed)
    out: List[str] = ["# Operations manual", ""]
    protected = [CODE, IMAGE, f"See {LINK} for the rest.", f"Set {INLINE} with care {CITE}.", TABLE]
    count, section = 0, 0
    while count < words:
        section += 1
        out += [f"## Part {section}: the {rng.choice(THINGS)}", ""]
        for _ in range(3):
            para = paragraph(rng)
            out += [para, ""]
            count += len(para.split())
        if protected and section % 3 == 0:
            out += [protected.pop(0), ""]
    out += protected
    return "\n".join(out).rstrip() + "\n"


def incident_report(words: int, seed: int = 5) -> Dict[str, Any]:
    """An English incident report with planted ids (INC-4101 and on) and the ids themselves, for the coverage check."""
    rng = random.Random(seed)
    ids = [f"INC-{4101 + i * 7}" for i in range(12)]
    out = ["# Incident report", ""]
    per = max(60, words // len(ids))
    for inc in ids:
        out += [f"## {inc}", "", f"{inc} began when the {rng.choice(THINGS)} stopped answering at {rng.randint(1, 23):02d}:{rng.randint(0, 59):02d}.", ""]
        count = 0
        while count < per:
            para = paragraph(rng, 4)
            out += [para, ""]
            count += len(para.split())
    return {"text": "\n".join(out).rstrip() + "\n", "ids": ids}


def outline(total: int, segment: int = 1000) -> List[Dict[str, Any]]:
    """A plan of sections for a text of `total` words, as a model would hand to plan_long_answer."""
    n = max(3, total // segment)
    each = total // n
    return [{"heading": f"Part {i}", "goal": f"Develop part {i} of {n} fully, with concrete detail, without summarising the others.", "target_words": each,
             "key_points": [f"point {i}.a", f"point {i}.b", f"point {i}.c"]} for i in range(1, n + 1)]


def tasks() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for words, topic in zip((3000, 6000, 10000), TOPICS):
        out.append({"id": f"write{words}", "kind": "write", "words": words, "language": "en",
                    "prompt": f"Write a detailed, well-structured guide of about {words} words on {topic}. Use headings, and develop every section fully; do not summarise.",
                    "outline": outline(words)})
    source = manual(3600)
    out.append({"id": "translate3600", "kind": "translate", "words": int(len(source.split()) * 1.15), "language": "it", "source": source, "protected": PROTECTED,
                "prompt": "Translate the following document into Italian. Keep headings, code, links, images, tables and numbers exactly as they are.\n\n" + source,
                "outline": []})
    report = incident_report(5000)
    out.append({"id": "analysis", "kind": "analysis", "words": 3000, "language": "en", "source": report["text"], "incidents": report["ids"],
                "prompt": "Analyze the incident report below in depth. Write at least 3000 words: discuss every incident by its id, its cause, its effect and what to change.\n\n" + report["text"],
                "task": "Analyze the incident report in depth: discuss every incident by its id, its cause, its effect and what to change.",
                "outline": outline(3000)})
    return out
