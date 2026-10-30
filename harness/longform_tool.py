"""`plan_long_answer`: the model says that a final text does not fit one turn and hands over its plan; the harness writes it in parts.

The tool is in the fixed group at the start of the tool list. It changes no file: the text goes to the person as the parts pass their
checks (`deliver`), progress goes out as events (`progress`), and the model gets a short report. The work is `serve.longform.LongAnswer`,
driven by the client of the conversation, so the loaded model and a cloud route are served the same way.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from harness.tools.registry import EffectClass, ToolRegistry
from serve import longform as lf

SOURCE_MAX_BYTES = 8 << 20
PREVIEW_WORDS = 60

DESCRIPTION = (
    "Use this when the final text you must write is longer than about 1500 words (a chapter, a report, a long translation or rewrite, a "
    "detailed analysis): such a text does not come out whole in one reply. Send the plan and the harness writes it part by part, checks each "
    "part and shows the text to the person itself, so do not write the text yourself afterwards. For a new text give `sections` (the "
    "outline) and `total_words`; for a translation or rewrite give `mode` and `source_path` (a file in the workspace) and no sections. "
    "Gather what you need with the other tools first; the parts are written from your plan and the conversation. It returns a short report.")

PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "What the text is for and must do; for a rewrite, what to change"},
        "mode": {"type": "string", "enum": list(lf.MODES), "description": "write (default), translate or rewrite"},
        "sections": {"type": "array", "description": "The outline of a new text, in order",
                     "items": {"type": "object",
                               "properties": {"id": {"type": "string"}, "heading": {"type": "string"},
                                              "goal": {"type": "string", "description": "What the section must do"},
                                              "target_words": {"type": "integer"},
                                              "key_points": {"type": "array", "items": {"type": "string"}}},
                               "required": ["heading", "goal", "target_words"]}},
        "total_words": {"type": "integer", "description": "The length of the whole text; the sum of the sections if left out"},
        "language": {"type": "string", "description": "The language of the text (required to translate)"},
        "style": {"type": "string", "description": "Voice and register, in a sentence"},
        "glossary": {"type": "object", "description": "Terms and how to write them", "additionalProperties": {"type": "string"}},
        "source_path": {"type": "string", "description": "translate and rewrite: the file in the workspace to work from"},
        "use_message": {"type": "boolean", "description": "translate and rewrite: work from the text in the person's message, as it was sent"},
    },
    "required": ["task"],
}


class LongBinding:
    """What the tool needs from the conversation that has it; each is set when the conversation is built."""

    def __init__(self) -> None:
        self.client: Any = None                                      # the conversation's model client (the loaded model or a cloud route)
        self.cancel: Optional[threading.Event] = None
        self.resolve: Optional[Callable[[str], Path]] = None         # a workspace path, checked
        self.deliver: Optional[Callable[[str], None]] = None         # shows text to the person
        self.progress: Optional[Callable[[Dict[str, Any]], None]] = None
        self.message: Optional[Callable[[], str]] = None             # the person's request that needs the text, when the harness kept it


def _warning_lines(warnings: List[Dict[str, Any]]) -> str:
    return "; ".join(f"part {w.get('segment')}: {w['code']}" + (f" ({w['detail']})" if w.get("detail") else "") for w in warnings[:8])


def run_plan(binding: LongBinding, args: Dict[str, Any]) -> str:
    if binding.client is None:
        return "No model is connected to this tool in this session; write the text yourself in several replies."
    raw: Dict[str, Any] = {"mode": args.get("mode") or "write", "task": args.get("task") or ""}
    for key, field in (("language", "target_language"), ("style", "style"), ("glossary", "glossary")):
        if args.get(key):
            raw[field] = args[key]
    if args.get("sections"):
        raw["outline"] = [{k: s.get(k) for k in ("heading", "goal", "target_words", "key_points")} for s in args["sections"] if isinstance(s, dict)]
    if isinstance(args.get("total_words"), (int, float)) and not isinstance(args.get("total_words"), bool) and args["total_words"] > 0:
        raw["target_words"] = int(args["total_words"])
    elif raw["mode"] == "write" and raw.get("outline"):
        total = sum(int(r.get("target_words") or 0) for r in raw["outline"])
        raw["target_words"] = max(lf.TARGET_WORDS_RANGE[0], total)
    if args.get("use_message"):
        raw["source"] = binding.message() if binding.message is not None else ""
        if not raw["source"].strip():
            return "There is no message text to work from; give `source_path`."
    elif args.get("source_path"):
        if binding.resolve is None:
            return "This session cannot read files for the tool."
        try:
            path = binding.resolve(str(args["source_path"]))
            if path.stat().st_size > SOURCE_MAX_BYTES:
                return f"{args['source_path']} is larger than {SOURCE_MAX_BYTES >> 20} MiB."
            raw["source"] = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError) as exc:
            return f"Cannot read {args['source_path']}: {exc}"
    try:
        spec = lf.Spec.from_request(raw, "")
    except lf.LongformError as exc:
        return f"The plan was not accepted: {exc}"
    if not spec.context_tokens:
        try:
            spec.context_tokens = int(binding.client.context_window() or 0)
        except Exception:                                            # a client that cannot say: the parts carry the whole text
            spec.context_tokens = 0
    run = lf.LongAnswer(binding.client, spec, cancel=binding.cancel)
    kept: List[str] = []
    done: Dict[str, Any] = {}
    try:
        for ev in run.run():
            if ev is None:
                continue
            if ev["event"] == "delta":
                (binding.deliver or kept.append)(ev["text"])
            else:
                if ev["event"] == "done":
                    done = ev
                if binding.progress is not None:
                    binding.progress(ev)
    except lf.LongformError as exc:
        return f"The plan was not accepted: {exc}"
    if binding.deliver is not None:
        binding.deliver("\n\n")
    text = run.text()
    lines = [f"Written in {done.get('segments')} of {done.get('of')} parts: {done.get('words')} words (planned {done.get('target_words')})."]
    if done.get("stopped"):
        lines.append(f"It stopped early ({done['stopped']}): the text is not complete.")
    if done.get("warnings"):
        lines.append("Problems found: " + _warning_lines(done["warnings"]) + ".")
    if binding.deliver is not None:
        words = text.split()
        lines.append("The text was shown to the person as it was written; do not write it again. Give a short wrap-up, and say about any "
                     "problem listed above what the person should check.")
        lines.append("Start: " + " ".join(words[:PREVIEW_WORDS]) + (" ..." if len(words) > PREVIEW_WORDS else ""))
        lines.append("End: " + ("... " if len(words) > PREVIEW_WORDS else "") + " ".join(words[-PREVIEW_WORDS:]))
    else:
        lines.append("The text:\n" + text)
    return "\n".join(lines)


def register_plan_long_answer(registry: ToolRegistry) -> LongBinding:
    """Adds `plan_long_answer` and keeps its binding on the registry as `registry.longform`."""
    binding = LongBinding()
    registry.longform = binding                                      # type: ignore[attr-defined]
    registry.register("plan_long_answer", DESCRIPTION, PARAMETERS, EffectClass.PURE, lambda **args: run_plan(binding, args), timeout=None)
    return binding
