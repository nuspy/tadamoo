"""E6: long answers, one call or parts of different sizes, or a plan.

Question: for a text longer than one reply writes well, which way delivers the words asked, keeps what must not change, repeats nothing and joins
cleanly: one call, parts of 1000, 2000 or 2500 words, or parts written from a plan?

Arms: A `seg2000` (parts of 2000 words, today's default: `long_answer` with `segment_words: 2000`), B `seg1000`, C `seg2500`, D `plan` (the model's
own outline replaced by a fixed plan, as `plan_long_answer` hands it over, parts of 2000 words), E `single` (one plain call); `A~aa` is A again
(docs/longform.md). `plan` has nothing to plan for a translation and is left out there. Fixture: `fixtures/longform_texts.py`, five tasks (3000, 6000
and 10000 words; a translation of a manual with protected content; an analysis of an incident report). The scorer is plain code: words delivered
against words asked (in total and per part), protected items byte for byte, repeated paragraphs and word groups, seams between parts, the language
and, for the analysis, how many of the planted incident ids are discussed. No model judges anything.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Sequence

from serve import longform as lf

from .agentrun import CountingClient
from .core import Arm, CaseTrial, Context, Experiment, Guard, Rule, with_control
from .fixtures import longform_texts

COLUMNS = ("success", "delivered_ratio", "part_ratio_min", "protected_intact", "coverage", "seam_issues", "repeat_paragraphs", "repeat_ratio", "parts",
           "prompt_tokens", "cached_tokens", "seconds")
RATIO_RANGE = (0.85, 1.3)
MAX_REPEAT_RATIO = 0.10
MIN_COVERAGE = 0.9
SHINGLE = 8

ARMS = with_control([
    Arm("A", "parts of 2000 words (today's default)", {"how": "segments", "segment_words": 2000}, "long_answer segment_words=2000"),
    Arm("B", "parts of 1000 words", {"how": "segments", "segment_words": 1000}, "long_answer segment_words=1000"),
    Arm("C", "parts of 2500 words", {"how": "segments", "segment_words": 2500}, "long_answer segment_words=2500"),
    Arm("D", "parts of 2000 words from a plan", {"how": "plan", "segment_words": 2000}, "plan_long_answer with the sections given"),
    Arm("E", "one plain call", {"how": "single"}, "no long_answer"),
], "A")

RULE = Rule(
    primary="success", better="higher", min_effect=0.15, baseline="A", challengers=("B", "C", "D", "E"),
    guards=(Guard("seconds", "lower", 1.0, relative=True),),
    text="A way beats the 2000-word parts (A) when its success rate (words delivered within 85% to 130% of the words asked, protected content intact, "
         "no repeated paragraph and under 10% repeated 8-word groups, no bad seam, the right language, and for the analysis 90% of the incident ids) is "
         "higher than the pooled A and A/A rate by more than the A/A gap and by at least 15 points, and it takes no more than twice A's seconds. "
         "If B or C or D beats A, the winner among them with the better delivered ratio is the one to recommend; E is the reference for what one call does.")

CLOSERS = re.compile(r"\b(in conclusion|in summary|to sum up|to conclude|in conclusione|in sintesi|per concludere)\b", re.I)
TERMINAL = tuple(".!?:;)]}\"'`|»…*_") + ("```",)


def tasks() -> List[Dict[str, Any]]:
    return longform_texts.tasks()


def words_of(text: str) -> int:
    return lf.count_words(text)


def seams(parts: Sequence[str]) -> List[str]:
    """What is wrong where one part meets the next: a part cut in the middle of a sentence, the same word or group of words at both sides, a conclusion
    in a part that is not the last, a heading written twice."""
    out: List[str] = []
    for i in range(len(parts) - 1):
        a, b = parts[i].rstrip(), parts[i + 1].lstrip()
        if not a or not b:
            out.append(f"part {i + 1} or {i + 2} is empty")
            continue
        last = a.splitlines()[-1].strip()
        if not last.endswith(TERMINAL) and not last.startswith("#") and lf.ends_midword(a):
            out.append(f"part {i + 1} ends in the middle of a sentence")
        aw, bw = a.lower().split(), b.lower().split()
        if len(aw) >= 6 and len(bw) >= 6 and aw[-6:] == bw[:6]:
            out.append(f"parts {i + 1} and {i + 2} repeat the same six words at the seam")
        elif aw and bw and aw[-1] == bw[0] and aw[-1].isalpha() and len(aw[-1]) >= 3:
            out.append(f"the word '{aw[-1]}' is doubled at the seam of parts {i + 1} and {i + 2}")
        if CLOSERS.search(a[-300:]):
            out.append(f"part {i + 1} closes the whole text but is not the last")
        head_a = [ln for ln in a.splitlines() if ln.startswith("#")]
        if head_a and b.splitlines()[0].strip() == head_a[-1].strip():
            out.append(f"the heading '{head_a[-1].strip()}' is written again at the start of part {i + 2}")
    return out


def repeats(text: str) -> Dict[str, float]:
    """Paragraphs of 80 characters or more that appear twice, and the share of 8-word groups that occur more than once."""
    paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) >= 80]
    duplicated = len(paragraphs) - len(set(paragraphs))
    words = text.lower().split()
    groups = [" ".join(words[i:i + SHINGLE]) for i in range(max(0, len(words) - SHINGLE + 1))]
    seen: Dict[str, int] = {}
    for g in groups:
        seen[g] = seen.get(g, 0) + 1
    repeated = sum(1 for g in groups if seen[g] > 1)
    return {"repeat_paragraphs": float(duplicated), "repeat_ratio": (repeated / len(groups)) if groups else 0.0}


def score_text(task: Dict[str, Any], text: str, parts: Sequence[str], part_ratios: Sequence[float]) -> Dict[str, float]:
    """The scores of one delivered text. `parts` are the pieces it was written in (one for a single call), `part_ratios` words over words asked, per part."""
    requested = task["words"]
    words = words_of(text)
    ratio = words / requested if requested else 0.0
    seam = seams(parts)
    rep = repeats(text)
    out: Dict[str, float] = {"words": float(words), "delivered_ratio": ratio, "part_ratio_min": min(part_ratios) if part_ratios else ratio,
                             "part_ratio_mean": (sum(part_ratios) / len(part_ratios)) if part_ratios else ratio, "parts": float(len(parts)),
                             "seam_issues": float(len(seam)), **rep}
    ok = RATIO_RANGE[0] <= ratio <= RATIO_RANGE[1] and not seam and rep["repeat_paragraphs"] == 0 and rep["repeat_ratio"] <= MAX_REPEAT_RATIO
    if task["kind"] == "translate":
        kept = [p for p in task["protected"] if p in text]
        out["protected_intact"] = len(kept) / len(task["protected"])
        seen = lf.detect_language(text)
        out["language_ok"] = 1.0 if seen == "it" else 0.0
        ok = ok and len(kept) == len(task["protected"]) and seen == "it"
    if task["kind"] == "analysis":
        out["coverage"] = sum(1 for i in task["incidents"] if i in text) / len(task["incidents"])
        ok = ok and out["coverage"] >= MIN_COVERAGE
    out["success"] = 1.0 if ok else 0.0
    return out


def notes_of(task: Dict[str, Any], s: Dict[str, float], seam_list: Sequence[str]) -> List[str]:
    out = []
    if not RATIO_RANGE[0] <= s["delivered_ratio"] <= RATIO_RANGE[1]:
        out.append(f"{int(s['words'])} words for {task['words']} asked ({s['delivered_ratio'] * 100:.0f}%)")
    out += list(seam_list)[:3]
    if s["repeat_paragraphs"]:
        out.append(f"{int(s['repeat_paragraphs'])} paragraph(s) written twice")
    if s["repeat_ratio"] > MAX_REPEAT_RATIO:
        out.append(f"{s['repeat_ratio'] * 100:.0f}% of the 8-word groups repeat")
    if "protected_intact" in s and s["protected_intact"] < 1:
        out.append(f"protected content kept in {s['protected_intact'] * 100:.0f}% of the items")
    if "language_ok" in s and not s["language_ok"]:
        out.append("the text is not in Italian")
    if "coverage" in s and s["coverage"] < MIN_COVERAGE:
        out.append(f"{s['coverage'] * 100:.0f}% of the incident ids are discussed")
    return out


def spec_for(arm: Arm, task: Dict[str, Any]) -> lf.Spec:
    raw: Dict[str, Any] = {"segment_words": arm.settings["segment_words"]}
    if task["kind"] == "translate":
        raw.update(mode="translate", source=task["source"], target_language=task["language"])
    else:
        raw.update(mode="write", task=task.get("task") or task["prompt"], target_words=task["words"], language=task["language"])
        if arm.settings["how"] == "plan":
            raw.update(outline=task["outline"], title=task["id"])
    return lf.Spec.from_request(raw, "")


def prior_for(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    if task["kind"] == "analysis":
        return [{"role": "user", "content": "Here is the incident report:\n\n" + task["source"]}, {"role": "assistant", "content": "I have read the report."}]
    return []


def run_text(arm: Arm, task: Dict[str, Any], rep: int, ctx: Context) -> CaseTrial:
    t0 = time.monotonic()
    trial = CaseTrial(arm.name, task["id"], rep)
    if arm.settings["how"] == "plan" and not task["outline"]:
        trial.invalid, trial.error = True, "nothing to plan for this task"
        return trial
    client = CountingClient(ctx.client)
    sampling = {"seed": ctx.seed_base + rep}
    parts: List[str] = []
    ratios: List[float] = []
    if arm.settings["how"] == "single":
        reply = client.chat_completion(messages=[{"role": "user", "content": task["prompt"]}], temperature=ctx.temperature, timeout=3600.0,
                                       max_tokens=lf.tokens_for_words(task["words"] * 1.6, task["language"]), extra={"reasoning_effort": "none", **sampling})
        choice = reply["choices"][0]
        text = (choice["message"].get("content") or "").strip()
        parts = [text]
        cut = choice.get("finish_reason") == "length"
    else:
        run = lf.LongAnswer(client, spec_for(arm, task), prior=prior_for(task), sampling={**sampling, **({"temperature": ctx.temperature} if ctx.temperature is not None else {})})
        warnings: List[Any] = []
        for ev in run.run():
            if ev and ev["event"] == "segment_done" and ev.get("target"):
                ratios.append(ev["words"] / ev["target"])
            elif ev and ev["event"] == "done":
                warnings = ev.get("warnings") or []
        text, parts, cut = run.text(), list(run.parts), False
        trial.metrics["warnings"] = len(warnings)
    scored = score_text(task, text, parts, ratios)
    trial.success = bool(scored.pop("success"))
    trial.notes = notes_of(task, scored, seams(parts)) + (["the output limit ended the text"] if cut else [])
    trial.metrics.update(scored)
    trial.metrics.update(prompt_tokens=client.prompt_tokens, cached_tokens=client.cached_tokens, completion_tokens=client.completion_tokens,
                         seconds=round(time.monotonic() - t0, 2))
    return trial


def experiment() -> Experiment:
    return Experiment(
        id="e6", title="Long answers: one call, parts or a plan", arms=ARMS, rule=RULE, columns=COLUMNS, tasks=tasks, run_trial=run_text, task_id=lambda t: t["id"],
        default_k=3, question="For a text longer than one reply writes well, which way delivers the words asked, keeps protected content, repeats nothing and joins cleanly?")
